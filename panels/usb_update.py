import copy
import logging
import os
import threading
import time

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk, Pango

from ks_includes.offline_update_client import (
    OfflineUpdateClient, TERMINAL_PHASES, error_message,
)
from ks_includes.offline_update_runtime import get_offline_update_runtime
from ks_includes.screen_panel import ScreenPanel


UI_ACK_ERRORS = frozenset((
    'UI_ACK_NOT_EXPECTED', 'UI_PID_OWNER_MISMATCH', 'UI_PROCESS_MISMATCH',
    'UI_PROCESS_UNCONFIRMED', 'INVALID_PID', 'ROOT_REQUIRED', 'UNTRUSTED_INSTALLATION',
    'INVALID_STATE', 'UPDATE_BUSY', 'COMMAND_FAILED',
))
UI_ACK_TRANSIENT = frozenset(('UI_ACK_REQUEST_FAILED', 'UPDATE_BUSY', 'COMMAND_FAILED', 'INVALID_STATE'))


def setup_message():
    return _('Preparation is not complete. Press Refresh status to retry.')


def localized_error(value):
    code = error_message(value)
    engine_errors = (
        (('SWUPDATE_VERSION_UNSUPPORTED', 'SWUPDATE_BINARY_MISMATCH', 'SWUPDATE_BUILD_MISMATCH',
          'SWUPDATE_BUILD_UNSAFE', 'SWUPDATE_UNTRUSTED_RUNTIME', 'SWUPDATE_CONFIG_MISMATCH',
          'SWUPDATE_ARCHITECTURE_MISMATCH', 'SWUPDATE_FEATURES_MISSING'),
         _('The SWUpdate engine is incompatible or has changed. Reinstall the verified engine.')),
        (('SWUPDATE_UNAVAILABLE', 'SWUPDATE_START_FAILED', 'SWUPDATE_PLATFORM_UNSUPPORTED'),
         _('SWUpdate could not be started. Check the engine installation.')),
        (('SWUPDATE_CHECK_FAILED', 'SWU_CPIO_FORMAT', 'SWU_CPIO_HEADER', 'SWU_CPIO_CHECKSUM',
          'SWU_HASH_MISMATCH', 'SWU_TRUNCATED', 'SWU_UNDECLARED_FILES', 'SWU_UNSAFE_MEMBER',
          'SWU_UNSAFE_NAME', 'SWU_SIGNATURE_SIZE'),
         _('SWUpdate rejected this package. Check its signature, script protocol and files.')),
        (('SWUPDATE_CHECK_TIMEOUT',),
         _('Package verification timed out. Check the storage device and retry.')),
        (('SWUPDATE_FAILED', 'SWUPDATE_EXECUTION_FAILED'),
         _('SWUpdate reported an installation failure. Review the recovery status.')),
        (('SWUPDATE_TIMEOUT',),
         _('The update timed out. Keep the printer out of service until recovery is complete.')),
        (('SWUPDATE_PROGRESS_PROTOCOL', 'SWUPDATE_PROGRESS_PEER', 'SWUPDATE_PROGRESS_SAVE_FAILED',
          'SWUPDATE_PROGRESS_INCOMPLETE', 'SWUPDATE_RESULT_UNCONFIRMED'),
         _('Update progress could not be confirmed. Review the recovery status before retrying.')),
        (('SWU_SCRIPT_API', 'SWU_METADATA_SCHEMA', 'SWU_HANDLER_CONTRACT', 'SWU_SELECTION_CONTRACT',
          'SWU_UNSUPPORTED_DESCRIPTION', 'SWU_HARDWARE', 'SWU_HARDWARE_MISMATCH', 'SWU_BOARD_PROFILES'),
         _('This package requires a different update format or script protocol.')),
        (('MANUAL_RECOVERY_REQUIRED',),
         _('This package requires manual recovery. Contact service support.')),
        (('PACKAGE_RESCUE_FAILED',),
         _('The recovery script failed. Service assistance is required.')),
        (('UNSUPPORTED_TASK_ENGINE',),
         _('This update task uses an unsupported engine. Contact service support.')),
        (('SWU_SCREEN_RESCUE_REQUIRED',),
         _('This screen update package has no valid recovery script.')),
    )
    for codes, message in engine_errors:
        if code in codes:
            return message
    messages = {
        'UPDATE_BUSY': _('Another update is running. Wait for it to finish.'),
        'CANCEL_NOT_ALLOWED': _('This update cannot be cancelled in its current state. Refresh the status.'),
        'CANCEL_COMMIT_PENDING': _('Installation has finished. Recover the update to finish saving its result.'),
        'PENDING_TASK_EXISTS': _('An unfinished update exists. Refresh the status to continue.'),
        'UNREGISTERED_BASELINE': _('The installed version must be registered before updating.'),
        'DOWNGRADE_BLOCKED': _('Installing an older version is not allowed.'),
        'RELEASE_IDENTITY_CHANGED': _('This package does not match the registered release.'),
        'PRINTER_NOT_IDLE': _('Finish printing, pausing or calibration before updating.'),
        'PRINTER_STATE_UNKNOWN': _('The printer state cannot be confirmed. Check the local connection.'),
        'WAIT_FOR_COOLING': _('Turn off heating and let the printer cool before updating.'),
        'INSUFFICIENT_SPACE': _('There is not enough internal storage for this update.'),
        'USB_REMOVED': _('The USB drive was removed before copying finished.'),
        'USB_PACKAGE_CHANGED': _('The USB package changed. Scan the drive again.'),
        'USB_COPY_INCOMPLETE': _('Copying from the USB drive did not finish.'),
        'PREPARATION_STALE': _('The printer changed after verification. Prepare the package again.'),
        'EXECUTOR_INTERRUPTED': _('The update process stopped. Review the recovery status.'),
        'SCREEN_NOT_READY': _('The updated screen did not start correctly.'),
        'RUNTIME_ASSETS_MISSING': _('This screen release is missing offline update resources.'),
        'RUNTIME_ASSETS_INVALID': _('The bundled offline update resources failed verification.'),
        'RELEASE_KEY_REQUIRED': _('This screen release is missing the update verification key.'),
        'PLATFORM_MISMATCH': _('This screen release has no update runtime for this CPU architecture.'),
        'UNTRUSTED_INSTALLATION': _('Offline update files or directories have unsafe ownership or permissions.'),
        'ROOT_REQUIRED': _('The screen user needs passwordless permission to prepare offline updates.'),
        'RUNTIME_SETUP_FAILED': _(
            'Offline update preparation could not finish. Check the screen installation and permissions.'),
        'RUNTIME_SETUP_TIMEOUT': _('Offline update preparation timed out. The screen will retry.'),
        'OFFLINE_UPDATE_NOT_CONFIGURED': setup_message(),
        'Update service timed out; refresh the status before trying again':
            _('Update service timed out; refresh the status before trying again'),
        'Offline update support is not installed or cannot be started':
            _('Offline update support is not installed or cannot be started'),
        'Offline update service could not start; install the helper and check its authorization':
            _('Offline update service could not start; install the helper and check its authorization'),
        'Offline update service returned an invalid response; check its installation':
            _('Offline update service returned an invalid response; check its installation'),
        'Offline update service failed; refresh the status':
            _('Offline update service failed; refresh the status'),
        'Unsupported update status; update the screen and helper together':
            _('Unsupported update status; update the screen and helper together'),
    }
    return messages.get(code, code)


