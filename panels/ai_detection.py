import logging
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango
from ks_includes.screen_panel import ScreenPanel


def _translation_strings():
    _("First Layer")
    _("First Layer Detection")
    _("Foreign Object")
    _("Foreign Object Detection")
    _("General Detection")
    _("Spaghetti")
    _("Spaghetti Detection")
    _("Warp Edge")
    _("Warp Edge Detection")
    _("Nozzle Blob Detection")
    _("Pause Count")
    _("Pause Count: %(count)d/%(required)d")


DETECTION_TYPES = [
    {
        "key": "spaghetti",
        "name": "Spaghetti Detection",
        "short_name": "Spaghetti",
        "macro": "AI_DETECT_SPAGHETTI",
        "default_threshold": 0.70,
        "default_interval": 30,
        "default_scheduled": True,
        "default_pause_consecutive": 3,
        "aliases": [],
    },
    {
        "key": "warphead",
        "name": "Nozzle Blob Detection",
        "short_name": "Nozzle Blob Detection",
        "macro": "AI_DETECT_WARPHEAD",
        "default_threshold": 0.75,
        "default_interval": 60,
        "default_scheduled": False,
        "force_scheduled": False,
        "default_pause_consecutive": 3,
        "aliases": ["Warp Head Detection", "Warp Head", "Nozzle Blob"],
    },
    {
        "key": "tooLessAndTooMuch",
        "name": "First Layer Detection",
        "short_name": "First Layer",
        "macro": "AI_DETECT_EXTRUSION",
        "default_threshold": 0.70,
        "default_interval": 60,
        "default_scheduled": False,
        "default_pause_consecutive": 1,
        "aliases": [
            "Extrusion Detection",
            "Extrusion",
            "First Layer Detection",
            "First Layer",
        ],
    },
    {
        "key": "warpEdgesAndNonStick",
        "name": "Warp Edge Detection",
        "short_name": "Warp Edge",
        "macro": "AI_DETECT_NONSTICK",
        "default_threshold": 0.70,
        "default_interval": 120,
        "default_scheduled": False,
        "default_pause_consecutive": 1,
        "aliases": [],
    },
    {
        "key": "foreignBody",
        "name": "Foreign Object Detection",
        "short_name": "Foreign Object",
        "macro": "AI_DETECT_FOREIGNBODY",
        "default_threshold": 0.60,
        "default_interval": 60,
        "default_scheduled": False,
        "force_scheduled": False,
        "default_pause_consecutive": 1,
        "aliases": ["Foreign Body Detection", "Foreign Body"],
    },
]

DEFAULT_PAUSE_CONSECUTIVE_DETECTIONS = 1

LEGACY_DETECTION_ALIASES = {
    "coco80": "General Detection",
    "ai_detect_coco80": "General Detection",
    "general detection": "General Detection",
    "general": "General Detection",
}


