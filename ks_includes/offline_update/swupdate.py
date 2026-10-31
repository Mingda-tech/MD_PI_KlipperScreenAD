"""One-shot adapter to the pinned, signed SWUpdate engine; no component installer here.

Protocol reference: SWUpdate 2026.05 include/progress_ipc.h and
include/swupdate_status.h (LGPL-2.1-or-later interface), API 2.0.0.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from collections import deque

from .common import UpdateError, atomic_bytes, atomic_json, no_links, read_json, regular, trusted_path
from .swu_package import hardware_revisions


VERSION = '2026.05'
SOURCE_COMMIT = '602edf8e57f940791034096c9e24206097b4b7a6'
API_VERSION = 0x00020000
ACK = struct.Struct('<I4s')
# C structure is explicitly packed, including the uint64 at offset 12.
FRAME = struct.Struct('<IIIQIII256s64sII2048s')
STATUSES = ('IDLE', 'START', 'RUN', 'SUCCESS', 'FAILURE', 'DOWNLOAD', 'DONE', 'SUBPROCESS', 'PROGRESS')
ENABLED = ('SCRIPTS', 'HW_COMPATIBILITY', 'SHELLSCRIPTHANDLER', 'SSL_IMPL_OPENSSL',
           'SIGNED_IMAGES', 'SIGALG_RAWRSA', 'HASH_VERIFY', 'BOOTLOADER_NONE',
           'BOOTLOADER_DEFAULT_NONE', 'BOOTLOADER_STATIC_LINKED')
DISABLED = ('LUA', 'LUAEXTERNAL', 'DOWNLOAD', 'SURICATTA', 'WEBSERVER', 'RAW', 'ARCHIVE',
            'MTD', 'ENCRYPTED_IMAGES', 'SIGALG_RSAPSS', 'SIGALG_CMS', 'SIGALG_GPG')
HELP_FLAGS = ('--check', '--key', '--hwrevision', '--preupdate', '--no-transaction-marker')


def _sha256(path):
    value = hashlib.sha256()
    with regular(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _trusted_file(path):
    path = regular(path).absolute()
    try:
        return trusted_path(path)
    except UpdateError as exc:
        raise UpdateError('SWUPDATE_UNTRUSTED_RUNTIME') from exc


def _environment(library_directory=None):
    value = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8',
             'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    if library_directory:
        value['LD_LIBRARY_PATH'] = str(library_directory)
    return value


def verify_runtime(policy, run=subprocess.run):
    """Check the deployed build identity before invoking any engine command."""
    config = policy.get('swupdate', {})
    if config.get('version') != VERSION:
        raise UpdateError('SWUPDATE_VERSION_UNSUPPORTED')
    binary = _trusted_file(config.get('binary', ''))
    expected = config.get('binarySha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or _sha256(binary) != expected:
        raise UpdateError('SWUPDATE_BINARY_MISMATCH')
    receipt_path = _trusted_file(config.get('buildReceipt', binary.parent / 'build-info.json'))
    receipt = read_json(receipt_path)
    if (receipt.get('schemaVersion') != 1 or receipt.get('version') != VERSION or
            receipt.get('sourceCommit') != SOURCE_COMMIT or receipt.get('binarySha256') != expected or
            not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('configSha256', ''))) or
            receipt.get('arch') != policy.get('device', {}).get('arch') or
            receipt.get('developmentBuild') is not False or
            receipt.get('socketPaths') != {'control': '', 'progress': ''}):
        raise UpdateError('SWUPDATE_BUILD_MISMATCH')
    compiled_config = _trusted_file(config.get('buildConfig', binary.parent / 'swupdate.config'))
    if _sha256(compiled_config) != receipt['configSha256']:
        raise UpdateError('SWUPDATE_CONFIG_MISMATCH')
    with binary.open('rb') as source:
        elf = source.read(20)
    machine = {'aarch64': 183, 'x86_64': 62}.get(receipt['arch'])
    if len(elf) < 20 or elf[:6] != b'\x7fELF\x02\x01' or int.from_bytes(elf[18:20], 'little') != machine:
        raise UpdateError('SWUPDATE_ARCHITECTURE_MISMATCH')
    features = receipt.get('features', {})
    if any(features.get(key) is not True for key in ENABLED) or any(
            features.get(key) is not False for key in DISABLED):
        raise UpdateError('SWUPDATE_BUILD_UNSAFE')
    public_key = _trusted_file(config.get('publicKey', policy.get('publicKey', '')))
    libraries = config.get('libraryFiles', {})
    if not isinstance(libraries, dict):
        raise UpdateError('SWUPDATE_BUILD_MISMATCH')
    for name, checksum in libraries.items():
        path = _trusted_file(name)
        if (path.parent != binary.parent / 'lib' or not isinstance(checksum, str)
                or not re.fullmatch(r'[0-9a-f]{64}', checksum) or _sha256(path) != checksum):
            raise UpdateError('SWUPDATE_BINARY_MISMATCH')
    library_directory = binary.parent / 'lib' if libraries else None
    try:
        version = run([str(binary), '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, env=_environment(library_directory), timeout=10, check=False)
        help_result = run([str(binary), '--help'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=_environment(library_directory), timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError('SWUPDATE_UNAVAILABLE') from exc
    version_text = version.stdout.decode('utf-8', errors='replace')
    help_text = help_result.stdout.decode('utf-8', errors='replace')
    if version.returncode != 0 or not re.search(r'(?<!\d)2026\.05(?:\.0)?(?=$|[\s+-])', version_text):
        raise UpdateError('SWUPDATE_VERSION_UNSUPPORTED')
    if help_result.returncode != 0 or any(flag not in help_text for flag in HELP_FLAGS):
        raise UpdateError('SWUPDATE_FEATURES_MISSING')
    return {'binary': str(binary), 'publicKey': str(public_key), 'version': VERSION,
            'binarySha256': expected, 'libraryDirectory': str(library_directory) if library_directory else ''}


def hardware_revision(policy):
    return 'MINGDA:' + hardware_revisions()[0]


def decode_ack(data):
    if len(data) != ACK.size:
        raise UpdateError('SWUPDATE_PROGRESS_PROTOCOL')
    version, magic = ACK.unpack(data)
    if version != API_VERSION or magic != b'ACK\x00':
        raise UpdateError('SWUPDATE_PROGRESS_PROTOCOL')


def decode_progress(data):
    if len(data) != FRAME.size:
        raise UpdateError('SWUPDATE_PROGRESS_PROTOCOL')
    version, status, download, _, steps, step, percent, image, handler, source, length, info = FRAME.unpack(data)
    if (version != API_VERSION or status >= len(STATUSES) or length > len(info) or source > 5 or
            steps > 1000000 or step > 1000000 or percent > 100 or download > 100):
        raise UpdateError('SWUPDATE_PROGRESS_PROTOCOL')
    native = STATUSES[status]
    # Failure scripts can add a step beyond the initially announced total.
    # Accept the official failure path while bounding the displayed percentage.
    progress = 0 if not steps else int(100 * (max(step - 1, 0) + percent / 100) / steps)
    if native in ('SUCCESS', 'DONE'):
        progress = 100
    message = info[:length].decode('utf-8', errors='replace') if length else ''
    return {'phase': 'INSTALLING', 'progress': max(0, min(100, progress)),
            'component': image.split(b'\x00', 1)[0].decode('utf-8', errors='replace'),
            'message': message[:2048], 'nativeStatus': native,
            'handler': handler.split(b'\x00', 1)[0].decode('utf-8', errors='replace')}


def _script_progress(path):
    """Optional signed-package UI detail; it can never declare task success."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            return None
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get('phase') not in ('INSTALLING', 'FLASHING', 'HEALTH_CHECK'):
            return None
        progress = value.get('progress')
        if type(progress) is not int or not 0 <= progress <= 99:
            return None
        if not isinstance(value.get('component', ''), str) or not isinstance(value.get('message', ''), str):
            return None
        detail = {}
        for name, maximum in (('stage', 64), ('unit', 32)):
            if name in value:
                text = value[name]
                if (not isinstance(text, str) or len(text) > maximum
                        or any(ord(char) < 32 or ord(char) == 127 for char in text)):
                    return None
                detail[name] = text
        if 'completed' in value or 'total' in value:
            completed, total = value.get('completed'), value.get('total')
            if (type(completed) is not int or type(total) is not int
                    or not 0 <= completed <= total <= 10 ** 12):
                return None
            detail.update(completed=completed, total=total)
        for name, maximum in (('elapsedSeconds', 604800), ('heartbeat', 2 ** 63 - 1)):
            if name in value:
                number = value[name]
                if type(number) is not int or not 0 <= number <= maximum:
                    return None
                detail[name] = number
        return {'phase': value['phase'], 'progress': progress, 'component': value.get('component', '')[:256],
                'message': value.get('message', '')[:2048], 'nativeStatus': 'PACKAGE_DETAIL', **detail}
    except (OSError, ValueError, TypeError):
        return None


