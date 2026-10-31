#!/usr/bin/env python3
"""Update existing managed SDK/AI applications without platform bootstrap actions."""
import ctypes
import contextlib
import hashlib
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
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive import extract_archive, extract_payload
from compatibility import (select_variant, validate_existing_support, validate_releases,
                           validate_runtime_dependencies)
from state_backup import capture_state, inspect_state, restore_state, verify_baseline
from retention import prepare_plan


class UpdateFailure(RuntimeError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise UpdateFailure('An installation or task path uses an unexpected symbolic link')
    return path


def sync_dir(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, data, mode=0o600):
    path = no_links(path)
    temporary = path.with_name('.' + path.name + '.partial')
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write((json.dumps(data, sort_keys=True) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    sync_dir(path.parent)


def sync_tree(path):
    # A single inode can have thousands of model-cache aliases in this bundle.
    synced = set()
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in files:
            child = Path(parent) / name
            metadata = child.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            if stat.S_ISLNK(metadata.st_mode) or identity in synced:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise UpdateFailure('Unexpected special file in a staged release')
            descriptor = os.open(str(child), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            synced.add(identity)
        sync_dir(parent)


def copy_release(source, destination):
    """Copy data into new inodes, then retain aliases inside this new release."""
    inodes = {}
    source, destination = no_links(source), no_links(destination)
    if destination.exists():
        raise UpdateFailure('A staging directory already exists')

    def copy_file(old, new):
        info = os.stat(old, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise UpdateFailure('Unsupported staged source entry')
        key = (info.st_dev, info.st_ino)
        if key in inodes:
            os.link(inodes[key], new)
        else:
            shutil.copy2(old, new, follow_symlinks=False)
            inodes[key] = str(new)
        return str(new)

    shutil.copytree(source, destination, symlinks=True, copy_function=copy_file)


def sync_filesystems(paths):
    """Persist staged trees once per filesystem instead of fsync per cache alias."""
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, 'syncfs', None)
    if function is None:
        raise UpdateFailure('Filesystem synchronization is unavailable')
    function.argtypes, function.restype = [ctypes.c_int], ctypes.c_int
    devices = set()
    for path in paths:
        path = no_links(path)
        device = path.stat().st_dev
        if device in devices:
            continue
        descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if function(descriptor):
                raise OSError(ctypes.get_errno(), 'Staged filesystem synchronization failed')
        finally:
            os.close(descriptor)
        devices.add(device)


def exchange(left, right):
    left, right = no_links(left), no_links(right)
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, 'renameat2', None)
    if function is None:
        raise UpdateFailure('Atomic release replacement is unavailable')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        raise OSError(ctypes.get_errno(), 'Atomic release replacement failed')
    sync_dir(left.parent)
    if right.parent != left.parent:
        sync_dir(right.parent)


class Installer:
    def __init__(self, materials=None, job=None, run=subprocess.run, api=None,
                 require_root=True, sleep=time.sleep, monotonic=time.monotonic):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise UpdateFailure('Linux root execution is required')
        self.materials = no_links(materials or os.environ['TMPDIR'])
        self.job = no_links(job or os.environ['MINGDA_JOB_DIR'])
        self.spec = json.loads((self.materials / 'release.json').read_text())
        expected = {'sdk': '/opt/mingda/apps/sdk', 'ai-runtime': '/opt/mingda/apps/ai-runtime'}
        services = {'sdk': 'mingda-app@sdk.service', 'ai-runtime': 'mingda-app@ai-runtime.service'}
        if (self.spec.get('schemaVersion') != 1 or self.spec.get('component') != 'sdk-ai'
                or not isinstance(self.spec.get('source'), dict)
                or self.spec.get('services') != services
                or (require_root and self.spec.get('targetRoots') != expected)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.job.name)):
            raise UpdateFailure('Invalid SDK/AI release declaration')
        self.roots = {k: no_links(v) for k, v in self.spec['targetRoots'].items()}
        self.profile_path = Path('/etc/mingda/device-profile.json')
        self.serial_path = Path('/etc/machine_sn')
        self.configuration_root = Path('/etc/mingda/apps')
        self.printer_data = Path('/home/mingda/printer_data')
        self.data_root = Path('/var/lib/mingda/apps/sdk')
        self.state_path = self.job / 'sdk-ai-state.json'
        self.run, self.api = run, api or self.http_json
        self.sleep, self.monotonic = sleep, monotonic
        self._progress_lock = threading.Lock()
        self._progress_last = None
        self._progress_at = 0
        self._started_at = time.monotonic()
        self._layout = None
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if self.state and any(self.state.get(k) != v for k, v in self.identity().items()):
            raise UpdateFailure('Recovery material does not belong to this task')

    def identity(self):
        return {'schemaVersion': 1, 'jobId': self.job.name, 'releaseId': self.spec['releaseId'],
                'specSha256': digest(self.materials / 'release.json')}

    def save(self, phase, **values):
        self.state.update(values, phase=phase, updatedAt=time.time())
        write_json(self.state_path, self.state)

    def progress(self, percent, message, *, stage='', completed=None, total=None, unit='', force=False):
        value = {'phase': 'HEALTH_CHECK' if stage == 'health' else 'INSTALLING', 'component': 'sdk',
                 'progress': min(99, max(0, int(percent))), 'message': str(message)[:512],
                 'stage': stage[:64], 'elapsedSeconds': int(time.monotonic() - self._started_at)}
        if completed is not None and total is not None:
            value.update(completed=int(completed), total=int(total), unit=unit[:32])
        with self._progress_lock:
            now = time.monotonic()
            signature = (value['stage'], value['progress'], value['phase'])
            if not force and signature == self._progress_last and now - self._progress_at < 1:
                return
            write_json(self.job / 'script-progress.json', value, 0o644)
            self._progress_last, self._progress_at = signature, now

    @contextlib.contextmanager
    def reporting_wait(self, percent, message, stage):
        stopped = threading.Event()
        started = time.monotonic()
        self.progress(percent, message, stage=stage, force=True)
        def heartbeat():
            while not stopped.wait(2):
                self.progress(percent, message + ' (' + str(int(time.monotonic() - started)) + ' s)', stage=stage)
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set(); thread.join(timeout=3)

    def command(self, args, timeout=30):
        result = self.run(list(map(str, args)), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout, check=False)
        if result.returncode:
            raise UpdateFailure('Command failed: ' + Path(str(args[0])).name)
        return result.stdout.strip()

    def service(self, name, *fields):
        values = self.command(['systemctl', 'show', name, *sum((['-p', f] for f in fields), [])])
        return dict(row.split('=', 1) for row in values.splitlines() if '=' in row)

    @staticmethod
    def http_json(port, endpoint):
        if port not in (5000, 7125, 7126, 8088) or not endpoint.startswith('/') or endpoint.startswith('//'):
            raise UpdateFailure('Only printer-local status requests are allowed')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:' + str(port) + endpoint, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateFailure('Printer-local status response is too large')
        return json.loads(raw)

    def klippy_query(self, method, params):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.printer_data / 'comms/klippy.sock'))
            connection.sendall(json.dumps({'id': 1, 'method': method, 'params': params}).encode() + b'\x03')
            pending = b''
            while len(pending) <= 2 * 1024 * 1024:
                block = connection.recv(65536)
                if not block:
                    break
                pending += block
                while b'\x03' in pending:
                    raw, pending = pending.split(b'\x03', 1)
                    value = json.loads(raw)
                    if value.get('id') == 1:
                        if not isinstance(value.get('result'), dict) or 'error' in value:
                            raise UpdateFailure('Klipper status could not be read')
                        return value['result']
        raise UpdateFailure('Klipper status is unavailable')

    def idle(self):
        info = self.klippy_query('info', {})
        if info.get('state') != 'ready':
            raise UpdateFailure('Klipper must be ready')
        names = ('webhooks', 'print_stats', 'pause_resume', 'idle_timeout', 'manual_probe', 'heaters')
        data = self.klippy_query('objects/query', {'objects': {name: None for name in names}})['status']
        if (data.get('webhooks', {}).get('state') != 'ready'
                or data.get('print_stats', {}).get('state') not in ('standby', 'complete', 'cancelled', 'error')
                or data.get('pause_resume', {}).get('is_paused') is not False
                or data.get('idle_timeout', {}).get('state') not in ('Idle', 'Ready')
                or data.get('manual_probe', {}).get('is_active') is not False):
            raise UpdateFailure('Printer is printing, paused, calibrating or unavailable')
        heaters = data.get('heaters', {}).get('available_heaters')
        if not isinstance(heaters, list) or not heaters:
            raise UpdateFailure('Heater state is unavailable')
        observed = self.klippy_query('objects/query', {'objects': {name: None for name in heaters}})['status']
        for name in heaters:
            value = observed.get(name, {})
            if (not isinstance(value.get('target'), (int, float)) or value['target'] != 0
                    or not isinstance(value.get('temperature'), (int, float)) or value['temperature'] > 60):
                raise UpdateFailure('Heaters must be off and below 60 C')
        return info

    def protected_services(self):
        return {name: self.service(name, 'MainPID', 'ActiveState')
                for name in ('klipper.service', 'moonraker.service', 'KlipperScreen.service',
                             'mingda-updater.service', 'janus.service')}

    @staticmethod
    def boot_id():
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def safety(self):
        self.idle()
        self.configuration_evidence()
        self.check_links()
        if not self.state.get('stopIntent'):
            self.inspection_idle(self.api(7126, '/api/v3/status'))
        current = self.protected_services()
        if current['klipper.service'] != self.state['protectedServices']['klipper.service']:
            boot = self.boot_id()
            if boot == self.state['bootId']:
                raise UpdateFailure('Klipper process changed without a machine reboot')
            if current['klipper.service'].get('ActiveState') != 'active':
                raise UpdateFailure('Klipper is unavailable after reboot')
            self.save(self.state['phase'], protectedServices=current, bootId=boot)

    def profile(self):
        path = no_links(self.profile_path)
        if (not path.is_file() or (os.name == 'posix' and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022))):
            raise UpdateFailure('An existing root-owned device profile is required')
        value = json.loads(path.read_text())
        serials = [row.strip().upper() for row in self.serial_path.read_text().splitlines()
                   if re.fullmatch(r'[A-Z0-9]{3}\d{4}[A-Z]\d{7}', row.strip().upper())]
        if value.get('serialNumber') not in serials:
            raise UpdateFailure('Device profile and machine identity differ')
        host = {}
        for row in Path('/etc/os-release').read_text().splitlines():
            if '=' in row:
                name, content = row.split('=', 1); host[name] = content.strip('"\'')
        if (value.get('arch') != platform.machine() or value.get('os') != host.get('ID')
                or value.get('osVersion') != host.get('VERSION_ID') or list(sys.version_info[:2]) != [3, 10]):
            raise UpdateFailure('The packaged runtime does not match this host profile')
        return value

    def configuration_evidence(self):
        # Hash preserved configuration/identity only, never model or cache files.
        paths = [self.profile_path, self.serial_path]
        for name in self.roots:
            base = no_links(self.configuration_root / name)
            if not base.is_dir():
                raise UpdateFailure('Existing managed application configuration is missing')
            paths.extend(p for p in base.rglob('*') if p.is_file())
        records = {}
        for path in paths:
            path = no_links(path)
            if os.name == 'posix' and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
                raise UpdateFailure('Managed configuration must be root-owned without group/other write access')
            if path.stat().st_size > 4 * 1024 * 1024:
                raise UpdateFailure('Managed application configuration exceeds the size limit')
            records[str(path)] = {'sha256': digest(path), 'mode': stat.S_IMODE(path.stat().st_mode),
                                  'uid': path.stat().st_uid, 'gid': path.stat().st_gid}
        if self.state and records != self.state['configurationFiles']:
            raise UpdateFailure('Preserved device configuration changed during the task')
        return records

    def current(self, name):
        link = self.roots[name] / 'current'
        if not link.is_symlink():
            raise UpdateFailure('Existing managed SDK and AI current links are required')
        target = link.resolve(strict=True)
        target.relative_to(self.roots[name] / 'releases')
        no_links(target)
        if not target.is_dir():
            raise UpdateFailure('A managed release baseline is unavailable')
        return {'link': os.readlink(link), 'target': str(target)}

    def deployment_check(self):
        for name, service in self.spec['services'].items():
            props = self.service(service, 'User', 'WorkingDirectory', 'ExecStart')
            expected = str(self.roots[name] / 'current')
            if (props.get('User') != 'mingda' or props.get('WorkingDirectory') != expected
                    or expected + '/bin/application' not in props.get('ExecStart', '')):
                raise UpdateFailure('Managed service does not match this package: ' + name)

    def materials_check(self):
        for name, checksum in self.spec['materials'].items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
                raise UpdateFailure('Invalid signed material name')
            if digest(no_links(self.materials / name)) != checksum:
                raise UpdateFailure('A signed package material changed')

    @staticmethod
    def inspection_idle(sdk):
        inspection = sdk.get('inspection') or {}
        if not isinstance(inspection, dict) or inspection.get('busy') or inspection.get('recoveryRequired'):
            raise UpdateFailure('SDK inspection is busy or needs recovery')

    def check(self):
        self.materials_check()
        self.deployment_check()
        profile = self.profile()
        row = select_variant(self.spec['source']['catalog'], profile)
        self.configuration_evidence()
        if self.spec['source'].get('layoutVersion') == 2:
            self.layout()
        if not self.state or not self.state.get('stopIntent'):
            inspect_state(self.data_root, additionalPaths=self.database_paths())
        if self.state:
            if row['aiRelease'] != self.state['selection']['aiRelease']:
                raise UpdateFailure('Selected runtime changed during recovery')
            self.check_links()
            return
        self.idle()
        for name in self.roots:
            self.current(name)

    def check_links(self):
        if not self.state:
            return
        for name in self.roots:
            link = self.roots[name] / 'current'
            if not link.is_symlink():
                raise UpdateFailure('A managed current link changed during this task')
            actual = str(link.resolve(strict=False))
            allowed = {self.state['baseline'][name]['target']}
            row = self.state.get('components', {}).get(name)
            if row:
                allowed.add(row['target'])
            if actual not in allowed:
                raise UpdateFailure('Another operation changed a managed current link')
            if not Path(actual).is_dir():
                # A rollback can lose power after withdrawing a newly created
                # target but before restoring its link. Only this exact intent
                # and saved candidate marker make a dangling link recoverable.
                if (not row or self.state['phase'] != 'ROLLBACK_INTENT'
                        or row['targetExisted'] or actual != row['target']
                        or not self.marker(Path(row['stage']))):
                    raise UpdateFailure('A managed current link became invalid')
            elif row and actual == row['target'] and actual != self.state['baseline'][name]['target']:
                if not self.marker(Path(actual)):
                    raise UpdateFailure('A current link selects an unowned candidate')

    def release_descriptor(self, directory, name):
        path = no_links(directory / 'component.json')
        value = json.loads(path.read_text())
        if value.get('componentId') != name or value.get('schemaVersion') != 1:
            raise UpdateFailure('Release identity differs from its component')
        for field in ('version', 'variantId'):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value.get(field, '')):
                raise UpdateFailure('Invalid release path identity')
        return value

    def database_paths(self):
        configuration = json.loads((self.configuration_root / 'sdk/config.json').read_text())
        detection = configuration.get('ai_detection') or {}
        messaging = configuration.get('messaging') or {}
        if not isinstance(detection, dict) or not isinstance(messaging, dict):
            raise UpdateFailure('SDK state configuration has an invalid shape')
        values = [detection.get('database_path') or str(self.printer_data / 'state/ai-agent.db')]
        if messaging.get('command_journal_path'):
            values.append(messaging['command_journal_path'])
        result = []
        for value in values:
            if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
                raise UpdateFailure('SDK persistent database paths must be absolute')
            result.append(no_links(value))
        return tuple(result)

    def backup_databases(self):
        backup = self.job / 'sdk-database-baseline'
        if self.state.get('databaseManifest') is not None:
            if not verify_baseline(backup, self.state['databaseManifest']):
                raise UpdateFailure('Saved SDK state baseline has changed')
            return
        self.save('DATABASE_BACKUP_INTENT')
        manifest = capture_state(self.data_root, backup, additionalPaths=self.database_paths(), run=self.run)
        self.save('DATABASE_SAVED', databaseManifest=manifest)

    def marker(self, path):
        marker = path / '.mdu-sdk-ai.json'
        return marker.is_file() and json.loads(no_links(marker).read_text()) == self.identity()

    def directories(self, name):
        row = self.state['components'][name]
        target, stage = no_links(row['target']), no_links(row['stage'])
        target.relative_to(self.roots[name] / 'releases')
        if stage != target.with_name('.' + target.name + '.mdu-' + self.job.name):
            raise UpdateFailure('Saved staging directory differs from this task')
        return target, stage

    def payload(self):
        folder = no_links(self.job / 'sdk-ai-source')
        marker = self.job / 'sdk-ai-extracted.json'
        if marker.is_file():
            if json.loads(marker.read_text()) != self.identity():
                raise UpdateFailure('Extracted material belongs to another task')
            return folder / self.spec['source']['archiveRoot']
        if folder.exists():
            if self.state.get('stopIntent'):
                raise UpdateFailure('The saved source extraction is incomplete after service disruption')
            shutil.rmtree(folder)
        folder.mkdir(mode=0o700)
        extracted = extract_archive(self.materials / 'sdk-ai-bundle.tar.gz', folder)
        if extracted.name != self.spec['source']['archiveRoot']:
            raise UpdateFailure('Source archive root differs')
        sync_tree(extracted)
        write_json(marker, self.identity())
        return extracted

    def layout(self):
        if self._layout is None:
            name = self.spec['source'].get('layoutFile')
            if name != 'layout.json' or name not in self.spec['materials']:
                raise UpdateFailure('Missing signed optimized layout')
            path = no_links(self.materials / name)
            if path.stat().st_size > 128 * 1024 * 1024:
                raise UpdateFailure('Optimized layout exceeds the size limit')
            layout = json.loads(path.read_text())
            if (layout.get('schemaVersion') != 1 or not isinstance(layout.get('payloads'), dict)
                    or not 3 <= len(layout['payloads']) <= 32
                    or not isinstance(layout.get('variants'), dict)
                    or set(layout.get('variantDescriptors', {})) != set(layout['variants'])):
                raise UpdateFailure('Invalid optimized SDK layout')
            for payload in [layout.get('sdk'), layout.get('runtimeCommon'), *layout['variants'].values()]:
                if (not isinstance(payload, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.tar\.gz', payload)
                        or payload not in self.spec['materials'] or payload not in layout['payloads']):
                    raise UpdateFailure('Optimized payload is not signed')
            self._layout = layout
        return self._layout

    def new_stage_parent(self, target):
        parent = no_links(target.parent)
        if not parent.exists():
            parent.mkdir(mode=0o755)
            os.chmod(parent, 0o755)  # The worker's private umask must not hide a published version.
            sync_dir(parent.parent)

    def prepare_optimized(self):
        layout, selected = self.layout(), self.state['selection']['aiRelease']
        if selected not in layout['variants']:
            raise UpdateFailure('Selected runtime is missing from the optimized layout')
        sdk_name, common_name, overlay_name = layout['sdk'], layout['runtimeCommon'], layout['variants'][selected]
        declarations = layout['payloads']
        work = [(sdk_name, 'sdk', None), (common_name, 'ai-runtime', None),
                (overlay_name, 'ai-runtime', declarations[common_name])]
        descriptors = {'sdk': layout['sdkDescriptor'], 'ai-runtime': layout['variantDescriptors'][selected]}
        selected_bytes = sum(declarations[name]['regularBytes'] for name, _, _ in work)
        required = selected_bytes + inspect_state(self.data_root, additionalPaths=self.database_paths())['bytes'] * 2 + 256 * 1024 * 1024
        entries = sum(declarations[name]['entryCount'] for name, _, _ in work)
        for directory in (self.job, *(p / 'releases' for p in self.roots.values())):
            if shutil.disk_usage(directory).free < required or os.statvfs(directory).f_favail < entries + 4096:
                raise UpdateFailure('Insufficient storage or inodes for the selected SDK/AI stages')
        components = self.state.get('components')
        if components is None:
            components = {}
            for name, descriptor in descriptors.items():
                if descriptor.get('componentId') != name or descriptor.get('schemaVersion') != 1:
                    raise UpdateFailure('Invalid optimized component identity')
                for field in ('version', 'variantId'):
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', str(descriptor.get(field, ''))):
                        raise UpdateFailure('Invalid optimized release path')
                target = no_links(self.roots[name] / 'releases' / descriptor['version'] / descriptor['variantId'])
                if target.exists() and self.release_descriptor(target, name) != descriptor:
                    raise UpdateFailure('Existing optimized release identity conflicts')
                declaration = declarations[sdk_name if name == 'sdk' else overlay_name]
                components[name] = {'target': str(target), 'stage': str(target.with_name('.' + target.name + '.mdu-' + self.job.name)),
                                    'targetExisted': target.exists(), 'descriptor': descriptor,
                                    'rootMode': declaration['rootMode'], 'rootMtimeNs': declaration['rootMtimeNs']}
            self.save('STAGING_INTENT', components=components)
        for name in components:
            target, stage = self.directories(name)
            self.new_stage_parent(target)
            stage.mkdir(mode=0o700)
        total = sum(declarations[name]['tarBytes'] for name, _, _ in work)
        completed = 0
        for payload, name, base in work:
            declaration, destination = declarations[payload], self.directories(name)[1]
            def report(done, size, message, before=completed, component=name):
                overall = before + done
                self.progress(5 + overall * 50 // max(total, 1),
                              component + ': ' + message + ' ' + str(overall // 1048576) + '/' + str(total // 1048576) + ' MiB',
                              stage='extract-' + component, completed=overall, total=total, unit='bytes')
            extract_payload(self.materials / payload, destination, declaration, base=base, progress=report)
            completed += declaration['tarBytes']
        sdk_stage, ai_stage = self.directories('sdk')[1], self.directories('ai-runtime')[1]
        info = validate_releases(sdk_stage, ai_stage, self.profile())
        if info['sdkVersion'] != self.spec['source']['sdkVersion']:
            raise UpdateFailure('Staged SDK differs from the signed source identity')
        validate_existing_support(info, klipper_root='/home/mingda/klipper', moonraker_root='/home/mingda/moonraker',
                                  config_root=self.printer_data / 'config')
        self.save('STAGING_INTENT', releaseInfo=info)
        for stage in (sdk_stage, ai_stage):
            write_json(stage / '.mdu-sdk-ai.json', self.identity(), 0o644)
        with self.reporting_wait(60, 'Persisting selected SDK/AI stages', 'sync'):
            sync_filesystems((sdk_stage, ai_stage))
        def dependency_run(args, **kwargs):
            sdk = str(args[0]).endswith('mingda-printer')
            with self.reporting_wait(70 if sdk else 78, 'Checking SDK offline dependencies' if sdk else 'Checking AI offline dependencies',
                                     'sdk-check' if sdk else 'ai-check'):
                return self.run(args, **kwargs)
        validate_runtime_dependencies(sdk_stage, ai_stage, dependency_run)
        self.progress(82, 'Selected SDK and AI stages are ready', stage='staged', force=True)
        self.save('STAGED')

    def prepare(self):
        # All candidate stages are private to this task while stopIntent is false.
        # Remove failed candidates before estimating free space for a retry.
        for name in self.state.get('components', {}):
            _, stage = self.directories(name)
            if stage.exists():
                if self.state.get('stopIntent'):
                    raise UpdateFailure('Cannot rebuild a stage after service disruption')
                shutil.rmtree(stage)
        if self.spec['source'].get('layoutVersion') == 2:
            self.prepare_optimized()
            return
        required = self.spec['source']['regularBytes'] * 3 + self.spec['source']['archiveBytes'] * 2 + 512 * 1024 * 1024
        required += inspect_state(self.data_root, additionalPaths=self.database_paths())['bytes'] * 2
        extracted_marker = self.job / 'sdk-ai-extracted.json'
        if extracted_marker.is_file() and json.loads(extracted_marker.read_text()) == self.identity():
            required -= self.spec['source']['regularBytes']
        for directory in (self.job, *(p / 'releases' for p in self.roots.values())):
            if shutil.disk_usage(directory).free < required:
                raise UpdateFailure('Insufficient storage for the source, two staged releases and recovery')
            if os.statvfs(directory).f_favail < self.spec['source']['entryCount'] + 50000:
                raise UpdateFailure('Insufficient filesystem inodes for the offline bundle')
        self.progress(10, 'Extracting the signed SDK/AI bundle and preserving cache hard links')
        source = self.payload()
        selected = self.state['selection']
        roots = {'sdk': source / selected['sdkRelease'], 'ai-runtime': source / selected['aiRelease']}
        info = validate_releases(roots['sdk'], roots['ai-runtime'], self.profile())
        validate_existing_support(info, klipper_root='/home/mingda/klipper',
                                  moonraker_root='/home/mingda/moonraker',
                                  config_root=self.printer_data / 'config')
        if info['sdkVersion'] != self.spec['source']['sdkVersion']:
            raise UpdateFailure('SDK version differs from the signed release')
        components = self.state.get('components')
        if components is None:
            components = {}
            for name, root in roots.items():
                descriptor = self.release_descriptor(root, name)
                target = no_links(self.roots[name] / 'releases' / descriptor['version'] / descriptor['variantId'])
                stage = target.with_name('.' + target.name + '.mdu-' + self.job.name)
                if target.exists():
                    if self.release_descriptor(target, name) != descriptor:
                        raise UpdateFailure('An existing release has a conflicting component identity')
                components[name] = {'target': str(target), 'stage': str(stage),
                                    'targetExisted': target.exists(), 'descriptor': descriptor}
            self.save('STAGING_INTENT', components=components, releaseInfo=info)
        for name in roots:
            target, stage = self.directories(name)
            if stage.exists():
                if self.state.get('stopIntent'):
                    raise UpdateFailure('Cannot rebuild a stage after services may have changed')
                shutil.rmtree(stage)
            self.new_stage_parent(target)
            copy_release(roots[name], stage)
            write_json(stage / '.mdu-sdk-ai.json', self.identity(), 0o644)
            sync_tree(stage)
        sdk_stage = self.directories('sdk')[1]
        ai_stage = self.directories('ai-runtime')[1]
        validated = validate_releases(sdk_stage, ai_stage, self.profile())
        if validated != self.state['releaseInfo']:
            raise UpdateFailure('Staged component identities differ')
        self.progress(35, 'Checking offline SDK and AI dependencies before stopping services')
        validate_runtime_dependencies(sdk_stage, ai_stage, self.run)
        self.save('STAGED')

    def stop(self):
        self.safety()
        for name in ('sdk', 'ai-runtime'):
            service = self.spec['services'][name]
            self.command(['systemctl', 'stop', service], 60)
            if self.service(service, 'MainPID').get('MainPID') != '0':
                raise UpdateFailure('Managed application did not stop: ' + name)

    def activate_link(self, name, target):
        link = self.roots[name] / 'current'
        temporary = link.with_name('.current.mdu-' + self.job.name)
        no_links(temporary.parent)
        if temporary.is_symlink():
            # Only a link to the already saved source/target is this task's intent.
            if os.readlink(temporary) not in (str(target), self.state['baseline'][name]['link']):
                raise UpdateFailure('Unexpected pending current link')
            temporary.unlink()
        elif temporary.exists():
            raise UpdateFailure('Pending current path is not a symbolic link')
        os.symlink(str(target), temporary)
        os.replace(temporary, link)
        sync_dir(link.parent)

    def switch(self):
        for name in ('ai-runtime', 'sdk'):
            target, stage = self.directories(name)
            row = self.state['components'][name]
            if not self.marker(target):
                if not self.marker(stage):
                    raise UpdateFailure('Verified candidate release is missing: ' + name)
                if 'rootMode' in row:
                    os.chmod(stage, row['rootMode'])
                    os.utime(stage, ns=(row['rootMtimeNs'], row['rootMtimeNs']))
                    sync_dir(stage)
                self.save(name + '_RELEASE_INTENT')
                if row['targetExisted']:
                    exchange(target, stage)
                else:
                    os.replace(stage, target)
                    sync_dir(target.parent)
            self.save(name + '_LINK_INTENT')
            self.activate_link(name, target)
            self.save(name + '_SWITCHED')

    def start(self):
        for name in ('ai-runtime', 'sdk'):
            self.command(['systemctl', 'start', self.spec['services'][name]], 60)

    @staticmethod
    def process_identity(name, pid, target):
        observed = (Path('/proc') / str(pid) / 'exe').stat()
        binaries = ([target / 'mingda-printer'] if name == 'sdk' else
                    [target / 'venv/bin/ai-detection-runtime', target / 'venv/bin/python'])
        if not any(path.is_file() and (path.stat().st_dev, path.stat().st_ino)
                   == (observed.st_dev, observed.st_ino) for path in binaries):
            raise UpdateFailure('Managed process does not execute the selected release')

    @staticmethod
    def validate_local_health(ai, sdk, expected, protocol):
        if (ai.get('status') != 'healthy' or ai.get('selfTest') != 'HEALTHY'
                or ai.get('loadedModels') != 6 or ai.get('runtimeVersion') != expected['aiVersion']
                or ai.get('variantId') != expected['aiVariant']
                or ai.get('modelBundleVersion') != expected['modelBundleVersion']):
            raise UpdateFailure('AI runtime identity or six-model health differs')
        if not protocol or sdk.get('inspectionError') or sdk.get('protocolVersion') != protocol:
            raise UpdateFailure('SDK local API or inspection state is unhealthy')
        if sdk.get('runtimeState') != 'READY' or sdk.get('npuState') != 'READY' or sdk.get('runtimeModelError'):
            raise UpdateFailure('SDK has not completed the local AI handshake')
        if sdk.get('runtimeVariantId') and sdk['runtimeVariantId'] != ai['variantId']:
            raise UpdateFailure('SDK and AI runtime report different model variants')
        if sdk.get('runtimeModelBundleVersion') and sdk['runtimeModelBundleVersion'] != ai['modelBundleVersion']:
            raise UpdateFailure('SDK and AI runtime report different model bundles')
        inspection = sdk.get('inspection') or {}
        if not isinstance(inspection, dict) or inspection.get('busy') or inspection.get('recoveryRequired'):
            raise UpdateFailure('SDK inspection is unavailable, busy or needs recovery')
        if expected.get('requiresKlipperInspection'):
            if (inspection.get('hardwareReady') is not True
                    or inspection.get('factoryProfileDigest') != expected['profileDigest']):
                raise UpdateFailure('Existing inspection support is not ready for this SDK')

    def health(self, target=True):
        deadline = self.monotonic() + 150
        while self.monotonic() < deadline:
            try:
                self.safety()
                for name in self.roots:
                    service = self.service(self.spec['services'][name], 'MainPID', 'ActiveState')
                    if service.get('ActiveState') != 'active' or not service.get('MainPID', '').isdigit() or int(service['MainPID']) < 2:
                        raise UpdateFailure('Managed application is not active')
                    expected = (self.state['components'][name]['target'] if target
                                else self.state['baseline'][name]['target'])
                    if self.current(name)['target'] != expected:
                        raise UpdateFailure('Managed current link selects the wrong release')
                    self.process_identity(name, int(service['MainPID']), Path(expected))
                    if target and not self.marker(Path(expected)):
                        raise UpdateFailure('Installed candidate has no task identity')
                ai = self.api(5000, '/api/v1/health')
                expected = self.state['releaseInfo'] if target else self.state['baselineHealth']['ai']
                sdk = self.api(7126, '/api/v3/status')
                self.validate_local_health(ai, sdk, expected, self.state['baselineHealth']['sdkProtocol'])
                return
            except Exception:
                self.sleep(1)
        raise UpdateFailure('SDK/AI local health verification timed out')

    def schedule_retention(self):
        if self.spec['source'].get('layoutVersion') != 2:
            return
        report_path = no_links(self.job / 'retention-report.json')
        if report_path.is_file():
            report = json.loads(report_path.read_text())
            if report.get('identity') != self.identity():
                raise UpdateFailure('Cleanup report does not belong to this task')
            if report.get('phase') == 'COMPLETE':
                return
        unit = 'klipperscreen-sdk-prune-' + self.job.name
        if self.service(unit + '.service', 'ActiveState').get('ActiveState') in ('active', 'activating'):
            self.save('COMPLETE', retentionScheduled=True, retentionScheduleError=None)
            return
        directory = no_links(self.job / 'sdk-retention')
        directory.mkdir(mode=0o700, exist_ok=True)
        plan = prepare_plan(self.job, self.state, self.roots, removeOldPackages=True)
        write_json(directory / 'plan.json', plan)
        script = no_links(directory / 'retention.py')
        raw = (self.materials / 'retention.py').read_bytes()
        descriptor = os.open(str(script), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o500)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        os.chmod(script, 0o500); sync_dir(directory)
        try:
            self.command(['systemd-run', '--unit=' + unit, '--service-type=exec', '--collect',
                          '--property=UMask=0077', '--property=TimeoutStopSec=30',
                          '/usr/bin/python3', '-I', '-B', script, '--job', self.job,
                          '--plan', directory / 'plan.json', '--wait-seconds', '300'], 15)
        except Exception:
            self.save('COMPLETE', retentionScheduleError='TEMPORARY_CLEANUP_TASK_FAILED')
            raise
        self.save('COMPLETE', retentionScheduled=True, retentionScheduleError=None)

    def install(self):
        self.check()
        if self.state is None:
            profile = self.profile()
            baseline = {name: self.current(name) for name in self.roots}
            original_ai = self.api(5000, '/api/v1/health')
            original_sdk = self.api(7126, '/api/v3/status')
            self.inspection_idle(original_sdk)
            baseline_info = validate_releases(Path(baseline['sdk']['target']),
                                              Path(baseline['ai-runtime']['target']), profile)
            self.validate_local_health(original_ai, original_sdk, baseline_info, original_sdk.get('protocolVersion'))
            for name in self.roots:
                process = self.service(self.spec['services'][name], 'MainPID', 'ActiveState')
                if (process.get('ActiveState') != 'active' or not process.get('MainPID', '').isdigit()
                        or int(process['MainPID']) < 2):
                    raise UpdateFailure('A managed application baseline is not running')
                self.process_identity(name, int(process['MainPID']), Path(baseline[name]['target']))
            self.state = {**self.identity(), 'phase': 'CAPTURED', 'baseline': baseline,
                          'selection': select_variant(self.spec['source']['catalog'], profile),
                          'configurationFiles': self.configuration_evidence(),
                          'protectedServices': self.protected_services(), 'bootId': self.boot_id(),
                          'stopIntent': False, 'attempt': 0, 'databaseManifest': None,
                          'baselineHealth': {'ai': baseline_info,
                                             'sdkProtocol': original_sdk.get('protocolVersion')}}
            self.save('CAPTURED')
        if self.state['phase'] == 'COMPLETE':
            try:
                self.health()
            except Exception:
                # A later health failure still uses the ordinary verified rollback.
                self.save('COMPLETE', retentionScheduleError=None)
                raise
            self.schedule_retention()
            return
        if self.state['phase'] in ('ROLLBACK_INTENT', 'ROLLED_BACK'):
            self.rollback()
            self.save('CAPTURED', stopIntent=False, attempt=self.state['attempt'] + 1)
        if not self.state['stopIntent']:
            self.prepare()
            self.safety()
            if {name: self.current(name) for name in self.roots} != self.state['baseline']:
                raise UpdateFailure('Installed releases changed while staging')
            self.save('STOP_INTENT', stopIntent=True)
        self.stop()
        self.backup_databases()
        self.progress(86, 'Switching the matching AI runtime and SDK releases', stage='switch', force=True)
        self.switch()
        self.save('START_INTENT')
        self.start()
        with self.reporting_wait(90, 'Checking the SDK local API and all six AI models', 'health'):
            self.health()
        self.save('COMPLETE', cloudReadiness='DEFERRED')
        self.schedule_retention()
        self.progress(99, 'SDK and six-model AI runtime verified; cloud readiness is deferred offline')

    def rollback(self):
        if self.state['phase'] == 'ROLLED_BACK':
            self.health(target=False)
            return
        self.safety()
        self.save('ROLLBACK_INTENT')
        self.stop()
        for name in ('sdk', 'ai-runtime'):
            target, stage = self.directories(name)
            if self.marker(target) and self.state['components'][name]['targetExisted']:
                if not stage.is_dir() or self.marker(stage):
                    raise UpdateFailure('Original release backup is unavailable')
                exchange(target, stage)
            elif self.marker(target):
                # A newly created candidate must leave its canonical path on
                # rollback, so the next same-task attempt installs a fresh stage.
                if stage.exists():
                    raise UpdateFailure('Unexpected staging path during candidate withdrawal')
                os.replace(target, stage)
                sync_dir(target.parent)
            baseline = self.state['baseline'][name]
            self.activate_link(name, baseline['link'])
        if self.state.get('databaseManifest') is not None:
            restore_state(self.job / 'sdk-database-baseline', self.state['databaseManifest'], run=self.run)
        self.start()
        self.health(target=False)
        self.save('ROLLED_BACK')

    def failure(self):
        if not self.state:
            return
        if self.state.get('phase') == 'COMPLETE' and self.state.get('retentionScheduleError'):
            return  # Verified applications remain installed; explicit recovery retries scheduling.
        if self.state.get('stopIntent'):
            self.rollback()
        else:
            self.save('FAILED_BEFORE_STOP')


if __name__ == '__main__':
    try:
        instance = Installer()
        action = sys.argv[1] if len(sys.argv) == 2 else ''
        if action not in ('check', 'install', 'failure'):
            raise UpdateFailure('Unsupported package action')
        getattr(instance, action)()
    except Exception as error:
        print('SDK/AI update: ' + str(error)[:240], file=sys.stderr)
        sys.exit(1)
