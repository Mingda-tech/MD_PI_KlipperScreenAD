# -*- coding: utf-8 -*-
"""AI Platform V3 status and policy client.

KlipperScreen presents Agent state and edits the Agent-owned policy. It does
not evaluate confidence, aggregate detections, or decide printer actions.
"""

import logging
import threading
import uuid

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango

from ks_includes.screen_panel import ScreenPanel


PROTOCOL_VERSION = "AI_PLATFORM_V3"
POLICY_SCHEMA_VERSION = 3
SENSITIVITIES = ("LOW", "MEDIUM", "HIGH")
ACTIONS = ("NOTIFY_ONLY", "AUTO_PAUSE")
INHERIT = "INHERIT"
DEFECT_TYPES = (
    "SPAGHETTI",
    "NOZZLE_BLOB",
    "FIRST_LAYER_EXTRUSION",
    "WARP_OR_DETACHMENT",
    "FOREIGN_OBJECT",
)
DEFECT_NAMES = {
    "SPAGHETTI": "Spaghetti",
    "NOZZLE_BLOB": "Nozzle Blob",
    "FIRST_LAYER_EXTRUSION": "First Layer Extrusion",
    "WARP_OR_DETACHMENT": "Warp or Detachment",
    "FOREIGN_OBJECT": "Foreign Object",
}
REASON_NAMES = {
    "DISABLED_BY_USER": "Disabled by user",
    "UNSUPPORTED_MODEL": "Unsupported printer model",
    "AGENT_OFFLINE": "AI Agent unavailable",
    "NPU_OFFLINE": "NPU unavailable",
    "NO_CAMERA": "No camera",
    "CAMERA_UNAVAILABLE": "Camera unavailable",
    "NOT_PRINTING": "Printer is not printing",
    "DETECTION_PENDING": "Waiting for the first AI detection",
    "DETECTION_STALE": "AI detection results are delayed",
    "CAPTURE_OR_INFERENCE_FAILED": "Camera capture or AI inference failed",
    "STALE_FRAME": "Camera snapshot is stale",
    "PRINT_SESSION_UNAVAILABLE": "Print session is unavailable",
    "PRINT_STATE_UNAVAILABLE": "Print state is unavailable",
    "MODEL_VERSION_MISMATCH": "AI model version mismatch",
    "PRINT_MUTED": "Muted for this print",
    "NO_ENABLED_DEFECTS": "All detection types are disabled",
    "NO_VALID_POLICY": "No valid AI policy",
    "POLICY_NOT_READY": "No valid AI policy",
    "POLICY_SIGNATURE_INVALID": "Policy verification failed",
}
SYNC_NAMES = {
    "SYNCED": "Synced",
    "APPLIED": "Applied",
    "LOCAL_APPLIED": "Applied locally",
    "LOCAL_READY": "Applied locally",
    "CACHE_LOADED": "Applied locally",
    "LOCAL_APPLIED_PENDING": "Applied locally, waiting for cloud sync",
    "LOCAL_PENDING_SYNC": "Applied locally, waiting for cloud sync",
    "PENDING_LOCAL": "Applying on this printer",
    "PENDING_CLOUD": "Waiting for cloud sync",
    "WAITING_FOR_CLOUD_SYNC": "Applied locally, waiting for cloud sync",
    "SYNCING": "Syncing with cloud",
    "REBASING": "Applied locally, merging cloud settings",
    "CONFLICT": "Applied locally, cloud settings conflict",
    "FAILED": "Applied locally, cloud sync failed",
    "ERROR": "Applied locally, cloud sync failed",
    "FAILED_RETRYABLE": "Applied locally, cloud sync failed",
    "FAILED_PERMANENT": "Applied locally, cloud sync failed",
}
SYNC_ONLY_REASONS = {
    "CLOUD_OFFLINE", "CLOUD_SYNC_FAILED", "CLOUD_SYNC_PENDING",
    "CLOUD_PROFILE_UNAVAILABLE", "CLOUD_UNAVAILABLE", "LOCAL_PENDING_SYNC",
    "PACKAGE_UPGRADE_REQUIRED", "PENDING_CLOUD",
    "POLICY_PENDING_SYNC", "POLICY_SYNC_FAILED", "POLICY_SYNC_PENDING",
    "SYNC_CONFLICT", "SYNC_FAILED", "SYNC_FAILED_USING_LOCAL",
    "WAITING_FOR_CLOUD_SYNC",
}
POLICY_TRUST_LOADING = {
    "BOOTSTRAPPING", "INITIALIZING", "LOADING", "LOADING_CACHE",
}
POLICY_TRUST_UNAVAILABLE = {
    "CACHE_CORRUPT", "INVALID", "NO_VALID_POLICY", "POLICY_NOT_READY",
    "SIGNATURE_INVALID", "UNAVAILABLE",
}
POLICY_TRUST_DEGRADED = {
    "CACHE_CORRUPT", "DEGRADED", "SAFE_DEGRADED", "SIGNATURE_INVALID",
    "UNAVAILABLE", "VALID_DEGRADED",
}
SYNC_PENDING_STATES = {
    "CLOUD_OFFLINE", "CLOUD_SYNC_PENDING", "LOCAL_APPLIED_PENDING",
    "LOCAL_ONLY", "LOCAL_PENDING_SYNC", "PENDING", "PENDING_CLOUD",
    "QUEUED", "RETRYING", "WAITING_FOR_CLOUD_SYNC",
}
SYNC_FAILED_STATES = {
    "CLOUD_PROFILE_UNAVAILABLE", "CLOUD_SYNC_FAILED", "ERROR", "FAILED",
    "FAILED_PERMANENT", "FAILED_RETRYABLE", "PACKAGE_UPGRADE_REQUIRED",
    "SYNC_FAILED", "SYNC_FAILED_USING_LOCAL",
}