class _ProgressReceiver:
    """Drain native IPC independently of potentially slow UI/journal writes."""
    LIMIT = 4096
    TERMINAL = frozenset(('SUCCESS', 'FAILURE', 'DONE'))

    def __init__(self, process, path, ready, socket_factory):
        self.process, self.path, self.ready = process, path, ready
        self.socket_factory = socket_factory
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.pending = deque()
        self.connection = None
        self.buffer = bytearray()
        self.acknowledged = self.success = self.failure = self.closed = self.eof = False
        self.error = None
        self.frame_count = self.byte_count = self.coalesced = self.queue_peak = 0
        self.last_progress = None
        self.thread = threading.Thread(target=self._run, daemon=True, name='swupdate-progress-receiver')

    def start(self):
        self.thread.start()

    def _error(self, error):
        with self.condition:
            if self.error is None:
                self.error = error
            self.condition.notify_all()

    def _event(self, event):
        with self.condition:
            self.frame_count += 1
            status = event['nativeStatus']
            self.success |= status == 'SUCCESS'
            self.failure |= status == 'FAILURE'
            self.last_progress = dict(event)
            if (status == 'PROGRESS' and self.pending and self.pending[-1]['nativeStatus'] == 'PROGRESS'
                    and all(self.pending[-1].get(key) == event.get(key) for key in ('component', 'handler'))):
                self.pending[-1] = dict(event)
                self.coalesced += 1
            else:
                if len(self.pending) >= self.LIMIT:
                    removable = next((index for index, item in enumerate(self.pending)
                                      if item['nativeStatus'] == 'PROGRESS'), None)
                    if removable is not None:
                        del self.pending[removable]
                        self.coalesced += 1
                    else:
                        # Non-coalescible overflow is not safe completion. Keep
                        # terminal facts independently even under queue pressure.
                        if self.error is None:
                            self.error = UpdateError('SWUPDATE_PROGRESS_QUEUE_OVERFLOW')
                        if status in self.TERMINAL:
                            removable = next((index for index, item in enumerate(self.pending)
                                              if item['nativeStatus'] not in self.TERMINAL), 0)
                            del self.pending[removable]
                        else:
                            self.condition.notify_all()
                            return
                self.pending.append(dict(event))
            self.queue_peak = max(self.queue_peak, len(self.pending))
            self.condition.notify_all()

    def _run(self):
        connection = None
        try:
            while not self.stopped.is_set():
                attempt = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
                attempt.settimeout(0.1)
                try:
                    attempt.connect(str(self.path))
                    if os.name == 'posix' and hasattr(socket, 'SO_PEERCRED'):
                        peer = attempt.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        if struct.unpack('3i', peer)[0] != self.process.pid:
                            raise UpdateError('SWUPDATE_PROGRESS_PEER')
                    connection = attempt
                    with self.condition:
                        self.connection = connection
                    break
                except (OSError, UpdateError) as error:
                    attempt.close()
                    if isinstance(error, UpdateError):
                        self._error(error)
                        return
                    if self.process.poll() is not None:
                        self._error(error)
                        return
                    self.stopped.wait(.025)
            while connection is not None and not self.stopped.is_set():
                try:
                    data = connection.recv(32768)
                except socket.timeout:
                    continue
                except (OSError, UpdateError) as error:
                    if not self.stopped.is_set():
                        self._error(error)
                    return
                if not data:
                    with self.condition:
                        if not self.stopped.is_set():
                            self.eof = True
                    return
                with self.condition:
                    self.byte_count += len(data)
                    self.buffer.extend(data)
                if not self.acknowledged and len(self.buffer) >= ACK.size:
                    decode_ack(bytes(self.buffer[:ACK.size]))
                    del self.buffer[:ACK.size]
                    # Publishing the barrier can fsync after its rename already
                    # made it visible. Keep all filesystem work off this thread.
                    with self.condition:
                        self.acknowledged = True
                        self.condition.notify_all()
                while self.acknowledged and len(self.buffer) >= FRAME.size:
                    event = decode_progress(bytes(self.buffer[:FRAME.size]))
                    del self.buffer[:FRAME.size]
                    self._event(event)
        except BaseException as error:
            self._error(error)
        finally:
            try:
                if connection is not None:
                    connection.close()
            except OSError as error:
                if not self.stopped.is_set():
                    self._error(error)
            finally:
                with self.condition:
                    self.connection = None
                    self.closed = True
                    self.condition.notify_all()

    def take(self, timeout=.05):
        with self.condition:
            if not self.pending and not self.closed:
                self.condition.wait(timeout)
            result = list(self.pending)
            self.pending.clear()
            return result

    def snapshot(self):
        with self.condition:
            return {'acknowledged': self.acknowledged, 'successSeen': self.success,
                    'failureSeen': self.failure, 'pendingBytes': len(self.buffer),
                    'frameCount': self.frame_count, 'receivedBytes': self.byte_count,
                    'lastNativeStatus': (self.last_progress or {}).get('nativeStatus'),
                    'receiverClosed': self.closed, 'receiverEOF': self.eof, 'receiverError': self.error,
                    'coalescedEvents': self.coalesced, 'queuePeak': self.queue_peak,
                    'queuedEvents': len(self.pending), 'lastProgress': self.last_progress}

    def stop(self):
        self.stopped.set()
        with self.condition:
            connection = self.connection
        if connection is not None:
            try:
                if hasattr(connection, 'shutdown'):
                    connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self.thread.join(timeout=2)
        return not self.thread.is_alive()


