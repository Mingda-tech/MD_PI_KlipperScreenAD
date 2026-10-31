"""Unprivileged, bounded JSON client for the installed offline update launcher.

The GTK caller must run requests in a worker thread.  No command or path from
the USB drive is interpreted by this client.
"""
import json
import math
from pathlib import Path
import re
import subprocess


LAUNCHER = '/usr/libexec/klipperscreen-update/launcher'
RUNTIME_SETUP = Path(__file__).resolve().parents[1] / 'scripts' / 'ensure-offline-update.py'
RUNTIME_STATES = frozenset(('READY', 'DEFERRED', 'BLOCKED'))
RUNTIME_REASONS = frozenset((
    'UPDATE_BUSY', 'PENDING_TASK_EXISTS', 'PRINTER_NOT_IDLE', 'PRINTER_STATE_UNKNOWN', 'WAIT_FOR_COOLING',
    'RUNTIME_ASSETS_MISSING', 'RUNTIME_ASSETS_INVALID', 'RELEASE_KEY_REQUIRED', 'PLATFORM_MISMATCH',
    'UNREGISTERED_BASELINE', 'UNTRUSTED_INSTALLATION', 'ROOT_REQUIRED', 'RUNTIME_SETUP_FAILED',
    'RUNTIME_SETUP_TIMEOUT',
))
TERMINAL_PHASES = frozenset(('IDLE', 'COMPLETE', 'CANCELLED', 'FAILED'))
PREPARATION_PHASES = frozenset(('IMPORTING', 'VERIFYING', 'PREPARED'))
INSTALLATION_PHASES = frozenset((
    'FLASHING', 'INSTALLING', 'HEALTH_CHECK',
))
KNOWN_PHASES = TERMINAL_PHASES | PREPARATION_PHASES | INSTALLATION_PHASES | {'RECOVERY_REQUIRED'}
SETUP_MISSING = frozenset((
    'policy', 'key', 'engine', 'receipt', 'engineConfig', 'inventory',
    'recoveryService',
))
# Frozen tasks may still answer through a previous launcher. These removed
# startup hooks are accepted only for response compatibility, never required.
LEGACY_SETUP_MISSING = frozenset(('klipperGate', 'linuxMcuGate'))


class OfflineUpdateError(Exception):
    """A local launcher request did not return a usable result."""


def valid_identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is not None


def validate_response(action, result):
    """Reject malformed helper replies before the GTK callback consumes them."""
    invalid = 'Offline update service returned an invalid response; check its installation'
    if action in ('status', 'scan'):
        if 'setupRequired' in result and not isinstance(result['setupRequired'], bool):
            raise OfflineUpdateError(invalid)
        if 'setupMissing' in result:
            missing = result['setupMissing']
            known = SETUP_MISSING | LEGACY_SETUP_MISSING
            if (not isinstance(missing, list) or len(missing) > len(known)
                    or not all(isinstance(item, str) and item in known for item in missing)
                    or len(set(missing)) != len(missing)):
                raise OfflineUpdateError(invalid)
            if any(item in LEGACY_SETUP_MISSING for item in missing):
                result = dict(result)
                result['setupMissing'] = [item for item in missing if item not in LEGACY_SETUP_MISSING]
                if result.get('setupRequired') is True and not result['setupMissing']:
                    result['setupRequired'] = False
    if action in ('prepare', 'start', 'recover') and not valid_identifier(result.get('jobId')):
        raise OfflineUpdateError('Offline update service did not return a job identifier')
    if action == 'scan':
        packages = result.get('packages')
        if not isinstance(packages, list) or len(packages) > 128:
            raise OfflineUpdateError(invalid)
        for package in packages:
            if (not isinstance(package, dict) or not valid_identifier(package.get('id'))
                    or not isinstance(package.get('name'), str) or not isinstance(package.get('mount'), str)
                    or isinstance(package.get('size'), bool) or not isinstance(package.get('size'), int)
                    or package['size'] < 0):
                raise OfflineUpdateError(invalid)
        if not isinstance(result.get('warnings', []), list):
            raise OfflineUpdateError(invalid)
    if action == 'status':
        if result.get('phase') not in KNOWN_PHASES:
            raise OfflineUpdateError('Unsupported update status; update the screen and helper together')
        if result['phase'] != 'IDLE' and not valid_identifier(result.get('jobId')):
            raise OfflineUpdateError(invalid)
        progress = result.get('progress')
        if progress is not None and (isinstance(progress, bool) or not isinstance(progress, (int, float))
                                     or not math.isfinite(progress)):
            raise OfflineUpdateError(invalid)
        for key in ('canCancel', 'recoveryRequired', 'awaitingUiReady', 'recoveryArtifactsRetained', 'commitPending'):
            if key in result and not isinstance(result[key], bool):
                raise OfflineUpdateError(invalid)
        manifest = result.get('manifest')
        if manifest is not None:
            if not isinstance(manifest, dict):
                raise OfflineUpdateError(invalid)
            for key in ('components', 'firmware', 'configChanges'):
                if not isinstance(manifest.get(key, []), list) or not all(
                        isinstance(row, dict) for row in manifest.get(key, [])):
                    raise OfflineUpdateError(invalid)
            for row in manifest.get('components', []) + manifest.get('firmware', []):
                if 'currentVersionKnown' in row and not isinstance(row['currentVersionKnown'], bool):
                    raise OfflineUpdateError(invalid)
    return result


