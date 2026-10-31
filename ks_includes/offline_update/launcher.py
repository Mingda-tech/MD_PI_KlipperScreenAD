"""Fixed privileged entry point. Accepts operations/opaque IDs, never commands or paths."""

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .common import (DEFAULT_LOCK, UpdateError, atomic_bytes, atomic_json, command, file_lock,
                     identifier, read_json, sync_dir, trusted_path, worker_running)
from .swu_engine import Engine
from .state import Store, TERMINAL
from .usb import discover, open_package
from .housekeeping import prune_jobs

CONFIG = Path('/etc/klipperscreen-update/policy.json')
STATE = Path('/var/lib/klipperscreen-updates')
RUNTIME = Path('/usr/libexec/klipperscreen-update')
SYSTEM_UNITS = Path('/etc/systemd/system')

# A version directory and a frozen job both carry their own immutable policy.
LOCAL_RUNTIME = Path(__file__).absolute().parents[1]
if (LOCAL_RUNTIME / 'runtime.json').is_file():
    RUNTIME = LOCAL_RUNTIME
    CONFIG = LOCAL_RUNTIME / 'policy.json'


def setup_files():
    return (
        ('policy', CONFIG),
        ('key', CONFIG.parent / 'release-public.pem'),
        ('engine', RUNTIME / 'swupdate'),
        ('receipt', RUNTIME / 'build-info.json'),
        ('engineConfig', RUNTIME / 'swupdate.config'),
        ('inventory', STATE / 'inventory.json'),
        ('recoveryService', SYSTEM_UNITS / 'klipperscreen-update-recovery.service'),
    )


def setup_status():
    """Report absent deployment files; formal admission still validates their contents."""
    missing = [name for name, path in setup_files() if not path.is_file()]
    return {'setupRequired': bool(missing), 'setupMissing': missing}


def trusted(path):
    return trusted_path(path)


def load_policy():
    policy = read_json(trusted(CONFIG))
    if CONFIG.parent == RUNTIME and (RUNTIME / 'runtime.json').is_file():
        # A task's native engine/key must stay in its frozen runner, too.
        libraries = policy['swupdate'].get('libraryFiles', {})
        policy['publicKey'] = str(RUNTIME / 'release-public.pem')
        policy['swupdate'].update(binary=str(RUNTIME / 'swupdate'),
                                  buildReceipt=str(RUNTIME / 'build-info.json'),
                                  buildConfig=str(RUNTIME / 'swupdate.config'),
                                  libraryFiles={str(RUNTIME / 'lib' / Path(name).name): checksum
                                                for name, checksum in libraries.items()})
    trusted(policy['publicKey'])
    if policy.get('schemaVersion') != 2 or policy.get('engine') != 'swupdate':
        raise UpdateError('POLICY_SCHEMA')
    return policy


def start_worker(store, tx, action):
    job = store.directory(tx['jobId'])
    runner = job / 'runner'
    if not runner.exists():
        trusted(RUNTIME)
        shutil.copytree(RUNTIME, runner, symlinks=False)
        sync_dir(job)
    unit = 'klipperscreen-update-' + tx['jobId']
    command(['systemd-run', '--unit=' + unit, '--service-type=exec', '--collect',
             '--property=UMask=0077', '--property=TimeoutStopSec=30',
             '/usr/bin/python3', '-I', str(runner / 'worker.py'), action, tx['jobId']], timeout=10)


def latest(store):
    if not (store.root / 'latest.json').exists():
        return {'ok': True, 'phase': 'IDLE', **setup_status()}
    return status(store, read_json(store.root / 'latest.json')['jobId'])


def status(store, job_id):
    result = read_json(store.directory(job_id) / 'status.json')
    if result['phase'] not in TERMINAL | {'PREPARED', 'RECOVERY_REQUIRED'}:
        # A killed helper must not leave the UI displaying progress indefinitely.
        try:
            active = command(['systemctl', 'show', 'klipperscreen-update-' + job_id,
                              '--property=ActiveState', '--value'], timeout=3)
        except Exception:
            active = 'unknown'
        tx = store.load(job_id)
        if active not in ('active', 'activating') and time.time() - tx['updatedAt'] > 15:
            result.update(phase='RECOVERY_REQUIRED', recoveryRequired=True,
                          error='EXECUTOR_INTERRUPTED', canCancel=False)
    return {**result, **setup_status()}


