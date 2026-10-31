"""Synchronize the screen-owned runtime without running a printer update.

Only the screen's fixed bootstrap imports this module.  A source bundle is a
screen release, never an install script selected from removable media.
"""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import uuid

from .common import (DEFAULT_LOCK, UpdateError, atomic_bytes, atomic_json, command, digest,
                     file_lock, no_links, read_json, sync_dir, trusted_path)
from .device import DeviceGate
from .state import TERMINAL
from .swupdate import verify_runtime
from .housekeeping import prune_jobs, prune_releases
from .state import Store


REPOSITORY = Path(__file__).resolve().parents[2]
RUNTIME = Path('/usr/libexec/klipperscreen-update')
STATE = Path('/var/lib/klipperscreen-updates')
UNITS = Path('/etc/systemd/system')
TMPFILES = Path('/etc/tmpfiles.d/klipperscreen-offline-update.conf')
LOCK = Path(DEFAULT_LOCK)
MODULES = ('__init__.py', 'common.py', 'device.py', 'state.py', 'swu_engine.py', 'swu_package.py',
           'swupdate.py', 'swupdate_barrier.py', 'launcher.py', 'usb.py', 'housekeeping.py')
DEFERRED = frozenset(('UPDATE_BUSY', 'PENDING_TASK_EXISTS', 'PRINTER_NOT_IDLE',
                      'PRINTER_STATE_UNKNOWN', 'WAIT_FOR_COOLING'))


def public_reason(code):
    if code == 'RUNTIME_BUNDLE_MISSING':
        return 'RUNTIME_ASSETS_MISSING'
    if code == 'RUNTIME_RELEASE_KEY_INVALID':
        return 'RELEASE_KEY_REQUIRED'
    if code == 'RUNTIME_BASELINE_INVALID':
        return 'UNREGISTERED_BASELINE'
    if code == 'RUNTIME_SOURCE_CHANGED':
        return 'UPDATE_BUSY'
    if code.startswith(('RUNTIME_BUNDLE_', 'SWUPDATE_', 'RUNTIME_POLICY_', 'RUNTIME_PROFILE_')):
        return 'RUNTIME_ASSETS_INVALID'
    if code in DEFERRED or code in ('ROOT_REQUIRED', 'PLATFORM_MISMATCH', 'UNREGISTERED_BASELINE',
                                    'UNTRUSTED_INSTALLATION'):
        return code
    return 'RUNTIME_SETUP_FAILED'


def trusted(path):
    return trusted_path(path)


def _object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UpdateError('RUNTIME_ASSETS_INVALID') from exc
    if not isinstance(value, dict):
        raise UpdateError('RUNTIME_ASSETS_INVALID')
    return value


def host_platform():
    # Distribution requirements belong to each signed update package. Runtime
    # initialization verifies the native engine and its dependencies directly.
    return {'arch': platform.machine()}


def select_profile(bundle, actual):
    """Select executable platform support without reading a printer identity."""
    manifest, files = bundle['manifest'], bundle['files']
    actual = {'arch': actual.get('arch')}
    if manifest['target'] != actual:
        raise UpdateError('PLATFORM_MISMATCH')
    candidates = []
    for item in manifest['profiles']:
        device = item['device']
        if device.get('arch') != actual['arch']:
            continue
        policy = _object(files[item['policy']])
        candidates.append((item, policy))
    if len(candidates) != 1:
        raise UpdateError('RUNTIME_PROFILE_INVALID')
    item, policy = candidates[0]
    policy['device'] = dict(actual)
    for key in ('deviceProfilePath', 'machineSerialNumber', 'hardwareProbe', 'boards', 'releaseReady'):
        policy.pop(key, None)
    return item, policy, dict(actual)