def _translation_strings():
    """Keep dynamic strings discoverable by the gettext extractor."""
    _("AI Detection")
    _("Checking...")
    _("Offline")
    _("Watching")
    _("Not Watching")
    _("Settings")
    _("Default Settings")
    _("Enabled")
    _("Sensitivity")
    _("Low")
    _("Medium")
    _("High")
    _("Action")
    _("Notify Only")
    _("Auto Pause")
    _("Follow Default")
    _("Detection Types")
    _("Configure")
    _("No detection types are available for this printer")
    _("Safe mode")
    _("Saved")
    _("Error")
    _("Applied")
    _("Synced")
    _("Applied locally")
    _("Applied locally, waiting for cloud sync")
    _("Applying on this printer")
    _("Waiting for cloud sync")
    _("Syncing with cloud")
    _("Applied locally, merging cloud settings")
    _("Applied locally, cloud settings conflict")
    _("Applied locally, cloud sync failed")
    _("Sync failed")
    _("Unsaved changes")
    _("Reloading current settings")
    _("Settings could not be applied")
    _("Settings unavailable")
    _("Policy changed elsewhere. Latest settings will be reloaded.")
    _("Disabled by user")
    _("Unsupported printer model")
    _("AI Agent unavailable")
    _("NPU unavailable")
    _("No camera")
    _("Camera unavailable")
    _("Printer is not printing")
    _("Waiting for the first AI detection")
    _("AI detection results are delayed")
    _("Camera capture or AI inference failed")
    _("Camera snapshot is stale")
    _("Print session is unavailable")
    _("Print state is unavailable")
    _("AI model version mismatch")
    _("Muted for this print")
    _("All detection types are disabled")
    _("No valid AI policy")
    _("AI configuration is being prepared")
    _("Policy verification failed")
    _("Spaghetti")
    _("Nozzle Blob")
    _("First Layer Extrusion")
    _("Warp or Detachment")
    _("Foreign Object")
    _("Off")
    _("For the pre-print foreign-object check, Critical blocks printing only when Auto Pause is selected.")
    _("Safe mode is active. The pre-print check will notify only and will not block printing.")
    _("AI Platform V3 is required")
    _("Close")


def _unwrap_response(response, expected_key=None):
    """Unwrap one response without mistaking nested status policy for status."""
    if not isinstance(response, dict):
        return None
    result = response.get("result", response.get("data", response))
    if not isinstance(result, dict):
        return None
    if expected_key and isinstance(result.get(expected_key), dict):
        return result[expected_key]
    return result