def ensure_available(store):
    if store.marker.exists():
        raise UpdateError('UPDATE_BUSY')
    if store.jobs.exists():
        for path in store.jobs.iterdir():
            if store.load(path.name)['phase'] not in TERMINAL:
                raise UpdateError('PENDING_TASK_EXISTS')


def import_package(store, tx):
    directory = store.directory(tx['jobId'])
    try:
        source_info = tx['source']
        if shutil.disk_usage(directory).free < source_info['size'] * 2 + 128 * 1024**2:
            raise UpdateError('INSUFFICIENT_SPACE')
        with open_package(source_info) as source, (directory / 'package.partial').open('xb') as target:
            copied = 0
            reported_at = time.monotonic()
            while True:
                if (directory / 'cancel').exists():
                    raise UpdateError('CANCELLED')
                block = source.read(1024 * 1024)
                if not block:
                    break
                if copied + len(block) > source_info['size']:
                    raise UpdateError('USB_PACKAGE_CHANGED')
                target.write(block)
                copied += len(block)
                tx['progress'] = min(9, int(copied * 10 / max(source_info['size'], 1)))
                now = time.monotonic()
                if now - reported_at >= 1:
                    store.save(tx)
                    reported_at = now
            target.flush()
            os.fsync(target.fileno())
        if copied != source_info['size']:
            raise UpdateError('USB_COPY_INCOMPLETE')
        os.replace(directory / 'package.partial', directory / 'package.mdu')
        sync_dir(directory)
        store.save(tx)  # Persist the completed copy even when it took less than a second.
        Engine(load_policy(), store).prepare(tx['jobId'])
    except Exception as exc:
        tx['error'] = exc.code if isinstance(exc, UpdateError) else 'IMPORT_FAILED'
        tx['phase'] = 'CANCELLED' if tx['error'] == 'CANCELLED' else 'FAILED'
        store.save(tx)


def cancel_task(store, job_id):
    """Close a stopped task, never kill an installer or claim a rollback.

    The caller owns control.lock. Also used by the new screen's bootstrap so
    pending jobs can be cancelled without replacing their frozen old runner.
    """
    tx = store.load(job_id)
    running = worker_running(job_id)
    directory = store.directory(job_id)
    if tx['phase'] in ('IMPORTING', 'VERIFYING') and not tx.get('sideEffectsMayHaveStarted') and running is not False:
        atomic_bytes(directory / 'cancel', b'cancel\n')
        return
    if running is not False:
        raise UpdateError('UPDATE_BUSY')
    with file_lock(DEFAULT_LOCK), file_lock(directory / 'worker.lock'):
        tx = store.load(job_id)
        if tx['phase'] == 'CANCELLED':
            store.release(tx)  # Retry a cancellation interrupted after its durable state write.
            return
        interrupted = (tx['phase'] in ('IMPORTING', 'VERIFYING', 'INSTALLING', 'FLASHING', 'HEALTH_CHECK')
                       and time.time() - tx['updatedAt'] > 15)
        if tx['phase'] not in ('PREPARED', 'FAILED', 'RECOVERY_REQUIRED') and not interrupted:
            raise UpdateError('CANCEL_NOT_ALLOWED')
        if tx.get('commitIntent'):
            raise UpdateError('CANCEL_COMMIT_PENDING')
        if store.marker.exists() and read_json(store.marker).get('jobId') != job_id:
            raise UpdateError('UPDATE_BUSY')
        atomic_bytes(directory / 'cancel', b'cancel\n')
        changed = tx.get('sideEffectsMayHaveStarted') is not False
        tx.update(cancelledFromPhase=tx['phase'], cancelledAt=time.time(), phase='CANCELLED',
                  awaitingUiReady=False, recoveryArtifactsRetained=changed,
                  resolvedBeforeInstallation=not changed)
        store.save(tx)
        store.release(tx)