class Panel(ScreenPanel):

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.menu = ['main_menu']
        self.settings = {}
        self.status_timeout = None
        self.ai_online = False
        self.pause_on_defect = True
        self.last_result = None
        self._active = False

        self.labels['main_menu'] = self._build_main_page()
        self.labels['settings_menu'] = self._build_settings_page()

        self.content.add(self.labels['main_menu'])

    # ==================== UI Build ====================

    def _build_main_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)

        # --- Status bar ---
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_row.get_style_context().add_class("frame-item")

        self.labels['status_label'] = Gtk.Label()
        self._set_status_label(_("Checking..."), "gray")
        self.labels['status_label'].set_hexpand(True)
        self.labels['status_label'].set_halign(Gtk.Align.START)

        settings_btn = self._gtk.Button("fine-tune", _("Settings"), "color3")
        settings_btn.connect("clicked", self.load_menu, 'settings', _("Settings"))

        status_row.pack_start(self.labels['status_label'], True, True, 5)
        status_row.pack_end(settings_btn, False, False, 5)
        page.pack_start(status_row, False, False, 0)

        # --- Pause on defect toggle ---
        pause_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        pause_row.get_style_context().add_class("frame-item")

        pause_label = Gtk.Label()
        pause_label.set_markup(
            f"<big><b>{GLib.markup_escape_text(_('Pause Print on Defect'))}</b></big>"
        )
        pause_label.set_hexpand(True)
        pause_label.set_halign(Gtk.Align.START)

        self.labels['pause_switch'] = Gtk.Switch()
        self.labels['pause_switch'].set_active(True)
        self.labels['pause_switch'].set_property("width-request", round(self._gtk.font_size * 7))
        self.labels['pause_switch'].set_property("height-request", round(self._gtk.font_size * 3.5))
        self.labels['pause_switch'].connect("notify::active", self.on_pause_toggled)

        pause_row.pack_start(pause_label, True, True, 5)
        pause_row.pack_end(self.labels['pause_switch'], False, False, 5)
        page.pack_start(pause_row, False, False, 0)

        # --- Latest detection result ---
        result_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        result_row.get_style_context().add_class("frame-item")

        result_title = Gtk.Label()
        result_title.set_markup(
            f"<big><b>{GLib.markup_escape_text(_('Latest Detection Result'))}</b></big>"
        )
        result_title.set_halign(Gtk.Align.START)

        self.labels['result_text'] = Gtk.Label()
        self.labels['result_text'].set_text(_("No detection results yet"))
        self.labels['result_text'].set_halign(Gtk.Align.START)
        self.labels['result_text'].set_line_wrap(True)
        self.labels['result_text'].set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)

        result_row.pack_start(result_title, False, False, 2)
        result_row.pack_start(self.labels['result_text'], False, False, 2)
        page.pack_start(result_row, False, False, 0)

        return page

    def _build_settings_page(self):
        grid = Gtk.Grid()
        grid.set_column_homogeneous(True)
        row_idx = 0

        available_detection_types = [
            dt for dt in DETECTION_TYPES if self._is_detection_macro_available(dt)
        ]
        for idx, dt in enumerate(available_detection_types):
            key = dt['key']

            # --- Section header ---
            header = Gtk.Label()
            header.set_markup(
                f"\n<big><b>{GLib.markup_escape_text(_(dt['name']))}</b></big>  <small>({key})</small>"
            )
            header.set_halign(Gtk.Align.START)
            grid.attach(header, 0, row_idx, 4, 1)
            row_idx += 1

            # --- Enable switch ---
            sw_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            sw_row.get_style_context().add_class("frame-item")

            en_label = Gtk.Label()
            en_label.set_markup(f"<b>{GLib.markup_escape_text(_('Enabled'))}</b>")
            self.labels[f'{key}_enabled'] = Gtk.Switch()
            macro_available = self._is_detection_macro_available(dt)
            self.labels[f'{key}_enabled'].set_active(macro_available)
            self.labels[f'{key}_enabled'].set_sensitive(macro_available)
            self.labels[f'{key}_enabled'].set_property("width-request", round(self._gtk.font_size * 5))
            self.labels[f'{key}_enabled'].set_property("height-request", round(self._gtk.font_size * 3))

            sw_row.pack_start(en_label, False, False, 5)
            sw_row.pack_start(self.labels[f'{key}_enabled'], False, False, 5)

            grid.attach(sw_row, 0, row_idx, 4, 1)
            row_idx += 1

            # --- Confidence threshold slider ---
            conf_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            conf_label = Gtk.Label()
            conf_label.set_markup(f"<b>{GLib.markup_escape_text(_('Confidence'))}</b>")
            conf_label.set_size_request(round(self._gtk.font_size * 5), -1)

            self.labels[f'{key}_threshold'] = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0.1, 1.0, 0.05)
            self.labels[f'{key}_threshold'].set_value(dt['default_threshold'])
            self.labels[f'{key}_threshold'].set_digits(2)
            self.labels[f'{key}_threshold'].set_hexpand(True)
            self.labels[f'{key}_threshold'].set_has_origin(True)
            self.labels[f'{key}_threshold'].get_style_context().add_class("option_slider")

            conf_row.pack_start(conf_label, False, False, 5)
            conf_row.pack_start(self.labels[f'{key}_threshold'], True, True, 5)
            grid.attach(conf_row, 0, row_idx, 4, 1)
            row_idx += 1

            # --- Auto-pause consecutive detection count ---
            pause_count_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            pause_count_label = Gtk.Label()
            pause_count_label.set_markup(
                f"<b>{GLib.markup_escape_text(_('Pause Count'))}</b>"
            )
            pause_count_label.set_size_request(round(self._gtk.font_size * 5), -1)

            self.labels[f'{key}_pause_count'] = Gtk.SpinButton.new_with_range(
                1, 99, 1
            )
            self.labels[f'{key}_pause_count'].set_value(
                dt.get(
                    'default_pause_consecutive',
                    DEFAULT_PAUSE_CONSECUTIVE_DETECTIONS
                )
            )
            self.labels[f'{key}_pause_count'].set_numeric(True)
            self.labels[f'{key}_pause_count'].set_property(
                "width-request", round(self._gtk.font_size * 6)
            )

            pause_count_row.pack_start(pause_count_label, False, False, 5)
            pause_count_row.pack_start(
                self.labels[f'{key}_pause_count'], False, False, 5
            )
            grid.attach(pause_count_row, 0, row_idx, 4, 1)
            row_idx += 1

            if idx < len(available_detection_types) - 1:
                separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                grid.attach(separator, 0, row_idx, 4, 1)
                row_idx += 1

        # --- Save button ---
        save_btn = self._gtk.Button(None, _("Save"), "color1")
        save_btn.connect("clicked", self.save_settings)
        grid.attach(save_btn, 1, row_idx, 2, 1)

        scroll = self._gtk.ScrolledWindow()
        scroll.add(grid)
        return scroll

    # ==================== Lifecycle ====================

    def activate(self):
        self._active = True
        self.fetch_status()
        self.fetch_settings()
        self.fetch_latest_result()
        self.status_timeout = GLib.timeout_add_seconds(30, self.fetch_status)

    def deactivate(self):
        self._active = False
        if self.status_timeout:
            GLib.source_remove(self.status_timeout)
            self.status_timeout = None

    def back(self):
        if len(self.menu) > 1:
            self.unload_menu()
            return True
        return False

    def process_update(self, action, data):
        if action in (
            "notify_ai_detection_result",
            "notify_detection_complete",
            "notify_ai_detection_detection_complete",
        ):
            result = self._normalize_result_payload(data)
            if result is not None:
                self.last_result = result
                self._update_result_display()
        elif action == "notify_ai_detection_defect_detected":
            result = self._normalize_result_payload(data)
            if result is not None:
                result = dict(result)
                result.setdefault("has_defect", True)
                self.last_result = result
                self._update_result_display()

    # ==================== REST API ====================

    def fetch_status(self):
        if not self._active:
            return False
        def _do():
            try:
                result = self._screen.apiclient.send_request("server/ai_detection/status")
            except Exception as e:
                logging.exception(f"AI detection: failed to fetch status: {e}")
                result = None
            GLib.idle_add(self._on_status_fetched, result)
        threading.Thread(target=_do, daemon=True).start()
        return self._active

    def _on_status_fetched(self, result):
        try:
            if result and isinstance(result, dict) and 'result' in result:
                data = result['result']
                if not isinstance(data, dict):
                    return
                self.ai_online = bool(data.get('service_available', False))
                color = "green" if self.ai_online else "red"
                text = _("Online") if self.ai_online else _("Offline")
                self._set_status_label(text, color)
                if 'pause_on_defect' in data:
                    self._set_pause_switch(bool(data['pause_on_defect']))
                if 'last_detection' in data and isinstance(data['last_detection'], dict):
                    self.last_result = data['last_detection']
                    self._update_result_display()
            else:
                self.ai_online = False
                self._set_status_label(_("Unknown"), "gray")
        except Exception as e:
            logging.exception(f"AI detection: error updating status UI: {e}")

    def fetch_settings(self):
        def _do():
            try:
                result = self._screen.apiclient.send_request("server/ai_detection/settings")
            except Exception as e:
                logging.exception(f"AI detection: failed to fetch settings: {e}")
                result = None
            GLib.idle_add(self._on_settings_fetched, result)
        threading.Thread(target=_do, daemon=True).start()

    def _on_settings_fetched(self, result):
        try:
            if not result or not isinstance(result, dict) or 'result' not in result:
                return
            data = result['result']
            if not isinstance(data, dict):
                return
            categories = data.get('categories', {})
            if not isinstance(categories, dict):
                return
            categories = self._set_detection_settings(categories)

            if 'pause_on_defect' in data:
                self._set_pause_switch(bool(data['pause_on_defect']))

            for dt in DETECTION_TYPES:
                key = dt['key']
                cat = categories.get(key, {})
                if not isinstance(cat, dict):
                    continue
                if f'{key}_enabled' in self.labels:
                    macro_available = self._is_detection_macro_available(dt)
                    self.labels[f'{key}_enabled'].set_active(bool(cat.get('enabled', False)))
                    self.labels[f'{key}_enabled'].set_sensitive(macro_available)
                if f'{key}_threshold' in self.labels:
                    try:
                        val = float(cat.get('confidence_threshold', dt['default_threshold']))
                        self.labels[f'{key}_threshold'].set_value(max(0.1, min(1.0, val)))
                    except (TypeError, ValueError):
                        pass
                if f'{key}_pause_count' in self.labels:
                    self.labels[f'{key}_pause_count'].set_value(
                        self._get_pause_consecutive_value(cat, dt)
                    )
        except Exception as e:
            logging.exception(f"AI detection: error updating settings UI: {e}")

    def fetch_latest_result(self):
        def _do():
            try:
                result = self._screen.apiclient.send_request("server/ai_detection/history?limit=1")
            except Exception as e:
                logging.exception(f"AI detection: failed to fetch history: {e}")
                result = None
            GLib.idle_add(self._on_history_fetched, result)
        threading.Thread(target=_do, daemon=True).start()

    def _on_history_fetched(self, result):
        try:
            if not result or not isinstance(result, dict) or 'result' not in result:
                return
            data = result['result']
            if not isinstance(data, dict):
                return
            records = data.get('records', [])
            if isinstance(records, list) and records:
                self.last_result = records[0]
                self._update_result_display()
        except Exception as e:
            logging.exception(f"AI detection: error updating history UI: {e}")

    def _update_result_display(self):
        if not self.last_result or not isinstance(self.last_result, dict):
            self.labels['result_text'].set_text(_("No detection results yet"))
            return
        r = self.last_result

        dtype = self._translate_detection_name(r.get('model_name', r.get('defect_type')))
        error_msg = self._get_result_error(r)
        if error_msg:
            escaped_type = GLib.markup_escape_text(dtype)
            escaped_error = GLib.markup_escape_text(error_msg)
            status = (
                f'<span foreground="red">'
                f'{GLib.markup_escape_text(_("Error"))}</span>'
            )
            self.labels['result_text'].set_markup(
                f"<b>{escaped_type}</b>\n{status}\n{escaped_error}"
            )
            return

        defect = bool(r.get('has_defect', False))
        status_label = _("Defect") if defect else _("Normal")
        status_color = "red" if defect else "green"
        escaped_type = GLib.markup_escape_text(dtype)
        status = f'<span foreground="{status_color}">{GLib.markup_escape_text(status_label)}</span>'
        lines = [f"<b>{escaped_type}</b>", status]
        pause_progress = self._format_pause_progress(r)
        if pause_progress:
            lines.append(GLib.markup_escape_text(pause_progress))
        self.labels['result_text'].set_markup("\n".join(lines))

    # ==================== User Actions ====================

    def on_pause_toggled(self, switch, gparam):
        active = switch.get_active()

        def _do():
            try:
                self._screen.apiclient.post_request(
                    "server/ai_detection/settings",
                    json={"pause_on_defect": active})
                GLib.idle_add(self._set_pause_switch, active)
            except Exception as e:
                logging.exception(f"AI detection: failed to update pause_on_defect: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def save_settings(self, widget):
        categories = {}
        for dt in DETECTION_TYPES:
            key = dt['key']
            existing = self.settings.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            try:
                detection_interval = int(existing.get('detection_interval', dt['default_interval']))
            except (TypeError, ValueError):
                detection_interval = dt['default_interval']
            macro_available = self._is_detection_macro_available(dt)
            has_controls = (
                f'{key}_enabled' in self.labels and
                f'{key}_threshold' in self.labels
            )
            if macro_available and has_controls:
                enabled = self.labels[f'{key}_enabled'].get_active()
                confidence_threshold = round(self.labels[f'{key}_threshold'].get_value(), 2)
            else:
                enabled = False
                confidence_threshold = existing.get('confidence_threshold', dt['default_threshold'])
                try:
                    confidence_threshold = round(float(confidence_threshold), 2)
                except (TypeError, ValueError):
                    confidence_threshold = dt['default_threshold']
            pause_count = self._get_pause_consecutive_value(existing, dt)
            if macro_available and f'{key}_pause_count' in self.labels:
                pause_count = int(self.labels[f'{key}_pause_count'].get_value())
            if not macro_available and f'{key}_enabled' in self.labels:
                self.labels[f'{key}_enabled'].set_active(False)
            category = {
                "enabled": enabled,
                "confidence_threshold": confidence_threshold,
                "scheduled": self._get_scheduled_value(existing, dt),
                "detection_interval": max(10, min(300, detection_interval)),
                "pause_consecutive_detections": max(
                    DEFAULT_PAUSE_CONSECUTIVE_DETECTIONS,
                    pause_count
                ),
            }
            categories[key] = category

        def _do():
            try:
                result = self._screen.apiclient.post_request(
                    "server/ai_detection/settings",
                    json={"categories": categories})
                if result:
                    GLib.idle_add(self._set_detection_settings, categories)
                    GLib.idle_add(self._screen.show_popup_message, _("Saved"), 1)
                else:
                    GLib.idle_add(self._screen.show_popup_message, _("Error"), 2)
            except Exception as e:
                logging.exception(f"AI detection: failed to save settings: {e}")
                GLib.idle_add(self._screen.show_popup_message, _("Error"), 2)
        threading.Thread(target=_do, daemon=True).start()

    # ==================== Helpers ====================

    def _set_status_label(self, text, color):
        self.labels['status_label'].set_markup(
            f'<big><b>{GLib.markup_escape_text(_("AI Status"))}:</b> '
            f'<span foreground="{color}">● {GLib.markup_escape_text(text)}</span></big>'
        )

    def _is_detection_enabled(self, defect_type):
        dt = next((d for d in DETECTION_TYPES if d['key'] == defect_type), None)
        if dt is not None and not self._is_detection_macro_available(dt):
            return False
        category = self.settings.get(defect_type)
        if isinstance(category, dict) and 'enabled' in category:
            return bool(category.get('enabled'))
        return dt is not None

    def _set_detection_settings(self, categories):
        if isinstance(categories, dict):
            categories = self._apply_detection_defaults(categories)
            self.settings = categories
            self._screen.update_ai_detection_settings_cache(categories=categories)
            return categories
        return {}

    def _get_gcode_macro_names(self):
        try:
            return {str(macro).casefold() for macro in self._printer.get_gcode_macros()}
        except Exception as e:
            logging.warning(f"AI detection: failed to read gcode macros: {e}")
            return set()

    def _is_detection_macro_available(self, detection_type):
        if not isinstance(detection_type, dict):
            return False
        macro = detection_type.get("macro")
        if not macro:
            return False
        return str(macro).casefold() in self._get_gcode_macro_names()

    def _apply_detection_defaults(self, categories):
        normalized = {
            str(key): dict(value)
            for key, value in categories.items()
            if isinstance(value, dict)
        }
        for dt in DETECTION_TYPES:
            key = dt["key"]
            category = normalized.get(key, {})
            macro_available = self._is_detection_macro_available(dt)
            if not macro_available:
                category["enabled"] = False
            elif "enabled" not in category:
                category["enabled"] = True
            category["pause_consecutive_detections"] = (
                self._get_pause_consecutive_value(category, dt)
            )
            if "scheduled" not in category or "force_scheduled" in dt:
                category["scheduled"] = self._get_scheduled_value(
                    category, dt
                )
            normalized[key] = category
        return normalized

    def _get_scheduled_value(self, category, detection_type):
        if "force_scheduled" in detection_type:
            return bool(detection_type["force_scheduled"])
        default_value = bool(detection_type.get("default_scheduled", False))
        if not isinstance(category, dict):
            return default_value
        return bool(category.get("scheduled", default_value))

    def _get_pause_consecutive_value(self, category, detection_type):
        default_value = detection_type.get(
            "default_pause_consecutive",
            DEFAULT_PAUSE_CONSECUTIVE_DETECTIONS
        )
        if not isinstance(category, dict):
            category = {}
        raw_value = category.get(
            "pause_consecutive_detections",
            default_value
        )
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = default_value
        return max(DEFAULT_PAUSE_CONSECUTIVE_DETECTIONS, value)

    def _normalize_result_payload(self, data):
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def _get_result_error(self, result):
        if not isinstance(result, dict):
            return None
        if result.get("success", True) is not False:
            return None
        error = (
            result.get("error") or
            result.get("message") or
            result.get("last_error") or
            result.get("service_error")
        )
        if error is None:
            return _("AI detection failed")
        return str(error)

    def _translate_detection_name(self, raw_name):
        if raw_name is None:
            return _("Unknown")

        value = str(raw_name)
        normalized = value.strip().casefold()
        for dt in DETECTION_TYPES:
            aliases = [
                dt["key"],
                dt["macro"],
                dt["name"],
                dt["short_name"],
                *dt.get("aliases", []),
            ]
            if normalized in {alias.casefold() for alias in aliases}:
                return _(dt["name"])
        if normalized in LEGACY_DETECTION_ALIASES:
            return _(LEGACY_DETECTION_ALIASES[normalized])
        return value

    def _format_pause_progress(self, result):
        if not isinstance(result, dict):
            return None
        if (
            "consecutive_defects" not in result or
            "pause_consecutive_detections" not in result
        ):
            return None
        try:
            count = int(result.get("consecutive_defects", 0))
            required = int(result.get("pause_consecutive_detections", 0))
        except (TypeError, ValueError):
            return None
        if required < DEFAULT_PAUSE_CONSECUTIVE_DETECTIONS:
            return None
        return _("Pause Count: %(count)d/%(required)d") % {
            "count": max(0, count),
            "required": required,
        }

    def _set_pause_switch(self, active):
        self.pause_on_defect = active
        self._screen.update_ai_detection_settings_cache(pause_on_defect=active)
        try:
            self.labels['pause_switch'].handler_block_by_func(self.on_pause_toggled)
            self.labels['pause_switch'].set_active(active)
            self.labels['pause_switch'].handler_unblock_by_func(self.on_pause_toggled)
        except Exception as e:
            logging.warning(f"AI detection: failed to update pause switch: {e}")