def initial_inventory(bundle, profile, run=command):
    """Keep registered entries; append missing IDs proven by bundled evidence."""
    from .bundle import validate_inventory
    installed = STATE / 'inventory.json'
    if installed.exists():
        inventory = read_json(trusted(installed))
        validate_inventory(inventory)
    else:
        inventory = {}
    declared = _object(bundle['files'][profile['baseline']])
    validate_inventory(declared, require_evidence=True)
    for name, row in declared.items():
        if name in inventory:
            continue
        evidence = row.get('evidence', {})
        path = Path(evidence.get('path', ''))
        if not path.is_absolute() or '..' in path.parts:
            continue
        try:
            kind = evidence.get('kind')
            if kind == 'git':
                expected = evidence.get('commit', '')
                if not re.fullmatch(r'[0-9a-f]{40}', expected):
                    raise UpdateError('UNREGISTERED_BASELINE')
                actual = run(['git', '--no-optional-locks', '-c', 'safe.directory=' + str(path),
                              '-C', str(path), 'rev-parse', 'HEAD'], timeout=5)
            elif kind == 'json':
                expected = evidence.get('value')
                actual = read_json(path).get(evidence.get('field', 'version'))
                if expected != row['version']:
                    raise UpdateError('UNREGISTERED_BASELINE')
            elif kind == 'text':
                expected = evidence.get('value')
                actual = path.read_text(encoding='utf-8').strip()
                if expected != row['version']:
                    raise UpdateError('UNREGISTERED_BASELINE')
            else:
                raise UpdateError('UNREGISTERED_BASELINE')
        except (OSError, ValueError, UpdateError):
            # Missing field evidence leaves the old version unknown. Package
            # scripts still validate actual targets before modifying a device.
            continue
        if actual != expected:
            continue
        inventory[name] = {key: value for key, value in row.items() if key != 'evidence'}
    return inventory


def ensure_no_pending():
    if any((STATE / name).exists() for name in ('offline-maintenance.json', 'maintenance.json')):
        raise UpdateError('PENDING_TASK_EXISTS')
    jobs = STATE / 'jobs'
    if jobs.exists():
        for path in jobs.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise UpdateError('INVALID_STATE')
            if read_json(path / 'journal.json').get('phase') not in TERMINAL:
                raise UpdateError('PENDING_TASK_EXISTS')


def legacy_gate_paths():
    return tuple(UNITS / (name + '.service.d/40-screen-update-gate.conf')
                 for name in ('klipper', 'klipper-mcu'))


def _legacy_gate(raw):
    normalized = raw.replace(b'\r\n', b'\n').strip()
    return normalized in (
        b'[Unit]\nRequires=klipperscreen-update-recovery.service\n'
        b'After=klipperscreen-update-recovery.service\n\n[Service]\n'
        b'ExecCondition=' + prefix + b'/usr/libexec/klipperscreen-update/launcher gate'
        for prefix in (b'', b'+'))


def _decouple_recovery(raw):
    """Recognize our old complete unit before changing any startup ordering."""
    normalized = raw.replace(b'\r\n', b'\n')
    old_order = b'Before=klipper.service klipper-mcu.service moonraker.service KlipperScreen.service'
    if old_order not in normalized.splitlines():
        core = {b'klipper.service', b'klipper-mcu.service', b'moonraker.service'}
        for line in normalized.splitlines():
            if line.split(b'=', 1)[0] in (b'Before', b'After', b'Requires', b'Wants', b'BindsTo'):
                if len(line.split(b'=', 1)) == 2 and core.intersection(line.split(b'=', 1)[1].split()):
                    raise UpdateError('LEGACY_STARTUP_CONFIG_UNRECOGNIZED')
        return raw
    canonical = re.sub(rb'^TimeoutStartSec=[0-9]+$', b'TimeoutStartSec=900', normalized, flags=re.MULTILINE)
    expected = (
        b'[Unit]\nDescription=KlipperScreen offline update recovery check\nAfter=local-fs.target\n'
        + old_order + b'\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        b'ExecStart=/usr/libexec/klipperscreen-update/launcher boot-recover\n'
        b'User=root\nUMask=0077\nTimeoutStartSec=900\n\n[Install]\nWantedBy=multi-user.target\n')
    if canonical.strip() != expected.strip():
        raise UpdateError('LEGACY_STARTUP_CONFIG_UNRECOGNIZED')
    return normalized.replace(old_order, b'Before=KlipperScreen.service')


