#!/usr/bin/env python3
"""Package-owned 600DU Klipper, pressure-probe configuration and CAN update."""
import hashlib
import glob
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from configuration import plan_configuration
from firmware import FirmwareUpdate, validate_image
from host import HostUpdate

CONFIGURATION_PATH = '/home/mingda/printer_data/config/load_cell.cfg'


class UpdateFailure(RuntimeError):
    pass


def reported_version(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if not value or value.casefold() in ('unknown', '未知', '?', 'n/a') else value


def semantic_version(value):
    if value is None:
        return None
    match = re.search(r'(?:^|[-\s])v(\d+)\.(\d+)(?:\.(\d+))?(?=$|[-+_\s])', value, re.IGNORECASE)
    if match is None:
        match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?(?=$|[-+_\s])', value)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3]) if match[3] is not None else None


def build_stamp(value):
    if value is None:
        return None
    match = re.search(r'(?<!\d)(20\d{6}_\d{6})(?!\d)', value)
    if match is None:
        return None
    try:
        return datetime.strptime(match[1], '%Y%m%d_%H%M%S')
    except ValueError:
        return None


def firmware_is_newer(current, target):
    """Reject a demonstrated downgrade; unknown version formats are admissible."""
    old, new = semantic_version(current), semantic_version(target)
    if old is not None and new is not None:
        if old[:2] != new[:2]:
            return old[:2] > new[:2]
        if old[2] is not None and new[2] is not None and old[2] != new[2]:
            return old[2] > new[2]
    old_date, new_date = build_stamp(current), build_stamp(target)
    return old_date is not None and new_date is not None and old_date > new_date


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def no_links(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise UpdateFailure('Symbolic link in update path')
    return path


def write_bytes(path, raw, mode=0o600):
    path = no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.partial')
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value, mode=0o600):
    write_bytes(path, (json.dumps(value, sort_keys=True) + '\n').encode(), mode)


class Installer:
    def __init__(self, materials=None, job=None, run=subprocess.run, api=None,
                 host_factory=HostUpdate, firmware_factory=FirmwareUpdate, require_root=True):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise UpdateFailure('Linux root execution is required')
        self.materials = no_links(materials or os.environ['TMPDIR'])
        self.job = no_links(job or os.environ['MINGDA_JOB_DIR'])
        if not self.materials.is_dir() or not self.job.is_dir() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.job.name):
            raise UpdateFailure('Invalid private task workspace')
        self.spec = json.loads((self.materials / 'release.json').read_text())
        if (self.spec.get('schemaVersion') != 1 or self.spec.get('service') != 'klipper.service'
                or self.spec.get('mcuSection') != 'mcu 600D_HB'
                or self.spec['firmware'].get('canbusUuid') != '4d4400000a02'
                or self.spec.get('configurationPath') != CONFIGURATION_PATH):
            raise UpdateFailure('This package is only for the declared 600DU head board')
        self.run = run
        self.api = api or self.http_json
        self.firmware_factory = firmware_factory
        self.host = host_factory(self.spec['host'], self.materials, self.job, progress=self.host_progress)
        self.configuration = no_links(self.spec['configurationPath'])
        self.state_path = self.job / '600du-state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if self.state and any(self.state.get(k) != v for k, v in self.identity().items()):
            raise UpdateFailure('Recovery material does not belong to this task')

    def identity(self):
        return {'schemaVersion': 1, 'jobId': self.job.name, 'releaseId': self.spec['releaseId'],
                'hostCommit': self.spec['host']['targetCommit'],
                'firmwareSha256': self.spec['firmware']['imageSha256'],
                'configurationTemplateSha256': self.spec['configurationTemplateSha256']}

    @staticmethod
    def http_json(endpoint, payload=None):
        if not endpoint.startswith('/') or endpoint.startswith('//'):
            raise UpdateFailure('Only printer-local API endpoints are permitted')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request('http://127.0.0.1:7125' + endpoint, data=data,
                                         headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateFailure('Printer-local API response is too large')
        return json.loads(raw)

    def command(self, args, timeout=30):
        result = self.run([str(a) for a in args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout, check=False)
        if result.returncode:
            raise UpdateFailure('Command failed: ' + str(args[0]) + ': ' + result.stderr[:300])
        return result.stdout.strip()

    def save(self, phase, **fields):
        if self.state is None:
            raise UpdateFailure('Original device evidence has not been captured')
        self.state.update(fields, phase=phase, updatedAt=time.time())
        write_json(self.state_path, self.state)

    def progress(self, value, message, component='klipper', phase='INSTALLING'):
        write_json(self.job / 'script-progress.json', {
            'phase': phase, 'progress': max(0, min(99, int(value))),
            'component': component, 'message': message}, 0o644)

    def host_progress(self, value, message):
        self.progress(int(value * .35), message)

    def firmware_record(self, stage, **fields):
        # These writes complete before the corresponding CAN side effect.
        if stage == 'BOOTLOADER_INTENT':
            fields['firmwareCompleted'] = False
        if fields.get('writeMayHaveStarted') or stage == 'WRITE_INTENT':
            fields['writeMayHaveStarted'] = True
        if self.state.get('writeMayHaveStarted'):
            fields['writeMayHaveStarted'] = True
        self.save(stage, **fields)

    def firmware_progress(self, value, message):
        self.progress(40 + int(value * .35), message, 'headboard-600du', 'FLASHING')

    @staticmethod
    def process_arguments(pid):
        return (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')

    def snapshot(self):
        info = self.api('/printer/info')['result']
        main = Path(self.spec.get('mainConfigurationPath', self.configuration.with_name('printer.cfg')))
        if (Path(info.get('klipper_path', '')).absolute() != Path(self.spec['host']['targetPath'])
                or Path(info.get('config_file', '')).resolve() != main.resolve()
                or Path(info.get('python_path', '')).absolute().parent != Path(self.spec['host']['pythonPath']).parent):
            raise UpdateFailure('Moonraker is not connected to the declared Klipper and active printer.cfg')
        pid = self.command(['systemctl', 'show', 'klipper.service', '--property=MainPID', '--value'])
        if not pid.isdecimal() or int(pid) < 2 or info.get('process_id') != int(pid):
            raise UpdateFailure('Moonraker is not connected to the fixed Klipper service process')
        argv = self.process_arguments(int(pid))
        if (len(argv) < 3 or Path(argv[0]).absolute().parent != Path(self.spec['host']['pythonPath']).parent
                or Path(argv[1]).absolute() != Path(self.spec['host']['targetPath']) / 'klippy/klippy.py'
                or Path(argv[2]).resolve() != main.resolve()):
            raise UpdateFailure('Klipper service arguments do not select this source and configuration')
        listed = self.api('/printer/objects/list')['result']['objects']
        names = [name for name in listed if name == 'mcu' or name.startswith('mcu ')]
        if self.spec['mcuSection'] not in names:
            raise UpdateFailure('The 600DU head MCU object is missing')
        from urllib.parse import quote
        query = '&'.join(quote(name, safe='') for name in names + [
            'webhooks', 'print_stats', 'pause_resume', 'idle_timeout', 'manual_probe', 'heaters'])
        status = self.api('/printer/objects/query?' + query)['result']['status']
        if (status['webhooks']['state'] != 'ready'
                or status['print_stats']['state'] not in ('standby', 'complete', 'cancelled', 'error')
                or status['pause_resume'].get('is_paused') is not False
                or status['idle_timeout']['state'] not in ('Idle', 'Ready')
                or status.get('manual_probe', {}).get('is_active')):
            raise UpdateFailure('The printer must be ready, idle and not paused or calibrating')
        heaters = status['heaters']['available_heaters']
        if heaters:
            data = self.api('/printer/objects/query?' + '&'.join(quote(name, safe='') for name in heaters))['result']['status']
            if any(data[name]['target'] != 0 or data[name]['temperature'] > 50 for name in heaters):
                raise UpdateFailure('Heaters must be off and at or below 50 C')
        active = self.api('/printer/objects/query?configfile')['result']['status']['configfile']['config']
        mcu = active.get(self.spec['mcuSection'], {})
        if (str(mcu.get('canbus_uuid', '')).lower() != self.spec['firmware']['canbusUuid']
                or mcu.get('canbus_interface', 'can0') != self.spec['firmware']['canInterface']):
            raise UpdateFailure('The configured CAN address does not match the 600DU guide')
        constants = status[self.spec['mcuSection']].get('mcu_constants', {})
        if (constants.get('MCU') != 'stm32f072xb' or constants.get('CANBUS_FREQUENCY') != 1000000
                or constants.get('CLOCK_FREQ') != 48000000 or constants.get('RESERVE_PINS_CAN') != 'PB8,PB9'):
            raise UpdateFailure('The active head MCU type or CAN bitrate is incompatible')
        versions = {name: reported_version(status[name].get('mcu_version')) for name in names}
        old = versions[self.spec['mcuSection']]
        if firmware_is_newer(old, self.spec['firmware']['firmwareVersion']):
            raise UpdateFailure('The detected head firmware is newer than this release')
        hardware = {}
        for name in names:
            constants = status[name].get('mcu_constants', {})
            connection = active.get(name, {})
            hardware[name] = {
                'constants': {key: constants[key] for key in (
                    'MCU', 'CLOCK_FREQ', 'CANBUS_FREQUENCY', 'RESERVE_PINS_CAN') if key in constants},
                'connection': {key: connection[key] for key in (
                    'serial', 'canbus_uuid', 'canbus_interface') if key in connection}}
        return {'mcus': versions, 'mcuHardware': hardware, 'activeConfig': active}

    def check_mcu_evidence(self, evidence, learn_versions=False):
        if set(evidence['mcus']) != set(self.state['originalMCUs']):
            raise UpdateFailure('Connected MCU identities changed')
        for name, original in self.state.get('originalMCUHardware', {}).items():
            actual = evidence['mcuHardware'][name]
            for group in ('constants', 'connection'):
                if any(actual.get(group, {}).get(key) != value for key, value in original[group].items()):
                    raise UpdateFailure('Connected MCU hardware identity changed')
        learned = False
        if learn_versions:
            for name, original in self.state['originalMCUs'].items():
                actual = evidence['mcus'][name]
                if original is not None and actual is not None and actual != original:
                    raise UpdateFailure('MCU application versions changed before installation')
                if original is None and actual is not None:
                    self.state['originalMCUs'][name] = actual
                    learned = True
            if learned:
                # Keep any version learned after an initially unknown reply as
                # recovery evidence before stopping or writing to the device.
                self.save(self.state['phase'])

    def service_check(self):
        props = self.command(['systemctl', 'show', 'klipper.service', '-p', 'User', '-p', 'WorkingDirectory', '-p', 'ExecStart'])
        values = dict(line.split('=', 1) for line in props.splitlines() if '=' in line)
        if (values.get('User') != 'mingda' or values.get('WorkingDirectory') != self.spec['host']['targetPath']
                or self.spec['host']['pythonPath'] not in values.get('ExecStart', '')):
            raise UpdateFailure('The existing Klipper service does not match this package')
        interface = json.loads(self.command(['ip', '-details', '-json', 'link', 'show', 'dev', 'can0']))
        if (len(interface) != 1 or 'UP' not in interface[0].get('flags', [])
                or interface[0].get('linkinfo', {}).get('info_data', {}).get('bittiming', {}).get('bitrate') != 1000000):
            raise UpdateFailure('CAN0 must already be configured and up at 1 Mbit/s')

    def configuration_graph(self):
        """Prove the file to migrate is included by the service's main config."""
        main = Path(self.spec.get('mainConfigurationPath', self.configuration.with_name('printer.cfg')))
        pending, seen = [main], {}
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
                if not match:
                    continue
                pattern = str(path.parent / match.group(1).strip())
                selected = [Path(name) for name in glob.glob(pattern)]
                if not selected and not glob.has_magic(pattern):
                    raise UpdateFailure('A configuration include is missing')
                pending.extend(selected)
        if self.configuration.resolve(strict=True) not in seen:
            raise UpdateFailure('The supplied load-cell file is not included by printer.cfg')
        return {str(path): value for path, value in seen.items()}

    def check_configuration_evidence(self):
        if not self.state:
            return self.configuration_graph()
        current = self.configuration_graph()
        if set(current) != set(self.state['configurationFiles']):
            raise UpdateFailure('Configuration include membership changed during the task')
        for name, expected in self.state['configurationFiles'].items():
            actual = current[name]
            if Path(name) == self.configuration.resolve(strict=True):
                if actual not in (self.state['originalConfigurationSha256'], self.state['nextConfigurationSha256']):
                    raise UpdateFailure('Field configuration changed after this task started')
            elif actual != expected:
                raise UpdateFailure('A calibration or included configuration file changed')
        return self.state['configurationFiles']

    def staged_import_check(self, active):
        # Import only definitions from the verified staged source. Do not load
        # configuration objects, start Klippy, or connect to any MCU.
        stage = getattr(self.host, 'stage', None)
        if stage is None:
            raise UpdateFailure('The staged host source path is unavailable')
        root = Path(stage) / 'klippy'
        if not root.is_dir() and self.state.get('writeMayHaveStarted'):
            root = Path(self.spec['host']['targetPath']) / 'klippy'
        names = {name.split()[0] for name in active if name.split()
                 and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name.split()[0])}
        old = Path(self.spec['host']['targetPath']) / 'klippy/extras'
        if any((old / (name + '.py')).is_file() and not (root / 'extras' / (name + '.py')).is_file() for name in names):
            raise UpdateFailure('A configured extension is missing from the staged Klipper release')
        modules = sorted(name for name in names if (root / 'extras' / (name + '.py')).is_file())
        code = ('import importlib,json,sys; sys.path.insert(0,sys.argv[1]); '
                '[importlib.import_module("extras."+name) for name in json.loads(sys.argv[2])]')
        self.command([self.spec['host']['pythonPath'], '-I', '-B', '-c', code, root, json.dumps(modules)], timeout=60)

    def recovery_activity_check(self):
        # A damaged MCU may prevent a full ready response. Explicitly observed
        # printing, pausing, probing or heating still forbids a repair attempt.
        try:
            status = self.api('/printer/objects/query?print_stats&pause_resume&manual_probe&heaters')['result']['status']
        except Exception:
            return
        if (status.get('print_stats', {}).get('state') in ('printing', 'paused')
                or status.get('pause_resume', {}).get('is_paused')
                or status.get('manual_probe', {}).get('is_active')):
            raise UpdateFailure('Recovery cannot interrupt printing, pausing or calibration')
        names = status.get('heaters', {}).get('available_heaters', [])
        if names:
            from urllib.parse import quote
            try:
                heaters = self.api('/printer/objects/query?' + '&'.join(quote(name, safe='') for name in names))['result']['status']
            except Exception:
                return
            if any(heaters.get(name, {}).get('target', 0) != 0
                   or heaters.get(name, {}).get('temperature', 0) > 50 for name in names):
                raise UpdateFailure('Recovery must wait for heating to stop and the printer to cool')

    def recovery_version_check(self):
        # A broken application can be repaired without a version response. If
        # it does answer as ready, its known version/hardware still constrain
        # recovery and must not be ignored merely because a prior write began.
        try:
            ready = self.api('/printer/objects/query?webhooks')['result']['status']['webhooks']['state'] == 'ready'
        except Exception:
            return
        if not ready:
            return
        evidence = self.snapshot()
        self.check_mcu_evidence(evidence)
        for name, original in self.state['originalMCUs'].items():
            actual = evidence['mcus'][name]
            if name != self.spec['mcuSection'] and original is not None and actual is not None and actual != original:
                raise UpdateFailure('An unchanged MCU application version changed before recovery')

    def materials_check(self):
        for name, expected in self.spec['materials'].items():
            if '/' in name or '\\' in name or name.startswith('.') or digest(no_links(self.materials / name)) != expected:
                raise UpdateFailure('Signed release material changed')
        validate_image(self.materials / 'head.bin', self.spec['firmware'])
        self.host.check()
        self.service_check()

    def check(self):
        self.materials_check()
        if self.state is not None:
            self.check_configuration_evidence()
            return
        self.configuration_graph()
        snapshot = self.snapshot()
        plan_configuration(self.configuration.read_bytes(), (self.materials / 'load_cell.cfg').read_bytes(), snapshot['activeConfig'])

    def install(self):
        self.check()
        if self.state is None:
            evidence = self.snapshot()
            original = self.configuration.read_bytes()
            plan = plan_configuration(original, (self.materials / 'load_cell.cfg').read_bytes(), evidence['activeConfig'])
            info = self.configuration.stat()
            write_bytes(self.job / 'load-cell.before.cfg', original)
            write_bytes(self.job / 'load-cell.next.cfg', plan['content'])
            self.state = {**self.identity(), 'phase': 'PREPARING', 'originalMCUs': evidence['mcus'],
                          'originalMCUHardware': evidence['mcuHardware'],
                          'configurationFiles': self.configuration_graph(),
                          'originalConfigurationSha256': hashlib.sha256(original).hexdigest(),
                          'nextConfigurationSha256': hashlib.sha256(plan['content']).hexdigest(),
                          'configurationMode': stat.S_IMODE(info.st_mode), 'configurationUid': info.st_uid,
                          'configurationGid': info.st_gid, 'writeMayHaveStarted': False}
            self.save('PREPARING')
        if self.state.get('writeMayHaveStarted'):
            self.recovery_activity_check()
            self.recovery_version_check()
        self.host.prepare()
        if not self.state.get('writeMayHaveStarted'):
            current = self.snapshot()  # Check again immediately before stopping Klipper.
            self.check_mcu_evidence(current, learn_versions=True)
            self.staged_import_check(current['activeConfig'])
            self.check_mcu_evidence(self.snapshot(), learn_versions=True)
        else:
            self.recovery_activity_check()
            self.recovery_version_check()
        self.check_configuration_evidence()
        if digest(self.configuration) not in (self.state['originalConfigurationSha256'], self.state['nextConfigurationSha256']):
            raise UpdateFailure('Configuration changed before the firmware write boundary')
        self.save('STOPPING_KLIPPER')
        self.command(['systemctl', 'stop', 'klipper.service'])
        self.save('KLIPPER_STOPPED')
        updater = self.firmware_factory(self.spec['firmware'], self.materials / 'head.bin',
                                        self.materials / 'flash_can.py', self.firmware_record, self.firmware_progress)
        updater.run()
        self.save('FIRMWARE_VERIFIED', firmwareCompleted=True)
        # A changed physical MCU forbids restoring only the old host/configuration.
        self.check_configuration_evidence()
        self.host.switch()
        self.save('HOST_SWITCHED')
        content = (self.job / 'load-cell.next.cfg').read_bytes()
        if hashlib.sha256(content).hexdigest() != self.state['nextConfigurationSha256']:
            raise UpdateFailure('Saved configuration migration material changed')
        self.check_configuration_evidence()
        write_bytes(self.configuration, content, self.state['configurationMode'])
        os.chown(self.configuration, self.state['configurationUid'], self.state['configurationGid'])
        self.save('CONFIGURATION_SWITCHED')
        self.command(['systemctl', 'start', 'klipper.service'])
        self.save('KLIPPER_STARTED')
        deadline = time.monotonic() + 40
        while True:
            try:
                info = self.api('/server/info')['result']
                if info.get('klippy_connected'):
                    self.api('/printer/firmware_restart', {})
                    break
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise UpdateFailure('Klipper did not reconnect for the application restart')
            time.sleep(.5)
        self.save('APPLICATION_RESTART_REQUESTED')
        self.verify(wait=True)

    def verify(self, wait=False):
        if not self.state or not self.state.get('firmwareCompleted'):
            raise UpdateFailure('Firmware readback has not been confirmed')
        self.host.verify()
        deadline = time.monotonic() + (120 if wait else 1)
        last = None
        while True:
            try:
                evidence = self.snapshot()
                self.check_mcu_evidence(evidence)
                for name, version in evidence['mcus'].items():
                    expected = self.spec['firmware']['firmwareVersion'] if name == self.spec['mcuSection'] else self.state['originalMCUs'][name]
                    if (name == self.spec['mcuSection'] or expected is not None) and version != expected:
                        raise UpdateFailure('An MCU did not report its expected application version')
                if digest(self.configuration) != self.state['nextConfigurationSha256']:
                    raise UpdateFailure('Active configuration changed during verification')
                loaded = evidence['activeConfig'].get('load_cell load_cell_head_l', {})
                if (float(loaded.get('version', 0)) != 2.0
                        or loaded.get('dout_pin') != '600D_HB:CS_DOUT'
                        or loaded.get('sclk_pin') != '600D_HB:CS_SCLK'):
                    raise UpdateFailure('Klipper did not load the migrated pressure-probe configuration')
                self.check_configuration_evidence()
                if self.command(['systemctl', 'show', 'klipper.service', '--property=ActiveState', '--value']) != 'active':
                    raise UpdateFailure('Klipper service is not active')
                self.save('VERIFIED')
                self.progress(99, 'Klipper, head firmware and pressure configuration verified', phase='HEALTH_CHECK')
                return
            except Exception as error:
                last = error
            if time.monotonic() >= deadline:
                raise UpdateFailure('Post-update health verification failed: ' + str(last))
            time.sleep(.5)

    def failure(self):
        if self.state is None:
            write_json(self.job / '600du-failure.json', {'phase': 'PRECHECK', 'physicalFirmwareWrite': False})
            return
        if self.state.get('writeMayHaveStarted'):
            self.save('RECOVERY_REQUIRED', recoveryRequired=True)
            return
        # No physical flash command was sent: original host/config may be restored.
        self.check_configuration_evidence()
        self.host.rollback_before_firmware(firmware_write_started=False)
        original = (self.job / 'load-cell.before.cfg').read_bytes()
        if hashlib.sha256(original).hexdigest() != self.state['originalConfigurationSha256']:
            raise UpdateFailure('Original configuration backup changed')
        write_bytes(self.configuration, original, self.state['configurationMode'])
        os.chown(self.configuration, self.state['configurationUid'], self.state['configurationGid'])
        self.command(['systemctl', 'start', 'klipper.service'])
        self.save('ORIGINAL_SOFTWARE_RESTORED', recoveryRequired=True)


def main():
    if sys.argv[1:] not in (['check'], ['install'], ['verify'], ['failure']):
        raise SystemExit(2)
    installer = Installer()
    getattr(installer, sys.argv[1])()


if __name__ == '__main__':
    main()
