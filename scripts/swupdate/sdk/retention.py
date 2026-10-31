#!/usr/bin/env python3
"""Remove obsolete managed SDK/AI releases only after the outer task commits."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import urllib.request

ROOTS = {'sdk': '/opt/mingda/apps/sdk', 'ai-runtime': '/opt/mingda/apps/ai-runtime'}
DEFAULT_LOCK = '/run/lock/klipperscreen-offline-update.lock'
IDENTITY_FIELDS = ('schemaVersion', 'jobId', 'releaseId', 'specSha256')


class RetentionError(RuntimeError):
    pass


class Deferred(RetentionError):
    pass


def safe(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise RetentionError('A retention path contains a symbolic link or traversal')
    return path


def read_json(path):
    path = safe(path)
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise RetentionError('Invalid retention metadata file')
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RetentionError('Retention metadata must be an object')
    return value


def sync_dir(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, data):
    path = safe(path)
    temporary = path.with_name('.' + path.name + '.partial')
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write((json.dumps(data, sort_keys=True) + '\n').encode())
        stream.flush(); os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path); sync_dir(path.parent)


def identity(state):
    result = {key: state.get(key) for key in IDENTITY_FIELDS}
    if (result['schemaVersion'] != 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', str(result['jobId']))
            or not isinstance(result['releaseId'], str) or not result['releaseId']
            or not re.fullmatch(r'[0-9a-f]{64}', str(result['specSha256']))):
        raise RetentionError('Invalid SDK task identity')
    return result


def prepare_plan(job, state, roots, *, removeOldPackages=False):
    job = safe(job)
    if state.get('phase') != 'COMPLETE' or identity(state)['jobId'] != job.name:
        raise RetentionError('Only a locally verified SDK task can request retention')
    if set(roots) != set(ROOTS) or type(removeOldPackages) is not bool:
        raise RetentionError('Invalid retention scope')
    return {'schemaVersion': 1, 'jobId': job.name, 'identity': identity(state),
            'targetRoots': {key: str(safe(value)) for key, value in roots.items()},
            'removeOldPackages': removeOldPackages}


@contextlib.contextmanager
def file_lock(path):
    import fcntl
    path = safe(path); path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RetentionError('Invalid update lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise Deferred('Another update operation is active') from error
        yield
    finally:
        os.close(descriptor)


def sdk_task(tx):
    manifest = tx.get('manifest') or {}
    rows = manifest.get('components')
    return (tx.get('engine') == 'swupdate' and isinstance(rows, list) and len(rows) == 2
            and {row.get('id') for row in rows if isinstance(row, dict)} == set(ROOTS)
            and manifest.get('firmware') == [] and manifest.get('touchesFirmware') is False
            and manifest.get('touchesScreen') is False)


class Retention:
    def __init__(self, job, plan, *, require_root=True, health=None, run=subprocess.run,
                 lock=file_lock, in_use=None, clock=time.monotonic, sleep=time.sleep):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise RetentionError('Linux root execution is required')
        self.job = safe(job)
        self.root = safe(self.job.parent.parent)
        if self.job.parent.name != 'jobs' or (require_root and self.root != Path('/var/lib/klipperscreen-updates')):
            raise RetentionError('Unexpected SDK task location')
        self.request = read_json(plan) if isinstance(plan, (str, Path)) else plan
        if (not isinstance(self.request, dict) or self.request.get('schemaVersion') != 1
                or self.request.get('jobId') != self.job.name
                or set(self.request.get('targetRoots', {})) != set(ROOTS)
                or type(self.request.get('removeOldPackages')) is not bool):
            raise RetentionError('Invalid retention plan')
        self.roots = {key: safe(value) for key, value in self.request['targetRoots'].items()}
        if require_root and {key: str(value) for key, value in self.roots.items()} != ROOTS:
            raise RetentionError('Retention is limited to the installed SDK and AI roots')
        self.run, self._health, self._lock = run, health or self.health, lock
        self.in_use = in_use or self.process_uses
        self.clock, self.sleep = clock, sleep
        self.report_path = self.job / 'retention-report.json'
        self.global_lock = Path(DEFAULT_LOCK)
        policy = self.job / 'runner/policy.json'
        if policy.is_file():
            self.global_lock = safe(read_json(policy).get('lockPath', DEFAULT_LOCK))

    @staticmethod
    def descriptor(path, component):
        data = read_json(path / 'component.json')
        if data.get('schemaVersion') != 1 or data.get('componentId') != component:
            raise RetentionError('A release does not belong to the declared component')
        for key in ('version', 'variantId'):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', str(data.get(key, ''))):
                raise RetentionError('Invalid managed release identity')
        return data

    def confirmed(self, *, check_health=True):
        journal = read_json(self.job / 'journal.json')
        state = read_json(self.job / 'sdk-ai-state.json')
        if (journal.get('phase') != 'COMPLETE' or journal.get('commitIntent') is not True
                or journal.get('nativeSuccess') is not True or journal.get('rescueCompleted')
                or state.get('phase') != 'COMPLETE'):
            raise Deferred('SDK installation has not committed')
        if (not sdk_task(journal) or journal.get('jobId') != self.job.name
                or journal['manifest'].get('releaseId') != state.get('releaseId')
                or identity(state) != self.request.get('identity')):
            raise RetentionError('The committed task does not match this cleanup plan')
        for path in self.job.parent.iterdir():
            if path == self.job:
                continue
            other = read_json(path / 'journal.json')
            if (other.get('phase') not in ('COMPLETE', 'FAILED', 'CANCELLED')
                    or sdk_task(other) and other.get('recoveryArtifactsRetained')):
                raise Deferred('Another unfinished update must retain its recovery materials')
        current = {}
        for name, root in self.roots.items():
            link = root / 'current'
            if not link.is_symlink():
                raise RetentionError('A managed current link is missing')
            target = safe(link.resolve(strict=True))
            expected = safe(state['components'][name]['target'])
            relative = target.relative_to(root / 'releases')
            if target != expected or len(relative.parts) != 2:
                raise RetentionError('Current release no longer belongs to this task')
            descriptor = self.descriptor(target, name)
            if relative.parts != (descriptor['version'], descriptor['variantId']):
                raise RetentionError('Current release path and identity differ')
            if name == 'sdk' and next(row['version'] for row in journal['manifest']['components'] if row['id'] == 'sdk') != descriptor['version']:
                raise RetentionError('Current SDK version differs from the committed release')
            expected = state.get('releaseInfo', {})
            version_key, variant_key = ('sdkVersion', 'sdkVariant') if name == 'sdk' else ('aiVersion', 'aiVariant')
            if ((version_key in expected and expected[version_key] != descriptor['version'])
                    or (variant_key in expected and expected[variant_key] != descriptor['variantId'])):
                raise RetentionError('Current component differs from its verified local identity')
            if read_json(target / '.mdu-sdk-ai.json') != identity(state):
                raise RetentionError('Current release marker does not match the committed task')
            current[name] = target
        if check_health:
            try:
                if self._health(state, current) is False:
                    raise Deferred('Current SDK/AI health is not confirmed')
            except OSError as error:
                raise Deferred('Current SDK/AI health is temporarily unavailable') from error
        return state, current

    def api(self, port, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:' + str(port) + path, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RetentionError('Local health response is too large')
        return json.loads(raw)

    def health(self, state, current):
        for name, root in current.items():
            result = self.run(['systemctl', 'show', 'mingda-app@' + name + '.service', '-p', 'MainPID', '-p', 'ActiveState'],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=10)
            values = dict(row.split('=', 1) for row in result.stdout.splitlines() if '=' in row)
            pid = values.get('MainPID', '')
            if result.returncode or values.get('ActiveState') != 'active' or not pid.isdecimal() or int(pid) < 2:
                raise Deferred('Current managed service is not running')
            actual = (Path('/proc') / pid / 'exe').stat()
            binaries = [root / 'mingda-printer'] if name == 'sdk' else [root / 'venv/bin/ai-detection-runtime', root / 'venv/bin/python']
            if not any(p.is_file() and (p.stat().st_dev, p.stat().st_ino) == (actual.st_dev, actual.st_ino) for p in binaries):
                raise Deferred('Managed process does not execute the current release')
        expected = state['releaseInfo']
        ai, sdk = self.api(5000, '/api/v1/health'), self.api(7126, '/api/v3/status')
        if (ai.get('status') != 'healthy' or ai.get('selfTest') != 'HEALTHY' or ai.get('loadedModels') != 6
                or ai.get('runtimeVersion') != expected['aiVersion'] or ai.get('variantId') != expected['aiVariant']
                or ai.get('modelBundleVersion') != expected['modelBundleVersion']
                or sdk.get('protocolVersion') != state['baselineHealth']['sdkProtocol']
                or sdk.get('runtimeState') != 'READY' or sdk.get('npuState') != 'READY'
                or sdk.get('runtimeModelError') or sdk.get('inspectionError')):
            raise Deferred('Current SDK/AI local health is not ready')
        for key, value in (('runtimeVariantId', ai['variantId']), ('runtimeModelBundleVersion', ai['modelBundleVersion'])):
            if sdk.get(key) and sdk[key] != value:
                raise Deferred('SDK and AI model identities differ')
        inspection = sdk.get('inspection') or {}
        if (not isinstance(inspection, dict) or inspection.get('busy') or inspection.get('recoveryRequired')
                or (expected.get('requiresKlipperInspection') and
                    (inspection.get('hardwareReady') is not True or inspection.get('factoryProfileDigest') != expected['profileDigest']))):
            raise Deferred('SDK inspection health is not ready')
        return True

    @staticmethod
    def process_uses(candidate):
        prefix = str(candidate) + '/'
        for process in Path('/proc').iterdir():
            if not process.name.isdecimal():
                continue
            for field in ('cwd', 'exe'):
                try:
                    value = os.readlink(process / field).removesuffix(' (deleted)')
                    if value == str(candidate) or value.startswith(prefix):
                        return True
                except FileNotFoundError:
                    pass
                except PermissionError:
                    return True
            try:
                with (process / 'maps').open() as source:
                    if any(prefix in row for row in source):
                        return True
            except FileNotFoundError:
                pass
            except PermissionError:
                return True
        return False

    def old_jobs(self):
        for job in self.job.parent.iterdir():
            if job == self.job:
                continue
            tx = read_json(job / 'journal.json')
            if not sdk_task(tx):
                continue
            state_path = job / 'sdk-ai-state.json'
            state = read_json(state_path) if state_path.is_file() else {}
            committed = (tx.get('phase') == 'COMPLETE' and tx.get('commitIntent') is True and tx.get('nativeSuccess') is True)
            rolled_back = tx.get('phase') == 'FAILED' and state.get('phase') == 'ROLLED_BACK'
            no_install = tx.get('phase') in ('FAILED', 'CANCELLED') and tx.get('resolvedBeforeInstallation') is True
            if committed or rolled_back or no_install:
                yield job, state

    def candidates(self, state, current):
        paths, skipped = {}, []
        def add(path, kind, name=None, owner=None):
            path = safe(path)
            if not path.exists():
                return
            if path in current.values() or any(path == p.parent or path in p.parents for p in current.values()):
                raise RetentionError('Refusing to remove a current release or ancestor')
            if path.is_file() and kind != 'package':
                raise RetentionError('A cleanup directory became a file')
            info = path.stat()
            paths[str(path)] = {'path': str(path), 'kind': kind, 'component': name, 'ownerJob': owner,
                                'device': info.st_dev, 'inode': info.st_ino}
        for name, root in self.roots.items():
            releases = safe(root / 'releases')
            for version in releases.iterdir():
                if version.is_symlink() or not version.is_dir() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', version.name):
                    skipped.append(str(version)); continue
                for path in version.iterdir():
                    if path == current[name]:
                        continue
                    try:
                        data = self.descriptor(safe(path), name)
                        if path.name != data['variantId'] or version.name != data['version']:
                            continue
                        add(path, 'release', name)
                    except (RetentionError, OSError, ValueError):
                        skipped.append(str(path))
        owners = [(self.job, state), *list(self.old_jobs())]
        for job, owner in owners:
            for name, row in owner.get('components', {}).items():
                if name not in self.roots:
                    continue
                target, stage = safe(row['target']), safe(row['stage'])
                target.relative_to(self.roots[name] / 'releases')
                if stage != target.with_name('.' + target.name + '.mdu-' + job.name):
                    raise RetentionError('Recorded stage is outside its owner task')
                if stage.is_dir():
                    self.descriptor(stage, name); add(stage, 'stage', name, job.name)
            add(job / 'sdk-ai-source', 'source', owner=job.name)
            if job != self.job and self.request['removeOldPackages']:
                add(job / 'package.mdu', 'package', owner=job.name)
        return list(paths.values()), skipped

    def plan(self):
        state, current = self.confirmed()
        candidates, skipped = self.candidates(state, current)
        return {'schemaVersion': 1, 'identity': self.request['identity'], 'phase': 'PLANNED',
                'current': {k: str(v) for k, v in current.items()}, 'candidates': candidates,
                'removed': [], 'skipped': skipped}

    def quarantine(self, row):
        if row['component']:
            base = self.roots[row['component']] / ('.retention-' + self.job.name)
        else:
            base = self.root / 'sdk-retention-trash' / self.job.name
        base = safe(base)
        if base.exists():
            if read_json(base / '.owner.json') != self.request['identity']:
                raise RetentionError('Cleanup quarantine belongs to a different task')
        else:
            base.mkdir(parents=True, mode=0o700); os.chmod(base, 0o700)
            write_json(base / '.owner.json', self.request['identity']); sync_dir(base.parent)
        return base / hashlib.sha256(row['path'].encode()).hexdigest()

    def scope(self, row, state, current):
        if (not isinstance(row, dict) or not isinstance(row.get('path'), str)
                or type(row.get('device')) is not int or type(row.get('inode')) is not int):
            raise RetentionError('Invalid saved cleanup candidate')
        path = safe(row['path'])
        if path in current.values() or any(path in p.parents for p in current.values()):
            raise RetentionError('A saved cleanup candidate contains a current release')
        kind, name = row.get('kind'), row.get('component')
        if kind == 'release' and name in self.roots:
            parts = path.relative_to(self.roots[name] / 'releases').parts
            if len(parts) != 2 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', p) for p in parts):
                raise RetentionError('Saved release cleanup path escaped its component')
            return
        owners = {self.job.name: (self.job, state)}
        owners.update({job.name: (job, saved) for job, saved in self.old_jobs()})
        owner = owners.get(row.get('ownerJob'))
        if owner is None:
            raise RetentionError('Saved cleanup owner is not an eligible SDK task')
        job, saved = owner
        if kind == 'stage' and name in self.roots:
            declared = saved.get('components', {}).get(name, {})
            target = safe(declared.get('target', ''))
            target.relative_to(self.roots[name] / 'releases')
            if path != target.with_name('.' + target.name + '.mdu-' + job.name) or str(path) != declared.get('stage'):
                raise RetentionError('Saved stage does not match its SDK task')
            return
        if kind == 'source' and name is None and path == job / 'sdk-ai-source':
            return
        if (kind == 'package' and name is None and job != self.job and self.request['removeOldPackages']
                and path == job / 'package.mdu'):
            return
        raise RetentionError('Saved cleanup path is outside the permitted scope')

    def cleanup_marker(self):
        return {'jobId': self.job.name, 'phase': 'SDK_CLEANUP', 'identity': self.request['identity']}

    def claim_marker(self):
        marker = self.root / 'offline-maintenance.json'
        if marker.exists():
            current = read_json(marker)
            if current.get('jobId') != self.job.name:
                raise Deferred('Maintenance belongs to another update task')
            if current.get('phase') == 'SDK_CLEANUP' and current != self.cleanup_marker():
                raise RetentionError('Cleanup maintenance belongs to different task material')
        write_json(marker, self.cleanup_marker())

    def release_marker(self):
        # worker/global locks are released first, so this never reverses the
        # control -> worker -> global acquisition order or stalls status reads.
        deadline = self.clock() + 30
        while True:
            try:
                with self._lock(self.root / 'control.lock'):
                    marker = self.root / 'offline-maintenance.json'
                    if marker.exists() and read_json(marker) == self.cleanup_marker():
                        marker.unlink(); sync_dir(self.root)
                    return
            except Deferred:
                if self.clock() >= deadline:
                    raise Deferred('Cleanup maintenance release needs a retry')
                self.sleep(min(.1, max(0, deadline - self.clock())))

    def prune(self):
        self.confirmed()  # Health can involve HTTP timeouts; do not hold control.lock.
        stack, claimed = contextlib.ExitStack(), False
        try:
            with self._lock(self.root / 'control.lock'):
                for path in (self.job / 'worker.lock', self.global_lock):
                    stack.enter_context(self._lock(path))
                self.confirmed(check_health=False)
                # If publishing the marker succeeds but its final fsync fails,
                # finally must still remove this exact owned marker.
                claimed = True
                self.claim_marker()
            return self.prune_locked()
        finally:
            stack.close()
            if claimed:
                self.release_marker()

    def prune_locked(self):
        state, current = self.confirmed(check_health=False)
        if self.report_path.is_file():
            report = read_json(self.report_path)
            if report.get('identity') != self.request['identity']:
                raise RetentionError('Cleanup report belongs to different task material')
            if report.get('phase') == 'COMPLETE':
                return report
        else:
            report = self.plan()
        report['phase'] = 'PRUNING'; write_json(self.report_path, report)
        for row in report['candidates']:
            if row['path'] in report['removed']:
                continue
            marker = self.root / 'offline-maintenance.json'
            if not marker.exists() or read_json(marker) != self.cleanup_marker():
                raise Deferred('Cleanup maintenance ownership changed')
            state, current = self.confirmed(check_health=False)
            self.scope(row, state, current)
            path, destination = safe(row['path']), self.quarantine(row)
            if destination.exists():
                info = destination.stat()
                if (info.st_dev, info.st_ino) != (row['device'], row['inode']):
                    raise RetentionError('Quarantined object identity changed')
            elif path.exists():
                info = path.stat()
                if (info.st_dev, info.st_ino) != (row['device'], row['inode']):
                    raise RetentionError('Obsolete object changed before cleanup')
                if self.in_use(path):
                    write_json(self.report_path, report)
                    raise Deferred('An obsolete release is still in use')
                os.replace(path, destination); sync_dir(path.parent); sync_dir(destination.parent)
            if destination.is_dir():
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise RetentionError('Safe descriptor-based removal is unavailable')
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            sync_dir(destination.parent)
            report['removed'].append(row['path']); write_json(self.report_path, report)
            if row['component']:
                parent = path.parent
                if parent not in (p.parent for p in current.values()):
                    try:
                        parent.rmdir(); sync_dir(parent.parent)
                    except OSError:
                        pass
        report.update(phase='COMPLETE', completedAt=time.time())
        write_json(self.report_path, report)
        return report

    def wait(self, timeout=300, *, dry_run=False):
        if not 0 <= timeout <= 3600:
            raise RetentionError('Cleanup wait must be between 0 and 3600 seconds')
        deadline = self.clock() + timeout
        while True:
            try:
                return self.plan() if dry_run else self.prune()
            except Deferred as error:
                if self.clock() >= deadline:
                    return {'phase': 'DEFERRED', 'jobId': self.job.name, 'reason': str(error)}
                self.sleep(min(1, max(0, deadline - self.clock())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--wait-seconds', type=int, default=300)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    result = Retention(args.job, args.plan).wait(args.wait_seconds, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['phase'] in ('COMPLETE', 'PLANNED', 'DEFERRED') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RetentionError, OSError, ValueError, KeyError) as error:
        print('SDK retention: ' + str(error), file=__import__('sys').stderr)
        raise SystemExit(1)
