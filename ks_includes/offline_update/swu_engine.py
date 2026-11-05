"""Admission/UI bridge to SWUpdate, with no program-specific installation handlers."""

import copy
import re
import time

from .common import DEFAULT_LOCK, UpdateError, atomic_json, digest, file_lock, identifier, read_json
from .device import DeviceGate
from .state import Store, TERMINAL
from . import swupdate
from .swu_package import read_preview


class Engine:
    def __init__(self, policy, store=None, device=None, native=None, preview=read_preview, lock=file_lock):
        self.policy = policy
        self.store = store or Store()
        self.device = device or DeviceGate(policy)
        self.native = native or swupdate
        self.preview = preview
        self.lock = lock
        self._progress_reports = {}

    def update(self, tx, phase, progress, **fields):
        tx.update(phase=phase, progress=progress, **fields)
        self.store.save(tx)

    def inventory(self):
        try:
            inventory = read_json(self.store.root / 'inventory.json')
        except UpdateError as exc:
            raise UpdateError('INVALID_INVENTORY') from exc
        self.validate_inventory(inventory)
        return inventory

    @staticmethod
    def validate_inventory(inventory):
        # A missing component is an unknown previous version. A damaged state
        # file is different: do not silently erase its known release ordering.
        try:
            if not isinstance(inventory, dict):
                raise ValueError()
            for name, row in inventory.items():
                identifier(name)
                if (not isinstance(row, dict)
                        or set(row) - {'version', 'sequence', 'releaseId', 'packageSha256', 'evidence'}
                        or not isinstance(row.get('version'), str) or not row['version']
                        or type(row.get('sequence')) is not int or row['sequence'] < 1):
                    raise ValueError()
                if 'releaseId' in row:
                    identifier(row['releaseId'])
                if 'packageSha256' in row and (
                        not isinstance(row['packageSha256'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', row['packageSha256'])):
                    raise ValueError()
                if 'evidence' in row:
                    evidence = row['evidence']
                    if not isinstance(evidence, dict):
                        raise ValueError()
                    kind = evidence.get('kind')
                    fields = {'git': {'kind', 'path', 'commit'}, 'json': {'kind', 'path', 'field', 'value'},
                              'text': {'kind', 'path', 'value'}}.get(kind)
                    path = evidence.get('path')
                    if (fields is None or set(evidence) != fields or not isinstance(path, str)
                            or not path.startswith('/') or '\\' in path
                            or any(part in ('.', '..') for part in path.split('/'))):
                        raise ValueError()
                    if kind == 'git':
                        if (not isinstance(evidence['commit'], str)
                                or not re.fullmatch(r'[0-9a-f]{40}', evidence['commit'])):
                            raise ValueError()
                    elif evidence['value'] != row['version']:
                        raise ValueError()
                    if kind == 'json' and (
                            not isinstance(evidence['field'], str)
                            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+~-]{0,127}', evidence['field'])):
                        raise ValueError()
        except (ValueError, TypeError, UpdateError) as exc:
            raise UpdateError('INVALID_INVENTORY') from exc

    def validate(self, metadata, inventory, package_hash):
        self.validate_inventory(inventory)
        for row in metadata.get('components', []) + metadata.get('firmware', []):
            old = inventory.get(row['id'])
            if old is None:
                # First-time managed updates have no trusted previous release.
                # Signature and package-owned compatibility checks still apply;
                # register the new release only after verified installation.
                continue
            if type(old.get('sequence')) is not int:
                raise UpdateError('INVALID_INVENTORY', row['id'])
            if row['sequence'] < old['sequence']:
                raise UpdateError('DOWNGRADE_BLOCKED', row['id'])
            if row['sequence'] == old['sequence'] and (
                    row['version'] != old.get('version') or package_hash != old.get('packageSha256')):
                raise UpdateError('RELEASE_IDENTITY_CHANGED', row['id'])
        if metadata.get('touchesFirmware'):
            if not metadata.get('firmware'):
                raise UpdateError('FIRMWARE_METADATA_REQUIRED')

    def verified(self, tx, mode='install'):
        job = self.store.directory(tx['jobId'])
        package = job / 'package.mdu'
        if tx.get('packageSha256') and digest(package) != tx['packageSha256']:
            raise UpdateError('CACHED_PACKAGE_CHANGED')
        # The pure parser refuses unsafe handlers/hooks before SWUpdate parses anything.
        candidate = self.preview(package)
        self.native.check(package, self.policy, job, mode=mode)
        return candidate

    def prepare(self, job_id):
        tx = self.store.load(job_id)
        try:
            self.update(tx, 'VERIFYING', 10)
            job = self.store.directory(job_id)
            if (job / 'cancel').exists():
                raise UpdateError('CANCELLED')
            self.device.validate_platform()
            metadata = self.verified(tx)
            if metadata.get('bootRescue'):
                self.verified(tx, mode='rescue')
            package_hash = digest(job / 'package.mdu')
            inventory = self.inventory()
            self.validate(metadata, inventory, package_hash)
            if (job / 'cancel').exists():
                raise UpdateError('CANCELLED')
            tx.update(engine='swupdate', engineVersion='2026.05', manifest=metadata,
                      packageSha256=package_hash, inventory=inventory, device=copy.deepcopy(self.policy['device']))
            self.update(tx, 'PREPARED', 15)
        except Exception as exc:
            code = exc.code if isinstance(exc, UpdateError) else 'SWUPDATE_PREPARATION_FAILED'
            self.update(tx, 'CANCELLED' if code == 'CANCELLED' else 'FAILED', 0, error=code)

    def progress(self, tx, value):
        package_detail = value.get('nativeStatus') == 'PACKAGE_DETAIL'
        if not package_detail and tx.get('scriptProgressStarted'):
            # Native progress describes payload staging, not the package's
            # subsequent work. Late frames must not erase a package stage.
            return
        phase = value.get('phase', 'INSTALLING')
        if phase not in ('INSTALLING', 'FLASHING', 'HEALTH_CHECK'):
            phase = 'INSTALLING'
        percent = max(0, min(99 if package_detail else 100, int(value.get('progress', 0))))
        message = str(value.get('message', ''))[:512]
        if not package_detail and message.lstrip().startswith('{'):
            message = ''  # Native reboot/control metadata is not operator-facing progress text.
        # Payload verification/staging owns 20..35; actual package work owns
        # 35..94. Neither native SUCCESS nor a script can claim task completion.
        mapped = 35 + percent * 59 // 99 if package_detail else 20 + percent * 15 // 100
        progress = max(tx.get('progress', 0), mapped)
        fields = {'component': str(value.get('component', ''))[:128], 'message': message}
        detail = {key: value[key] for key in ('stage', 'completed', 'total', 'unit', 'elapsedSeconds', 'heartbeat')
                  if key in value} if package_detail else {}
        if package_detail:
            fields.update(scriptProgressStarted=True, scriptProgressPercent=percent, progressDetail=detail)
        previous_detail = tx.get('progressDetail') or {}
        force = (not package_detail and tx['jobId'] not in self._progress_reports
                 or package_detail and not tx.get('scriptProgressStarted')
                 or phase != tx.get('phase')
                 or package_detail and detail.get('stage') and detail['stage'] != previous_detail.get('stage')
                 or package_detail and percent == 99 and tx.get('scriptProgressPercent') != 99)
        same = (phase == tx.get('phase') and progress == tx.get('progress')
                and all(tx.get(key) == item for key, item in fields.items()))
        if same:
            return
        now = time.monotonic()
        if not force and now - self._progress_reports.get(tx['jobId'], float('-inf')) < 1:
            return  # Counts/heartbeats can arrive much faster than useful UI writes.
        self.update(tx, phase, progress, **fields)
        self._progress_reports[tx['jobId']] = now

    def finish(self, tx):
        metadata = tx['manifest']
        if metadata.get('touchesScreen'):
            self.update(tx, 'HEALTH_CHECK', 95, component='klipperscreen', awaitingUiReady=True)
            deadline = time.monotonic() + self.policy.get('uiReadyTimeoutSeconds', 60)
            while not (self.store.directory(tx['jobId']) / 'ui-ready.json').exists():
                if time.monotonic() >= deadline:
                    raise UpdateError('SCREEN_NOT_READY')
                time.sleep(.5)
            tx['awaitingUiReady'] = False
        # Package postinst performs component-specific verification; additionally verify admission returns.
        self.device.idle()
        # All installation and health checks have passed. From this durable
        # boundary onward, recovery finishes bookkeeping without replay/rescue.
        tx['commitIntent'] = True
        # Older executors could rescue after commitIntent, even without a
        # rescueCompleted record. Only this protocol rules out that rollback.
        tx['commitProtocol'] = 1
        self.store.save(tx)
        self.commit(tx)

    def commit(self, tx):
        if (type(tx.get('commitProtocol')) is not int or tx['commitProtocol'] != 1
                or tx.get('commitIntent') is not True or tx.get('nativeSuccess') is not True
                or tx.get('rescueCompleted')):
            raise UpdateError('INVALID_COMMIT_STATE')
        metadata = tx['manifest']
        self.validate_inventory(tx['inventory'])
        inventory = copy.deepcopy(tx['inventory'])
        for row in metadata.get('components', []) + metadata.get('firmware', []):
            inventory[row['id']] = {'version': row['version'], 'sequence': row['sequence'],
                                    'releaseId': metadata['releaseId'], 'packageSha256': tx['packageSha256']}
        current = self.inventory()
        if current != tx['inventory'] and current != inventory:
            raise UpdateError('PREPARATION_STALE')
        if current != inventory:
            atomic_json(self.store.root / 'inventory.json', inventory)
        self.update(tx, 'COMPLETE', 100, error=None, awaitingUiReady=False,
                    message='SWUpdate verified installation completed')
        self.store.release(tx)

    def rescue(self, tx, boot=False):
        if not tx.get('manifest', {}).get('bootRescue'):
            return
        self.verified(tx, mode='rescue')
        self.store.maintain(tx)
        self.native.install(self.store.directory(tx['jobId']) / 'package.mdu', self.policy,
                            self.store.directory(tx['jobId']), lambda value: None,
                            mode='rescue', boot_recovery=boot)
        tx['rescueCompleted'] = True
        self.store.save(tx)

    def run(self, job_id, recovering=False):
        with self.lock(self.policy.get('lockPath', DEFAULT_LOCK)):
            tx = self.store.load(job_id)
            if tx.get('engine') != 'swupdate':
                raise UpdateError('UNSUPPORTED_TASK_ENGINE')
            if tx['phase'] != ('RECOVERY_REQUIRED' if recovering else 'PREPARED'):
                raise UpdateError('INVALID_TASK_PHASE')
            if self.store.marker.exists() and read_json(self.store.marker).get('jobId') != job_id:
                raise UpdateError('UPDATE_BUSY')
            try:
                for path in self.store.jobs.iterdir():
                    if path.name != job_id and self.store.load(path.name).get('phase') not in TERMINAL:
                        raise UpdateError('PENDING_TASK_EXISTS')
                if recovering and tx.get('commitProtocol') == 1:
                    self.commit(tx)
                    return
                if not recovering:
                    # Check at confirmation, then again after potentially slow
                    # package verification, before executing any package code.
                    self.device.idle()
                metadata = self.verified(tx)
                if metadata != tx['manifest'] or self.policy['device'] != tx['device']:
                    raise UpdateError('PREPARATION_STALE')
                self.device.validate_platform()
                self.validate(metadata, tx['inventory'], tx['packageSha256'])
                if recovering:
                    if metadata.get('recoveryMode') != 'replay':
                        raise UpdateError('MANUAL_RECOVERY_REQUIRED')
                else:
                    if self.inventory() != tx['inventory']:
                        raise UpdateError('PREPARATION_STALE')
                    self.device.idle()
                    if (self.store.directory(job_id) / 'cancel').exists():
                        raise UpdateError('CANCELLED')
                self.update(tx, 'INSTALLING', max(20, tx.get('progress', 0)), awaitingUiReady=False)
                self.store.maintain(tx)
                # A replay after an older executor's rescue is a new attempt;
                # its previous success/commit evidence must not survive it.
                for field in ('commitIntent', 'commitProtocol', 'nativeSuccess', 'rescueCompleted'):
                    tx.pop(field, None)
                # Signed preinst is arbitrary code, so record the boundary before native execution.
                tx['sideEffectsMayHaveStarted'] = True
                self.store.save(tx)
                ack = self.store.directory(job_id) / 'ui-ready.json'
                if ack.exists():
                    ack.unlink()
                result = self.native.install(self.store.directory(job_id) / 'package.mdu', self.policy,
                                             self.store.directory(job_id), lambda value: self.progress(tx, value),
                                             mode='recover' if recovering else 'install')
                if result.get('success') is not True or result.get('nativeStatus') != 'SUCCESS':
                    raise UpdateError('SWUPDATE_RESULT_UNCONFIRMED')
                tx['nativeSuccess'] = True
                self.store.save(tx)
                self.finish(tx)
            except Exception as exc:
                code = exc.code if isinstance(exc, UpdateError) else 'SWUPDATE_EXECUTION_FAILED'
                # Keep the original installation evidence separate from later
                # recovery attempts. A refusal to replay is not its root cause.
                original = tx.get('error') if recovering else code
                if not tx.get('firstError') and original and original != 'MANUAL_RECOVERY_REQUIRED':
                    tx['firstError'] = original
                if recovering:
                    tx['lastRecoveryError'] = code
                    if code == 'MANUAL_RECOVERY_REQUIRED':
                        code = tx.get('firstError') or tx.get('error') or code
                if tx.get('sideEffectsMayHaveStarted'):
                    self.update(tx, 'RECOVERY_REQUIRED', tx.get('progress', 0), error=code, awaitingUiReady=False)
                    self.store.maintain(tx)
                    if tx.get('commitProtocol') != 1:
                        try:
                            self.rescue(tx)
                        except Exception:
                            tx['rescueError'] = 'PACKAGE_RESCUE_FAILED'
                    self.store.maintain(tx)
                    self.store.save(tx)
                else:
                    self.update(tx, 'CANCELLED' if code == 'CANCELLED' else 'FAILED', 0, error=code)
                    self.store.release(tx)

    def boot_recover(self):
        with self.lock(self.policy.get('lockPath', DEFAULT_LOCK)):
            for path in sorted(self.store.jobs.glob('*')):
                tx = self.store.load(path.name)
                if tx['phase'] in TERMINAL:
                    if self.store.marker.exists() and read_json(self.store.marker).get('jobId') == tx['jobId']:
                        self.store.release(tx)
                    continue
                if tx.get('engine') != 'swupdate':
                    raise UpdateError('UNSUPPORTED_TASK_ENGINE')
                if tx.get('commitProtocol') == 1:
                    self.commit(tx)
                    continue
                if tx['phase'] == 'PREPARED':
                    continue
                if tx.get('sideEffectsMayHaveStarted'):
                    self.update(tx, 'RECOVERY_REQUIRED', tx.get('progress', 0), awaitingUiReady=False)
                    self.store.maintain(tx)
                    try:
                        self.rescue(tx, boot=True)  # File restoration only; no synchronous service starts at boot.
                    finally:
                        self.store.maintain(tx)
                else:
                    self.update(tx, 'FAILED', 0, error='INTERRUPTED_BEFORE_NATIVE_INSTALL')
                    if self.store.marker.exists() and read_json(self.store.marker).get('jobId') == tx['jobId']:
                        self.store.release(tx)
