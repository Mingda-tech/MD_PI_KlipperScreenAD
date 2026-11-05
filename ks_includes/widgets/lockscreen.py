"""Touch-friendly PIN entry and the local screen lock surface."""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from ks_includes.screen_lock import is_valid_pin, is_valid_pin_hash, verify_pin


def lock_label(text, style=None):
    label = Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
    label.set_justify(Gtk.Justification.CENTER)
    if style:
        label.get_style_context().add_class(style)
    return label


class PinPad(Gtk.Box):
    """A PIN is kept as text so leading zeroes are preserved."""

    def __init__(self, screen, submit_callback, submit_label=None):
        compact = min(getattr(screen, "width", 800), getattr(screen, "height", 480)) < 400
        font_size = getattr(screen.gtk, "font_size", 16)
        gap = 4 if compact else max(6, round(font_size * .45))
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=gap)
        self.screen = screen
        self.submit_callback = submit_callback
        self._pin = ""
        self._enabled = True
        self.get_style_context().add_class("pin-pad")
        if compact:
            self.get_style_context().add_class("pin-pad-compact")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.entry = Gtk.Entry(
            editable=False, visibility=False, max_length=6,
            input_purpose=Gtk.InputPurpose.PIN, halign=Gtk.Align.FILL,
        )
        self.entry.set_alignment(0.5)
        self.entry.set_width_chars(8)
        self.entry.set_invisible_char("\u25cf")
        self.entry.set_placeholder_text(_("4-6 digit PIN"))
        self.entry.get_accessible().set_name(_("PIN"))
        self.entry.connect("key-press-event", self._entry_key)
        self.pack_start(self.entry, False, False, 0)

        key_gap = 4 if compact else max(4, round(font_size * .35))
        self.grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True,
                             column_spacing=key_gap, row_spacing=key_gap)
        self.grid.set_hexpand(True)
        self.grid.set_vexpand(True)
        for index, digit in enumerate("123456789"):
            self._add_key(digit, self._digit, index % 3, index // 3, digit)
        self._add_key(_("Clear"), self._clear_clicked, 0, 3)
        self._add_key("0", self._digit, 1, 3, "0")
        self._add_key(_("Backspace"), self._backspace_clicked, 2, 3)
        self.pack_start(self.grid, True, True, 0)
        self.submit = Gtk.Button()
        self.submit.set_size_request(-1, 44)
        self.submit.add(lock_label(submit_label or _("Unlock")))
        self.submit.get_accessible().set_name(submit_label or _("Unlock"))
        self.submit.get_style_context().add_class("pin-submit")
        self.submit.connect("clicked", self._submit)
        self.pack_start(self.submit, False, False, 0)
        self._sync()

    def _add_key(self, label, callback, col, row, *args):
        button = Gtk.Button(hexpand=True, vexpand=True)
        button.get_style_context().add_class("pin-digit" if args else "pin-edit")
        if callback == self._backspace_clicked:
            size = int(getattr(self.screen.gtk, "font_size", 16) * 1.3)
            button.add(self.screen.gtk.Image("backspace", size, size))
        else:
            button.add(lock_label(label))
        button.get_accessible().set_name(label)
        button.set_can_focus(False)
        button.connect("clicked", callback, *args)
        self.grid.attach(button, col, row, 1, 1)

    def get_pin(self):
        return self._pin

    def clear(self):
        self._pin = ""
        self._sync()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.grid.set_sensitive(self._enabled)
        self._sync()

    def _sync(self):
        self.entry.set_text(self._pin)
        self.submit.set_sensitive(self._enabled and is_valid_pin(self._pin))

    def _digit(self, widget, digit):
        if self._enabled and len(self._pin) < 6:
            self._pin += digit
            self._sync()
            self.entry.grab_focus()

    def _clear_clicked(self, widget=None):
        if self._enabled:
            self.clear()

    def _backspace_clicked(self, widget=None):
        if self._enabled:
            self._pin = self._pin[:-1]
            self._sync()

    def _submit(self, widget=None):
        if self._enabled and is_valid_pin(self._pin):
            self.submit_callback(self._pin)

    def _entry_key(self, widget, event):
        return self.handle_key(event)

    def handle_key(self, event):
        name = Gdk.keyval_name(event.keyval) or ""
        if len(name) == 1 and name in "0123456789":
            self._digit(None, name)
        elif name.startswith("KP_") and name[3:] in tuple("0123456789"):
            self._digit(None, name[3:])
        elif name == "BackSpace":
            self._backspace_clicked()
        elif name in ("Delete", "KP_Delete", "Escape"):
            self._clear_clicked()
        elif name in ("Return", "KP_Enter"):
            self._submit()
        # Never let a PIN entry become a path to navigation or native paste.
        return True


class LockScreen(Gtk.Box):
    def __init__(self, screen):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.screen = screen
        self._request = 0
        self._busy = False
        self._message = ""
        self._error = False
        self.get_style_context().add_class("local-lock-screen")
        self.set_hexpand(True)
        self.set_vexpand(True)
        font_size = getattr(screen.gtk, "font_size", 16)
        compact = min(getattr(screen, "width", 800), getattr(screen, "height", 480)) < 400
        if compact:
            self.get_style_context().add_class("lock-compact")
        orientation = Gtk.Orientation.VERTICAL if screen.vertical_mode else Gtk.Orientation.HORIZONTAL
        normal = Gtk.Box(orientation=orientation, spacing=8 if compact else round(font_size * .9),
                         homogeneous=not screen.vertical_mode)
        # Keep ultrawide displays from turning the PIN form into a wide strip.
        padding = font_size if compact else font_size * 2
        width = min(getattr(screen, "width", 800) - padding, font_size * 48)
        normal.set_size_request(max(1, int(width)), -1)
        normal.set_halign(Gtk.Align.CENTER)
        if screen.vertical_mode:
            self.get_style_context().add_class("lock-portrait")
        context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8 if compact else round(font_size * .8),
                          valign=Gtk.Align.CENTER)
        context.get_style_context().add_class("local-lock-context")
        context.set_hexpand(True)
        symbol = Gtk.Box(halign=Gtk.Align.CENTER)
        symbol.get_style_context().add_class("local-lock-symbol")
        size = int(getattr(screen.gtk, "font_size", 16) * (1.8 if compact else 2.4))
        symbol.pack_start(screen.gtk.Image("lock", size, size), False, False, 0)
        context.pack_start(symbol, False, False, 0)
        context.pack_start(lock_label(_("Screen locked"), "local-lock-title"), False, False, 0)
        note = lock_label(_("Printing continues while the screen is locked."), "local-lock-note")
        note.set_max_width_chars(28)
        context.pack_start(note, False, False, 0)
        normal.pack_start(context, False if screen.vertical_mode else True, True, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4 if compact else round(font_size * .45))
        card.get_style_context().add_class("local-lock-card")
        self.status = lock_label("", "local-lock-status")
        self.status.set_max_width_chars(30)
        status_parent = context if compact and not screen.vertical_mode else card
        status_parent.pack_start(self.status, False, False, 0)
        self.pad = PinPad(screen, self._attempt_unlock)
        card.pack_start(self.pad, True, True, 0)
        normal.pack_start(card, True, True, 0)
        self.pack_start(normal, True, True, 0)
        self.reset()

    def reset(self):
        # Invalidates any PBKDF2 completion queued before a relock/admin lock.
        self._request += 1
        self._busy = False
        self._error = False
        self._message = _("Enter your 4-6 digit PIN to unlock.")
        self.pad.clear()
        self.refresh()
        self.pad.entry.grab_focus()

    def refresh(self):
        policy = self.screen.screen_lock
        cooldown = policy.remaining_cooldown()
        valid_hash = is_valid_pin_hash(policy.pin_hash)
        if not valid_hash:
            text = _("PIN configuration is invalid. Contact the administrator to reset it.")
        elif cooldown:
            text = _("Try again in %d seconds.") % cooldown
        else:
            text = self._message
        self.status.set_text(text)
        style = self.status.get_style_context()
        if self._error or cooldown or not valid_hash:
            style.add_class("lock-feedback-error")
        else:
            style.remove_class("lock-feedback-error")
        self.pad.set_enabled(valid_hash and not self._busy and not cooldown)

    def handle_key(self, event):
        return self.pad.handle_key(event)

    def _attempt_unlock(self, pin):
        policy = self.screen.screen_lock
        if self._busy or policy.remaining_cooldown() or not policy.locked:
            return
        if getattr(self.screen, "device_lock_active", False):
            return
        self._busy = True
        self._error = False
        self._request += 1
        request = self._request
        pin_hash = policy.pin_hash
        self._message = _("Verifying PIN...")
        self.refresh()

        def verify():
            try:
                result = verify_pin(pin, pin_hash)
                failed = False
            except Exception:
                result, failed = False, True
            GLib.idle_add(self._finish_unlock, request, pin_hash, result, failed)

        threading.Thread(target=verify, daemon=True).start()

    def _finish_unlock(self, request, pin_hash, result, failed):
        policy = self.screen.screen_lock
        if request != self._request:
            return False
        self._busy = False
        self.pad.clear()
        if policy.pin_hash != pin_hash or not policy.locked:
            return False
        if getattr(self.screen, "device_lock_active", False):
            self.reset()
            return False
        if failed:
            self._error = True
            self._message = _("Unable to verify PIN. Try again.")
        elif result:
            self.screen.unlock_screen()
            return False
        else:
            self._error = True
            policy.record_failure()
            self._message = _("Incorrect PIN.")
        self.refresh()
        return False