def retire_legacy_startup_hooks(run=command, lock=file_lock):
    """Remove only our retired core-service hooks, even before task admission.

    No service is stopped here. Replacing an update runtime may be deferred by
    a task, but that must not preserve its old dependency on Klipper startup.
    """
    journal_path = STATE / 'startup-hook-migration.json'

    def candidates():
        changes = {}
        for path in legacy_gate_paths():
            if path.exists() or path.is_symlink():
                raw = trusted(path).read_bytes()
                if not _legacy_gate(raw):
                    raise UpdateError('LEGACY_STARTUP_CONFIG_UNRECOGNIZED')
                changes[path] = (raw, None)
        recovery = UNITS / 'klipperscreen-update-recovery.service'
        if recovery.exists() or recovery.is_symlink():
            raw = trusted(recovery).read_bytes()
            replacement = _decouple_recovery(raw)
            if replacement != raw:
                changes[recovery] = (raw, replacement)
        return changes

    def journal():
        if not journal_path.exists():
            return None
        value = read_json(trusted(journal_path))
        if (not isinstance(value, dict) or value.get('schemaVersion') != 1
                or value.get('phase') not in ('PENDING', 'COMPLETE')):
            raise UpdateError('INVALID_STATE')
        return value

    changes, previous = candidates(), journal()
    if not changes and (previous is None or previous['phase'] == 'COMPLETE'):
        return False
    STATE.mkdir(parents=True, exist_ok=True, mode=0o711)
    trusted(STATE)
    with lock(STATE / 'control.lock'):
        changes, previous = candidates(), journal()
        if not changes and (previous is None or previous['phase'] == 'COMPLETE'):
            return False
        if changes:
            backup_path = STATE / 'retired-startup-hooks' / (uuid.uuid4().hex + '.json')
            backup = {str(path): {
                'data': base64.b64encode(raw).decode('ascii'),
                'mode': stat.S_IMODE(path.stat().st_mode)}
                for path, (raw, _) in changes.items()}
            no_links(backup_path, backup_path.anchor)
            atomic_json(backup_path, {'schemaVersion': 1, 'files': backup})
            previous = {'schemaVersion': 1, 'phase': 'PENDING', 'backup': str(backup_path)}
            atomic_json(journal_path, previous)
            for path, (raw, replacement) in changes.items():
                if trusted(path).read_bytes() != raw:
                    raise UpdateError('RUNTIME_SOURCE_CHANGED')
                if replacement is None:
                    path.unlink()
                    sync_dir(path.parent)
                else:
                    write_file(path, replacement, stat.S_IMODE(path.stat().st_mode))
        # A failed reload leaves PENDING so the next pass retries it even when
        # the retired files have already gone. Never restore the old hooks.
        run(['systemctl', 'daemon-reload'], timeout=10)
        atomic_json(journal_path, {**previous, 'phase': 'COMPLETE'})
    return True


def source_files(repository):
    repository = Path(repository)
    result = {}
    for name in MODULES:
        path = no_links(repository / 'ks_includes/offline_update' / name, repository)
        result['offline_update/' + name] = path.read_bytes()
    for name in ('launcher', 'worker.py'):
        path = no_links(repository / 'scripts/offline-update' / name, repository)
        result[name] = path.read_bytes().replace(b'\r\n', b'\n')
    for name in ('klipperscreen-update-recovery.service',):
        path = no_links(repository / 'scripts/offline-update' / name, repository)
        result['units/' + name] = path.read_bytes().replace(b'\r\n', b'\n')
    for name in ('bundle.py', 'runtime_install.py'):
        path = no_links(repository / 'ks_includes/offline_update' / name, repository)
        result['bootstrap/' + name] = path.read_bytes()
    path = no_links(repository / 'scripts/ensure-offline-update.py', repository)
    result['bootstrap/ensure-offline-update.py'] = path.read_bytes()
    return result


