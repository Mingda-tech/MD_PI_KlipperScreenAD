"""Screen status and internal task ownership; installation belongs to SWUpdate."""

import os
from pathlib import Path
import tempfile
import time

from .common import UpdateError, atomic_json, identifier, read_json, sync_dir

TERMINAL = {'COMPLETE', 'CANCELLED', 'FAILED'}


def commit_recovery(tx):
    return (type(tx.get('commitProtocol')) is int and tx['commitProtocol'] == 1
            and tx.get('commitIntent') is True and tx.get('nativeSuccess') is True
            and not tx.get('rescueCompleted'))


class Store:
    def __init__(self, root='/var/lib/klipperscreen-updates'):
        self.root = Path(root)
        self.jobs = self.root / 'jobs'
        self.marker = self.root / 'offline-maintenance.json'

    def directory(self, job_id):
        return self.jobs / identifier(job_id)

    def load(self, job_id):
        return read_json(self.directory(job_id) / 'journal.json')

    def save(self, tx):
        tx['updatedAt'] = time.time()
        directory = self.directory(tx['jobId'])
        existing = directory.exists()
        if existing:
            atomic_json(directory / 'journal.json', tx)
        public = {key: tx.get(key) for key in ('jobId', 'phase', 'progress', 'component', 'message', 'error',
                                            'firstError', 'lastRecoveryError')}
        metadata = tx.get('manifest')
        if metadata:
            public['manifest'] = {key: metadata.get(key, '') for key in ('releaseId', 'version', 'recoveryMode')}
            for group in ('components', 'firmware'):
                public['manifest'][group] = []
                for row in metadata.get(group, []):
                    inventory = tx.get('inventory')
                    previous_row = inventory.get(row['id']) if isinstance(inventory, dict) else None
                    previous = previous_row.get('version', '') if isinstance(previous_row, dict) else ''
                    known = (isinstance(previous, str) and bool(previous.strip())
                             and previous.strip().lower() not in ('unknown', '?', 'n/a', '未知'))
                    public['manifest'][group].append({
                        'id': row['id'], 'version': row['version'],
                        'currentVersion': previous if known else '', 'currentVersionKnown': known})
            public['manifest']['configChanges'] = [
                {'path': row.get('path', ''), 'op': row.get('op', '')}
                for row in metadata.get('configChanges', [])]
        public.update(ok=True, engine='SWUpdate',
                      canRecover=tx['phase'] == 'RECOVERY_REQUIRED' and (
                          commit_recovery(tx) or (metadata or {}).get('recoveryMode') == 'replay'),
                      commitRecovery=tx['phase'] == 'RECOVERY_REQUIRED' and commit_recovery(tx),
                      canCancel=(not tx.get('sideEffectsMayHaveStarted') and
                                 tx['phase'] in ('IMPORTING', 'VERIFYING', 'PREPARED') or
                                 tx['phase'] in ('FAILED', 'RECOVERY_REQUIRED') and not tx.get('commitIntent')),
                      recoveryRequired=tx['phase'] == 'RECOVERY_REQUIRED',
                      commitPending=bool(tx.get('commitIntent') and tx['phase'] != 'COMPLETE'),
                      recoveryArtifactsRetained=bool(tx.get('recoveryArtifactsRetained')),
                      awaitingUiReady=tx.get('awaitingUiReady', False))
        if existing:
            atomic_json(directory / 'status.json', public, 0o644)
        else:
            # Publish a new task only after both records are durable. A killed
            # first writer may leave its staging directory, but task admission
            # and boot recovery enumerate only jobs/, never these drafts.
            self.jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
            sync_dir(self.root)
            with tempfile.TemporaryDirectory(prefix='.job-staging-', dir=self.root) as temporary:
                stage = Path(temporary)
                atomic_json(stage / 'journal.json', tx)
                atomic_json(stage / 'status.json', public, 0o644)
                os.replace(stage, directory)
                sync_dir(self.jobs)
        latest = self.root / 'latest.json'
        if not latest.exists() or read_json(latest) != {'jobId': tx['jobId']}:
            atomic_json(latest, {'jobId': tx['jobId']}, 0o644)

    def maintain(self, tx):
        atomic_json(self.marker, {'jobId': tx['jobId'], 'phase': tx['phase']}, 0o644)

    def release(self, tx):
        if self.marker.exists():
            if read_json(self.marker).get('jobId') != tx['jobId']:
                raise UpdateError('MAINTENANCE_OWNER_MISMATCH')
            self.marker.unlink()
            sync_dir(self.root)