def _policy_version(policy):
    try:
        return max(0, int((policy or {}).get("policyVersion") or 0))
    except (TypeError, ValueError):
        return 0


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.menu = ["main_menu"]
        self._active = False
        self._refreshing = False
        self._refresh_pending = False
        self._updating_controls = False
        self._saving = False
        self._save_failed = False
        self._conflict_reloading = False
        self._dirty = False
        self._policy_loaded = False
        self._refresh_completed = False
        self._protocol_ready = False
        self._status_request_ok = False
        self._policy_request_ok = False
        self._policy_compatible = False
        self._policy_generation = 0
        self._pending_request_id = None
        self._baseline_settings = None
        self._editing_defect = None
        self._icon_cache = {}
        self.status_timeout = None
        self.status = {}
        self.policy = {}
        self.supported_defects = set()
        self.defect_drafts = {
            defect_type: {"sensitivity": INHERIT, "actionMode": INHERIT}
            for defect_type in DEFECT_TYPES
        }

        self.labels["main_menu"] = self._build_main_page()
        self._build_defect_popover()
        self._update_sync_summary()
        self._update_control_sensitivity()
        self._set_icon("status_icon", "info", 1.35)
        self.content.add(self.labels["main_menu"])

    @staticmethod
    def _defect_key(defect_type):
        return defect_type.lower()

    def _build_main_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.get_style_context().add_class("ai-page")
        page.pack_start(self._build_status_bar(), False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.get_style_context().add_class("ai-body")
        defaults = self._build_defaults_panel()
        defects = self._build_defects_panel()
        self.labels["defaults_panel"] = defaults
        self.labels["defects_panel"] = defects
        body.pack_start(defaults, False, False, 0)
        body.pack_start(defects, False, False, 0)

        scroll = self._gtk.ScrolledWindow()
        scroll.add(body)
        page.pack_start(scroll, True, True, 0)
        page.pack_end(self._build_save_bar(), False, False, 0)
        return page

    def _build_status_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.get_style_context().add_class("ai-status-bar")
        bar.get_style_context().add_class("ai-status-loading")
        self.labels["status_bar"] = bar

        icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        icon_box.set_halign(Gtk.Align.CENTER)
        icon_box.set_valign(Gtk.Align.CENTER)
        icon_box.get_style_context().add_class("ai-status-icon")
        self.labels["status_icon"] = Gtk.Image()
        icon_box.pack_start(self.labels["status_icon"], True, True, 0)
        bar.pack_start(icon_box, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_valign(Gtk.Align.CENTER)
        self.labels["status"] = Gtk.Label(label=_("Checking..."))
        self.labels["status"].set_halign(Gtk.Align.START)
        self.labels["status"].set_xalign(0)
        self.labels["status_detail"] = Gtk.Label()
        self.labels["status_detail"].set_halign(Gtk.Align.START)
        self.labels["status_detail"].set_xalign(0)
        self.labels["status_detail"].set_line_wrap(True)
        self.labels["status_detail"].set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.labels["status_detail"].set_ellipsize(Pango.EllipsizeMode.END)
        self.labels["status_detail"].set_single_line_mode(True)
        self.labels["status_detail"].set_no_show_all(True)
        self.labels["status_detail"].get_style_context().add_class("ai-muted")
        text.pack_start(self.labels["status"], False, False, 0)
        text.pack_start(self.labels["status_detail"], False, False, 0)
        bar.pack_start(text, True, True, 0)

        master = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        master.set_valign(Gtk.Align.CENTER)
        master.get_style_context().add_class("ai-master-control")
        enabled_label = Gtk.Label()
        enabled_label.set_markup("<b>{}</b>".format(GLib.markup_escape_text(_("Enabled"))))
        self.labels["enabled"] = self._switch(_("Enabled"))
        self.labels["enabled"].connect("notify::active", self._on_enabled_changed)
        master.pack_start(enabled_label, False, False, 0)
        master.pack_start(self.labels["enabled"], False, False, 0)
        bar.pack_end(master, False, False, 0)
        return bar

    def _build_defaults_panel(self):
        vertical = self._screen.vertical_mode
        panel = Gtk.Box(
            orientation=(Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL),
            spacing=8,
        )
        panel.get_style_context().add_class("ai-settings-panel")
        panel.get_style_context().add_class("ai-card")
        panel.get_style_context().add_class("ai-defaults-card")
        panel.set_valign(Gtk.Align.START)
        self.labels["sensitivity"] = self._combo(
            _("Sensitivity"),
            (("LOW", _("Low")), ("MEDIUM", _("Medium")), ("HIGH", _("High"))),
            self._on_global_combo_changed,
        )
        sensitivity_row = self._setting_row(_("Sensitivity"), self.labels["sensitivity"])
        sensitivity_row.get_style_context().add_class("ai-global-setting")
        panel.pack_start(sensitivity_row, True, True, 0)
        self.labels["action"] = self._combo(
            _("Action"),
            (("NOTIFY_ONLY", _("Notify Only")), ("AUTO_PAUSE", _("Auto Pause"))),
            self._on_global_combo_changed,
        )
        action_row = self._setting_row(_("Action"), self.labels["action"])
        action_row.get_style_context().add_class("ai-global-setting")
        panel.pack_start(action_row, True, True, 0)
        return panel

    def _build_defects_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        panel.get_style_context().add_class("ai-settings-panel")
        panel.get_style_context().add_class("ai-card")
        panel.get_style_context().add_class("ai-defects-card")
        panel.set_valign(Gtk.Align.START)
        panel.pack_start(self._section_title(_("Detection Types")), False, False, 2)
        state = Gtk.Label(label=_("Checking..."))
        state.set_halign(Gtk.Align.START)
        state.set_xalign(0)
        state.set_line_wrap(True)
        state.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        state.set_no_show_all(True)
        state.show()
        self.labels["defects_state"] = state
        panel.pack_start(state, False, False, 8)
        for defect_type in DEFECT_TYPES:
            panel.pack_start(self._build_defect_row(defect_type), False, False, 0)
        return panel

    def _build_defect_row(self, defect_type):
        key = self._defect_key(defect_type)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("ai-compact-row")
        row.get_style_context().add_class("ai-defect-row")
        row.set_size_request(-1, max(48, round(self._gtk.font_size * 2.35)))
        self.labels["defect_{}_row".format(key)] = row
        name = Gtk.Label()
        name.set_markup("<b>{}</b>".format(GLib.markup_escape_text(_(DEFECT_NAMES[defect_type]))))
        name.set_halign(Gtk.Align.START)
        name.set_xalign(0)
        name.set_hexpand(True)
        summary = Gtk.Label()
        summary.set_halign(Gtk.Align.START)
        summary.set_xalign(0)
        summary.set_ellipsize(Pango.EllipsizeMode.END)
        summary.set_single_line_mode(True)
        summary.set_no_show_all(True)
        summary.get_style_context().add_class("ai-muted")
        self.labels["defect_{}_summary".format(key)] = summary
        if self._screen.vertical_mode:
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            text.set_valign(Gtk.Align.CENTER)
            text.pack_start(name, False, False, 0)
            text.pack_start(summary, False, False, 0)
            row.pack_start(text, True, True, 4)
        else:
            summary.set_size_request(max(210, round(self._gtk.font_size * 9.5)), -1)
            row.pack_start(name, True, True, 4)
            row.pack_start(summary, False, False, 4)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_valign(Gtk.Align.CENTER)
        switch = self._switch(_(DEFECT_NAMES[defect_type]))
        switch.connect("notify::active", self._on_defect_enabled_changed, defect_type)
        self.labels["defect_{}_enabled".format(key)] = switch
        controls.pack_start(switch, False, False, 0)
        configure = self._gtk.Button(
            "arrow-right", None, "ai-config-button", scale=0.38,
        )
        configure.set_no_show_all(True)
        configure.set_hexpand(False)
        configure.set_vexpand(False)
        configure.set_size_request(
            max(48, round(self._gtk.font_size * 2.2)),
            max(48, round(self._gtk.font_size * 2.2)),
        )
        configure.get_accessible().set_name(
            "{}: {}".format(_(DEFECT_NAMES[defect_type]), _("Configure"))
        )
        configure.connect("clicked", self._show_defect_editor, defect_type)
        self.labels["defect_{}_configure".format(key)] = configure
        controls.pack_start(configure, False, False, 0)
        row.pack_end(controls, False, False, 2)
        row.show_all()
        row.hide()
        row.set_no_show_all(True)
        return row

    def _build_save_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("ai-save-bar")
        bar.get_style_context().add_class("ai-save-loading")
        self.labels["save_bar"] = bar
        self.labels["sync_icon"] = Gtk.Image()
        bar.pack_start(self.labels["sync_icon"], False, False, 4)
        self.labels["sync_summary"] = Gtk.Label(label=_("Checking..."))
        self.labels["sync_summary"].set_halign(Gtk.Align.START)
        self.labels["sync_summary"].set_xalign(0)
        self.labels["sync_summary"].set_line_wrap(True)
        bar.pack_start(self.labels["sync_summary"], True, True, 0)
        save = self._gtk.Button(
            "complete", _("Apply"), "ai-apply-button", scale=0.48,
            position=Gtk.PositionType.LEFT, lines=1
        )
        save.set_hexpand(False)
        save.set_vexpand(False)
        save.set_size_request(
            max(128, round(self._gtk.font_size * 6.3)),
            max(52, round(self._gtk.font_size * 2.65)),
        )
        save.connect("clicked", self.save_settings)
        self.labels["save"] = save
        bar.pack_end(save, False, False, 4)
        return bar

    def _build_defect_popover(self):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        content.get_style_context().add_class("ai-popover")
        content.set_size_request(
            min(round(self._gtk.font_size * 18), round(self._gtk.content_width * 0.58)), -1
        )
        self.labels["defect_editor_title"] = self._section_title("")
        content.pack_start(self.labels["defect_editor_title"], False, False, 0)
        self.labels["defect_editor_sensitivity"] = self._combo(
            _("Sensitivity"),
            ((INHERIT, _("Follow Default")), ("LOW", _("Low")),
             ("MEDIUM", _("Medium")), ("HIGH", _("High"))),
            self._on_defect_combo_changed,
        )
        content.pack_start(
            self._setting_row(_("Sensitivity"), self.labels["defect_editor_sensitivity"]),
            False, False, 0
        )
        self.labels["defect_editor_action"] = self._combo(
            _("Action"),
            ((INHERIT, _("Follow Default")), ("NOTIFY_ONLY", _("Notify Only")),
             ("AUTO_PAUSE", _("Auto Pause"))),
            self._on_defect_combo_changed,
        )
        content.pack_start(
            self._setting_row(_("Action"), self.labels["defect_editor_action"]),
            False, False, 0
        )
        preflight = Gtk.Label(
            label=_(
                "For the pre-print foreign-object check, Critical blocks printing only when Auto Pause is selected."
            )
        )
        preflight.set_halign(Gtk.Align.START)
        preflight.set_xalign(0)
        preflight.set_line_wrap(True)
        preflight.set_no_show_all(True)
        self.labels["defect_editor_preflight"] = preflight
        content.pack_start(preflight, False, False, 2)
        close = self._gtk.Button(None, _("Close"), "color2", lines=1)
        close.set_vexpand(False)
        close.set_size_request(-1, round(self._gtk.font_size * 2.8))
        close.connect("clicked", self._close_defect_editor)
        content.pack_end(close, False, False, 0)
        popover = Gtk.Popover()
        popover.set_position(
            Gtk.PositionType.BOTTOM if self._screen.vertical_mode else Gtk.PositionType.LEFT
        )
        popover.add(content)
        self.labels["defect_popover"] = popover

    @staticmethod
    def _section_title(title):
        label = Gtk.Label()
        label.set_markup("<big><b>{}</b></big>".format(GLib.markup_escape_text(title)))
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0)
        label.get_style_context().add_class("ai-section-title")
        return label

    def _setting_row(self, title, control):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("ai-compact-row")
        row.get_style_context().add_class("ai-setting-row")
        row.set_size_request(-1, max(48, round(self._gtk.font_size * 2.35)))
        label = Gtk.Label()
        label.set_markup("<b>{}</b>".format(GLib.markup_escape_text(title)))
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0)
        label.set_hexpand(True)
        row.pack_start(label, True, True, 6)
        row.pack_end(control, False, False, 5)
        return row

    def _combo(self, accessible_name, choices, callback):
        combo = Gtk.ComboBoxText()
        combo.get_style_context().add_class("ai-combo")
        for value, label in choices:
            combo.append(value, label)
        combo.set_active(0)
        combo.set_size_request(
            max(152, round(self._gtk.font_size * 6.7)),
            max(44, round(self._gtk.font_size * 2.05)),
        )
        combo.get_accessible().set_name(accessible_name)
        combo.connect("changed", callback)
        return combo

    @staticmethod
    def _set_combo(combo, value, choices, default):
        selected = value if value in choices else default
        if not combo.set_active_id(selected):
            combo.set_active(0)

    @staticmethod
    def _combo_value(combo, default):
        return str(combo.get_active_id() or default).upper()

    @staticmethod
    def _set_optional_text(label, text):
        label.set_text(text)
        label.set_visible(bool(text))

    @staticmethod
    def _replace_style_class(widget, classes, active_class):
        context = widget.get_style_context()
        for style_class in classes:
            context.remove_class(style_class)
        context.add_class(active_class)

    def _switch(self, accessible_name):
        switch = Gtk.Switch()
        switch.get_style_context().add_class("ai-compact-switch")
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_size_request(
            max(72, round(self._gtk.font_size * 3.25)),
            max(44, round(self._gtk.font_size * 1.8)),
        )
        switch.get_accessible().set_name(accessible_name)
        return switch

    def activate(self):
        self._active = True
        self.refresh()
        self.status_timeout = GLib.timeout_add_seconds(15, self.refresh)

    def deactivate(self):
        self._active = False
        self._refresh_pending = False
        if self.status_timeout:
            GLib.source_remove(self.status_timeout)
            self.status_timeout = None
        popover = self.labels.get("defect_popover")
        if popover and popover.get_visible():
            popover.popdown()

    def back(self):
        popover = self.labels.get("defect_popover")
        if popover and popover.get_visible():
            popover.popdown()
            return True
        return False

    def process_update(self, action, data):
        if action in (
            "notify_server_info", "notify_klippy_ready", "notify_ai_status_updated",
            "notify_ai_policy_applied", "notify_ai_preflight_result",
        ):
            self.refresh()

    def refresh(self):
        if not self._active:
            return False
        if self._refreshing:
            self._refresh_pending = True
            return True
        self._refreshing = True
        generation = self._policy_generation

        def request():
            status = self._request("server/ai_detection/status")
            policy = self._request("server/ai_detection/policy")
            GLib.idle_add(
                self._apply_response, status, policy, generation
            )

        threading.Thread(target=request, daemon=True).start()
        return True

    def _request(self, endpoint):
        try:
            response = self._screen.apiclient.send_request(endpoint)
            return response if isinstance(response, dict) else None
        except Exception as error:
            logging.warning("AI Platform V3 endpoint %s unavailable: %s", endpoint, error)
            return None

    def _apply_response(self, status_response, policy_response, generation):
        self._refreshing = False
        status = _unwrap_response(status_response, "status")
        policy = _unwrap_response(policy_response, "policy")
        self._refresh_completed = True
        self._status_request_ok = status is not None
        policy_response_is_current = generation == self._policy_generation
        if policy_response_is_current:
            self._policy_request_ok = policy is not None
        self.status = status if status is not None else {}
        status_v3 = bool(
            self._status_request_ok
            and self.status.get("protocolVersion") == PROTOCOL_VERSION
            and str(self.status.get("policySchemaVersion") or "") == str(POLICY_SCHEMA_VERSION)
        )
        policy_v3 = bool(
            policy is not None
            and policy.get("protocolVersion") == PROTOCOL_VERSION
            and str(policy.get("policySchemaVersion") or "") == str(POLICY_SCHEMA_VERSION)
        )
        if policy_response_is_current:
            self._policy_compatible = policy_v3
        self._protocol_ready = bool(status_v3 and self._policy_compatible)

        incoming_version = _policy_version(policy)
        current_version = _policy_version(self.policy)
        policy_not_older = current_version <= 0 or incoming_version >= current_version
        conflict_policy_is_newer = incoming_version > current_version

        if (
            policy_response_is_current
            and policy_v3
            and self._conflict_reloading
            and conflict_policy_is_newer
        ):
            self._conflict_reloading = False
            self._save_failed = False
            self._dirty = False
            self.policy = policy
            self._apply_policy()
        elif (
            policy_response_is_current
            and policy_v3
            and policy_not_older
            and not self._dirty
            and not self._saving
        ):
            self.policy = policy
            self._apply_policy()
        else:
            self._update_control_sensitivity()
        self._apply_status()
        self._update_defect_summaries()
        self._update_sync_summary()
        if self._refresh_pending and self._active:
            self._refresh_pending = False
            GLib.idle_add(self._run_pending_refresh)
        return False

    def _run_pending_refresh(self):
        self.refresh()
        return False

    def _agent_policy_value(self, key, policy=None):
        status_policy = self.status.get("policy")
        if not isinstance(status_policy, dict):
            status_policy = {}
        sources = [policy] if isinstance(policy, dict) else []
        sources.extend((self.status, status_policy, self.policy))
        for source in sources:
            if isinstance(source, dict) and source.get(key) is not None:
                return source.get(key)
        return None

    def _policy_trust_state(self, policy=None):
        return str(self._agent_policy_value("policyTrustState", policy) or "").upper()

    def _legacy_policy_unconfigured(self, policy=None):
        candidate = policy if isinstance(policy, dict) else self.policy
        if not isinstance(candidate, dict):
            candidate = {}
        status_policy = self.status.get("policy")
        if not candidate and isinstance(status_policy, dict):
            candidate = status_policy
        supported = candidate.get("supportedDefectTypes") or []
        model_version = str(candidate.get("modelVersion") or "").upper()
        return bool(
            not supported
            and model_version == "UNCONFIGURED"
            and (candidate.get("safeDegraded") or self.status.get("safeDegraded"))
        )

    def _no_valid_policy(self, policy=None):
        trust_state = self._policy_trust_state(policy)
        return bool(
            trust_state in POLICY_TRUST_UNAVAILABLE
            or self._legacy_policy_unconfigured(policy)
        )

    def _runtime_reason(self):
        reason = str(self.status.get("notWatchingReason") or "").upper()
        sync_only = reason in SYNC_ONLY_REASONS
        if sync_only:
            reason = ""
        if self._policy_trust_state() in (
            "POLICY_SIGNATURE_INVALID", "SIGNATURE_INVALID"
        ):
            reason = "POLICY_SIGNATURE_INVALID"
        elif self._no_valid_policy():
            reason = "NO_VALID_POLICY"
        return reason or None, sync_only

    def _policy_sync_state(self, policy=None):
        if isinstance(policy, dict):
            state = policy.get("policySyncState")
            if state is None:
                state = policy.get("applyStatus")
        else:
            state = self._agent_policy_value("policySyncState")
            if state is None:
                state = self._agent_policy_value("applyStatus")
        return str(state or "").upper()

    def _policy_sync_error(self, policy=None):
        return str(self._agent_policy_value("policySyncError", policy) or "").strip()

    def _policy_outbox_depth(self, policy=None):
        try:
            return max(
                0,
                int(self._agent_policy_value("policyOutboxDepth", policy) or 0),
            )
        except (TypeError, ValueError):
            return 0

    def _agent_sync_text(self, policy=None):
        state = self._policy_sync_state(policy)
        outbox_depth = self._policy_outbox_depth(policy)
        sync_error = self._policy_sync_error(policy)
        if state == "CONFLICT":
            return _("Applied locally, cloud settings conflict")
        if state == "REBASING":
            return _("Applied locally, merging cloud settings")
        if state == "SYNCING":
            return _("Syncing with cloud")
        if state in SYNC_PENDING_STATES or outbox_depth > 0:
            return _("Applied locally, waiting for cloud sync")
        if state in SYNC_FAILED_STATES or sync_error:
            return _("Applied locally, cloud sync failed")
        return _(SYNC_NAMES.get(state or "APPLIED", state or "APPLIED"))

    def _apply_status(self):
        online = bool(
            self._status_request_ok
            and self.status.get("protocolVersion") == PROTOCOL_VERSION
            and str(self.status.get("policySchemaVersion") or "") == str(POLICY_SCHEMA_VERSION)
        )
        reason, sync_only_reason = self._runtime_reason() if online else (None, False)
        trust_state = self._policy_trust_state()
        loading_policy = online and trust_state in POLICY_TRUST_LOADING and not reason
        sync_state = self._policy_sync_state()
        cloud_sync_only = bool(
            sync_state in SYNC_PENDING_STATES
            or sync_state in SYNC_FAILED_STATES
            or sync_state in ("CONFLICT", "REBASING", "SYNCING")
            or self._policy_sync_error()
            or self._policy_outbox_depth() > 0
        )
        reported_watching = self.status.get("watching")
        if (sync_only_reason or cloud_sync_only) and not reason:
            watching = online
        elif isinstance(reported_watching, bool):
            watching = reported_watching and not reason and not loading_policy
        else:
            watching = online and not reason and not loading_policy
        safe_degraded = self._safe_degraded() if online else False
        if loading_policy:
            state_text = _("Checking...")
            icon_name = "info"
        elif watching:
            state_text = _("Watching")
            icon_name = "warning" if safe_degraded else "check_pass"
        elif online:
            state_text = _("Not Watching")
            icon_name = "warning" if safe_degraded else "info"
        else:
            state_text = _("Offline")
            icon_name = "check_fail"
        self.labels["status"].set_markup(
            "<big><b>{}</b></big>".format(GLib.markup_escape_text(state_text))
        )
        self._set_icon("status_icon", icon_name, 1.35)
        if loading_policy:
            status_class = "ai-status-loading"
        elif watching and not safe_degraded:
            status_class = "ai-status-watching"
        elif online:
            status_class = "ai-status-idle"
        else:
            status_class = "ai-status-offline"
        self._replace_style_class(
            self.labels["status_bar"],
            ("ai-status-loading", "ai-status-watching", "ai-status-idle", "ai-status-offline"),
            status_class,
        )
        details = []
        if loading_policy:
            details.append(_("AI configuration is being prepared"))
        elif reason:
            details.append(_(REASON_NAMES.get(reason, reason)))
        if not online:
            details.append(_("AI Platform V3 is required") if self._status_request_ok
                           else _("AI Agent unavailable"))
        if safe_degraded and not reason:
            details.append("{} · {}".format(_("Safe mode"), _("Notify Only")))
        self._set_optional_text(self.labels["status_detail"], " · ".join(details))

    def _apply_policy(self):
        self._updating_controls = True
        try:
            supported = self.policy.get("supportedDefectTypes")
            if not isinstance(supported, list):
                supported = list((self.policy.get("defectPolicies") or {}).keys())
            macro_state = str(self.policy.get("macroCapabilityState") or "").upper()
            macro_supported = self.policy.get("macroSupportedDefectTypes")
            if (
                macro_state not in ("READY", "UNAVAILABLE")
                or not isinstance(macro_supported, list)
            ):
                supported = []
            else:
                macro_supported = {
                    str(item).upper() for item in macro_supported
                    if str(item).upper() in DEFECT_TYPES
                }
                supported = [
                    item for item in supported if str(item).upper() in macro_supported
                ]
            self.supported_defects = {
                str(item).upper() for item in supported
                if str(item).upper() in DEFECT_TYPES
            }
            self.labels["enabled"].set_active(
                bool(self.supported_defects and self.policy.get("enabled", True))
            )
            self._set_combo(
                self.labels["sensitivity"], str(self.policy.get("sensitivity") or "MEDIUM").upper(),
                SENSITIVITIES, "MEDIUM"
            )
            self._set_combo(
                self.labels["action"], str(self.policy.get("actionMode") or "NOTIFY_ONLY").upper(),
                ACTIONS, "NOTIFY_ONLY"
            )
            overrides = self.policy.get("defectOverrides") or {}
            if not isinstance(overrides, dict):
                overrides = {}
            for defect_type in DEFECT_TYPES:
                key = self._defect_key(defect_type)
                override = overrides.get(defect_type) or {}
                if not isinstance(override, dict):
                    override = {}
                self.labels["defect_{}_enabled".format(key)].set_active(
                    bool(override.get("enabled", True))
                )
                sensitivity = str(override.get("sensitivity") or INHERIT).upper()
                action = str(override.get("actionMode") or INHERIT).upper()
                self.defect_drafts[defect_type] = {
                    "sensitivity": sensitivity if sensitivity in (INHERIT,) + SENSITIVITIES else INHERIT,
                    "actionMode": action if action in (INHERIT,) + ACTIONS else INHERIT,
                }
            self._policy_loaded = True
            self._dirty = False
            self._baseline_settings = self._settings_snapshot()
        finally:
            self._updating_controls = False
        self._update_defect_summaries()
        self._update_control_sensitivity()

    def _safe_degraded(self):
        status_policy = self.status.get("policy")
        if not isinstance(status_policy, dict):
            status_policy = {}
        return bool(
            self._policy_trust_state() in POLICY_TRUST_DEGRADED
            or self.status.get("safeDegraded") or status_policy.get("safeDegraded")
            or (self._policy_compatible and self.policy.get("safeDegraded"))
        )

    def _update_control_sensitivity(self):
        controls_ready = bool(
            self._protocol_ready and self.supported_defects
            and not self._no_valid_policy()
            and not self._saving and not self._conflict_reloading
        )
        subordinate_ready = controls_ready and self.labels["enabled"].get_active()
        for panel_key in ("defaults_panel", "defects_panel"):
            panel = self.labels.get(panel_key)
            if panel:
                context = panel.get_style_context()
                if subordinate_ready:
                    context.remove_class("ai-card-disabled")
                else:
                    context.add_class("ai-card-disabled")
        self.labels["enabled"].set_sensitive(controls_ready)
        self.labels["sensitivity"].set_sensitive(subordinate_ready)
        action_ready = subordinate_ready and not self._safe_degraded()
        self.labels["action"].set_sensitive(action_ready)
        for defect_type in DEFECT_TYPES:
            key = self._defect_key(defect_type)
            supported = defect_type in self.supported_defects
            self.labels["defect_{}_enabled".format(key)].set_sensitive(subordinate_ready and supported)
            self.labels["defect_{}_configure".format(key)].set_sensitive(subordinate_ready and supported)
        editor_ready = subordinate_ready and self._editing_defect in self.supported_defects
        self.labels["defect_editor_sensitivity"].set_sensitive(editor_ready)
        self.labels["defect_editor_action"].set_sensitive(
            editor_ready and not self._safe_degraded()
        )
        self.labels["save"].set_sensitive(controls_ready and self._dirty and not self._saving)

    def _update_defect_summaries(self):
        if not self._policy_loaded:
            self.labels["defects_state"].set_text(_("Checking..."))
            self.labels["defects_state"].show()
            return
        empty_text = _("No valid AI policy") if self._no_valid_policy() else _(
            "No detection types are available for this printer"
        )
        self.labels["defects_state"].set_text(empty_text)
        has_supported_defects = bool(self.supported_defects)
        self.labels["defects_state"].set_visible(not has_supported_defects)
        self.labels["defaults_panel"].set_visible(has_supported_defects)
        global_sensitivity = self._combo_value(self.labels["sensitivity"], "MEDIUM")
        global_action = self._combo_value(self.labels["action"], "NOTIFY_ONLY")
        sensitivity_names = {"LOW": _("Low"), "MEDIUM": _("Medium"), "HIGH": _("High")}
        action_names = {"NOTIFY_ONLY": _("Notify Only"), "AUTO_PAUSE": _("Auto Pause")}
        effective_policies = self.policy.get("defectPolicies") or {}
        if not isinstance(effective_policies, dict):
            effective_policies = {}
        for defect_type in DEFECT_TYPES:
            key = self._defect_key(defect_type)
            supported = defect_type in self.supported_defects
            self.labels["defect_{}_row".format(key)].set_visible(supported)
            enabled = self.labels["defect_{}_enabled".format(key)].get_active()
            draft = self.defect_drafts[defect_type]
            sensitivity = draft.get("sensitivity", INHERIT)
            action = draft.get("actionMode", INHERIT)
            effective_sensitivity = global_sensitivity if sensitivity == INHERIT else sensitivity
            effective_action = global_action if action == INHERIT else action
            server_effective = effective_policies.get(defect_type)
            if not self._dirty and isinstance(server_effective, dict):
                effective_sensitivity = str(server_effective.get("sensitivity") or effective_sensitivity).upper()
                effective_action = str(server_effective.get("actionMode") or effective_action).upper()
            if self._safe_degraded() and effective_action == "AUTO_PAUSE":
                effective_action = "NOTIFY_ONLY"
            if not supported:
                summary = ""
            elif not enabled:
                summary = _("Off")
            else:
                summary = "{} · {}".format(
                    sensitivity_names.get(effective_sensitivity, _("Medium")),
                    action_names.get(effective_action, _("Notify Only")),
                )
            summary_label = self.labels["defect_{}_summary".format(key)]
            summary_label.set_text(summary)
            summary_label.set_visible(bool(summary))
            self.labels["defect_{}_configure".format(key)].set_visible(supported)

    def _update_sync_summary(self):
        if self._saving:
            value = _("Applying on this printer")
            state_class = "ai-save-loading"
            icon_name = "hourglass"
        elif self._conflict_reloading:
            value = _("Reloading current settings")
            state_class = "ai-save-warning"
            icon_name = "warning"
        elif self._save_failed:
            value = _("Settings could not be applied")
            state_class = "ai-save-error"
            icon_name = "check_fail"
        elif self._dirty:
            value = _("Unsaved changes")
            state_class = "ai-save-dirty"
            icon_name = "warning"
        elif self._refresh_completed and not self._policy_compatible:
            value = _("Settings unavailable")
            state_class = "ai-save-error"
            icon_name = "check_fail"
        elif not self._policy_loaded:
            value = _("Checking...")
            state_class = "ai-save-loading"
            icon_name = "hourglass"
        else:
            value = self._agent_sync_text()
            sync_state = self._policy_sync_state()
            if (
                sync_state in SYNC_FAILED_STATES or sync_state == "CONFLICT"
                or self._policy_sync_error()
            ):
                state_class = "ai-save-error"
                icon_name = "check_fail"
            elif (
                sync_state in SYNC_PENDING_STATES
                or sync_state in ("REBASING", "SYNCING")
                or self._policy_outbox_depth() > 0
            ):
                state_class = "ai-save-warning"
                icon_name = "hourglass"
            else:
                state_class = "ai-save-synced"
                icon_name = "check_pass"
        self._replace_style_class(
            self.labels["save_bar"],
            ("ai-save-loading", "ai-save-dirty", "ai-save-warning",
             "ai-save-error", "ai-save-synced"),
            state_class,
        )
        self._set_icon("sync_icon", icon_name, 0.8)
        self.labels["sync_summary"].set_markup(
            "<b>{}</b>".format(GLib.markup_escape_text(value))
        )

    def _mark_dirty(self):
        if self._updating_controls or self._saving:
            return
        if self._save_failed:
            self._pending_request_id = None
        self._save_failed = False
        current_settings = self._settings_snapshot()
        self._dirty = bool(
            self._baseline_settings is None
            or current_settings != self._baseline_settings
        )
        if self._dirty and not self._pending_request_id:
            self._pending_request_id = str(uuid.uuid4())
        elif not self._dirty:
            self._pending_request_id = None
        self._update_sync_summary()
        self._update_control_sensitivity()

    def _on_enabled_changed(self, switch, param):
        if self._updating_controls:
            return
        self._mark_dirty()
        self._update_control_sensitivity()

    def _on_defect_enabled_changed(self, switch, param, defect_type):
        if self._updating_controls:
            return
        self._mark_dirty()
        self._update_defect_summaries()

    def _on_global_combo_changed(self, combo):
        if self._updating_controls or combo.get_active_id() is None:
            return
        self._mark_dirty()
        self._update_defect_summaries()

    def _on_defect_combo_changed(self, combo):
        if self._updating_controls or self._editing_defect not in DEFECT_TYPES:
            return
        self.defect_drafts[self._editing_defect] = {
            "sensitivity": self._combo_value(self.labels["defect_editor_sensitivity"], INHERIT),
            "actionMode": self._combo_value(self.labels["defect_editor_action"], INHERIT),
        }
        self._mark_dirty()
        self._update_defect_summaries()

    def _show_defect_editor(self, widget, defect_type):
        if defect_type not in self.supported_defects:
            return
        self._editing_defect = defect_type
        draft = self.defect_drafts[defect_type]
        self._updating_controls = True
        try:
            self.labels["defect_editor_title"].set_markup(
                "<big><b>{}</b></big>".format(GLib.markup_escape_text(_(DEFECT_NAMES[defect_type])))
            )
            self._set_combo(
                self.labels["defect_editor_sensitivity"], draft.get("sensitivity", INHERIT),
                (INHERIT,) + SENSITIVITIES, INHERIT
            )
            self._set_combo(
                self.labels["defect_editor_action"], draft.get("actionMode", INHERIT),
                (INHERIT,) + ACTIONS, INHERIT
            )
        finally:
            self._updating_controls = False
        self._update_control_sensitivity()
        popover = self.labels["defect_popover"]
        popover.set_relative_to(widget)
        popover.show_all()
        if defect_type == "FOREIGN_OBJECT":
            preflight_text = _(
                "Safe mode is active. The pre-print check will notify only and will not block printing."
            ) if self._safe_degraded() else _(
                "For the pre-print foreign-object check, Critical blocks printing only when Auto Pause is selected."
            )
            self.labels["defect_editor_preflight"].set_text(preflight_text)
            self.labels["defect_editor_preflight"].show()
        else:
            self.labels["defect_editor_preflight"].hide()

    def _close_defect_editor(self, widget):
        self.labels["defect_popover"].popdown()

    def _set_icon(self, label_key, icon_name, scale):
        cache_key = (icon_name, scale)
        pixbuf = self._icon_cache.get(cache_key)
        if pixbuf is None:
            size = round(self._gtk.font_size * scale)
            pixbuf = self._gtk.PixbufFromIcon(icon_name, size, size)
            self._icon_cache[cache_key] = pixbuf
        if pixbuf is not None:
            self.labels[label_key].set_from_pixbuf(pixbuf)
        else:
            self.labels[label_key].clear()

    def _build_defect_overrides(self):
        overrides = {}
        for defect_type in DEFECT_TYPES:
            if defect_type not in self.supported_defects:
                continue
            key = self._defect_key(defect_type)
            item = {"enabled": self.labels["defect_{}_enabled".format(key)].get_active()}
            draft = self.defect_drafts[defect_type]
            if draft.get("sensitivity", INHERIT) != INHERIT:
                item["sensitivity"] = draft["sensitivity"]
            if draft.get("actionMode", INHERIT) != INHERIT:
                item["actionMode"] = draft["actionMode"]
            overrides[defect_type] = item
        return overrides

    def _settings_snapshot(self):
        return {
            "enabled": self.labels["enabled"].get_active(),
            "sensitivity": self._combo_value(self.labels["sensitivity"], "MEDIUM"),
            "actionMode": self._combo_value(self.labels["action"], "NOTIFY_ONLY"),
            "defectOverrides": self._build_defect_overrides(),
        }

    def save_settings(self, widget):
        if self._saving or not self._dirty:
            return
        if not self.supported_defects:
            self._screen.show_popup_message(_("Unsupported printer model"), 2)
            return
        if not self._protocol_ready:
            self._screen.show_popup_message(_("AI Agent unavailable"), 2)
            return
        if not self._pending_request_id:
            self._pending_request_id = str(uuid.uuid4())
        payload = {
            "protocolVersion": PROTOCOL_VERSION,
            "policySchemaVersion": POLICY_SCHEMA_VERSION,
            "basePolicyVersion": int(
                self.policy.get("policyVersion") or self.policy.get("basePolicyVersion") or 0
            ),
            "enabled": self.labels["enabled"].get_active(),
            "sensitivity": self._combo_value(self.labels["sensitivity"], "MEDIUM"),
            "actionMode": self._combo_value(self.labels["action"], "NOTIFY_ONLY"),
            "defectOverrides": self._build_defect_overrides(),
            "clientRequestId": self._pending_request_id,
        }
        self._saving = True
        self._gtk.Button_busy(self.labels["save"], True)
        self._update_sync_summary()
        self._update_control_sensitivity()

        def request():
            try:
                response = self._screen.apiclient.post_request(
                    "server/ai_detection/policy", json=payload
                )
                policy = _unwrap_response(response, "policy")
                if policy is None:
                    status = str(getattr(self._screen.apiclient, "status", "") or "")
                    GLib.idle_add(self._settings_failed, status)
                else:
                    GLib.idle_add(self._settings_saved, policy)
            except Exception as error:
                logging.warning("AI Platform V3 settings update failed: %s", error)
                GLib.idle_add(self._settings_failed, str(error))

        threading.Thread(target=request, daemon=True).start()

    def _settings_saved(self, policy):
        valid = bool(
            isinstance(policy, dict) and policy.get("protocolVersion") == PROTOCOL_VERSION
            and int(policy.get("policySchemaVersion") or 0) == POLICY_SCHEMA_VERSION
        )
        legacy_apply_status = str(policy.get("applyStatus") or "APPLIED").upper()
        explicit_sync_state = policy.get("policySyncState") is not None
        if not valid or (
            not explicit_sync_state and legacy_apply_status in ("FAILED", "ERROR")
        ):
            return self._settings_failed(legacy_apply_status)
        self._saving = False
        self._save_failed = False
        self._dirty = False
        self._pending_request_id = None
        self._policy_generation += 1
        self._policy_request_ok = True
        self._policy_compatible = True
        self.policy = policy
        self._gtk.Button_busy(self.labels["save"], False)
        self._apply_policy()
        self._update_sync_summary()
        sync_state = self._policy_sync_state(policy)
        if sync_state == "PENDING_LOCAL":
            message = _("Applying on this printer")
        elif (
            sync_state in SYNC_PENDING_STATES
            or sync_state in SYNC_FAILED_STATES
            or sync_state in ("CONFLICT", "REBASING", "SYNCING")
            or self._policy_outbox_depth(policy) > 0
            or self._policy_sync_error(policy)
        ):
            message = self._agent_sync_text(policy)
        else:
            message = _("Saved")
        self._screen.show_popup_message(message, 1)
        self.refresh()
        return False

    def _settings_failed(self, error_status=""):
        self._saving = False
        self._gtk.Button_busy(self.labels["save"], False)
        if "409" in str(error_status):
            self._save_failed = False
            self._conflict_reloading = True
            self._dirty = True
            self._pending_request_id = None
            self._policy_generation += 1
            self._update_control_sensitivity()
            self._update_sync_summary()
            self._screen.show_popup_message(
                _("Policy changed elsewhere. Latest settings will be reloaded."), 2
            )
            self.refresh()
        else:
            self._save_failed = True
            self._dirty = True
            if (
                "400" in str(error_status)
                or "426" in str(error_status)
                or str(error_status).upper() in ("FAILED", "ERROR")
            ):
                self._pending_request_id = None
            self._update_control_sensitivity()
            self._update_sync_summary()
            self._screen.show_popup_message(_("Error"), 2)
        return False
