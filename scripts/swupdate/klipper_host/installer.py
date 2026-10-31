#!/usr/bin/env python3
"""Offline Klipper host software update. Never writes MCU firmware or configs."""
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host import HostUpdate

TARGET_PATH = '/home/mingda/klipper'
PYTHON_PATH = '/home/mingda/klippy-env/bin/python'
MAIN_CONFIG = '/home/mingda/printer_data/config/printer.cfg'
SERVICE = 'klipper.service'


class UpdateFailure(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def no_links(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise UpdateFailure('Linked update material or task workspace')
    return path


def write_bytes(path, raw, mode=0o600):
    path = no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name('.' + path.name + '.partial')
    fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(partial, mode)
    os.replace(partial, path)
    fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value, mode=0o600):
    write_bytes(path, (json.dumps(value, sort_keys=True) + '\n').encode(), mode)


def reported_version(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if not value or value.casefold() in ('unknown', '?', 'n/a', '未知') else value


def diagnostic_message(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value[:4096])
    # Store the health message, never an environment/configuration dump. Values
    # in assignment lines and common credential forms are not diagnostic data.
    value = re.sub(r'''(?im)^(\s*(?:export\s+)?["']?[A-Za-z_][\w.-]*["']?\s*[=:])[^\n]*''',
                   r'\1 [redacted]', value)
    value = re.sub(r'''(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b["']?\s*[=:]\s*(?:"[^"]*"|'[^']*'|\S+)''',
                   r'\1=[redacted]', value)
    value = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', value)
    return value[:512]


class Installer:
    def __init__(self, materials=None, job=None, run=subprocess.run, api=None,
                 host_factory=HostUpdate, require_root=True):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise UpdateFailure('Linux root execution is required')
        self.materials = no_links(materials or os.environ['TMPDIR'])
        self.job = no_links(job or os.environ['MINGDA_JOB_DIR'])
        if (not self.materials.is_dir() or not self.job.is_dir()
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.job.name)):
            raise UpdateFailure('Invalid private update workspace')
        self.spec = json.loads((self.materials / 'release.json').read_text())
        if (self.spec.get('schemaVersion') != 1 or self.spec.get('service') != SERVICE
                or not isinstance(self.spec.get('host'), dict)
                or not isinstance(self.spec.get('materials'), dict)
                or not isinstance(self.spec.get('mainConfigurationPath'), str)):
            raise UpdateFailure('Invalid software-only release declaration')
        if require_root and (self.spec['host'].get('targetPath') != TARGET_PATH
                             or self.spec['host'].get('pythonPath') != PYTHON_PATH
                             or self.spec['mainConfigurationPath'] != MAIN_CONFIG):
            raise UpdateFailure('Declared software paths do not match this package')
        self.main_config = Path(self.spec['mainConfigurationPath']).absolute()
        self.run = run
        self.api = api or self.http_json
        self.host = host_factory(self.spec['host'], self.materials, self.job, progress=self.progress)
        self.state_path = self.job / 'klipper-software-state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if self.state and any(self.state.get(key) != value for key, value in self.identity().items()):
            raise UpdateFailure('Recovery material does not belong to this task')

    def identity(self):
        return {'schemaVersion': 1, 'jobId': self.job.name, 'releaseId': self.spec['releaseId'],
                'hostCommit': self.spec['host']['targetCommit'],
                'bundleSha256': self.spec['host']['bundleSha256'],
                'mainConfigurationPath': str(self.main_config),
                'specSha256': digest(self.materials / 'release.json')}

    @staticmethod
    def http_json(endpoint, payload=None):
        if not endpoint.startswith('/') or endpoint.startswith('//'):
            raise UpdateFailure('Only printer-local API endpoints are permitted')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        raw = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request('http://127.0.0.1:7125' + endpoint, data=raw,
                                         headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=5) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise UpdateFailure('Printer-local API response is too large')
        return json.loads(data)

    def command(self, args, timeout=30):
        result = self.run([str(value) for value in args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode:
            raise UpdateFailure('Command failed: ' + str(args[0]) + ': ' + result.stderr[:300])
        return result.stdout.strip()

    def save(self, phase, **fields):
        if self.state is None:
            raise UpdateFailure('Original program evidence has not been captured')
        self.state.update(fields, phase=phase, updatedAt=time.time())
        write_json(self.state_path, self.state)

    def progress(self, value, message):
        write_json(self.job / 'script-progress.json', {
            'phase': 'INSTALLING', 'progress': max(0, min(99, int(value))),
            'component': 'klipper', 'message': str(message)[:512]}, 0o644)

    @staticmethod
    def process_arguments(pid):
        return (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')

    def service_check(self):
        properties = self.command(['systemctl', 'show', SERVICE, '-p', 'User', '-p', 'WorkingDirectory', '-p', 'ExecStart'])
        values = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
        if (values.get('User') != 'mingda' or values.get('WorkingDirectory') != self.spec['host']['targetPath']
                or self.spec['host']['pythonPath'] not in values.get('ExecStart', '')):
            raise UpdateFailure('The fixed Klipper service does not match this package')

    def configuration_graph(self):
        pending, seen = [self.main_config], {}
        while pending:
            path = pending.pop().resolve(strict=True)
            if path in seen:
                continue
            if len(seen) >= 256 or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise UpdateFailure('Unsupported configuration include graph')
            raw = path.read_bytes()
            seen[path] = hashlib.sha256(raw).hexdigest()
            for line in raw.decode('utf-8-sig').splitlines():
                match = re.match(r'^\s*\[include\s+([^\]]+)\]\s*(?:[#;].*)?$', line, re.IGNORECASE)
                if match:
                    pattern = str(path.parent / match.group(1).strip())
                    paths = [Path(name) for name in glob.glob(pattern)]
                    if not paths and not glob.has_magic(pattern):
                        raise UpdateFailure('A configured include is missing')
                    pending.extend(paths)
        return {str(path): value for path, value in seen.items()}

    def configuration_check(self):
        current = self.configuration_graph()
        if self.state and current != self.state['configurationFiles']:
            raise UpdateFailure('Configuration contents or include membership changed during the task')
        target = Path(self.spec['host']['targetPath']).absolute()
        for name, checksum in current.items():
            try:
                relative = str(Path(name).relative_to(target)).replace(os.sep, '/')
            except ValueError:
                continue
            incoming = self.spec['host'].get('targetFiles', {}).get(relative)
            if incoming is not None and incoming != checksum:
                raise UpdateFailure('An active printing configuration conflicts with the source release')
        return current

    def live_info(self):
        info = self.api('/printer/info')['result']
        if (Path(info.get('klipper_path', '')).absolute() != Path(self.spec['host']['targetPath'])
                or Path(info.get('config_file', '')).resolve() != self.main_config.resolve()
                or Path(info.get('python_path', '')).absolute().parent != Path(self.spec['host']['pythonPath']).parent):
            raise UpdateFailure('Moonraker is not connected to the declared source, environment and printer.cfg')
        pid = self.command(['systemctl', 'show', SERVICE, '--property=MainPID', '--value'])
        if not pid.isdecimal() or int(pid) < 2 or info.get('process_id') != int(pid):
            raise UpdateFailure('Moonraker is not connected to the fixed Klipper service process')
        argv = self.process_arguments(int(pid))
        if (len(argv) < 3 or Path(argv[0]).absolute().parent != Path(self.spec['host']['pythonPath']).parent
                or Path(argv[1]).absolute() != Path(self.spec['host']['targetPath']) / 'klippy/klippy.py'
                or Path(argv[2]).resolve() != self.main_config.resolve()):
            raise UpdateFailure('Klipper service arguments select a different program or configuration')
        return info

    def mcu_evidence(self):
        names = [name for name in self.api('/printer/objects/list')['result']['objects']
                 if name == 'mcu' or name.startswith('mcu ')]
        if not names:
            raise UpdateFailure('No connected MCU objects are available for identity verification')
        status = self.api('/printer/objects/query?' + '&'.join(urllib.parse.quote(name, safe='') for name in names))['result']['status']
        active = self.api('/printer/objects/query?configfile')['result']['status']['configfile']['config']
        hardware, versions = {}, {}
        for name in names:
            constants, configuration = status[name].get('mcu_constants', {}), active.get(name, {})
            if (not constants.get('MCU') or not isinstance(constants.get('CLOCK_FREQ'), (int, float))
                    or constants['CLOCK_FREQ'] <= 0
                    or not (configuration.get('serial') or configuration.get('canbus_uuid'))):
                raise UpdateFailure('Connected MCU hardware identity is unavailable')
            hardware[name] = {
                'constants': {key: constants[key] for key in ('MCU', 'CLOCK_FREQ', 'CANBUS_FREQUENCY', 'RESERVE_PINS_CAN') if key in constants},
                'connection': {key: configuration[key] for key in ('serial', 'canbus_uuid', 'canbus_interface') if key in configuration}}
            versions[name] = reported_version(status[name].get('mcu_version'))
        return {'mcus': versions, 'hardware': hardware, 'activeSections': sorted(active)}

    def snapshot(self):
        self.live_info()
        status = self.api('/printer/objects/query?webhooks')['result']['status']['webhooks']
        if status.get('state') != 'ready':
            raise UpdateFailure('Klipper is ' + str(status.get('state', 'unknown')) + ': '
                                + diagnostic_message(status.get('state_message')))
        self.known_activity_check(require_idle=True)
        return self.mcu_evidence()

    def check_mcus(self, observed, learn=False):
        if set(observed['mcus']) != set(self.state['originalMCUs']) or observed['hardware'] != self.state['originalHardware']:
            raise UpdateFailure('The connected MCU identities changed during the software update')
        changed = False
        for name, before in self.state['originalMCUs'].items():
            after = observed['mcus'][name]
            if before is not None and after is not None and before != after:
                raise UpdateFailure('MCU firmware version changed during a software-only update')
            if learn and before is None and after is not None:
                self.state['originalMCUs'][name] = after
                changed = True
        if changed:
            self.save(self.state['phase'])

    def known_activity_check(self, require_idle=False):
        try:
            status = self.api('/printer/objects/query?print_stats&pause_resume&idle_timeout&manual_probe&heaters')['result']['status']
        except Exception:
            if require_idle:
                raise UpdateFailure('Printer inactivity cannot be confirmed')
            return
        if (status.get('print_stats', {}).get('state') in ('printing', 'paused')
                or status.get('pause_resume', {}).get('is_paused')
                or status.get('manual_probe', {}).get('is_active')):
            raise UpdateFailure('Repair cannot interrupt printing, pausing or calibration')
        if require_idle and (status.get('print_stats', {}).get('state') not in ('standby', 'complete', 'cancelled', 'error')
                             or status.get('pause_resume', {}).get('is_paused') is not False
                             or status.get('idle_timeout', {}).get('state') not in ('Idle', 'Ready')
                             or not isinstance(status.get('heaters', {}).get('available_heaters'), list)):
            raise UpdateFailure('Printer inactivity cannot be confirmed')
        names = status.get('heaters', {}).get('available_heaters', [])
        if names:
            try:
                heaters = self.api('/printer/objects/query?' + '&'.join(urllib.parse.quote(name, safe='') for name in names))['result']['status']
            except Exception:
                if require_idle:
                    raise UpdateFailure('Heater inactivity cannot be confirmed')
                return
            if require_idle and any(not isinstance(heaters.get(name, {}).get(key), (int, float))
                                    for name in names for key in ('target', 'temperature')):
                raise UpdateFailure('Heater inactivity cannot be confirmed')
            if any(heaters.get(name, {}).get('target', 0) != 0 or heaters.get(name, {}).get('temperature', 0) > 50 for name in names):
                raise UpdateFailure('Repair must wait for heating to stop and the printer to cool')

    def materials_check(self):
        for name, expected in self.spec['materials'].items():
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                    or not re.fullmatch(r'[0-9a-f]{64}', str(expected))
                    or digest(no_links(self.materials / name)) != expected):
                raise UpdateFailure('Signed software release material changed')
        self.host.check()
        self.service_check()

    def check(self):
        self.materials_check()
        self.configuration_check()
        if self.state is None:
            self.snapshot()

    def installed(self):
        return bool(self.host.state and self.host._new(self.host.target))

    def staged_import_check(self, sections):
        root = Path(self.host.target if self.installed() else self.host.stage) / 'klippy'
        old = Path(self.spec['host']['targetPath']) / 'klippy/extras'
        names = {name.split()[0] for name in sections if name.split()
                 and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name.split()[0])}
        if any((old / (name + '.py')).is_file() and not (root / 'extras' / (name + '.py')).is_file() for name in names):
            raise UpdateFailure('A configured extension is missing from the staged software release')
        modules = sorted(name for name in names if (root / 'extras' / (name + '.py')).is_file())
        code = ('import importlib,json,sys; sys.path.insert(0,sys.argv[1]); '
                '[importlib.import_module("extras."+name) for name in json.loads(sys.argv[2])]')
        self.command([self.spec['host']['pythonPath'], '-I', '-B', '-c', code, root, json.dumps(modules)], timeout=60)

    def staged_configuration_check(self):
        original = Path(self.spec['host']['targetPath']).resolve()
        staged = Path(self.host.target if self.installed() else self.host.stage)
        for name, checksum in self.state['configurationFiles'].items():
            try:
                relative = Path(name).relative_to(original)
            except ValueError:
                continue
            candidate = staged / relative
            if not candidate.is_file() or digest(candidate) != checksum:
                raise UpdateFailure('An active configuration inside the program directory was not preserved')

    def install(self):
        self.check()
        if self.state is None:
            evidence = self.snapshot()
            baseline = self.host.check()
            self.state = {**self.identity(), 'phase': 'CAPTURED', 'attempt': 0,
                          'configurationFiles': self.configuration_check(),
                          'originalMCUs': evidence['mcus'], 'originalHardware': evidence['hardware'],
                          'activeSections': evidence['activeSections'], 'originalSource': baseline,
                          'serviceStopRequested': False, 'softwareSwitchRequested': False}
            self.save('CAPTURED')
        self.known_activity_check()
        self.host.prepare()
        self.staged_configuration_check()
        self.staged_import_check(self.state['activeSections'])
        self.configuration_check()
        self.state['attempt'] += 1
        if self.installed():
            self.known_activity_check()
            # An interrupted task may have switched files before saving its
            # own checkpoint. The host marker is stronger than a phase label.
        else:
            try:
                evidence = self.snapshot()
            except Exception:
                if not self.state.get('serviceStopRequested'):
                    raise
                self.known_activity_check()
            else:
                self.check_mcus(evidence, learn=True)
        self.configuration_check()
        self.save('STOPPING', serviceStopRequested=True)
        self.command(['systemctl', 'stop', SERVICE])
        self.configuration_check()
        self.save('SWITCHING', softwareSwitchRequested=True)
        self.host.switch()
        self.save('SWITCHED')
        self.command(['systemctl', 'start', SERVICE])
        self.save('STARTED')
        self.verify(wait=True)

    def health_observation(self):
        health = {'connected': False, 'identityVerified': False, 'processId': None,
                  'state': 'unknown', 'stateMessage': '', 'stateSource': 'unavailable'}
        message = ''
        try:
            health['connected'] = self.api('/server/info')['result'].get('klippy_connected') is True
        except Exception:
            pass
        try:
            info = self.live_info()
            health['processId'] = info['process_id']
            health['identityVerified'] = True
            health['state'] = str(info.get('state', 'unknown'))[:32]
            message = info.get('state_message', '')
            health['stateSource'] = 'printer_info'
        except Exception:
            pass
        try:
            webhooks = self.api('/printer/objects/query?webhooks')['result']['status']['webhooks']
            health['state'] = str(webhooks.get('state', 'unknown'))[:32]
            message = webhooks.get('state_message', '')
            health['stateSource'] = 'webhooks'
        except Exception:
            pass
        health['stateMessage'] = diagnostic_message(message)
        key = (health['processId'], health['state'], hashlib.sha256(str(message).encode()).hexdigest())
        return health, key

    def record_health(self, purpose, health, reason):
        # This method is used only in installation/recovery health waits. The
        # pre-install check remains read-only and never writes diagnostics.
        path = self.job / 'klipper-software-health.json'
        record = {**health, 'at': time.time(), 'purpose': purpose,
                  'attempt': self.state['attempt'], 'reason': reason}
        history = []
        if path.exists():
            previous = json.loads(no_links(path).read_text())
            history = previous.get('history', [])[-63:]
        compared = {key: value for key, value in record.items() if key != 'at'}
        if not history or {key: value for key, value in history[-1].items() if key != 'at'} != compared:
            history.append(record)
        write_json(path, {'schemaVersion': 1, 'latest': record, 'history': history})

    def wait_snapshot(self, purpose, wait):
        deadline = time.monotonic() + (120 if wait else 1)
        # A persisted intent also counts when its POST timed out or the
        # controller was interrupted before observing the response.
        requested = any(item.get('attempt') == self.state['attempt'] and item.get('purpose') == purpose
                        for item in self.state.get('applicationRestarts', []))
        stable_key, stable_since = None, None
        last = None
        while True:
            try:
                observed = self.snapshot()
            except Exception as error:
                last = error
            else:
                health, _ = self.health_observation()
                if (health['connected'] and health['identityVerified'] and health['stateSource'] == 'webhooks'
                        and health['state'] == 'ready'):
                    self.record_health(purpose, health, 'Ready')
                    return observed
                last = UpdateFailure('Health state changed during the readiness check')
            health, key = self.health_observation()
            now = time.monotonic()
            reason = 'Waiting for Klipper readiness'
            candidate = (health['connected'] and health['identityVerified']
                         and health['stateSource'] == 'webhooks'
                         and health['state'] in ('error', 'shutdown'))
            if not candidate:
                stable_key, stable_since = None, None
            elif key != stable_key:
                stable_key, stable_since = key, now
            if wait and not requested and candidate and now < deadline and now - stable_since >= 1:
                try:
                    # Hardware constants are populated only after identify.
                    # startup (including slow CAN identify) never reaches here.
                    evidence = self.mcu_evidence()
                    self.check_mcus(evidence)
                    self.configuration_check()
                    self.known_activity_check(require_idle=True)
                    # Re-read the error and service identity immediately before
                    # POST: an error -> startup transition must cancel the reset.
                    final_health, final_key = self.health_observation()
                    if time.monotonic() >= deadline:
                        reason = 'Health verification deadline expired; no application restart'
                    elif (not final_health['connected'] or not final_health['identityVerified']
                            or final_health['stateSource'] != 'webhooks'
                            or final_health['state'] not in ('error', 'shutdown') or final_key != key):
                        health = final_health
                        stable_key, stable_since = None, None
                        reason = 'Health state changed before application restart'
                    else:
                        requested = True
                        requests = list(self.state.get('applicationRestarts', []))
                        requests.append({'attempt': self.state['attempt'], 'purpose': purpose,
                                         'processId': health['processId'], 'at': time.time()})
                        self.save(self.state['phase'], applicationRestarts=requests)
                        self.record_health(purpose, health, 'Application restart intent saved')
                        # fsync may be slow; an external restart during that
                        # I/O must not turn this saved intent into a startup reset.
                        post_health, post_key = self.health_observation()
                        if time.monotonic() >= deadline:
                            reason = 'Health verification deadline expired after saving intent; no application restart'
                        elif (not post_health['connected'] or not post_health['identityVerified']
                              or post_health['stateSource'] != 'webhooks'
                              or post_health['state'] not in ('error', 'shutdown') or post_key != key):
                            health = post_health
                            reason = 'Health state changed after saving intent; no application restart'
                        else:
                            try:
                                self.api('/printer/firmware_restart', {})
                            except Exception as error:
                                last = error
                            reason = 'Application restart requested; waiting for readiness'
                        stable_key, stable_since = None, None
                except Exception as error:
                    last = error
                    reason = 'Application restart blocked by identity or inactivity checks'
            self.record_health(purpose, health, reason)
            if time.monotonic() >= deadline:
                detail = health['state'] + ': ' + health['stateMessage']
                raise UpdateFailure('Software health verification failed: ' + detail + '; '
                                    + diagnostic_message(str(last)))
            time.sleep(.5)

    def verify(self, wait=False):
        if not self.state or not self.installed():
            raise UpdateFailure('This task has not installed the host release')
        self.host.verify()
        observed = self.wait_snapshot('install', wait)
        self.check_mcus(observed)
        self.configuration_check()
        if self.command(['systemctl', 'show', SERVICE, '--property=ActiveState', '--value']) != 'active':
            raise UpdateFailure('Updated Klipper service is not active')
        self.save('VERIFIED')
        self.progress(99, 'Klipper software verified; MCU applications and printing configurations unchanged')

    def failure(self):
        if self.state is None:
            write_json(self.job / 'klipper-software-failure.json', {'phase': 'PRECHECK', 'programChanged': False})
            return
        self.configuration_check()
        if not self.installed():
            original = self.host.check()
            if any(original.get(key) != self.state['originalSource'].get(key) for key in ('head', 'remote')):
                raise UpdateFailure('The unchanged original software source cannot be confirmed')
            # Staging rejection must not stop an untouched, printing original.
            active = self.command(['systemctl', 'show', SERVICE, '--property=ActiveState', '--value'])
            if active == 'active':
                self.save('ORIGINAL_UNCHANGED', recoveryRequired=True)
                return
            self.known_activity_check()
            self.command(['systemctl', 'start', SERVICE])
            observed = self.wait_snapshot('rollback', True)
            self.check_mcus(observed)
            self.save('ORIGINAL_SOFTWARE_RESTORED', recoveryRequired=True)
            return
        self.known_activity_check()
        self.save('ROLLBACK_STOPPING')
        self.command(['systemctl', 'stop', SERVICE])
        self.host.rollback_before_firmware(firmware_write_started=False)
        self.save('ROLLBACK_FILES_RESTORED')
        self.command(['systemctl', 'start', SERVICE])
        observed = self.wait_snapshot('rollback', True)
        self.check_mcus(observed)
        self.configuration_check()
        self.save('ORIGINAL_SOFTWARE_RESTORED', recoveryRequired=True)


def main():
    if sys.argv[1:] not in (['check'], ['install'], ['verify'], ['failure']):
        raise SystemExit(2)
    installer = Installer()
    getattr(installer, sys.argv[1])()


if __name__ == '__main__':
    main()