def runtime_version(bundle, sources, policy):
    material = {'bundle': bundle['runtimeVersion'], 'policy': policy,
                'source': {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(sources.items())}}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def runtime_policy(policy, release, bundle):
    result = copy.deepcopy(policy)
    result['lockPath'] = str(LOCK)
    engine = bundle['manifest']['engine']
    result['publicKey'] = str(release / 'release-public.pem')
    settings = result['swupdate']
    settings.pop('publicKey', None)
    settings.update(binary=str(release / 'swupdate'), buildReceipt=str(release / 'build-info.json'),
                    buildConfig=str(release / 'swupdate.config'),
                    binarySha256=bundle['hashes'][engine['binary']])
    settings['libraryFiles'] = {str(release / 'lib' / Path(name).name): bundle['hashes'][name]
                                for name in bundle['manifest'].get('libraries', [])}
    return result


def staged_files(bundle, sources):
    files = {name: raw for name, raw in sources.items() if not name.startswith('bootstrap/')}
    engine = bundle['manifest']['engine']
    for target, source in (('swupdate', engine['binary']), ('build-info.json', engine['receipt']),
                           ('swupdate.config', engine['config']),
                           ('release-public.pem', bundle['manifest']['publicKey'])):
        files[target] = bundle['files'][source]
    for name in bundle['manifest'].get('libraries', []):
        files['lib/' + Path(name).name] = bundle['files'][name]
    return files


def expected_mode(name):
    return 0o755 if name in ('launcher', 'worker.py', 'swupdate') else 0o644


def file_matches(path, raw, mode):
    try:
        trusted(path)
        return (path.is_file() and path.read_bytes() == raw
                and (os.name != 'posix' or stat.S_IMODE(path.stat().st_mode) == mode))
    except (OSError, UpdateError):
        return False


def installed_files_match(release, files, policy, version, check_mode=True):
    try:
        trusted(release)
        for name, raw in files.items():
            path = trusted(release / name)
            if digest(path) != hashlib.sha256(raw).hexdigest():
                return False
            if check_mode and os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) != expected_mode(name):
                return False
        return (read_json(trusted(release / 'policy.json')) == policy
                and read_json(trusted(release / 'runtime.json')) == {'schemaVersion': 1, 'runtimeVersion': version})
    except (OSError, UpdateError):
        return False


def system_files(sources, policy, group):
    recovery = sources['units/klipperscreen-update-recovery.service']
    timeout = max(900, int(policy['swupdate'].get('checkTimeoutSeconds', 300)) +
                  int(policy['swupdate'].get('rescueTimeoutSeconds', 300)) + 120)
    recovery = recovery.replace(b'TimeoutStartSec=900', ('TimeoutStartSec=' + str(timeout)).encode())
    return {
        RUNTIME / 'launcher': (sources['launcher'], 0o755),
        UNITS / 'klipperscreen-update-recovery.service': (recovery, 0o644),
        TMPFILES: (('f %s 0660 root %s - -\n' % (LOCK, group)).encode(), 0o644),
    }


def write_file(path, raw, mode):
    no_links(path, Path(path).anchor)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    trusted(path.parent)
    atomic_bytes(path, raw, mode)


def prepare_lock():
    """Create or reuse this updater's private lock without replacing its inode."""
    import grp
    import pwd
    try:
        before = LOCK.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1:
            raise UpdateError('INVALID_LOCK')
        group = grp.getgrgid(before.st_gid)
    except FileNotFoundError:
        account = pwd.getpwuid(int(os.environ.get('SUDO_UID', '0')))
        group = grp.getgrgid(account.pw_gid)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise UpdateError('INVALID_LOCK')
        os.fchown(descriptor, 0, group.gr_gid)
        os.fchmod(descriptor, 0o660)
    finally:
        os.close(descriptor)
    return group.gr_name


def _backup(paths):
    result = {}
    for path in paths:
        if path.exists():
            trusted(path)
            result[str(path)] = {'data': base64.b64encode(path.read_bytes()).decode('ascii'),
                                 'mode': stat.S_IMODE(path.stat().st_mode)}
        else:
            result[str(path)] = None
    return result