def worker(action, job_id):
    policy, store = load_policy(), Store(STATE)
    with file_lock(store.directory(job_id) / 'worker.lock'):
        if action == 'prepare':
            import_package(store, store.load(job_id))
        elif action in ('start', 'recover'):
            if action == 'recover':
                tx = store.load(job_id)
                if tx['phase'] not in TERMINAL | {'PREPARED', 'RECOVERY_REQUIRED'}:
                    tx['phase'] = 'RECOVERY_REQUIRED'
                    store.save(tx)
            try:
                Engine(policy, store).run(job_id, recovering=action == 'recover')
            except Exception as exc:
                tx = store.load(job_id)
                tx['error'] = exc.code if isinstance(exc, UpdateError) else 'EXECUTOR_FAILED'
                if tx['phase'] == 'PREPARED':
                    tx['phase'] = 'FAILED'
                else:
                    tx['phase'] = 'RECOVERY_REQUIRED'
                store.save(tx)
        else:
            raise UpdateError('INVALID_ACTION')
    if store.load(job_id)['phase'] in TERMINAL:
        schedule_cleanup()


def schedule_cleanup():
    # The stable bridge survives removal of this task's frozen runner. Its
    # separate worker waits until this unit has exited before reclaiming files.
    try:
        command(['systemd-run', '--unit=klipperscreen-update-cleanup-' + uuid.uuid4().hex[:8],
                 '--service-type=exec', '--collect', '--property=UMask=0077',
                 '/usr/bin/python3', '-I', '/usr/libexec/klipperscreen-update/launcher', 'cleanup'], timeout=10)
    except Exception:
        logging.warning('Offline update cleanup will retry at the next scan or startup')


def cleanup_history(store):
    deadline = time.monotonic() + 120
    while True:
        try:
            with file_lock(store.root / 'control.lock'), file_lock(DEFAULT_LOCK):
                if prune_jobs(store):
                    return
        except UpdateError:
            pass
        if time.monotonic() >= deadline:
            return  # Retain evidence and retry at the next scan/startup.
        time.sleep(2)


def _process_identity(proc):
    # comm can contain spaces and closing parentheses; the fields after its
    # final ')' still have fixed positions (state, ppid, ..., starttime).
    header, fields = (proc / 'stat').read_text().rsplit(')', 1)
    fields = fields.split()
    pid, parent, started = int(header.split(' ', 1)[0]), int(fields[1]), int(fields[19])
    if pid != int(proc.name) or parent < 0 or started < 1 or fields[0] in ('Z', 'X'):
        raise ValueError('Invalid process identity')
    return pid, parent, started, proc.stat().st_uid


def _screen_process(identity):
    # Cage/logind can move the whole screen tree into a login-session scope.
    # systemd's fixed service MainPID remains the anchor even in that layout.
    query = ['systemctl', 'show', 'KlipperScreen.service', '--property=MainPID', '--value']
    try:
        main = command(query, timeout=3)
        if not main.isdecimal() or int(main) < 2:
            return False
        processes = Path('/proc')
        anchor = _process_identity(processes / main)
        current, chain, seen = identity, [], set()
        for _ in range(64):
            if current[0] in seen:
                return False
            seen.add(current[0])
            chain.append(current)
            if current[0] == anchor[0]:
                # Recheck both the service and process start times before ACK.
                # A disappearing/reused PID or restarted service is not proof.
                return (current == anchor and command(query, timeout=3) == main
                        and all(_process_identity(processes / str(row[0])) == row for row in chain))
            if current[1] < 2:
                return False
            parent = _process_identity(processes / str(current[1]))
            if parent[2] > current[2]:
                return False
            current = parent
    except (OSError, ValueError, IndexError, UpdateError, subprocess.TimeoutExpired):
        return False
    return False