ACTIVE_PHASES = frozenset(('IMPORTING', 'VERIFYING', 'INSTALLING', 'FLASHING', 'HEALTH_CHECK'))


def recovery_available(status):
    # New helpers distinguish durable commit recovery from package replay.
    # Old frozen helpers may omit these fields; preserve their replay behavior
    # unless an explicit mode or prior manual-recovery refusal rules it out.
    if 'canRecover' in status:
        return status['canRecover'] is True
    if status.get('commitRecovery') is True:
        return True
    mode = (status.get('manifest') or {}).get('recoveryMode')
    if mode:
        return mode == 'replay'
    return status.get('error') != 'MANUAL_RECOVERY_REQUIRED' and status.get('lastRecoveryError') != 'MANUAL_RECOVERY_REQUIRED'


def status_summary(status, runtime):
    """One operator-facing title and next step; diagnostic details live separately."""
    phase = status.get('phase', 'IDLE')
    if phase == 'PREPARED' and status.get('workerActive') is True:
        phase = 'VERIFYING'
    if status.get('recoveryRequired'):
        phase = 'RECOVERY_REQUIRED'
    if phase == 'CANCELLED' and status.get('recoveryArtifactsRetained'):
        return (_('Update cancelled'),
                _('Installed changes were not undone. Recovery files were kept. '
                  'Check the printer before using it.'), 'warning')
    if status.get('jobId') and phase != 'IDLE':
        if phase == 'RECOVERY_REQUIRED' and not recovery_available(status):
            return (_('Recovery required'), _('This package requires manual recovery. Contact service support.'), 'warning')
        summaries = {
            'IMPORTING': (_('Copying update package'),
                          _('Keep the USB drive connected until copying finishes.'), 'busy'),
            'VERIFYING': (_('Verifying update package'), _('Checking the signature and file integrity.'), 'busy'),
            'PREPARED': (_('Ready to install'),
                         _('Signature and files verified. System and board checks run after confirmation.'), 'neutral'),
            'INSTALLING': (_('Installing update'), _('Keep the printer powered on. The screen may restart.'), 'busy'),
            'FLASHING': (_('Updating firmware'),
                         _('Do not turn off the printer. Firmware will be verified after writing.'), 'busy'),
            'HEALTH_CHECK': (_('Verifying the result'),
                             _('Keep the printer powered on while the updated programs are checked.'), 'busy'),
            'COMPLETE': (_('Update complete'), _('The update has been verified. You can use the printer.'), 'success'),
            'CANCELLED': (_('Update cancelled'), _('Scan again to choose another update package.'), 'neutral'),
            'FAILED': (_('Update not completed'), _('Open Details to check the cause before trying again.'), 'error'),
            'RECOVERY_REQUIRED': (_('Recovery required'),
                                  _('Keep the printer powered on and press Recover update to continue.'), 'warning'),
        }
        return summaries.get(phase, (_('Reading update status'), _('Please wait for the current task status.'), 'busy'))
    state = runtime.get('runtimeState', 'CHECKING')
    if state == 'CHECKING':
        return (_('Preparing USB update'),
                _('The screen is preparing this feature automatically. Please wait.'), 'busy')
    if state == 'DEFERRED':
        return (_('Preparation on hold'),
                _('The screen will retry when the printer and existing update tasks are ready.'), 'warning')
    if state == 'BLOCKED' or status.get('setupRequired'):
        return (_('USB update unavailable'), setup_message(), 'warning')
    return (_('Update from a USB drive'), _('Insert a USB drive, then press Scan USB drive.'), 'neutral')


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title or _('USB Update'))
        self.client = OfflineUpdateClient()
        self.runtime = get_offline_update_runtime(screen, GLib)
        self.runtime_state = self.runtime.snapshot
        self._runtime_callback = None
        self._active = False
        self._generation = 0
        self._inflight = False
        self._timer = None
        self._dialog = None
        self._reset_ui_ack()
        self.status = {'phase': 'IDLE'}
        self.job_id = None
        self.packages = []
        self._notice = None
        self._warnings = []
        self._status_loaded = False
        self._status_refresh_pending = False
        self._runtime_epoch = 0
        self._presentation = None
        self._details_manifest = None
        self.buttons = {}
        self.content.get_style_context().add_class('usb-update')
        self.content.set_spacing(8)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for key, text, icon, callback in (
            ('scan', _('Scan USB drive'), 'usb-update', self.scan),
            ('refresh', _('Refresh status'), 'refresh', self.refresh),
        ):
            button = self._gtk.Button(icon, text, scale=self.bts * .55, position=Gtk.PositionType.LEFT, lines=1)
            button.get_style_context().add_class('usb-tool')
            if key == 'scan':
                button.get_style_context().add_class('usb-scan')
            button.set_hexpand(False)
            button.set_vexpand(False)
            button.connect('clicked', callback)
            self.buttons[key] = button
            if key == 'scan':
                toolbar.pack_start(button, False, False, 0)
            else:
                toolbar.pack_end(button, False, False, 0)
        self.content.pack_start(toolbar, False, False, 0)
        self.summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.summary.get_style_context().add_class('usb-summary')
        self.message = self._label('', 'usb-title')
        self.description = self._label('', 'usb-description')
        self.summary.pack_start(self.message, False, False, 0)
        self.summary.pack_start(self.description, False, False, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_no_show_all(True)
        self.progress.get_style_context().add_class('usb-progress')
        self.summary.pack_start(self.progress, False, False, 2)
        self.diagnostics = Gtk.Expander(label=_('Details'))
        self.diagnostics.set_no_show_all(True)
        self.diagnostics.get_style_context().add_class('usb-diagnostics')
        self.diagnostic_text = self._label('', 'usb-diagnostic-text')
        self.diagnostics.add(self.diagnostic_text)
        self.summary.pack_start(self.diagnostics, False, False, 0)
        scroll = self._gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.pack_start(self.summary, False, False, 0)
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.details.get_style_context().add_class('usb-contents')
        body.pack_start(self.details, False, False, 0)
        scroll.add(body)
        self.content.pack_start(scroll, True, True, 0)
        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.actions.set_no_show_all(True)
        for key, text, icon, callback in (
            ('start', _('Install update'), 'arrow-up', self.confirm_install),
            ('cancel', _('Cancel'), 'cancel', self.cancel),
            ('recover', _('Recover update'), 'refresh', self.confirm_recovery),
        ):
            button = self._gtk.Button(icon, text, scale=self.bts * .6, position=Gtk.PositionType.LEFT)
            button.get_style_context().add_class('usb-action')
            button.get_style_context().add_class('usb-secondary' if key == 'cancel' else 'usb-primary')
            button.set_vexpand(False)
            button.show_all()
            button.set_no_show_all(True)
            button.connect('clicked', callback)
            self.buttons[key] = button
            self.actions.pack_start(button, True, True, 0)
        self.content.pack_start(self.actions, False, False, 0)
        self._render_summary()
        self._update_buttons()

    @staticmethod
    def _label(text='', style=None, xalign=0, max_width_chars=1):
        label = Gtk.Label(label=text, wrap=True, xalign=xalign, max_width_chars=max_width_chars)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_hexpand(True)
        if style:
            label.get_style_context().add_class(style)
        label.show()
        return label

    def _render_summary(self):
        status = self.status
        if (not self._status_loaded and status.get('phase') in TERMINAL_PHASES
                and not status.get('recoveryRequired')):
            status = {'phase': 'IDLE'}  # A cached terminal task is not a fresh status reply.
        title, description, tone = status_summary(status, self.runtime_state)
        if not self._status_loaded and self._runtime_ready():
            title, description, tone = (_('Reading update status'),
                                        _('Please wait for the current task status.'), 'busy')
        if self._notice and (self.job_id or (self._runtime_ready() and not self.status.get('setupRequired'))):
            title, description, tone = self._notice[:3]
        presentation = (title, description, tone)
        if presentation != self._presentation:
            self._presentation = presentation
            logging.info('USB update view: runtime=%s phase=%s statusLoaded=%s tone=%s',
                         self.runtime_state.get('runtimeState'), self.status.get('phase'),
                         self._status_loaded, tone)
        self.message.set_text(title)
        self.description.set_text(description)
        context = self.summary.get_style_context()
        for name in ('neutral', 'busy', 'success', 'warning', 'error'):
            context.remove_class('usb-' + name)
        context.add_class('usb-' + tone)
        details = []
        error = status.get('error')
        failed = status.get('phase') in ('FAILED', 'RECOVERY_REQUIRED') or status.get('recoveryRequired')
        if failed and status.get('firstError'):
            error = status['firstError']
        if self._notice and len(self._notice) > 3:
            error = self._notice[3]
        if error:
            details.append(localized_error(error))
            details.append(_('Error code: %s') % error_message(error))
        recovery_error = status.get('lastRecoveryError')
        if failed and recovery_error and recovery_error != error:
            details.append(_('Recover update') + ': ' + localized_error(recovery_error))
            details.append(_('Error code: %s') % error_message(recovery_error))
        if self._ui_ack_error and self._ui_ack_state_job == self.job_id:
            details.append(localized_error('SCREEN_NOT_READY'))
            details.append(_('Error code: %s') % self._ui_ack_error)
        state, reason = self.runtime_state.get('runtimeState'), self.runtime_state.get('runtimeReason')
        if reason and (not self.job_id or state == 'BLOCKED'):
            details.append(localized_error(reason))
        if status.get('component'):
            details.append(_('Component: %s') % status['component'])
        if status.get('message'):
            details.append(str(status['message']))
        details.extend(localized_error(warning) for warning in self._warnings)
        self.diagnostic_text.set_text('\n'.join(dict.fromkeys(details)))
        self.diagnostics.set_visible(bool(details))
        if not details:
            self.diagnostics.set_expanded(False)
        self.progress.set_visible(bool(self.job_id and 'progress' in self.status
                                       and self.status.get('phase') in ACTIVE_PHASES
                                       and not self.status.get('recoveryRequired')))

    def _show_notice(self, title, description, tone='neutral', error=None):
        self._notice = (title, description, tone, error) if error else (title, description, tone)
        self._render_summary()

    def activate(self):
        if self._timer is not None:
            GLib.source_remove(self._timer)
        self._active = True
        self._generation += 1
        self._inflight = False
        self._status_loaded = False
        self._status_refresh_pending = False
        self._notice = None
        self._warnings = []
        self.runtime_state = self.runtime.snapshot
        self._clear_details()
        self._render_summary()
        self._update_buttons()
        if self._runtime_callback is not None:
            self.runtime.unsubscribe(self._runtime_callback)
        generation = self._generation
        self._runtime_callback = lambda state: self._runtime_changed(generation, state)
        self.runtime.subscribe(self._runtime_callback)
        self.runtime.ensure()
        self._timer = GLib.timeout_add_seconds(2, self._poll)

    def deactivate(self):
        self._active = False
        self._generation += 1
        if self._runtime_callback is not None:
            self.runtime.unsubscribe(self._runtime_callback)
            self._runtime_callback = None
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._dialog is not None:
            self._gtk.remove_dialog(self._dialog)
            self._dialog = None

    def _runtime_ready(self):
        return self.runtime_state.get('runtimeState') == 'READY'

    def _worker_busy(self):
        # A failed probe remains pending; do not enable a duplicate execution.
        return 'workerActive' in self.status and self.status['workerActive'] is not False

    def _can_start_prepared(self):
        if (self._inflight or not self._status_loaded or not self.job_id
                or self.job_id != self.status.get('jobId') or self.status.get('phase') != 'PREPARED'
                or self.status.get('recoveryRequired') or self.status.get('setupRequired') is not False
                or self._worker_busy() or not self.runtime.helper_available()):
            return False
        return self._runtime_ready() or (
            self.runtime_state.get('runtimeState') == 'DEFERRED'
            and self.runtime_state.get('runtimeReason') == 'PENDING_TASK_EXISTS')

    def _can_cancel(self):
        if self._inflight or not self._status_loaded or not self.job_id or self.status.get('commitPending'):
            return False
        phase = self.status.get('phase')
        # Older frozen helpers report canCancel=False for failed tasks. The
        # current bootstrap handles cancellation without replacing that helper.
        if phase in ('FAILED', 'RECOVERY_REQUIRED'):
            return not self._worker_busy()
        return bool(self.status.get('canCancel') and phase in ('IMPORTING', 'VERIFYING', 'PREPARED')
                    and (phase != 'PREPARED' or not self._worker_busy()))

    def _can_recover(self):
        return bool(not self._inflight and self._status_loaded and self.job_id and not self._worker_busy()
                    and self.runtime.helper_available() and recovery_available(self.status)
                    and (self.status.get('phase') == 'RECOVERY_REQUIRED' or self.status.get('recoveryRequired')))

    def _runtime_changed(self, generation, state):
        if not self._active or generation != self._generation:
            return
        changed = state != self.runtime_state
        if changed:
            self._runtime_epoch += 1
            if state.get('runtimeState') == 'READY':
                self._status_loaded = False
                self._notice = None
        self.runtime_state = dict(state)
        self._render_summary()
        self._update_buttons()
        # Existing jobs remain readable/recoverable even if setup is deferred.
        if changed or (not self._status_loaded and not self._inflight):
            self._queue_status()

    def _queue_status(self):
        if not self._active or not self.runtime.helper_available():
            return
        if self._inflight or (self.runtime_state.get('runtimeState') == 'CHECKING' and not self._has_pending_job()):
            self._status_refresh_pending = True
            return
        self._status_refresh_pending = False
        self._request('status', self.job_id if self._status_loaded else None, self._show_status)

    def _request(self, action, identifier, callback, pid=None):
        if not self._active or self._inflight:
            return
        if action in ('prepare', 'start', 'recover', 'cancel') and not self._status_loaded:
            self._render_summary()
            return
        if (action == 'prepare' and (not self._runtime_ready() or self.status.get('setupRequired'))
                or action == 'start' and not self._can_start_prepared()):
            self._render_summary()
            return
        if not self.runtime.helper_available():
            return
        self._inflight = True
        self._update_buttons()
        generation = self._generation
        context = (action, self._runtime_epoch, identifier)

        def worker():
            try:
                if pid is None:
                    result = self.client.request(action, identifier)
                else:
                    result = self.client.request(action, identifier, pid)
                if (action == 'status' and result.get('jobId')
                        and result.get('phase') in ('PREPARED', 'RECOVERY_REQUIRED', 'FAILED')):
                    job_id = result['jobId']
                    active = self.client.worker_active(job_id)
                    # A worker can finish between the first reply and the unit
                    # probe. Read after observing exit so its final state wins.
                    if active is False:
                        result = self.client.request('status', job_id)
                    result = dict(result)
                    result['workerActive'] = active
                failure = None
            except Exception as exc:
                result, failure = None, str(exc)
                logging.warning('USB update %s request failed: %s', action, type(exc).__name__)
            GLib.idle_add(self._response, generation, callback, result, failure, context)

        threading.Thread(target=worker, daemon=True).start()

    def _response(self, generation, callback, result, failure, context=None):
        if not self._active or generation != self._generation:
            return False
        self._inflight = False
        failed = bool(failure)
        stale = bool(context and context[0] in ('status', 'scan') and context[1] != self._runtime_epoch)
        ui_ack = bool(context and context[0] == 'ui-ready')
        stale_ack = bool(ui_ack and (
            len(context) > 2 and context[2] != self.job_id
            or self.status.get('phase') != 'HEALTH_CHECK' or not self.status.get('awaitingUiReady')))
        if stale:
            logging.debug('Discarding stale USB %s response', context[0])
            self._status_refresh_pending = True
        elif stale_ack:
            pass
        elif failure and ui_ack:
            self._ui_ack_failed(failure)
        elif failure:
            if self.job_id or self._runtime_ready():
                self._show_notice(_('Could not read update status'), localized_error(failure), 'warning', failure)
            self._status_refresh_pending = not self._status_loaded
        elif not failure:
            try:
                callback(result)
            except Exception:
                failed = True
                logging.exception('Unable to display offline update response')
                if ui_ack:
                    self._ui_ack_failed('UI_ACK_REQUEST_FAILED')
                else:
                    self._show_notice(_('Could not read update status'), _('Press Refresh status to retry.'), 'warning')
                    self._status_refresh_pending = not self._status_loaded
        self._update_buttons()
        # Runtime completion may arrive during a request. Fetch a fresh reply
        # after releasing the single-flight slot instead of accepting old setup flags.
        if self._status_refresh_pending and not failed and not self._inflight:
            self._queue_status()
        return False

    def _poll(self):
        if not self._active:
            return False
        if self._status_refresh_pending:
            self._queue_status()
        elif self.job_id and (self.status.get('phase') in ACTIVE_PHASES or self._worker_busy()):
            self.refresh()
        return True

    def refresh(self, widget=None):
        # Only a user click bypasses the retry delay.  Job polling must not
        # spawn another root setup process every two seconds.
        if widget is not None:
            self._reset_ui_ack(self.job_id)
        self.runtime.ensure(force=widget is not None)
        self._queue_status()

    def scan(self, widget=None):
        if self._has_pending_job() or not self.runtime.helper_available():
            return
        self._show_notice(_('Scanning USB drives'), _('Looking for .mdu files on connected USB drives.'), 'busy')
        self._request('scan', None, self._show_packages)

    def _clear_details(self):
        for child in self.details.get_children():
            self.details.remove(child)
        self._details_manifest = None

    def _show_packages(self, result):
        self._clear_details()
        self.packages = result.get('packages') or []
        self._warnings = result.get('warnings') or []
        self.job_id = None
        self.status = {'phase': 'IDLE', **self._setup_fields(result)}
        self.progress.set_fraction(0)
        self.progress.set_text('')
        if self.packages:
            self._show_notice(_('Choose an update package'),
                              _('Select a file to copy it to the printer and verify it.'))
        else:
            self._show_notice(_('No update package found'),
                              _('Place a .mdu file in the USB root or MINGDA-Update folder, then scan again.'))
        for package in self.packages:
            try:
                size = '{:.1f} MiB'.format(float(package.get('size', 0)) / (1024 * 1024))
            except (ValueError, TypeError):
                size = ''
            button = Gtk.Button()
            button.set_hexpand(True)
            button.get_style_context().add_class('usb-package')
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.pack_start(self._gtk.Image('file', int(self._gtk.img_scale), int(self._gtk.img_scale)), False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            text.pack_start(self._label(package.get('name', ''), 'usb-package-name'), False, False, 0)
            text.pack_start(self._label('{}  ·  {}'.format(package.get('mount', ''), size), 'usb-muted'),
                            False, False, 0)
            row.pack_start(text, True, True, 0)
            button.add(row)
            button.connect('clicked', self.prepare, package.get('id'))
            self.details.pack_start(button, False, False, 0)
        self.details.show_all()

    def _reset_ui_ack(self, job_id=None):
        self._ui_ack_job = None
        self._ui_ack_state_job = job_id
        self._ui_ack_error = None
        self._ui_ack_attempts = 0
        self._ui_ack_retry_at = 0

    def _ui_ack_failed(self, failure):
        code = error_message(failure)
        if code not in UI_ACK_ERRORS:
            code = 'UI_ACK_REQUEST_FAILED'
        self._ui_ack_state_job = self.job_id
        self._ui_ack_error = code
        self._ui_ack_retry_at = (time.monotonic() + 5 * self._ui_ack_attempts
                                 if code in UI_ACK_TRANSIENT and self._ui_ack_attempts < 3 else float('inf'))
        logging.warning('USB update display handshake failed: job=%s code=%s attempt=%d',
                        self.job_id, code, self._ui_ack_attempts)
        self._render_summary()

    def _ack_ui_ready(self, generation, job_id):
        if (not self._active or generation != self._generation or job_id != self.job_id
                or self._ui_ack_job == job_id or self.status.get('phase') != 'HEALTH_CHECK'
                or self.status.get('component') != 'klipperscreen'
                or not self.status.get('awaitingUiReady') or self._inflight):
            return False
        if self._ui_ack_state_job != job_id:
            self._reset_ui_ack(job_id)
        if self._ui_ack_attempts >= 3 or time.monotonic() < self._ui_ack_retry_at:
            return False
        if self._screen.get_realized() and self._screen.get_mapped() and self._screen.get_window() is not None:
            self._ui_ack_attempts += 1
            self._request('ui-ready', job_id, self._ui_ready_accepted, os.getpid())
        return False

    def _ui_ready_accepted(self, result):
        self._ui_ack_job = self.job_id
        self._ui_ack_error = None
        self._render_summary()

    def prepare(self, widget, package_id):
        if self._has_pending_job() or not self._runtime_ready() or self.status.get('setupRequired'):
            return
        self._request('prepare', package_id, self._job_started)

    def _job_started(self, result):
        same_job = result['jobId'] == self.job_id
        self.job_id = result['jobId']
        self._reset_ui_ack(self.job_id)
        if same_job:
            self.status = {**self.status, 'workerActive': True, 'canCancel': False}
        else:
            self.status = {'jobId': self.job_id, 'phase': 'IMPORTING', 'canCancel': False}
            self.progress.set_fraction(0)
            self.progress.set_text('')
        self._status_loaded = False
        self._warnings = []
        self._show_notice(_('Reading update status'), _('Please wait for the current task status.'), 'busy')
        if not same_job:
            self._clear_details()
        self.refresh()

    def _show_status(self, status):
        if (status.get('jobId') != self.job_id or self.status.get('phase') == 'HEALTH_CHECK'
                and status.get('phase') != 'HEALTH_CHECK'):
            self._reset_ui_ack(status.get('jobId'))
        self._status_loaded = True
        self.status = {**status, **self._setup_fields(status)}
        self.job_id = status.get('jobId')
        phase = status.get('phase', 'IDLE')
        self._notice = None
        self._warnings = []
        self._render_summary()
        try:
            progress = max(0, min(100, float(status.get('progress') or 0)))
        except (ValueError, TypeError):
            progress = 0
        self.progress.set_fraction(progress / 100)
        self.progress.set_text('{:.0f}%'.format(progress) if self.job_id else '')
        manifest = status.get('manifest') or {}
        if manifest != self._details_manifest:
            self._render_manifest(manifest)
        if (phase == 'HEALTH_CHECK' and status.get('component') == 'klipperscreen'
                and status.get('awaitingUiReady') and self._ui_ack_job != self.job_id):
            # An unchanged health reply must still retry the visible-window ACK.
            GLib.idle_add(self._ack_ui_ready, self._generation, self.job_id)

    def _render_manifest(self, manifest):
        self._clear_details()
        if manifest:
            release = _('Release: %s') % (manifest.get('version') or manifest.get('releaseId', ''))
            self.details.pack_start(self._label(release, 'usb-release-title'), False, False, 0)
            for group, heading in (('components', _('Programs')), ('firmware', _('Firmware'))):
                rows = manifest.get(group) or []
                if rows:
                    self.details.pack_start(self._label(heading, 'usb-section-title'), False, False, 0)
                    for row in rows:
                        current = row.get('currentVersion')
                        known = (row.get('currentVersionKnown') is not False
                                 and isinstance(current, str) and bool(current.strip())
                                 and current.strip().lower() not in ('unknown', '?', 'n/a', '未知'))
                        current = current if known else _('Unknown')
                        version = '{} → {}'.format(current, row.get('version', ''))
                        if not known:
                            # Isolate translated RTL text; GTK can recreate its
                            # Pango layout after wrapping or resizing the label.
                            version = '\u200e\u2068{}\u2069 → {}'.format(current, row.get('version', ''))
                        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                        item.get_style_context().add_class('usb-component-row')
                        name = row.get('id', '')
                        name = {'klipperscreen': 'KlipperScreen', 'klipper': 'Klipper',
                                'moonraker': 'Moonraker'}.get(name, name)
                        item.pack_start(self._label(str(name), 'usb-component-name'), True, True, 0)
                        version_label = self._label(version, 'usb-version', 1, 28)
                        # Keep old -> new readable in right-to-left languages.
                        version_label.set_direction(Gtk.TextDirection.LTR)
                        item.pack_end(version_label, False, False, 0)
                        self.details.pack_start(item, False, False, 0)
            changes = manifest.get('configChanges') or []
            if changes:
                self.details.pack_start(self._label(_('Configuration changes'), 'usb-section-title'), False, False, 0)
                for change in changes:
                    if isinstance(change, dict):
                        # Show only human-readable descriptions or names, never config values.
                        change = (change.get('description') or change.get('path')
                                  or change.get('id') or _('Configuration changes'))
                    self.details.pack_start(self._label(str(change), 'usb-muted'), False, False, 0)
        self.details.show_all()
        self._details_manifest = copy.deepcopy(manifest)

    def _has_pending_job(self):
        return bool(self.job_id and (
            self.status.get('recoveryRequired') or self.status.get('phase') not in TERMINAL_PHASES
        ))

    def _setup_fields(self, result):
        # Status and USB scans carry the same deployment state.  Keep the last
        # known state if talking to an older helper that omits these fields.
        return {key: result.get(key, self.status.get(key))
                for key in ('setupRequired', 'setupMissing') if key in result or key in self.status}

    def _update_buttons(self):
        ready = not self._inflight and self._status_loaded
        configured = self._runtime_ready() and not self.status.get('setupRequired')
        helper = self.runtime.helper_available()
        phase = self.status.get('phase')
        self.buttons['scan'].set_sensitive(ready and helper and not self._has_pending_job())
        self.buttons['refresh'].set_sensitive(not self._inflight)
        self.buttons['start'].set_sensitive(self._can_start_prepared())
        self.buttons['cancel'].set_sensitive(helper and self._can_cancel())
        self.buttons['recover'].set_sensitive(self._can_recover())
        visible = {
            'start': phase == 'PREPARED' and not self.status.get('recoveryRequired'),
            'cancel': bool(self.job_id and (self.status.get('canCancel') or phase in ('FAILED', 'RECOVERY_REQUIRED'))),
            'recover': bool(self.job_id and recovery_available(self.status)
                            and (phase == 'RECOVERY_REQUIRED' or self.status.get('recoveryRequired'))),
        }
        for name, show in visible.items():
            self.buttons[name].set_visible(show)
        self.actions.set_visible(any(visible.values()))
        for child in self.details.get_children():
            if isinstance(child, Gtk.Button):
                child.set_sensitive(ready and configured and not self._has_pending_job())

    def confirm_install(self, widget=None):
        if not self._can_start_prepared():
            return
        self._confirm('start', _(
            'Install this update? Programs and firmware listed in this package will be updated. '
            'Keep the printer powered on.'
        ))

    def confirm_recovery(self, widget=None):
        if not self._can_recover():
            return
        self._confirm('recover', _(
            'Recover the interrupted update? Keep the printer powered on until recovery finishes.'
        ))

    def _confirm(self, action, message):
        if self._dialog is not None:
            return
        content = Gtk.Label(
            label=message, wrap=True, max_width_chars=1, hexpand=True, vexpand=True,
            justify=Gtk.Justification.CENTER,
        )
        buttons = [
            {'name': _('Continue'), 'response': Gtk.ResponseType.OK},
            {'name': _('Cancel'), 'response': Gtk.ResponseType.CANCEL},
        ]
        self._dialog = self._gtk.Dialog(_('USB Update'), buttons, content, self._confirmed, action, self.job_id)
        self._dialog.connect('destroy', self._dialog_destroyed)

    def _dialog_destroyed(self, dialog):
        # The administrative lock can destroy dialogs without a response.
        # An older dialog may also finish destruction after a new one opens.
        if self._dialog is dialog:
            self._dialog = None

    def _confirmed(self, dialog, response, action, job_id):
        self._gtk.remove_dialog(dialog)
        self._dialog = None
        if response != Gtk.ResponseType.OK or job_id != self.job_id or not self._active:
            return
        if action == 'start' and not self._can_start_prepared():
            return
        if action == 'recover' and not self._can_recover():
            return
        if action == 'cancel' and not self._can_cancel():
            return
        self._request(action, job_id, self._cancelled if action == 'cancel' else self._job_started)

    def _cancelled(self, result):
        self.runtime.ensure(force=True)
        self.refresh()

    def cancel(self, widget=None):
        if not self._can_cancel():
            return
        if self.status.get('phase') in ('FAILED', 'RECOVERY_REQUIRED'):
            self._confirm('cancel', _(
                'Cancel this failed update? This ends the task without undoing installed changes. '
                'Recovery files will be kept. Check the printer before using it.'))
        else:
            self._request('cancel', self.job_id, self._cancelled)