def _restore(journal, allowed):
    backup = journal.get('backup', {})
    retired = {str(path) for path in legacy_gate_paths()}
    if not isinstance(backup, dict) or set(backup) - {str(path) for path in allowed} - retired:
        raise UpdateError('INVALID_STATE')
    for path in allowed:
        if str(path) not in backup or str(path) in retired:
            continue
        entry = backup[str(path)]
        if entry is None:
            no_links(path, path.anchor)
            if path.exists():
                path.unlink()
                sync_dir(path.parent)
        else:
            raw = base64.b64decode(entry['data'], validate=True)
            if path == UNITS / 'klipperscreen-update-recovery.service':
                raw = _decouple_recovery(raw)
            write_file(path, raw, entry['mode'])


def _result(state, reason='', version='', changed=False):
    return {'ok': True, 'runtimeState': state, 'runtimeReason': reason,
            'runtimeVersion': version, 'changed': changed}


def root_access():
    return os.name == 'posix' and os.geteuid() == 0


def discard_stage(stage):
    if stage is None or not stage.exists():
        return
    root = RUNTIME / 'releases'
    if stage.parent != root or not re.fullmatch(r'[0-9a-f]{64}\.staging-[0-9a-f]{32}', stage.name):
        raise UpdateError('INVALID_STATE')
    no_links(stage, root)
    if stage.resolve().parent != root.resolve():
        raise UpdateError('INVALID_STATE')
    shutil.rmtree(stage)
    sync_dir(root)