def needs_attention(status):
    return bool(status.get('jobId') and (
        status.get('recoveryRequired') or status.get('recoveryArtifactsRetained')
        or status.get('phase') not in TERMINAL_PHASES
    ))


def error_message(error):
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or 'Unknown error')
    return str(error or 'Unknown error')


class OfflineUpdateClient:
    def __init__(self, run=subprocess.run):
        self._run = run

    @staticmethod
    def helper_available():
        try:
            return Path(LAUNCHER).is_file()
        except OSError:
            return False

    def worker_active(self, job_id):
        """Read only this task's unit; no change to core services or frozen runners."""
        if not valid_identifier(job_id):
            raise OfflineUpdateError('Invalid update identifier')
        try:
            result = self._run(
                ['/usr/bin/systemctl', 'show', 'klipperscreen-update-' + job_id + '.service',
                 '--property=ActiveState', '--value'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        state = result.stdout.strip()
        if result.returncode == 0 and state in ('active', 'activating', 'reloading', 'deactivating'):
            return True
        if state in ('inactive', 'failed'):
            return False
        return None

    def ensure_runtime(self):
        """Synchronize the runtime bundled with this screen, without a shell."""
        argv = ['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', str(RUNTIME_SETUP), 'ensure']
        try:
            completed = self._run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', timeout=45, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OfflineUpdateError('RUNTIME_SETUP_TIMEOUT') from exc
        except OSError as exc:
            raise OfflineUpdateError('RUNTIME_SETUP_FAILED') from exc
        try:
            if len(completed.stdout) > 64 * 1024:
                raise ValueError('response too large')
            result = json.loads(completed.stdout)
            if (not isinstance(result, dict) or result.get('ok') is not True
                    or result.get('runtimeState') not in RUNTIME_STATES
                    or not isinstance(result.get('changed'), bool)
                    or not isinstance(result.get('runtimeVersion'), str)
                    or (result['runtimeVersion'] and re.fullmatch(r'[0-9a-f]{64}', result['runtimeVersion']) is None)
                    or not isinstance(result.get('runtimeReason'), str)
                    or (result['runtimeReason'] and result['runtimeReason'] not in RUNTIME_REASONS)
                    or (result['runtimeState'] != 'READY' and not result['runtimeReason'])
                    or (result['runtimeState'] == 'READY' and result['runtimeReason'])
                    or completed.returncode):
                raise ValueError('invalid runtime response')
        except (ValueError, TypeError):
            # Do not expose sudo/interpreter output or local configuration.
            raise OfflineUpdateError('RUNTIME_SETUP_FAILED') from None
        return result

    def request(self, action, identifier=None, pid=None):
        if action not in ('scan', 'prepare', 'start', 'status', 'cancel', 'recover', 'ui-ready'):
            raise OfflineUpdateError('Unsupported offline update action')
        if action in ('prepare', 'start', 'cancel', 'recover', 'ui-ready') and identifier is None:
            raise OfflineUpdateError('Missing update identifier')
        if action == 'scan' and identifier is not None:
            raise OfflineUpdateError('Scan does not accept an identifier')
        argv = ['/usr/bin/sudo', '-n', LAUNCHER, action]
        if action == 'cancel':
            argv = ['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', str(RUNTIME_SETUP), action]
        if identifier is not None:
            if not valid_identifier(identifier):
                raise OfflineUpdateError('Invalid update identifier')
            argv.append(identifier)
        if action == 'ui-ready':
            if isinstance(pid, bool) or not isinstance(pid, int) or not 2 <= pid <= 2147483647:
                raise OfflineUpdateError('Invalid screen process identifier')
            argv.append(str(pid))
        elif pid is not None:
            raise OfflineUpdateError('Unexpected screen process identifier')
        try:
            completed = self._run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                timeout=20, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OfflineUpdateError('Update service timed out; refresh the status before trying again') from exc
        except OSError as exc:
            raise OfflineUpdateError('Offline update support is not installed or cannot be started') from exc
        if completed.returncode and not completed.stdout.strip():
            # sudo/interpreter errors go to stderr.  Report a startup failure
            # without showing potentially sensitive host output in the panel.
            raise OfflineUpdateError(
                'Offline update service could not start; install the helper and check its authorization'
            )
        try:
            if len(completed.stdout) > 1024 * 1024:
                raise ValueError('response too large')
            result = json.loads(completed.stdout)
            if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                raise ValueError('invalid envelope')
        except (ValueError, TypeError) as exc:
            # stderr may contain sensitive host configuration; do not expose it.
            raise OfflineUpdateError(
                'Offline update service returned an invalid response; check its installation'
            ) from exc
        if not result['ok']:
            raise OfflineUpdateError(error_message(result.get('error')))
        if completed.returncode:
            raise OfflineUpdateError('Offline update service failed; refresh the status')
        return validate_response(action, result)
