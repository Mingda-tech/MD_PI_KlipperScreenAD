#!/usr/bin/env python3
"""Run the authenticated publisher platform installer in its offline mode.

The original installer owns its supported checks and ordinary error rollback.
This bridge deliberately does not claim replay recovery after an interrupted
installation, and never substitutes the managed SDK updater's old-API checks.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive import (prepare_archive, extract_prepared_archive, load_published_archive,
                     extract_published_archive)


WORKSPACE_ROOT = Path('/opt/mingda/.offline-platform-updates')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
SPEC_FIELDS = {'schemaVersion', 'installMode', 'releaseId', 'source', 'sdkVersion', 'updaterVersion'}
AUDIT_FIELDS = {'name', 'sha256', 'bytes'}
PROGRESS_INTERVAL = 5.0
SOURCE_FIELDS = {'filename', 'sha256', 'bytes', 'root', 'regularBytes', 'entryCount'}
PHASES = {'NEW', 'EXTRACTING', 'SOURCE_READY', 'VERIFIED', 'INSTALLING', 'COMPLETE', 'FAILED'}


class PlatformUpdateError(RuntimeError):
    pass


def safe(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise PlatformUpdateError('A platform task path contains a link or traversal')
    return path


def digest(path):
    checksum = hashlib.sha256()
    with safe(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def read_json(path):
    path = safe(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise PlatformUpdateError('Platform task metadata is missing or too large')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as error:
        raise PlatformUpdateError('Platform task metadata is invalid') from error
    if not isinstance(value, dict):
        raise PlatformUpdateError('Platform task metadata must be an object')
    return value


def sync_dir(path):
    if os.name != 'posix':
        return
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value, mode=0o600):
    path = safe(path)
    temporary = path.with_name('.' + path.name + '.partial')
    safe(temporary)
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), mode)
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_progress(path, value):
    """Progress is replaceable observation; transaction checkpoints remain durable."""
    path = safe(path)
    temporary = path.with_name('.' + path.name + '.progress-partial')
    safe(temporary)
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o644)
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class PlatformInstaller:
    def __init__(self, materials=None, job=None, run=subprocess.run, require_root=True,
                 workspace_root=WORKSPACE_ROOT):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise PlatformUpdateError('Platform installation requires Linux root execution')
        self.strict = require_root
        self.materials = safe(materials or os.environ['TMPDIR'])
        self.job = safe(job or os.environ['MINGDA_JOB_DIR'])
        if not self.materials.is_dir() or not self.job.is_dir() or not IDENTIFIER.fullmatch(self.job.name):
            raise PlatformUpdateError('Invalid persistent platform task workspace')
        self.run = run
        self.spec_path = safe(self.materials / 'platform-release.json')
        self.spec = read_json(self.spec_path)
        self.spec_sha256 = digest(self.spec_path)
        self._validate_spec()
        self.audit_path = safe(self.materials / self.spec['audit']['name']) if self.spec['schemaVersion'] == 2 else None
        self.archive = safe(self.materials / self.spec['source']['filename'])
        self.workspace_root = safe(workspace_root)
        if self.workspace_root == self.workspace_root.parent:
            raise PlatformUpdateError('The platform extraction root cannot be a filesystem root')
        self.workspace = safe(self.workspace_root / self.job.name)
        self.staging = safe(self.workspace_root / ('.' + self.job.name + '.extracting'))
        self.source = self.workspace / self.spec['source']['root']
        self.state_path = safe(self.job / 'sdk-platform-state.json')
        self._progress_lock = threading.Lock()
        self._started = time.monotonic()
        self._progress_state = None
        self._progress_written = float('-inf')
        self._progress_highwater = 0
        self._source_audit = None
        self.state = read_json(self.state_path) if self.state_path.exists() else None
        self._timings = dict((self.state or {}).get('timings', {}))
        if self.state is not None:
            if (self.state.get('phase') not in PHASES
                    or any(self.state.get(key) != value for key, value in self.identity().items())):
                raise PlatformUpdateError('Platform recovery metadata belongs to another task or package')

    def _validate_spec(self):
        value = self.spec
        version = value.get('schemaVersion')
        fields = SPEC_FIELDS | {'audit'} if version == 2 else SPEC_FIELDS
        if (set(value) != fields or type(version) is not int
                or version not in (1, 2) or value.get('installMode') != 'platform-offline'
                or not isinstance(value.get('releaseId'), str) or not IDENTIFIER.fullmatch(value['releaseId'])):
            raise PlatformUpdateError('Invalid platform release declaration')
        for key in ('sdkVersion', 'updaterVersion'):
            if not isinstance(value.get(key), str) or not re.fullmatch(r'\d+\.\d+\.\d+', value[key]):
                raise PlatformUpdateError('Invalid declared platform component version')
        source = value.get('source')
        if (not isinstance(source, dict) or set(source) != SOURCE_FIELDS
                or source.get('filename') != 'platform.tar.gz'
                or not isinstance(source.get('sha256'), str) or not SHA256.fullmatch(source['sha256'])
                or not isinstance(source.get('root'), str) or not IDENTIFIER.fullmatch(source['root'])):
            raise PlatformUpdateError('Invalid original platform archive declaration')
        for key, maximum in (('bytes', 8 * 1024 ** 3), ('regularBytes', 8 * 1024 ** 3), ('entryCount', 500000)):
            if type(source.get(key)) is not int or not 0 < source[key] <= maximum:
                raise PlatformUpdateError('Invalid original platform archive size or entry count')
        if version == 2:
            audit = value['audit']
            if (not isinstance(audit, dict) or set(audit) != AUDIT_FIELDS
                    or audit.get('name') != 'platform-audit.json'
                    or not isinstance(audit.get('sha256'), str) or not SHA256.fullmatch(audit['sha256'])
                    or type(audit.get('bytes')) is not int or not 0 < audit['bytes'] <= 2 * 1024 ** 2):
                raise PlatformUpdateError('Invalid signed platform audit reference')

    def identity(self):
        return {'schemaVersion': 1, 'jobId': self.job.name, 'releaseId': self.spec['releaseId'],
                'specSha256': self.spec_sha256, 'sourceSha256': self.spec['source']['sha256'],
                'workspace': str(self.workspace)}

    def save(self, phase, **fields):
        value = dict(self.state or self.identity())
        value.update(fields, phase=phase, updatedAt=time.time(), timings=dict(self._timings))
        atomic_json(self.state_path, value)
        self.state = value

    def progress(self, percent, message, stage, completed=None, total=None, unit='entries', force=False):
        now = time.monotonic()
        with self._progress_lock:
            previous = self._progress_state or {}
            self._progress_highwater = max(self._progress_highwater, min(99, max(0, int(percent))))
            value = {'phase': 'INSTALLING', 'component': 'sdk', 'progress': self._progress_highwater,
                     'stage': stage, '_message': str(message)[:320]}
            if completed is not None or total is not None:
                if (type(completed) is not int or type(total) is not int
                        or not 0 <= completed <= total or total < 1):
                    raise PlatformUpdateError('Invalid platform work counters')
                value.update(completed=completed, total=total, unit=unit)
            self._progress_state = value
            finished = total is not None and completed == total
            if force or finished or previous.get('stage') != stage or now - self._progress_written >= PROGRESS_INTERVAL:
                self._write_progress(now)

    def _write_progress(self, now):
        value = dict(self._progress_state)
        elapsed = int(now - self._started)
        message = value.pop('_message')
        if 'completed' in value:
            message += ' | {:,}/{:,} {} ({}%)'.format(
                value['completed'], value['total'], value['unit'], value['completed'] * 100 // value['total'])
        # Existing screens render message, but do not render progressDetail.
        value.update(message=(message + ' | elapsed ' + str(elapsed) + ' s')[:512], elapsedSeconds=elapsed)
        atomic_progress(self.job / 'script-progress.json', value)
        self._progress_written = now

    @contextlib.contextmanager
    def reporting_wait(self, percent, message, stage):
        stopped = threading.Event()
        self.progress(percent, message, stage)

        def heartbeat():
            while not stopped.wait(PROGRESS_INTERVAL):
                with self._progress_lock:
                    if self._progress_state and self._progress_state['stage'] == stage:
                        self._write_progress(time.monotonic())

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=3)

    @contextlib.contextmanager
    def timing(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self._timings[name] = round(time.monotonic() - started, 3)

    def _guard_replay(self):
        if self.state and self.state['phase'] in ('INSTALLING', 'FAILED'):
            raise PlatformUpdateError(
                'The previous platform attempt needs manual review; automatic installation replay is disabled'
            )

    def _trusted_directory(self, path):
        path = safe(path)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise PlatformUpdateError('The platform source workspace is not a directory')
        if self.strict and (info.st_uid != 0 or info.st_mode & 0o022):
            raise PlatformUpdateError('Platform source directories must be root-owned and protected')
        return path

    def _ensure_workspace_root(self):
        # Check existing ancestors before creating anything below the fixed
        # root; no ordinary user may choose or replace source workspace names.
        if self.strict:
            for path in (self.workspace_root, *self.workspace_root.parents):
                if path.exists():
                    self._trusted_directory(path)
        missing = []
        current = self.workspace_root
        while not current.exists():
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            path.mkdir(mode=0o755)
            # The native task uses a private umask. Newly created public
            # source parents must remain traversable for mingda probes.
            os.chmod(path, 0o755)
        self._trusted_directory(self.workspace_root)
        os.chmod(self.workspace_root, 0o755)

    def _verify_archive(self):
        if (digest(self.spec_path) != self.spec_sha256
                or not self.archive.is_file() or not stat.S_ISREG(self.archive.stat().st_mode)
                or self.archive.stat().st_size != self.spec['source']['bytes']):
            raise PlatformUpdateError('The signed original platform archive has changed')
        if self.audit_path is not None:
            audit = self.spec['audit']
            if (not self.audit_path.is_file() or self.audit_path.stat().st_size != audit['bytes']
                    or digest(self.audit_path) != audit['sha256']):
                raise PlatformUpdateError('The signed platform safety audit has changed')
        elif digest(self.archive) != self.spec['source']['sha256']:
            raise PlatformUpdateError('The signed original platform archive has changed')

    def _published_archive(self):
        certificate = read_json(self.audit_path)
        expected = self.spec['source']
        prepared = load_published_archive(self.archive, certificate,
                                          expected_sha256=expected['sha256'],
                                          expected_entries=expected['entryCount'])
        facts = prepared.facts
        if (facts.get('root') != expected['root'] or facts.get('regularBytes') != expected['regularBytes']
                or facts.get('sdkVersion') != self.spec['sdkVersion']):
            raise PlatformUpdateError('Published platform audit differs from the signed release')
        return prepared

    def _check_source(self, workspace):
        workspace = self._trusted_directory(workspace)
        marker = read_json(workspace / '.source-identity.json')
        if marker != self.identity():
            raise PlatformUpdateError('The platform source workspace belongs to another task')
        source = self._trusted_directory(workspace / self.spec['source']['root'])
        if (source / 'lock-screen/manifest.json').exists() or (source / 'lock-screen/manifest.json').is_symlink():
            raise PlatformUpdateError('Platform bundles containing a screen patch require a dedicated package')
        for name in ('install.sh', 'scripts/platform-install.sh'):
            path = safe(source / name)
            if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
                raise PlatformUpdateError('The original platform installation entry is missing')
            if self.strict and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
                raise PlatformUpdateError('The original platform installation entry is not protected')
        return source

    def _remove_staging(self):
        path = safe(self.staging)
        if path.parent != self.workspace_root or path.name != '.' + self.job.name + '.extracting':
            raise PlatformUpdateError('Invalid platform extraction artifact path')
        if path.exists():
            self._trusted_directory(path)
            shutil.rmtree(path)
            sync_dir(path.parent)

    @staticmethod
    def _protect_tree(directory):
        # GNU find visits the tree once and batches only the chmod operations
        # that are needed. -P never follows the legitimate SDK file symlink.
        # Keep the staging root private until all descendants are protected.
        directory = safe(directory)
        command = ['/usr/bin/find', '-P', str(directory), '-mindepth', '1',
                   '(', '(', '-type', 'd', '(', '!', '-perm', '-0555', '-o', '-perm', '/022', ')',
                   '-exec', '/bin/chmod', 'a+rx,go-w', '--', '{}', '+', ')', '-o',
                   '(', '-type', 'f', '-perm', '/022',
                   '-exec', '/bin/chmod', 'go-w', '--', '{}', '+', ')', ')']
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=600, check=False,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        if result.returncode:
            raise PlatformUpdateError('Could not protect the extracted platform source permissions')

    def _prepare(self):
        self._verify_archive()
        self._ensure_workspace_root()
        published = None
        if self.audit_path is not None:
            with self.reporting_wait(3, 'Verifying the signed publisher archive audit', 'archive'), \
                    self.timing('publishedAuditSeconds'):
                published = self._published_archive()
        if self.workspace.exists():
            if self.state is None:
                raise PlatformUpdateError('A platform source workspace already exists without a task checkpoint')
            return self._check_source(self.workspace)
        if self.staging.exists() and (self.state is None or self.state['phase'] != 'EXTRACTING'):
            raise PlatformUpdateError('A foreign platform extraction workspace already exists')
        expected = self.spec['source']

        def audit_progress(completed, total):
            self.progress(3 + completed * 22 // total, 'Checking the original platform archive',
                          'archive', completed, total)

        if published is not None:
            prepared = published
        else:
            with self.reporting_wait(3, 'Checking the original platform archive', 'archive'), \
                    self.timing('legacyArchiveAuditSeconds'):
                prepared = prepare_archive(self.archive, expected_sha256=expected['sha256'],
                                           expected_entries=expected['entryCount'], progress=audit_progress)
        facts = prepared.facts
        self._source_audit = prepared
        if (facts.get('root') != expected['root'] or facts.get('regularBytes') != expected['regularBytes']
                or facts.get('entryCount') != expected['entryCount']
                or facts.get('sdkVersion') != self.spec['sdkVersion']):
            raise PlatformUpdateError('Original platform archive layout differs from its signed declaration')
        required = expected['regularBytes'] + 256 * 1024 ** 2
        if shutil.disk_usage(self.workspace_root).free < required:
            raise PlatformUpdateError('Insufficient space for the complete original platform source')
        if hasattr(os, 'statvfs') and os.statvfs(self.workspace_root).f_favail < expected['entryCount'] + 1000:
            raise PlatformUpdateError('Insufficient filesystem inodes for the original platform source')
        self.save('EXTRACTING', installationStarted=False)
        self._remove_staging()
        self.staging.mkdir(mode=0o700)
        try:
            def extract_progress(completed, total):
                self.progress(25 + completed * 55 // total, 'Extracting the original platform archive',
                              'extract', completed, total, unit=getattr(prepared, 'progress_unit', 'entries'))

            with self.reporting_wait(25, 'Extracting the original platform archive', 'extract'), \
                    self.timing('sourceExtractionSeconds'):
                extractor = extract_published_archive if published is not None else extract_prepared_archive
                extracted = extractor(prepared, self.staging, progress=extract_progress)
                if safe(extracted) != self.staging / expected['root']:
                    raise PlatformUpdateError('The extracted platform root differs from the release')
                self._verify_archive()
            with self.reporting_wait(80, 'Protecting extracted platform source permissions', 'permissions'), \
                    self.timing('sourcePermissionsSeconds'):
                if published is None or facts.get('requiresDirectoryPermissionFixes') is not False:
                    self._protect_tree(self.staging)
                    message = 'Platform source permissions verified'
                else:
                    # The authenticated audit proves every explicit directory
                    # already has public read/execute bits. Native umask 022
                    # removed ordinary write bits while creating all members.
                    message = 'Platform source permissions protected during native extraction'
                self.progress(88, message, 'permissions', force=True)
                atomic_json(self.staging / '.source-identity.json', self.identity(), 0o644)
                self._check_source(self.staging)
                os.chmod(self.staging, 0o755)
                os.replace(self.staging, self.workspace)
                sync_dir(self.workspace_root)
            self.save('SOURCE_READY', installationStarted=False)
        except Exception:
            # Installation has not started. Only this task's known extraction
            # artifact can be discarded on a subsequent explicit attempt.
            raise
        return self._check_source(self.workspace)

    def _run_source(self, verify_only):
        source = self._check_source(self.workspace)
        action = 'check' if verify_only else 'install'
        log_path = safe(self.job / ('sdk-platform-' + action + '.log'))
        descriptor = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        argv = ['/bin/bash', str(source / 'install.sh'), '--offline']
        if verify_only:
            argv.append('--verify-only')
        environment = dict(os.environ)
        environment.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8',
                           PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', MINGDA_OFFLINE_INSTALL='true',
                           TMPDIR='/tmp', TMP='/tmp', TEMP='/tmp')
        percent = 90 if verify_only else 93
        message = ('Running the original platform offline preflight' if verify_only
                   else 'Running the original platform offline installation')
        with os.fdopen(descriptor, 'a', encoding='utf-8') as log:
            os.fchmod(log.fileno(), 0o600)
            log.write('\n=== original platform ' + action + ' ===\n')
            log.flush()
            with self.reporting_wait(percent, message, action), self.timing('offline' + action.title() + 'Seconds'):
                result = self.run(argv, cwd=source, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, env=environment,
                                  timeout=1800 if verify_only else 7200, check=False)
            # Ordinary CompletedProcess mocks can return captured output even
            # though the real subprocess writes directly to the private log.
            for value in (getattr(result, 'stdout', None), getattr(result, 'stderr', None)):
                if value:
                    log.write(value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value))
            log.flush()
            os.fsync(log.fileno())
        if result.returncode != 0:
            raise PlatformUpdateError(
                'Original platform offline ' + action + ' failed; inspect ' + log_path.name
            )

    def check(self):
        if self.state and self.state['phase'] == 'COMPLETE':
            return
        self._guard_replay()
        self._prepare()
        try:
            self._run_source(True)
        except Exception:
            self.save('FAILED', installationStarted=False, manualRecoveryRequired=False, failedAction='check')
            raise
        self.save('VERIFIED', installationStarted=False)

    def install(self):
        if self.state and self.state['phase'] == 'COMPLETE':
            self._cleanup_complete_source()
            return
        self._guard_replay()
        if not self.state or self.state['phase'] != 'VERIFIED':
            self.check()
        else:
            self._prepare()
        self.save('INSTALLING', installationStarted=True, manualRecoveryRequired=True)
        try:
            self._run_source(False)
        except Exception:
            self.save('FAILED', installationStarted=True, manualRecoveryRequired=True, failedAction='install')
            raise
        self.save('COMPLETE', installationStarted=True, manualRecoveryRequired=False,
                  cloudReadiness='DEFERRED', cleanupPending=True)
        self._cleanup_complete_source()
        self.progress(99, 'Original platform offline installation completed; cloud readiness is deferred', 'complete')

    def _cleanup_complete_source(self):
        if not self.state or self.state['phase'] != 'COMPLETE':
            return
        try:
            workspace = safe(self.workspace)
            if workspace.parent != self.workspace_root or workspace.name != self.job.name:
                raise PlatformUpdateError('Invalid completed platform source workspace')
            if workspace.exists():
                self._trusted_directory(workspace)
                if read_json(workspace / '.source-identity.json') != self.identity():
                    raise PlatformUpdateError('Completed platform source identity cannot be confirmed')
                with self.reporting_wait(97, 'Removing the completed platform source workspace', 'cleanup'), \
                        self.timing('sourceCleanupSeconds'):
                    shutil.rmtree(workspace)
                sync_dir(self.workspace_root)
            self.save('COMPLETE', cleanupPending=False, sourceRemoved=True, cleanupError=None)
        except Exception as error:
            # A finished publisher installation remains successful when only
            # disposal of this task's public source files needs another try.
            # Its private checkpoint and command logs are never removed here.
            try:
                self.save('COMPLETE', cleanupPending=True, cleanupError=type(error).__name__)
            except OSError:
                print('Platform source cleanup could not save its retry state', file=sys.stderr)

    def failure(self):
        if self.state and self.state['phase'] == 'COMPLETE':
            self.save('COMPLETE', outerFailureReported=True)
            return
        started = bool(self.state and self.state.get('installationStarted'))
        self.save('FAILED', installationStarted=started, manualRecoveryRequired=started, outerFailureReported=True)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('check', 'install', 'failure'):
        raise SystemExit(2)
    try:
        getattr(PlatformInstaller(), sys.argv[1])()
    except Exception as error:
        print('Platform offline update: ' + str(error)[:240], file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == '__main__':
    main()