class SWUpdateRunner:
    def __init__(self, policy, popen=subprocess.Popen, socket_factory=socket.socket,
                 verifier=verify_runtime, clock=time.monotonic, sleep=time.sleep):
        self.policy = policy
        self.popen = popen
        self.socket_factory = socket_factory
        self.verifier = verifier
        self.clock = clock
        self.sleep = sleep

    def _prepare(self, package, jobroot, check_only, mode, boot_recovery=False):
        if mode not in ('install', 'recover', 'rescue'):
            raise UpdateError('SWUPDATE_MODE_INVALID')
        if type(boot_recovery) is not bool or (boot_recovery and mode != 'rescue'):
            raise UpdateError('SWUPDATE_MODE_INVALID')
        runtime = self.verifier(self.policy)
        package = regular(package).absolute()
        jobroot = Path(jobroot).absolute()
        no_links(jobroot, jobroot.anchor)
        no_links(package, jobroot)
        token = uuid.uuid4().hex[:8]
        temporary = jobroot / 'native-tmp' / token
        sockets = jobroot / 'native-run' / token
        for directory in (temporary, sockets):
            directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            os.chmod(directory, 0o700)
        socket_path = sockets / 'swupdateprog'
        if len(os.fsencode(socket_path)) >= 108:
            raise UpdateError('SWUPDATE_SOCKET_PATH_TOO_LONG')
        cfg = sockets / 'swupdate.cfg'
        atomic_bytes(cfg, b'globals = { bootloader = "none"; reboot = "disabled"; loglevel = 3; };\n')
        args = [runtime['binary'], '-f', str(cfg), '-i', str(package), '-k', runtime['publicKey'],
                '-H', hardware_revision(self.policy), '-e', 'stable,rescue' if mode == 'rescue' else 'stable,main',
                '-m', '-M']
        environment = _environment(runtime.get('libraryDirectory'))
        environment.update(TMPDIR=str(temporary), RUNTIME_DIRECTORY=str(sockets), MINGDA_JOB_DIR=str(jobroot),
                           MINGDA_UPDATE_ACTION=mode, MINGDA_PROGRESS_FILE=str(jobroot / 'script-progress.json'),
                           MINGDA_SWUPDATE_READY=str(sockets / 'progress-ready'),
                           MINGDA_BOOT_RECOVERY='1' if boot_recovery else '0')
        if check_only:
            args.append('-c')
        else:
            barrier = Path(__file__).with_name('swupdate_barrier.py').resolve()
            args.extend(['-P', '/usr/bin/python3 -I ' + shlex.quote(str(barrier))])
        return args, environment, sockets, socket_path

    def _launch(self, args, environment, sockets):
        log_path = sockets / 'swupdate.log'
        try:
            process = self.popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        except OSError as exc:
            raise UpdateError('SWUPDATE_START_FAILED') from exc

        def drain():
            remaining = 8 * 1024 * 1024
            try:
                with log_path.open('xb') as log:
                    os.chmod(log_path, 0o600)
                    for block in iter(lambda: process.stdout.read(8192), b''):
                        if remaining:
                            log.write(block[:remaining])
                            remaining = max(0, remaining - len(block))
            except (OSError, ValueError):
                # Continue draining if opening/writing the log failed, avoiding a full pipe.
                try:
                    for _ in iter(lambda: process.stdout.read(8192), b''):
                        pass
                except (OSError, ValueError):
                    pass
        logger = threading.Thread(target=drain, daemon=True)
        logger.start()
        return process, logger

    @staticmethod
    def _stop(process):
        if process.poll() is not None:
            return
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def check(self, package, jobroot, mode='install'):
        args, environment, sockets, _ = self._prepare(package, jobroot, True, mode)
        process, logger = self._launch(args, environment, sockets)
        timeout = self.policy.get('swupdate', {}).get('checkTimeoutSeconds', 300)
        try:
            code = process.wait(timeout=timeout)
            if code != 0:
                raise UpdateError('SWUPDATE_CHECK_FAILED')
            return {'success': True, 'returnCode': 0}
        except subprocess.TimeoutExpired as exc:
            self._stop(process)
            raise UpdateError('SWUPDATE_CHECK_TIMEOUT') from exc
        finally:
            logger.join(timeout=2)

    def install(self, package, jobroot, on_progress, mode='install', boot_recovery=False):
        if not hasattr(socket, 'AF_UNIX'):
            raise UpdateError('SWUPDATE_PLATFORM_UNSUPPORTED')
        args, environment, sockets, socket_path = self._prepare(package, jobroot, False, mode, boot_recovery)
        process, logger = self._launch(args, environment, sockets)
        config = self.policy.get('swupdate', {})
        timeout = (config.get('rescueTimeoutSeconds', 300) if mode == 'rescue'
                   else config.get('installTimeoutSeconds', 3600))
        deadline = self.clock() + timeout
        receiver = _ProgressReceiver(process, socket_path, environment['MINGDA_SWUPDATE_READY'], self.socket_factory)
        progress_error = None
        barrier_published = False
        callback_error_type = barrier_error_type = None
        last_detail = None
        exit_deadline = None
        result_error = None
        code = None
        progress_file = Path(jobroot) / 'script-progress.json'
        if progress_file.exists():
            progress_file.unlink()
        receiver.start()
        try:
            while True:
                if self.clock() >= deadline:
                    self._stop(process)
                    raise UpdateError('SWUPDATE_TIMEOUT')
                code = process.poll()
                if code is not None and exit_deadline is None:
                    exit_deadline = self.clock() + 2
                events = receiver.take()
                before_publish = receiver.snapshot()
                if (before_publish['acknowledged'] and before_publish['receiverError'] is None
                        and not before_publish['failureSeen'] and not barrier_published and barrier_error_type is None):
                    try:
                        atomic_bytes(environment['MINGDA_SWUPDATE_READY'], b'ACK\n')
                        barrier_published = True
                    except Exception as error:
                        barrier_error_type = type(error).__name__
                        if progress_error is None:
                            progress_error = UpdateError('SWUPDATE_PROGRESS_BARRIER_SAVE_FAILED')
                for event in events:
                    try:
                        on_progress(event)
                    except Exception as error:
                        if callback_error_type is None:
                            callback_error_type = type(error).__name__
                        if progress_error is None:
                            progress_error = UpdateError('SWUPDATE_PROGRESS_SAVE_FAILED')
                detail = _script_progress(progress_file)
                if detail is not None and detail != last_detail:
                    try:
                        on_progress(detail)
                    except Exception as error:
                        if callback_error_type is None:
                            callback_error_type = type(error).__name__
                        if progress_error is None:
                            progress_error = UpdateError('SWUPDATE_PROGRESS_SAVE_FAILED')
                    last_detail = detail
                snapshot = receiver.snapshot()
                # Receiving remains independent even while a callback fsyncs.
                # Do not stop at SUCCESS: later FAILURE/bad/partial frames must
                # still be consumed before declaring a confirmed result.
                if code is not None and snapshot['receiverClosed']:
                    if not snapshot['queuedEvents']:
                        break
                    continue  # Drain already received terminal events even after a slow callback.
                if exit_deadline is not None and self.clock() >= exit_deadline:
                    break
                if snapshot['receiverClosed']:
                    self.sleep(0.025)
            snapshot = receiver.snapshot()
            if code != 0 or snapshot['failureSeen']:
                raise UpdateError('SWUPDATE_FAILED')
            if (progress_error is not None or snapshot['receiverError'] is not None
                    or not snapshot['acknowledged'] or not snapshot['successSeen']
                    or not barrier_published
                    or snapshot['pendingBytes'] or snapshot['queuedEvents']
                    or not snapshot['receiverClosed'] or not snapshot['receiverEOF']):
                raise UpdateError('SWUPDATE_PROGRESS_INCOMPLETE')
            return {'success': True, 'returnCode': 0, 'nativeStatus': 'SUCCESS',
                    'lastProgress': snapshot['lastProgress']}
        except BaseException as error:
            result_error = error
            if process.poll() is None:
                self._stop(process)
            raise
        finally:
            receiver_stopped = receiver.stop()
            snapshot = receiver.snapshot()
            receiver_error = snapshot.pop('receiverError')
            diagnostic_error = progress_error or receiver_error
            diagnostic = {key: value for key, value in snapshot.items() if key != 'lastProgress'}
            diagnostic.update(returnCode=process.poll(), receiverStopped=receiver_stopped,
                              barrierPublished=barrier_published,
                              progressErrorCode=getattr(diagnostic_error, 'code', None),
                              progressErrorType=type(diagnostic_error).__name__ if diagnostic_error else None,
                              receiverErrorCode=getattr(receiver_error, 'code', None),
                              receiverErrorType=type(receiver_error).__name__ if receiver_error else None,
                              callbackErrorType=callback_error_type, barrierErrorType=barrier_error_type,
                              resultErrorCode=getattr(result_error, 'code', None),
                              resultErrorType=type(result_error).__name__ if result_error else None)
            try:
                atomic_json(sockets / 'native-result.json', diagnostic)
            except Exception:
                if result_error is None:
                    raise UpdateError('SWUPDATE_PROGRESS_INCOMPLETE')
            finally:
                logger.join(timeout=2)
            if not receiver_stopped and result_error is None:
                raise UpdateError('SWUPDATE_PROGRESS_INCOMPLETE')


def check(package, policy, jobroot, mode='install'):
    return SWUpdateRunner(policy).check(package, jobroot, mode)


def install(package, policy, jobroot, on_progress, mode='install', boot_recovery=False):
    return SWUpdateRunner(policy).install(package, jobroot, on_progress, mode, boot_recovery)
