import json
import logging
import threading
import time
from datetime import datetime
from io import BytesIO

import gi
import netifaces
import requests

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk, Pango

from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):
    SDK_QR_ENDPOINT = "http://127.0.0.1:8765/app/device/qr-pairing"
    MACHINE_SN_PATH = "/etc/machine_sn"
    REQUEST_TIMEOUT = 12
    TERMS_KEY = "device_qr_terms_agreed"
    AUTO_REFRESH_MARGIN_SECONDS = 30

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self._active = False
        self._request_serial = 0
        self._auto_refresh_timer = None
        self._expiry_timer = None
        self._has_valid_qr = False
        self._last_updated_text = _("Never")
        self.terms_dialog = None

        self.labels["status"] = Gtk.Label()
        self.labels["status"].set_halign(Gtk.Align.CENTER)
        self.labels["status"].set_justify(Gtk.Justification.CENTER)
        self.labels["status"].set_line_wrap(True)
        self.labels["status"].set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.labels["status"].set_max_width_chars(40)

        self.labels["qr_image"] = Gtk.Image()

        self.labels["updated_at"] = Gtk.Label(label=f"{_('Last updated')}: {self._last_updated_text}")
        self.labels["updated_at"].set_halign(Gtk.Align.CENTER)

        self.labels["refresh"] = self._gtk.Button("refresh", None, "color1", self.bts)
        self.labels["refresh"].connect("clicked", self.refresh_qrcode)

        self.labels["network"] = self._gtk.Button("network", None, "color3", self.bts)
        self.labels["network"].connect("clicked", self.open_network_settings)

        button_row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        button_row.pack_start(self.labels["refresh"], False, False, 0)
        button_row.pack_start(self.labels["network"], False, False, 0)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_hexpand(True)
        content_box.set_vexpand(True)
        content_box.set_valign(Gtk.Align.CENTER)
        content_box.set_halign(Gtk.Align.CENTER)
        content_box.set_margin_top(24)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        content_box.pack_start(self.labels["status"], False, False, 0)
        content_box.pack_start(self.labels["qr_image"], True, True, 0)
        content_box.pack_start(self.labels["updated_at"], False, False, 0)
        content_box.pack_start(button_row, False, False, 0)

        self.content.add(content_box)
        self._set_waiting_terms_state()

    def activate(self):
        self._active = True
        self.refresh_qrcode()

    def deactivate(self):
        self._active = False
        self._request_serial += 1
        self._cancel_refresh_timers()
        if self.terms_dialog is not None:
            self._gtk.remove_dialog(self.terms_dialog)
            self.terms_dialog = None

    def refresh_qrcode(self, widget=None):
        self._request_qrcode(auto_refresh=False)

    def _request_qrcode(self, auto_refresh=False):
        self._cancel_auto_refresh_timer()
        self._request_serial += 1
        if not self._has_accepted_terms():
            self._set_waiting_terms_state()
            self._show_terms_dialog()
            return

        request_id = self._request_serial
        preserve_qr = self._has_valid_qr
        self._set_loading_state(_("Checking network connection..."), preserve_qr)
        threading.Thread(
            target=self._load_qrcode,
            args=(request_id, preserve_qr, auto_refresh),
            daemon=True,
        ).start()

    def open_network_settings(self, widget=None):
        self._screen.show_panel("network", _("Network"))

    def _show_terms_dialog(self):
        if self.terms_dialog is not None:
            return

        terms_label = Gtk.Label()
        terms_label.set_halign(Gtk.Align.START)
        terms_label.set_valign(Gtk.Align.START)
        terms_label.set_justify(Gtk.Justification.LEFT)
        terms_label.set_xalign(0)
        terms_label.set_line_wrap(True)
        terms_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        terms_label.set_max_width_chars(48)
        terms_label.set_text("\n\n".join((
            _("Please read and agree before enabling connected features:"),
            _(
                "1. Connected features may send your device serial number, device status, network details, logs, and "
                "any enabled camera images to Mingda and its service providers for pairing, remote access, security, "
                "diagnostics, updates, and support."
            ),
            _(
                "2. If you enable camera or remote viewing, you confirm that you are authorized to capture and share "
                "images at the device location and will comply with applicable privacy and surveillance laws."
            ),
            _(
                "3. Remote control may start, pause, stop, or change a print job and may affect motion, temperature, "
                "and material use. Use it only when the printer and surrounding area are safe."
            ),
            _(
                "4. Online features depend on Internet, cloud, and third-party services and may be interrupted, "
                "delayed, region-limited, or unavailable. The service is provided on an as available basis to the "
                "maximum extent permitted by law."
            ),
            _(
                "5. You must protect your account, password, tokens, and pairing QR code and install updates promptly. "
                "By tapping Agree, this device will store your choice locally; if you do not agree, connected features "
                "will remain unavailable."
            ),
        )))

        message = self._gtk.ScrolledWindow(steppers=False)
        message.set_size_request(self._gtk.width - 30, int(self._gtk.height * 0.55))
        message.add(terms_label)

        buttons = [
            {"name": _("Back"), "response": Gtk.ResponseType.CANCEL},
            {"name": _("Agree"), "response": Gtk.ResponseType.OK},
        ]
        self.terms_dialog = self._gtk.Dialog(
            _("Terms of Use"),
            buttons,
            message,
            self._handle_terms_response,
        )

    def _handle_terms_response(self, dialog, response_id):
        if self.terms_dialog is dialog:
            self.terms_dialog = None
        self._gtk.remove_dialog(dialog)

        if response_id != Gtk.ResponseType.OK:
            self._screen._menu_go_back()
            return

        if not self._screen.set_save_variable(self.TERMS_KEY, True):
            self._set_error_state(_("Failed to save the terms acceptance state."))
            return

        self.refresh_qrcode()

    def _load_qrcode(self, request_id, preserve_qr, auto_refresh):
        device_sn = self._read_machine_sn()
        if not device_sn:
            GLib.idle_add(
                self._finish_request,
                request_id,
                "error",
                _("Unable to read the device serial number."),
                None,
                None,
                preserve_qr,
                auto_refresh,
            )
            return

        if not self._has_local_network():
            GLib.idle_add(
                self._finish_request,
                request_id,
                "error",
                _("No network connection detected."),
                None,
                None,
                preserve_qr,
                auto_refresh,
            )
            return

        GLib.idle_add(self._set_loading_state, _("Requesting device QR code..."), preserve_qr)

        try:
            response = requests.post(
                self.SDK_QR_ENDPOINT,
                timeout=self.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            data, qr_payload = self._validate_response(payload, device_sn)
            png_bytes = self._build_qr_png(qr_payload)
            updated_at = self._format_timestamp(data.get("ts"))
        except requests.exceptions.RequestException as e:
            logging.error(f"Device QR code request failed: {e}")
            GLib.idle_add(
                self._finish_request,
                request_id,
                "error",
                _("QR code service is unreachable."),
                None,
                None,
                preserve_qr,
                auto_refresh,
            )
            return
        except ValueError as e:
            logging.error(f"Device QR code response invalid: {e}")
            GLib.idle_add(
                self._finish_request,
                request_id,
                "error",
                _("Invalid QR code service response."),
                None,
                None,
                preserve_qr,
                auto_refresh,
            )
            return
        except RuntimeError as e:
            logging.error(f"Device QR code generation failed: {e}", exc_info=True)
            GLib.idle_add(
                self._finish_request,
                request_id,
                "error",
                str(e),
                None,
                None,
                preserve_qr,
                auto_refresh,
            )
            return

        GLib.idle_add(
            self._finish_request,
            request_id,
            "success",
            _("QR code is ready. Scan to continue."),
            png_bytes,
            updated_at,
            preserve_qr,
            auto_refresh,
            data.get("exp"),
        )

    def _finish_request(
        self,
        request_id,
        state,
        message,
        png_bytes,
        updated_at,
        preserve_qr,
        auto_refresh,
        expires_at=None,
    ):
        if not self._active or request_id != self._request_serial:
            return False

        self._set_refresh_sensitive(True)

        if state != "success":
            if preserve_qr and self._has_valid_qr:
                if auto_refresh:
                    self._set_status(_("Unable to refresh automatically. The current QR code will expire soon."), "red")
                else:
                    self._set_status(message, "red")
            else:
                self.labels["qr_image"].clear()
                self._has_valid_qr = False
                self._set_status(message, "red")
            return False

        pixbuf = self._png_bytes_to_pixbuf(png_bytes)
        if pixbuf is None:
            self.labels["qr_image"].clear()
            self._has_valid_qr = False
            self._set_status(_("Failed to generate the QR code image."), "red")
            return False

        self.labels["qr_image"].set_from_pixbuf(pixbuf)
        self._has_valid_qr = True
        self._last_updated_text = updated_at
        self.labels["updated_at"].set_text(f"{_('Last updated')}: {self._last_updated_text}")
        self._schedule_auto_refresh(expires_at)
        self._schedule_expiry_timer(expires_at)
        self._set_status(message, "green")
        return False

    def _has_accepted_terms(self):
        value = self._screen.get_save_variable(self.TERMS_KEY, "False")
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _read_machine_sn(self):
        try:
            with open(self.MACHINE_SN_PATH, "r", encoding="utf-8") as sn_file:
                for line in sn_file:
                    value = line.strip()
                    if value and not value.startswith("#"):
                        return value
        except Exception as e:
            logging.error(f"Unable to read machine SN from {self.MACHINE_SN_PATH}: {e}")
        return None

    def _has_local_network(self):
        try:
            gateways = netifaces.gateways()
            default_gateway = gateways.get("default", {}).get(netifaces.AF_INET)
            if default_gateway is None:
                return False
            interface = default_gateway[1]
            if not interface or interface == "lo":
                return False
            addresses = netifaces.ifaddresses(interface)
            return bool(addresses.get(netifaces.AF_INET) or addresses.get(netifaces.AF_INET6))
        except Exception as e:
            logging.error(f"Unable to determine network state: {e}")
            return False

    def _validate_response(self, payload, expected_device_sn):
        if not isinstance(payload, dict):
            raise ValueError("response is not a JSON object")
        if str(payload.get("version")) != "2":
            raise ValueError(f"unsupported QR version: {payload.get('version')}")
        for key in ("sid", "deviceSn", "token", "ts", "exp"):
            value = payload.get(key)
            if key not in payload:
                raise ValueError(f"missing response field: {key}")
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"empty response field: {key}")

        if str(payload.get("deviceSn")).strip() != expected_device_sn:
            raise ValueError("response device serial number does not match this printer")

        try:
            issued_at = int(payload.get("ts"))
            expires_at = int(payload.get("exp"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid QR timestamps") from exc
        if issued_at <= 0 or expires_at <= issued_at:
            raise ValueError("invalid QR expiry")
        if expires_at <= int(time.time() * 1000):
            raise ValueError("QR pairing response is already expired")

        qr_uri = payload.get("uri")
        if isinstance(qr_uri, str) and qr_uri.strip():
            qr_payload = qr_uri.strip()
        else:
            qr_data = payload.get("payload")
            if not isinstance(qr_data, dict):
                raise ValueError("response QR payload is missing")
            if (
                qr_data.get("type") != "mingda3d.printer.bind"
                or str(qr_data.get("version")) != "2"
            ):
                raise ValueError("response QR payload is invalid")
            qr_payload = json.dumps(qr_data, ensure_ascii=False, separators=(",", ":"))

        return payload, qr_payload

    def _build_qr_png(self, payload):
        try:
            import qrcode
        except ImportError as e:
            raise RuntimeError(_("Failed to generate the QR code image.")) from e

        qr_code = qrcode.QRCode(border=4, box_size=1)
        qr_code.add_data(payload)
        qr_code.make(fit=True)
        target_size = self._get_qr_image_size()
        module_count = qr_code.modules_count + qr_code.border * 2
        qr_code.box_size = max(1, target_size // module_count)
        image = qr_code.make_image(fill_color="black", back_color="white").convert("RGB")

        image_bytes = BytesIO()
        image.save(image_bytes, format="PNG")
        return image_bytes.getvalue()

    def _png_bytes_to_pixbuf(self, png_bytes):
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(png_bytes)
            loader.close()
            return loader.get_pixbuf()
        except Exception as e:
            logging.error(f"Unable to convert QR image to pixbuf: {e}")
            return None

    def _get_qr_image_size(self):
        width = int(self._screen.width * (0.65 if self._screen.vertical_mode else 0.38))
        height = int(self._screen.height * (0.42 if self._screen.vertical_mode else 0.58))
        return max(220, min(width, height))

    def _format_timestamp(self, timestamp_ms):
        try:
            timestamp = float(timestamp_ms) / 1000.0
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _set_waiting_terms_state(self):
        self.labels["qr_image"].clear()
        self._has_valid_qr = False
        self._last_updated_text = _("Never")
        self.labels["updated_at"].set_text(f"{_('Last updated')}: {self._last_updated_text}")
        self._set_refresh_sensitive(True)
        self._set_status(_("Waiting for terms acceptance."))

    def _set_loading_state(self, message, preserve_qr=False):
        if not preserve_qr:
            self.labels["qr_image"].clear()
        self._set_refresh_sensitive(False)
        self._set_status(message)
        return False

    def _set_error_state(self, message):
        self.labels["qr_image"].clear()
        self._has_valid_qr = False
        self._set_refresh_sensitive(True)
        self._set_status(message, "red")

    def _seconds_until_expiry(self, expires_at):
        try:
            remaining = (int(expires_at) - int(time.time() * 1000)) / 1000.0
        except (TypeError, ValueError):
            return 1
        return max(1, int(remaining))

    def _schedule_auto_refresh(self, expires_at):
        self._cancel_auto_refresh_timer()
        refresh_after = max(
            1,
            self._seconds_until_expiry(expires_at) - self.AUTO_REFRESH_MARGIN_SECONDS,
        )
        self._auto_refresh_timer = GLib.timeout_add_seconds(refresh_after, self._handle_auto_refresh)

    def _handle_auto_refresh(self):
        self._auto_refresh_timer = None
        if not self._active:
            return False
        self._request_qrcode(auto_refresh=True)
        return False

    def _schedule_expiry_timer(self, expires_at):
        self._cancel_expiry_timer()
        expires_after = self._seconds_until_expiry(expires_at)
        self._expiry_timer = GLib.timeout_add_seconds(expires_after, self._expire_qrcode)

    def _expire_qrcode(self):
        self._expiry_timer = None
        if not self._active or not self._has_valid_qr:
            return False
        self.labels["qr_image"].clear()
        self._has_valid_qr = False
        self._set_refresh_sensitive(True)
        self._set_status(_("QR code expired. Tap Refresh to load a new one."), "red")
        return False

    def _cancel_refresh_timers(self):
        self._cancel_auto_refresh_timer()
        self._cancel_expiry_timer()

    def _cancel_auto_refresh_timer(self):
        if self._auto_refresh_timer is not None:
            GLib.source_remove(self._auto_refresh_timer)
            self._auto_refresh_timer = None

    def _cancel_expiry_timer(self):
        if self._expiry_timer is not None:
            GLib.source_remove(self._expiry_timer)
            self._expiry_timer = None

    def _set_status(self, message, color=None):
        escaped = GLib.markup_escape_text(message)
        if color:
            self.labels["status"].set_markup(f'<span foreground="{color}">{escaped}</span>')
        else:
            self.labels["status"].set_text(message)

    def _set_refresh_sensitive(self, is_sensitive):
        self.labels["refresh"].set_sensitive(is_sensitive)