def _synchronize(repository=REPOSITORY, run=command, loader=None, device_factory=DeviceGate,
                 verifier=verify_runtime, lock=file_lock, platform_probe=host_platform):
    """One bounded, idempotent pass. GTK schedules retries for deferred results."""
    version = ''
    stage = None
    if not root_access():
        return _result('BLOCKED', 'ROOT_REQUIRED')
    try:
        retire_legacy_startup_hooks(run=run, lock=lock)
        # A pending task has priority even if the new source release has missing assets.
        if STATE.exists():
            trusted(STATE)
            ensure_no_pending()
        if loader is None:
            from .bundle import load_bundle
            loader = load_bundle
        bundle = loader(Path(repository) / 'resources/offline-update')
        profile, policy, _ = select_profile(bundle, platform_probe())
        sources = source_files(repository)
        version = runtime_version(bundle, sources, policy)
        files = staged_files(bundle, sources)
        release = RUNTIME / 'releases' / version
        desired_policy = runtime_policy(policy, release, bundle)
        group = prepare_lock()
        STATE.mkdir(parents=True, exist_ok=True, mode=0o711)
        os.chmod(STATE, 0o711)
        trusted(STATE)
        with lock(STATE / 'control.lock'), lock(LOCK):
            ensure_no_pending()
            prune_jobs(Store(STATE))
            external = system_files(sources, policy, group)
            pointer = RUNTIME / 'current.json'
            inventory_path = STATE / 'inventory.json'
            transaction_paths = [*external, pointer, inventory_path]
            journal_path = STATE / 'runtime-setup.json'
            if journal_path.exists():
                previous = read_json(trusted(journal_path))
                if previous.get('phase') not in ('COMPLETE', 'ROLLED_BACK'):
                    _restore(previous, transaction_paths)
                    run(['systemctl', 'daemon-reload'], timeout=10)
                    atomic_json(journal_path, {**previous, 'phase': 'ROLLED_BACK'})
            inventory = initial_inventory(bundle, profile, run)
            before_inventory = read_json(trusted(inventory_path)) if inventory_path.exists() else None
            if before_inventory is not None and any(
                    inventory.get(name) != row for name, row in before_inventory.items()):
                raise UpdateError('RUNTIME_BASELINE_INVALID')
            if (pointer.exists() and read_json(trusted(pointer)) == {'schemaVersion': 1, 'runtimeVersion': version}
                    and installed_files_match(release, files, desired_policy, version)
                    and before_inventory is not None and inventory == before_inventory
                    and all(file_matches(path, raw, mode) for path, (raw, mode) in external.items())):
                # System loaders or libraries may change independently of this
                # release. Recheck native execution even when its bytes match.
                device_factory(desired_policy).validate_platform()
                verifier(desired_policy)
                prune_releases(RUNTIME)
                return _result('READY', version=version)
            # This installs only the updater's own runtime and screen recovery
            # unit. Printer readiness is checked when an update is started.
            RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o755)
            trusted(RUNTIME)
            release.parent.mkdir(exist_ok=True, mode=0o755)
            trusted(release.parent)
            stage = release.with_name(version + '.staging-' + uuid.uuid4().hex)
            stage.mkdir(mode=0o755)
            for name, raw in files.items():
                mode = expected_mode(name)
                write_file(stage / name, raw, mode)
            atomic_json(stage / 'runtime.json', {'schemaVersion': 1, 'runtimeVersion': version}, 0o644)
            # Verify the exact copied material before changing the active runtime.
            stage_policy = runtime_policy(policy, stage, bundle)
            atomic_json(stage / 'policy.json', desired_policy)
            verifier(stage_policy)
            # Re-read source to detect a concurrent Git checkout not using the shared lock.
            if (source_files(repository) != sources
                    or loader(Path(repository) / 'resources/offline-update')['runtimeVersion']
                    != bundle['runtimeVersion']):
                raise UpdateError('RUNTIME_SOURCE_CHANGED')
            ensure_no_pending()
            if release.exists():
                trusted(release)
                if installed_files_match(release, files, desired_policy, version, check_mode=False):
                    # An interrupted previous pass may have left a verified version.
                    for name in files:
                        os.chmod(release / name, expected_mode(name))
                    discard_stage(stage)
                    stage = None
                else:
                    raise UpdateError('RUNTIME_VERSION_DAMAGED')
            else:
                os.replace(stage, release)
                stage = None
                sync_dir(release.parent)
            journal = {'schemaVersion': 1, 'phase': 'SWITCHING', 'runtimeVersion': version,
                       'backup': _backup(transaction_paths)}
            atomic_json(journal_path, journal)
            try:
                if inventory != before_inventory:
                    atomic_json(inventory_path, inventory)
                for path, (raw, mode) in external.items():
                    if not file_matches(path, raw, mode):
                        write_file(path, raw, mode)
                run(['systemctl', 'daemon-reload'], timeout=10)
                run(['systemctl', 'enable', 'klipperscreen-update-recovery.service'], timeout=10)
                atomic_json(pointer, {'schemaVersion': 1, 'runtimeVersion': version}, 0o644)
                # Execute no scripts from the update package: this checks the installed bridge only.
                reply = _object(run(['/usr/bin/python3', '-I', str(RUNTIME / 'launcher'), 'status'], timeout=5))
                if reply.get('ok') is not True or reply.get('setupRequired') is not False:
                    raise UpdateError('RUNTIME_SELF_CHECK_FAILED')
                device_factory(desired_policy).validate_platform()
                atomic_json(journal_path, {**journal, 'phase': 'COMPLETE'})
            except Exception:
                _restore(journal, transaction_paths)
                run(['systemctl', 'daemon-reload'], timeout=10)
                atomic_json(journal_path, {**journal, 'phase': 'ROLLED_BACK'})
                raise
            prune_releases(RUNTIME)
            return _result('READY', version=version, changed=True)
    except UpdateError as exc:
        reason = public_reason(exc.code)
        return _result('DEFERRED' if reason in DEFERRED else 'BLOCKED', reason, version)
    except (OSError, ValueError, KeyError, TypeError):
        return _result('BLOCKED', 'RUNTIME_SETUP_FAILED', version)
    finally:
        try:
            discard_stage(stage)
        except (OSError, UpdateError):
            pass


def ensure_runtime(repository=REPOSITORY, run=command, **options):
    result = _synchronize(repository, run=run, **options)
    if result['runtimeState'] == 'READY':
        try:
            # boot-recover acquires the same shared lock.  Start it only AFTER
            # synchronization releases that lock, before admitting a package.
            # RemainAfterExit prevents later Klipper restarts from rerunning it
            # inside an active package's maintenance window.
            run(['systemctl', 'start', 'klipperscreen-update-recovery.service'], timeout=20)
        except (OSError, UpdateError):
            return _result('BLOCKED', 'RUNTIME_SETUP_FAILED', result['runtimeVersion'], result['changed'])
    return result
