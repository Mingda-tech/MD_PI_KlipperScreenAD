"""Local PIN setup; credentials never leave the printer."""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ks_includes.screen_lock import hash_pin, is_valid_pin, verify_pin
from ks_includes.screen_panel import ScreenPanel
from ks_includes.widgets.lockscreen import PinPad, lock_label


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title or _("Screen Lock"))
        self._active = False
        self._generation = 0
        self._timer = None
        self._busy = False
        self._stage = "overview"
        self._mode = None
        self._new_pin = ""
        self._form_hash = ""
        self._message = ""
        self.pad = None
        self._status = None
        self._show_overview()

    def activate(self):
        self._active = True
        self._show_overview()
        if self._timer is None:
            self._timer = GLib.timeout_add_seconds(1, self._tick)

    def deactivate(self):
        self._active = False
        self._generation += 1
        self._new_pin = ""
        if self.pad:
            self.pad.clear()
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def cancel_edit(self):
        """Discard credentials and invalidate worker results when a lock takes over."""
        self._generation += 1
        self._new_pin = ""
        if self.pad:
            self.pad.clear()
        if self._stage != "overview":
            self._show_overview()

    def _is_current(self):
        panels = getattr(self._screen, "_cur_panels", [])
        return bool(
            self._active and panels and self._screen.panels.get(panels[-1]) is self
            and not self._screen.is_ui_locked()
        )

    def _clear(self):
        self._generation += 1
        self._busy = False
        self.pad = None
        for child in self.content.get_children():
            self.content.remove(child)
            child.destroy()

    def _button(self, label, callback, style="lock-secondary"):
        """Keep actions centered at their natural width with a font-relative minimum."""
        button = Gtk.Button(hexpand=False, halign=Gtk.Align.CENTER)
        button.set_size_request(int(getattr(self._gtk, "font_size", 16) * 9), 44)
        text = lock_label(label)
        text.set_max_width_chars(18)
        button.add(text)
        button.get_accessible().set_name(label)
        button.get_style_context().add_class("lock-action")
        button.get_style_context().add_class(style)
        button.connect("clicked", callback)
        return button

    @staticmethod
    def _label(text, style):
        label = lock_label(text, style)
        label.set_max_width_chars(42)
        label.set_xalign(0)
        label.set_justify(Gtk.Justification.LEFT)
        return label

    def _show_overview(self, message=""):
        self._clear()
        self._stage = "overview"
        self._mode = None
        self._new_pin = ""
        policy = self._screen.screen_lock
        scroll = self._gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        font_size = getattr(self._gtk, "font_size", 16)
        compact = min(getattr(self._screen, "width", 800), getattr(self._screen, "height", 480)) < 400
        group_gap = 6 if compact else round(font_size * .65)
        card_gap = 5 if compact else round(font_size * .45)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=group_gap)
        box.get_style_context().add_class("lock-settings")
        if compact:
            box.get_style_context().add_class("lock-settings-compact")
        box.set_halign(Gtk.Align.CENTER)
        content_width = getattr(self._gtk, "content_width", getattr(self._screen, "width", 800))
        # Reserve the scroller's 2.5em scrollbar, box padding and content margins.
        width = min(34 * font_size, max(1, content_width - 4.2 * font_size))
        box.set_size_request(int(width), -1)

        credentials = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=card_gap)
        credentials.get_style_context().add_class("lock-settings-card")
        state = self._label(_("PIN lock enabled.") if policy.enabled else _("PIN lock disabled."),
                            "lock-settings-title")
        state.get_style_context().add_class("lock-state-enabled" if policy.enabled else "lock-state-disabled")
        credentials.pack_start(state, False, False, 0)
        credentials.pack_start(self._label(_("With a PIN set, the screen locks at every startup."),
                                           "lock-section-copy"), False, False, 0)
        buttons = Gtk.Box(spacing=4 if compact else round(font_size * .4))
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_homogeneous(True)
        buttons.get_style_context().add_class("lock-settings-actions")
        if policy.enabled:
            buttons.pack_start(self._button(_("Change PIN"), lambda w: self._begin("change"),
                                             "lock-primary"), True, True, 0)
            buttons.pack_start(self._button(_("Disable PIN lock"), lambda w: self._begin("disable"),
                                             "lock-danger"),
                               True, True, 0)
        else:
            buttons.pack_start(self._button(_("Set PIN"), lambda w: self._begin("create"),
                                             "lock-primary"), True, True, 0)
        credentials.pack_start(buttons, False, False, 0)
        box.pack_start(credentials, False, False, 0)

        orientation = Gtk.Orientation.VERTICAL if self._screen.vertical_mode else Gtk.Orientation.HORIZONTAL
        row = Gtk.Box(orientation=orientation, spacing=card_gap)
        row.get_style_context().add_class("lock-settings-card")
        automatic = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3 if compact else round(font_size * .2))
        automatic.pack_start(self._label(_("Automatic screen lock"), "lock-section-title"), False, False, 0)
        automatic.pack_start(self._label(_("Lock after inactivity."), "lock-section-copy"), False, False, 0)
        row.pack_start(automatic, True, True, 0)
        self.timeout_combo = Gtk.ComboBoxText()
        self.timeout_combo.get_style_context().add_class("lock-timeout")
        self.timeout_combo.set_valign(Gtk.Align.CENTER)
        self.timeout_combo.get_accessible().set_name(_("Automatic screen lock"))
        options = (
            (0, _("Never")), (60, _("1 minute")), (120, _("2 minutes")),
            (300, _("5 minutes")), (900, _("15 minutes")),
            (1800, _("30 minutes")), (3600, _("60 minutes")),
        )
        for seconds, label in options:
            self.timeout_combo.append(str(seconds), label)
        self.timeout_combo.set_active_id(str(policy.timeout))
        self.timeout_combo.set_sensitive(policy.enabled)
        self.timeout_combo.connect("changed", self._timeout_changed)
        row.pack_start(self.timeout_combo, False, False, 0)
        box.pack_start(row, False, False, 0)
        immediate = self._button(_("Lock now"), self._screen.lock_screen)
        immediate.get_style_context().add_class("lock-single-action")
        immediate.set_sensitive(policy.enabled)
        box.pack_start(immediate, False, False, 0)
        self._status = self._label(message, "lock-settings-feedback")
        if message:
            box.pack_start(self._status, False, False, 0)
        scroll.add(box)
        self.content.pack_start(scroll, True, True, 0)
        self.content.show_all()

    def _begin(self, mode):
        if not self._is_current():
            return
        self._mode = mode
        self._new_pin = ""
        self._form_hash = self._screen.screen_lock.pin_hash
        self._show_pin_stage("new" if mode == "create" else "current")

    def _show_pin_stage(self, stage, message=None):
        self._clear()
        self._stage = stage
        prompt = {
            "current": _("Enter current PIN."),
            "new": _("Enter a new 4-6 digit PIN."),
            "confirm": _("Enter the new PIN again."),
        }[stage]
        self._message = message or prompt
        portrait = self._screen.vertical_mode
        compact = min(getattr(self._screen, "width", 800), getattr(self._screen, "height", 480)) < 400
        font_size = getattr(self._gtk, "font_size", 16)
        orientation = Gtk.Orientation.VERTICAL if portrait else Gtk.Orientation.HORIZONTAL
        box = Gtk.Box(orientation=orientation, spacing=4 if compact else round(font_size * .9))
        box.get_style_context().add_class("lock-settings")
        box.get_style_context().add_class("lock-setup")
        if compact:
            box.get_style_context().add_class("lock-setup-compact")
        context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3 if compact else round(font_size * .55),
                          valign=Gtk.Align.CENTER)
        context.get_style_context().add_class("lock-setup-context")
        context.set_hexpand(True)
        title = {
            "create": _("Set PIN"),
            "change": _("Change PIN"),
            "disable": _("Disable PIN lock"),
        }[self._mode]
        context.pack_start(lock_label(title, "local-lock-title"), False, False, 0)
        steps = ("current",) if self._mode == "disable" else (
            ("new", "confirm") if self._mode == "create" else ("current", "new", "confirm")
        )
        context.pack_start(lock_label(_("Step %(current)d of %(total)d") % {
            "current": steps.index(stage) + 1, "total": len(steps),
        }, "lock-step"), False, False, 0)
        self._status = lock_label(self._message, "local-lock-status")
        self._status.set_max_width_chars(26)
        context.pack_start(self._status, False, False, 0)
        cancel = self._button(_("Cancel"), lambda w: self._show_overview())
        cancel.get_style_context().add_class("lock-single-action")
        if not portrait:
            context.pack_start(cancel, False, False, 0)
        box.pack_start(context, False if portrait else True, True, 0)
        submit_label = _("Disable PIN lock") if self._mode == "disable" else (
            _("Save") if stage == "confirm" else _("Continue")
        )
        self.pad = PinPad(self._screen, self._submit_pin, submit_label)
        box.pack_start(self.pad, True, True, 0)
        if portrait:
            box.pack_start(cancel, False, False, 0)
        self.content.pack_start(box, True, True, 0)
        self.content.show_all()
        self._refresh_form()
        self.pad.entry.grab_focus()

    def handle_key(self, event):
        if self._stage != "overview" and self.pad is not None:
            return self.pad.handle_key(event)
        return False

    def back(self):
        if self._stage != "overview":
            self._show_overview()
            return True
        return False

    def _tick(self):
        if not self._active:
            self._timer = None
            return False
        self._refresh_form()
        return True

    def _refresh_form(self):
        if self.pad is None:
            return
        cooldown = self._screen.screen_lock.remaining_cooldown()
        self.pad.set_enabled(not self._busy and not cooldown)
        self._status.set_text(_("Try again in %d seconds.") % cooldown if cooldown else self._message)

    def _submit_pin(self, pin):
        if not self._is_current() or self._busy or self._screen.screen_lock.remaining_cooldown():
            return
        if not is_valid_pin(pin):
            return
        if self._screen.screen_lock.pin_hash != self._form_hash:
            self._show_overview(_("PIN settings changed. Start again."))
            return
        if self._stage == "new":
            self._new_pin = pin
            self._show_pin_stage("confirm")
            return
        if self._stage == "confirm" and pin != self._new_pin:
            self.pad.clear()
            self._message = _("PINs do not match. Try again.")
            self._refresh_form()
            return
        self._busy = True
        self._message = _("Verifying PIN...") if self._stage == "current" else _("Saving PIN...")
        self._refresh_form()
        generation = self._generation
        original_hash = self._form_hash
        stage = self._stage

        def work():
            try:
                result = verify_pin(pin, original_hash) if stage == "current" else hash_pin(pin)
                failed = False
            except Exception:
                result, failed = None, True
            GLib.idle_add(self._finish_pin, generation, original_hash, stage, result, failed)

        threading.Thread(target=work, daemon=True).start()

    def _finish_pin(self, generation, original_hash, stage, result, failed):
        if generation != self._generation:
            return False
        self._busy = False
        self.pad.clear()
        if not self._is_current():
            if self._active:
                self.cancel_edit()
            else:
                self._new_pin = ""
            return False
        policy = self._screen.screen_lock
        if policy.pin_hash != original_hash:
            self._show_overview(_("PIN settings changed. Start again."))
            return False
        if failed:
            self._message = _("Unable to verify PIN. Try again.")
            self._refresh_form()
            return False
        if stage == "current":
            if not result:
                policy.record_failure()
                self._message = _("Incorrect PIN.")
                self._refresh_form()
            else:
                policy.reset_attempts()
                if self._mode == "disable":
                    self._save("", 0)
                else:
                    self._show_pin_stage("new")
        else:
            self._new_pin = ""
            self._save(result, policy.timeout)
        return False

    def _save(self, pin_hash, timeout):
        if self._screen.set_screen_lock_config(pin_hash, timeout):
            self._show_overview(_("Lock settings saved."))
        else:
            self._show_overview(_("Unable to save lock settings. Check configuration permissions and try again."))

    def _timeout_changed(self, combo):
        if not self._is_current():
            return
        timeout = combo.get_active_id()
        if timeout is not None:
            self._save(self._screen.screen_lock.pin_hash, int(timeout))