def ui_ready(store, job_id, pid):
    if not isinstance(pid, str) or not pid.isdecimal() or len(pid) > 10 or int(pid) < 2:
        raise UpdateError('INVALID_PID')
    tx = store.load(job_id)
    if not tx.get('awaitingUiReady') or tx['phase'] != 'HEALTH_CHECK':
        raise UpdateError('UI_ACK_NOT_EXPECTED')
    try:
        identity = _process_identity(Path('/proc') / pid)
    except (OSError, ValueError, IndexError) as exc:
        raise UpdateError('UI_PROCESS_MISMATCH') from exc
    if 'SUDO_UID' in os.environ and identity[3] != int(os.environ['SUDO_UID']):
        raise UpdateError('UI_PID_OWNER_MISMATCH')
    if not _screen_process(identity):
        raise UpdateError('UI_PROCESS_MISMATCH')
    atomic_json(store.directory(job_id) / 'ui-ready.json', {'pid': int(pid), 'at': time.time()})
    return {'ok': True}


def dispatch(argv):
    if os.geteuid() != 0:
        raise UpdateError('ROOT_REQUIRED')
    store = Store(STATE)
    store.root.mkdir(mode=0o711, parents=True, exist_ok=True)
    trusted(store.root)
    if not argv:
        raise UpdateError('INVALID_ACTION')
    action, args = argv[0], argv[1:]
    if action == 'status' and len(args) <= 1:
        return status(store, identifier(args[0])) if args else latest(store)
    if action == 'ui-ready' and len(args) == 2:
        return ui_ready(store, identifier(args[0]), args[1])
    if action == 'boot-recover' and not args and 'SUDO_UID' not in os.environ:
        Engine(load_policy(), store).boot_recover()
        schedule_cleanup()
        return {'ok': True}
    if action == 'cleanup' and not args and 'SUDO_UID' not in os.environ:
        cleanup_history(store)
        return {'ok': True}
    with file_lock(store.root / 'control.lock'):
        if action == 'scan' and not args:
            try:
                with file_lock(DEFAULT_LOCK):
                    prune_jobs(store)
            except UpdateError:
                pass  # Scanning can still proceed while an installation owns the lock.
            rows, warnings = discover()
            atomic_json(store.root / 'scan.json', {row['id']: row for row in rows})
            return {'ok': True, 'packages': [{k: row[k] for k in ('id', 'name', 'size', 'mount')} for row in rows],
                    'warnings': warnings, **setup_status()}
        if action == 'prepare' and len(args) == 1:
            ensure_available(store)
            with file_lock(DEFAULT_LOCK):
                prune_jobs(store)
            if setup_status()['setupRequired']:
                raise UpdateError('OFFLINE_UPDATE_NOT_CONFIGURED')
            source = read_json(store.root / 'scan.json').get(identifier(args[0]))
            if not source:
                raise UpdateError('PACKAGE_NOT_FOUND')
            tx = {'jobId': uuid.uuid4().hex, 'phase': 'IMPORTING', 'progress': 0, 'source': source,
                  'createdAt': time.time(), 'engine': 'swupdate', 'sideEffectsMayHaveStarted': False}
            store.save(tx)
            try:
                start_worker(store, tx, 'prepare')
            except Exception:
                tx.update(phase='FAILED', error='EXECUTOR_START_FAILED')
                store.save(tx)
                raise
            return {'ok': True, 'jobId': tx['jobId']}
        if action in ('start', 'recover', 'cancel') and len(args) == 1:
            tx = store.load(identifier(args[0]))
            if action == 'cancel':
                cancel_task(store, tx['jobId'])
            else:
                current = status(store, tx['jobId'])
                expected = 'PREPARED' if action == 'start' else 'RECOVERY_REQUIRED'
                if current['phase'] != expected:
                    raise UpdateError('INVALID_TASK_PHASE')
                start_worker(store, tx, action)
            return {'ok': True, 'jobId': tx['jobId']}
    raise UpdateError('INVALID_ACTION')


def main():
    try:
        result = dispatch(sys.argv[1:])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': exc.code if isinstance(exc, UpdateError) else 'INSTALLER_ERROR'}))
        raise SystemExit(1)
