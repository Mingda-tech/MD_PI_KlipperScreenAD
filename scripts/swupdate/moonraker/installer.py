#!/usr/bin/env python3
"""Offline Moonraker replacement with a private, replayable database baseline."""
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request


class UpdateFailure(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise UpdateFailure('A linked deployment or task directory is unsupported')
    return path


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, data, mode=0o600):
    path = safe_path(path)
    part = path.with_name('.' + path.name + '.partial')
    fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write((json.dumps(data, sort_keys=True) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(part, mode)
    os.replace(part, path)
    sync_dir(path.parent)


def sync_tree(path):
    for root, directories, files in os.walk(path, followlinks=False):
        for name in files:
            p = Path(root) / name
            if p.is_symlink():
                continue
            fd = os.open(str(p), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        sync_dir(root)


def exchange(left, right):
    left, right = safe_path(left), safe_path(right)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        raise UpdateFailure('Atomic directory exchange is unavailable')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        raise OSError(ctypes.get_errno(), 'Atomic directory exchange failed')
    sync_dir(left.parent)
    if right.parent != left.parent:
        sync_dir(right.parent)


def tree_manifest(path):
    path = safe_path(path)
    if not path.is_dir():
        raise UpdateFailure('A database baseline directory is missing')
    result = {}
    for p in sorted(path.rglob('*')):
        if p.is_symlink() or not (p.is_dir() or p.is_file()):
            raise UpdateFailure('A database baseline contains an unsupported entry')
        if p.is_file():
            result[str(p.relative_to(path))] = {'sha256': digest(p), 'bytes': p.stat().st_size}
    return result


class Installer:
    def __init__(self, materials=None, job=None, run=subprocess.run, api=None,
                 require_root=True, sleep=time.sleep, monotonic=time.monotonic):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise UpdateFailure('Linux root execution is required')
        self.materials = safe_path(materials or os.environ['TMPDIR'])
        self.job = safe_path(job or os.environ['MINGDA_JOB_DIR'])
        if (not self.materials.is_dir() or not self.job.is_dir()
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.job.name)):
            raise UpdateFailure('Invalid private task directory')
        self.spec = json.loads((self.materials / 'release.json').read_text())
        expected = {'component': 'moonraker', 'service': 'moonraker.service',
                    'targetPath': '/home/mingda/moonraker',
                    'environmentPath': '/home/mingda/moonraker-env',
                    'dataPath': '/home/mingda/printer_data',
                    'configurationPath': '/home/mingda/printer_data/config/moonraker.conf'}
        if (self.spec.get('schemaVersion') != 1 or self.spec.get('component') != 'moonraker'
                or not isinstance(self.spec.get('source'), dict)
                or not isinstance(self.spec.get('materials'), dict)
                or not isinstance(self.spec.get('dependencies'), dict)
                or not isinstance(self.spec.get('wheels'), dict)
                or (require_root and any(self.spec.get(k) != v for k, v in expected.items()))):
            raise UpdateFailure('Invalid Moonraker release declaration')
        self.target = safe_path(self.spec['targetPath'])
        self.environment = safe_path(self.spec['environmentPath'])
        self.data = safe_path(self.spec['dataPath'])
        self.config = safe_path(self.spec['configurationPath'])
        self.database = safe_path(self.data / 'database')
        self.stage = self.target.with_name('.moonraker-mdu-' + self.job.name)
        self.env_stage = self.environment.with_name('.moonraker-env-mdu-' + self.job.name)
        self.db_backup = self.job / 'moonraker-database-baseline'
        self.run, self.api = run, api or self.http_json
        self.sleep, self.monotonic = sleep, monotonic
        self.state_path = self.job / 'moonraker-state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if self.state and any(self.state.get(k) != v for k, v in self.identity().items()):
            raise UpdateFailure('Saved task and release do not match')

    def identity(self):
        return {'schemaVersion': 1, 'jobId': self.job.name, 'releaseId': self.spec['releaseId'],
                'specSha256': digest(self.materials / 'release.json'),
                'targetCommit': self.spec['source']['targetCommit']}

    def save(self, phase, **fields):
        self.state.update(fields, phase=phase, updatedAt=time.time())
        atomic_json(self.state_path, self.state)

    def progress(self, percent, message):
        atomic_json(self.job / 'script-progress.json', {'phase': 'INSTALLING',
                    'progress': min(99, max(0, percent)), 'component': 'moonraker',
                    'message': message}, 0o644)

    @staticmethod
    def http_json(endpoint):
        if not endpoint.startswith('/') or endpoint.startswith('//'):
            raise UpdateFailure('Only local printer API endpoints are allowed')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:7125' + endpoint, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateFailure('Local API response exceeds the size limit')
        return json.loads(raw)['result']

    def command(self, args, timeout=30):
        completed = self.run(list(map(str, args)), stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=timeout, check=False)
        if completed.returncode:
            # Do not persist subprocess output, which may contain database/config values.
            raise UpdateFailure('Command failed: ' + Path(str(args[0])).name)
        return completed.stdout.strip()

    def git(self, directory, *args):
        return self.command(['git', '--no-optional-locks', '-c', 'safe.directory=' + str(directory),
                             '-C', directory, *args])

    def service(self, name, *properties):
        raw = self.command(['systemctl', 'show', name, *sum((['-p', p] for p in properties), [])])
        return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)

    @staticmethod
    def arguments(pid):
        return (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')

    def deployment_check(self, running=True):
        props = self.service(self.spec['service'], 'User', 'ExecStart', 'MainPID')
        if props.get('User') != 'mingda' or str(self.environment / 'bin/python') not in props.get('ExecStart', ''):
            raise UpdateFailure('Moonraker service does not select the declared environment')
        if not running:
            return
        pid = props.get('MainPID', '0')
        if not pid.isdecimal() or int(pid) < 2:
            raise UpdateFailure('Moonraker is not running')
        argv = self.arguments(pid)
        if len(argv) < 2 or Path(argv[0]).absolute() != self.environment / 'bin/python':
            raise UpdateFailure('Unexpected Moonraker interpreter')
        if Path(argv[1]).absolute() != self.target / 'moonraker/moonraker.py':
            raise UpdateFailure('Unexpected Moonraker source entry point')
        # This package deliberately preserves the installed service and argument file.
        # Requiring explicit paths prevents silently updating another database/config.
        for flags, expected in ((('-d', '--datapath'), self.data), (('-c', '--configfile'), self.config)):
            values = [Path(argv[i + 1]).absolute() for i, arg in enumerate(argv[:-1]) if arg in flags]
            if not values and flags[0] == '-c' and self.config == self.data / 'config/moonraker.conf':
                continue  # Moonraker's normal default for the explicit data path.
            if values != [expected]:
                raise UpdateFailure('Moonraker service data/config arguments differ from this package')
        self._process_id = int(pid)

    def configuration_evidence(self):
        if not self.config.is_file() or not self.database.is_dir():
            raise UpdateFailure('Printer configuration or database directory is missing')
        rows = {}
        for p in self.config.parent.rglob('*'):
            if p.is_symlink():
                target = p.resolve(strict=True)
                if not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
                    raise UpdateFailure('Linked configuration must resolve to a bounded regular file')
                rows[str(p.relative_to(self.config.parent))] = {
                    'link': os.readlink(p), 'resolved': str(target), 'sha256': digest(target)}
            elif p.is_file():
                if len(rows) >= 4096 or p.stat().st_size > 16 * 1024 * 1024:
                    raise UpdateFailure('Configuration tree is too large')
                rows[str(p.relative_to(self.config.parent))] = digest(p)
        if self.state and rows != self.state['configurationFiles']:
            raise UpdateFailure('Printer configuration changed during the update')
        return rows

    def idle(self, api=None):
        api = api or self.api
        server = api('/server/info')
        if server.get('klippy_connected') is not True or server.get('klippy_state') != 'ready':
            raise UpdateFailure('Klipper must be connected and ready')
        status = api('/printer/objects/query?webhooks&print_stats&pause_resume&idle_timeout&manual_probe&heaters')['status']
        if (status.get('webhooks', {}).get('state') != 'ready'
                or status.get('print_stats', {}).get('state') not in ('standby', 'complete', 'cancelled', 'error')
                or status.get('pause_resume', {}).get('is_paused') is not False
                or status.get('idle_timeout', {}).get('state') != 'Idle'
                or status.get('manual_probe', {}).get('is_active') is not False):
            raise UpdateFailure('Printer is printing, paused, calibrating or unavailable')
        heater_names = status.get('heaters', {}).get('available_heaters')
        if not isinstance(heater_names, list) or not heater_names:
            raise UpdateFailure('Heater state is unavailable')
        import urllib.parse
        heaters = api('/printer/objects/query?' + '&'.join(urllib.parse.quote(n, safe='') for n in heater_names))['status']
        for name in heater_names:
            row = heaters.get(name, {})
            if (not isinstance(row.get('target'), (int, float)) or row['target'] != 0
                    or not isinstance(row.get('temperature'), (int, float)) or row['temperature'] > 60):
                raise UpdateFailure('Heaters must be off and below 60 C')
        return server

    def klippy_query(self, method, params):
        # Read-only Klipper UDS requests keep recovery safe when Moonraker is down.
        address = self.data / 'comms/klippy.sock'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(address))
            connection.sendall(json.dumps({'id': 1, 'method': method, 'params': params}).encode() + b'\x03')
            raw = b''
            while len(raw) <= 2 * 1024 * 1024:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                raw += chunk
                while b'\x03' in raw:
                    message, raw = raw.split(b'\x03', 1)
                    row = json.loads(message)
                    if row.get('id') == 1:
                        if 'error' in row or not isinstance(row.get('result'), dict):
                            raise UpdateFailure('Klipper recovery status could not be read')
                        return row['result']
        raise UpdateFailure('Klipper recovery status is unavailable')

    def direct_api(self, endpoint):
        if endpoint == '/server/info':
            info = self.klippy_query('info', {})
            return {'klippy_connected': True, 'klippy_state': info.get('state')}
        prefix = '/printer/objects/query?'
        if not endpoint.startswith(prefix):
            raise UpdateFailure('Unsupported recovery status request')
        from urllib.parse import unquote
        names = [unquote(name) for name in endpoint[len(prefix):].split('&')]
        return self.klippy_query('objects/query', {'objects': {name: None for name in names}})

    @staticmethod
    def boot_id():
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def safety_check(self):
        if self.state.get('stopIntent'):
            # Moonraker itself is the component being repaired: an accessible
            # API may still be disconnected from an otherwise ready Klipper.
            # Once disruption is possible, use Klipper's read-only UDS directly.
            self.idle(api=self.direct_api)
        else:
            self.idle()
        current = self.service('klipper.service', 'MainPID', 'ActiveState')
        if current != self.state['klipperService']:
            boot = self.boot_id()
            if (boot == self.state.get('bootId') or current.get('ActiveState') != 'active'
                    or not current.get('MainPID', '').isdecimal() or int(current['MainPID']) < 2):
                raise UpdateFailure('Klipper process changed without a machine reboot')
            # A real reboot can restart Klipper normally. Only adopt its new PID
            # after the same read-only ready/idle checks; never restart it here.
            self.save(self.state['phase'], bootId=boot, klipperService=current)
        self.configuration_evidence()

    def validate_materials(self):
        for name, checksum in self.spec['materials'].items():
            if '/' in name or '\\' in name or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
                raise UpdateFailure('Invalid material name')
            p = safe_path(self.materials / name)
            if not p.is_file() or digest(p) != checksum:
                raise UpdateFailure('A signed release material has changed')
        if digest(self.materials / 'moonraker.bundle') != self.spec['source']['bundleSha256']:
            raise UpdateFailure('Source bundle digest differs')
        if not re.fullmatch(r'[0-9a-f]{40}', self.spec['source']['targetCommit']):
            raise UpdateFailure('Invalid source commit')

    def source_baseline(self):
        if not self.target.is_dir() or not (self.target / '.git').is_dir() or not self.environment.is_dir():
            raise UpdateFailure('Installed Moonraker source/environment is missing')
        origin = self.git(self.target, 'remote', 'get-url', 'origin')
        if origin.rstrip('/').removesuffix('.git').lower() != self.spec['source']['expectedRemote'].removesuffix('.git').lower():
            raise UpdateFailure('Installed Moonraker has a different source origin')
        if self.git(self.target, 'status', '--porcelain', '--untracked-files=all'):
            raise UpdateFailure('Installed Moonraker has local source changes')
        head = self.git(self.target, 'rev-parse', 'HEAD')
        branch = self.git(self.target, 'symbolic-ref', '--short', 'HEAD')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,100}', branch):
            raise UpdateFailure('Unsupported Moonraker branch')
        # Unknown historic commits remain eligible. A known descendant is a downgrade.
        if head != self.spec['source']['targetCommit'] and head not in self.spec['source']['allowedAncestors']:
            check = self.run(['git', '-c', 'safe.directory=' + str(self.target), '-C', str(self.target),
                              'merge-base', '--is-ancestor', self.spec['source']['targetCommit'], head],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
            if check.returncode == 0:
                raise UpdateFailure('The installed Moonraker is newer than this source release')
        return {'head': head, 'origin': origin, 'branch': branch,
                'uid': self.target.stat().st_uid, 'gid': self.target.stat().st_gid,
                'envUid': self.environment.stat().st_uid, 'envGid': self.environment.stat().st_gid}

    def check(self):
        self.validate_materials()
        self.deployment_check(running=False)
        if platform.machine() != self.spec['architecture'] or list(sys.version_info[:2]) != self.spec['pythonVersion']:
            raise UpdateFailure('This dependency release requires ARM64 Python 3.10')
        if not self.state or not self.state.get('stopIntent'):
            self.deployment_check()
            self.configuration_evidence()
            self.idle()
            return self.source_baseline()
        # Recovery must work while the API is stopped. No new operation starts here.
        if not self.db_backup.is_dir() and self.state.get('databaseManifest') is not None:
            raise UpdateFailure('Saved database baseline is missing')
        return self.state['baseline']

    def marker(self, directory, kind):
        path = directory / ('.git/mdu-moonraker.json' if kind == 'source' else '.mdu-moonraker.json')
        if not path.exists():
            return False
        return json.loads(safe_path(path).read_text()) == self.identity()

    def extract_wheels(self):
        directory = safe_path(self.job / 'moonraker-wheels')
        if directory.exists():
            expected = set(self.spec['wheels'])
            complete = (set(p.name for p in directory.iterdir()) == expected
                        and all((directory / name).is_file() and not (directory / name).is_symlink()
                                and digest(directory / name) == checksum
                                for name, checksum in self.spec['wheels'].items()))
            if not complete:
                if self.state.get('stopIntent'):
                    raise UpdateFailure('Cached offline dependencies changed after service disruption')
                # A power loss during pre-stop extraction can leave a partial
                # cache. Only this task's private cache may be reconstructed.
                shutil.rmtree(directory)
        if not directory.exists():
            directory.mkdir(mode=0o700)
            with tarfile.open(self.materials / 'wheels.tar.gz', 'r:gz') as archive:
                seen, total = set(), 0
                for member in archive:
                    name = member.name
                    if (name not in self.spec['wheels'] or name in seen or not member.isfile()
                            or '/' in name or '\\' in name or not name.endswith('.whl')
                            or member.size > 100 * 1024 * 1024):
                        raise UpdateFailure('Unsafe or unexpected offline dependency')
                    seen.add(name)
                    total += member.size
                    if total > 512 * 1024 * 1024:
                        raise UpdateFailure('Offline dependency payload is too large')
                    with archive.extractfile(member) as source, (directory / name).open('xb') as output:
                        shutil.copyfileobj(source, output)
                if seen != set(self.spec['wheels']):
                    raise UpdateFailure('Offline dependency payload is incomplete')
        for name, checksum in self.spec['wheels'].items():
            if digest(safe_path(directory / name)) != checksum:
                raise UpdateFailure('Offline dependency digest differs')
        return directory

    def source_files_check(self, path):
        if self.git(path, 'rev-parse', 'HEAD') != self.spec['source']['targetCommit']:
            raise UpdateFailure('Source checkout does not match the signed commit')
        for name, checksum in self.spec['source']['targetFiles'].items():
            p = safe_path(path / name)
            if not p.is_file() or digest(p) != checksum:
                raise UpdateFailure('A staged Moonraker source file differs')
        if self.git(path, 'status', '--porcelain', '--untracked-files=all'):
            raise UpdateFailure('The staged source contains unexpected changes')

    def environment_check(self, path, source):
        script = """
import sys,importlib,importlib.metadata,json
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
dependencies=json.loads(sys.argv[2])
for name,version in dependencies.items():
 assert importlib.metadata.version(name)==version,name
import lmdb,streaming_form_data,PIL._imaging,libnacl,tornado,zeroconf
for name in json.loads(sys.argv[3]):
 # Application registers these objects directly; neither is a component module.
 if name in ('jsonrpc','internal_transport'):
  name='application'
 importlib.import_module('moonraker.components.'+name)
import moonraker.server
print('offline-imports-ok')
"""
        components = self.state.get('components', [])
        self.command([path / 'bin/python', '-I', '-B', '-c', script, source,
                      json.dumps(self.spec['dependencies']), json.dumps(components)], 120)
        self.command([path / 'bin/python', '-I', '-B', '-m', 'pip', 'check'], 60)

    def chown_tree(self, path, uid, gid):
        for root, directories, files in os.walk(path, followlinks=False):
            os.chown(root, uid, gid)
            for name in directories + files:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)

    def prepare(self):
        wheel_bytes = (self.materials / 'wheels.tar.gz').stat().st_size
        database_bytes = sum(row['bytes'] for row in tree_manifest(self.database).values())
        required = self.spec['source']['targetBytes'] * 2 + wheel_bytes * 8 + database_bytes * 2 + 128 * 1024 * 1024
        for directory in (self.target.parent, self.environment.parent, self.database.parent, self.job):
            if shutil.disk_usage(directory).free < required:
                raise UpdateFailure('Insufficient storage for source, environment and database recovery')
        if self.stage.exists() or self.env_stage.exists():
            # Rebuild only an incomplete stage captured before service disruption.
            if self.state.get('stopIntent'):
                return
            for p in (self.stage, self.env_stage):
                if p.exists():
                    shutil.rmtree(safe_path(p))
        self.progress(10, 'Preparing Moonraker source and offline Python dependencies')
        self.command(['git', 'clone', '--no-hardlinks', '--no-checkout', self.materials / 'moonraker.bundle', self.stage], 180)
        source = self.spec['source']
        branch = self.state['baseline']['branch']
        self.git(self.stage, 'checkout', '-B', branch, source['targetCommit'])
        self.git(self.stage, 'remote', 'set-url', 'origin', source['expectedRemote'])
        self.git(self.stage, 'update-ref', 'refs/remotes/origin/' + branch, source['targetCommit'])
        self.git(self.stage, 'branch', '--set-upstream-to=origin/' + branch, branch)
        self.source_files_check(self.stage)
        atomic_json(self.stage / '.git/mdu-moonraker.json', self.identity())
        wheels = self.extract_wheels()
        self.command(['/usr/bin/python3', '-I', '-B', '-m', 'venv', '--copies', self.env_stage], 120)
        self.command([self.env_stage / 'bin/python', '-I', '-B', '-m', 'pip', '--isolated',
                      'install', '--no-index', '--no-cache-dir', '--no-compile', '--disable-pip-version-check',
                      '--only-binary=:all:', '--no-deps', '--find-links', wheels,
                      '-r', self.materials / 'requirements.lock'], 600)
        self.environment_check(self.env_stage, self.stage)
        # venv console entry points must refer to the final stable prefix after rename.
        old, new = str(self.env_stage), str(self.environment)
        for p in (self.env_stage / 'bin').iterdir():
            if p.is_file() and not p.is_symlink():
                raw = p.read_bytes()
                if b'\0' not in raw[:2048]:
                    p.write_bytes(raw.replace(old.encode(), new.encode()))
        cfg = self.env_stage / 'pyvenv.cfg'
        cfg.write_text(cfg.read_text().replace(old, new))
        atomic_json(self.env_stage / '.mdu-moonraker.json', self.identity())
        baseline = self.state['baseline']
        self.chown_tree(self.stage, baseline['uid'], baseline['gid'])
        self.chown_tree(self.env_stage, baseline['envUid'], baseline['envGid'])
        sync_tree(self.stage)
        sync_tree(self.env_stage)
        self.save('STAGED')

    def stopped(self):
        self.safety_check()
        self.command(['systemctl', 'stop', self.spec['service']], 60)
        pid = self.service(self.spec['service'], 'MainPID').get('MainPID')
        if pid != '0':
            raise UpdateFailure('Moonraker has not stopped')

    def backup_database(self):
        if self.state.get('databaseManifest') is not None:
            if tree_manifest(self.db_backup) != self.state['databaseManifest']:
                raise UpdateFailure('Saved database baseline has changed')
            return
        if self.db_backup.exists():
            shutil.rmtree(safe_path(self.db_backup))
        self.save('DATABASE_BACKUP_INTENT')
        before = tree_manifest(self.database)
        shutil.copytree(self.database, self.db_backup, copy_function=shutil.copy2)
        sync_tree(self.db_backup)
        after = tree_manifest(self.db_backup)
        if after != before or tree_manifest(self.database) != before:
            raise UpdateFailure('The stopped database changed during backup')
        self.save('DATABASE_SAVED', databaseManifest=before)

    def assert_klipper_unchanged(self):
        current = self.service('klipper.service', 'MainPID', 'ActiveState')
        if current != self.state['klipperService']:
            raise UpdateFailure('Klipper process changed during the Moonraker update')

    def wait_healthy(self, target=True):
        deadline = self.monotonic() + 100
        last = 'Moonraker API is unavailable'
        while self.monotonic() < deadline:
            try:
                self.deployment_check()
                server = self.idle()
                if server.get('failed_components'):
                    raise UpdateFailure('Moonraker reports failed components')
                missing = set(self.state['components']) - set(server.get('components', []))
                if missing:
                    raise UpdateFailure('Previously enabled Moonraker components did not load')
                self.configuration_evidence()
                self.assert_klipper_unchanged()
                if target:
                    self.source_files_check(self.target)
                    self.environment_check(self.environment, self.target)
                elif self.git(self.target, 'rev-parse', 'HEAD') != self.state['baseline']['head']:
                    raise UpdateFailure('The original source baseline was not restored')
                return
            except Exception as exc:
                last = str(exc)[:160]
                self.sleep(1)
        raise UpdateFailure('Moonraker health verification timed out: ' + last)

    def install(self):
        self.check()
        if self.state is None:
            server = self.idle()
            self.state = {**self.identity(), 'phase': 'CAPTURED', 'baseline': self.source_baseline(),
                          'configurationFiles': self.configuration_evidence(),
                          'components': [n for n in server['components'] if re.fullmatch(r'[a-z][a-z0-9_]*', n)],
                          'klipperService': self.service('klipper.service', 'MainPID', 'ActiveState'),
                          'bootId': self.boot_id(),
                          'stopIntent': False, 'databaseManifest': None}
            self.save('CAPTURED')
        if self.state['phase'] == 'COMPLETE':
            self.safety_check()
            self.wait_healthy()
            return
        if self.state['phase'] in ('ROLLBACK_INTENT', 'ROLLED_BACK'):
            self.rollback()
            if self.source_baseline() != self.state['baseline']:
                raise UpdateFailure('The restored source baseline changed before retry')
            # The supervisor retains this recovery task and its lock. Retry the
            # same verified release after a confirmed rollback; requesting a new
            # task here would leave the old pending task permanently blocking it.
            self.save('CAPTURED', stopIntent=False, attempt=self.state.get('attempt', 0) + 1)
        if not self.state.get('stopIntent'):
            self.prepare()
            self.deployment_check()
            self.idle()
            self.configuration_evidence()
            if self.source_baseline() != self.state['baseline']:
                raise UpdateFailure('The installed source changed during staging')
            self.assert_klipper_unchanged()
            self.save('STOP_INTENT', stopIntent=True)
        self.progress(55, 'Restarting Moonraker; the local API will reconnect shortly')
        self.stopped()
        self.backup_database()
        if not self.marker(self.target, 'source'):
            if not self.marker(self.stage, 'source'):
                raise UpdateFailure('Staged source identity is missing')
            self.save('SOURCE_SWITCH_INTENT')
            exchange(self.target, self.stage)
        self.save('SOURCE_SWITCHED')
        if not self.marker(self.environment, 'environment'):
            if not self.marker(self.env_stage, 'environment'):
                raise UpdateFailure('Staged environment identity is missing')
            self.save('ENVIRONMENT_SWITCH_INTENT')
            exchange(self.environment, self.env_stage)
        self.save('SWITCHED')
        self.source_files_check(self.target)
        self.environment_check(self.environment, self.target)
        self.configuration_evidence()
        self.save('START_INTENT')
        self.command(['systemctl', 'start', self.spec['service']], 60)
        self.progress(85, 'Verifying Moonraker, original components and Klipper connection')
        self.wait_healthy()
        self.save('COMPLETE')
        self.progress(99, 'Moonraker source, dependencies and local API verified')

    def rollback(self):
        if self.state['phase'] == 'ROLLED_BACK':
            self.safety_check()
            self.wait_healthy(target=False)
            return
        self.safety_check()
        self.save('ROLLBACK_INTENT')
        self.stopped()
        if self.marker(self.target, 'source'):
            exchange(self.target, self.stage)
        if self.marker(self.environment, 'environment'):
            exchange(self.environment, self.env_stage)
        if self.state.get('databaseManifest') is not None:
            if tree_manifest(self.db_backup) != self.state['databaseManifest']:
                raise UpdateFailure('Database baseline cannot be verified for rollback')
            restored = safe_path(self.database.with_name('.moonraker-db-restore-' + self.job.name))
            if restored.exists():
                shutil.rmtree(restored)
            shutil.copytree(self.db_backup, restored, copy_function=shutil.copy2)
            # Retain correct data ownership while keeping the backup private to root.
            self.chown_tree(restored, self.database.stat().st_uid, self.database.stat().st_gid)
            sync_tree(restored)
            exchange(self.database, restored)
            self.save('ROLLBACK_INTENT', databaseRestored=True)
        self.configuration_evidence()
        if self.git(self.target, 'rev-parse', 'HEAD') != self.state['baseline']['head']:
            raise UpdateFailure('Source baseline cannot be verified for rollback')
        self.command(['systemctl', 'start', self.spec['service']], 60)
        self.wait_healthy(target=False)
        self.save('ROLLED_BACK')

    def failure(self):
        if not self.state:
            return
        if self.state.get('stopIntent'):
            self.rollback()
        else:
            self.save('FAILED_BEFORE_STOP')


def main():
    installer = Installer()
    action = sys.argv[1] if len(sys.argv) == 2 else ''
    if action == 'check':
        installer.check()
    elif action == 'install':
        installer.install()
    elif action == 'failure':
        installer.failure()
    else:
        raise UpdateFailure('Unsupported package stage')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Moonraker update: ' + str(exc)[:240], file=sys.stderr)
        sys.exit(1)
