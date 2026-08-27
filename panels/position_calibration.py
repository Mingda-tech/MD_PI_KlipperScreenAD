import logging
import math

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango

from ks_includes.position_calibration import (
    build_position_command,
    controls_for_state,
    next_state_for_status,
    parse_protocol_response,
    validate_coordinates,
)
from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):
    DISTANCES = ("0.1", "0.5", "1", "5", "10")
    TARGETS = (
        ("PROBE_DEPLOY", "probe-deploy"),
        ("PROBE_STOW", "probe-stow"),
        ("CLEAN", "nozzle-clean"),
        ("FINE_CLEAN", "fine-clean"),
    )

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.state = "idle"
        self.selected_target = None
        self.selected_label = None
        self.distance = "0.1"
        self.position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.saved_position = None
        self.session_started = False
        self.pending_back = False
        self.error_from_state = None
        self.error_code = None
        self.saved_feedback_timeout = None
        self.bounds = self._load_axis_bounds()

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.selection_view = self._build_selection_view()
        self.detail_view = self._build_detail_view()
        self.stack.add_named(self.selection_view, "selection")
        self.stack.add_named(self.detail_view, "detail")
        self.content.add(self.stack)
        self._show_selection()

    def _load_axis_bounds(self):
        bounds = {}
        for axis in ("x", "y", "z"):
            try:
                section = self._printer.get_config_section(f"stepper_{axis}")
                minimum = float(section.get("position_min", 0))
                maximum = float(section["position_max"])
                if math.isfinite(minimum) and math.isfinite(maximum) and minimum <= maximum:
                    bounds[axis] = (minimum, maximum)
            except (KeyError, TypeError, ValueError):
                logging.warning("Unable to load %s movement limits for position calibration", axis.upper())
        return bounds

    def _build_selection_view(self):
        view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        view.get_style_context().add_class("position-calibration-selection")

        heading = Gtk.Label(label=_("Select a position to calibrate"))
        heading.set_halign(Gtk.Align.START)
        heading.get_style_context().add_class("position-calibration-heading")
        view.pack_start(heading, False, False, 0)

        target_grid = self._gtk.HomogeneousGrid()
        self.target_buttons = {}
        for index, (target, icon) in enumerate(self.TARGETS):
            label = self._target_label(target)
            button = self._gtk.Button(icon, label, scale=1.0, lines=1)
            button.get_style_context().add_class("position-calibration-target")
            button.connect("clicked", self._select_target, target, label)
            target_grid.attach(button, index % 2, index // 2, 1, 1)
            self.target_buttons[target] = button
        view.pack_start(target_grid, True, True, 0)

        self.selection_status = Gtk.Label()
        self.selection_status.set_halign(Gtk.Align.START)
        self.selection_status.set_line_wrap(True)
        self.selection_status.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.selection_status.get_style_context().add_class("position-calibration-notice")
        view.pack_end(self.selection_status, False, False, 0)
        return view

    def _build_detail_view(self):
        view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        view.get_style_context().add_class("position-calibration-detail")

        self.step_labels = []
        step_grid = self._gtk.HomogeneousGrid()
        for index, text in enumerate((_("Prepare"), _("Adjust"), _("Save"))):
            label = Gtk.Label(label=text)
            label.get_style_context().add_class("position-calibration-step")
            step_grid.attach(label, index, 0, 1, 1)
            self.step_labels.append(label)
        view.pack_start(step_grid, False, False, 0)

        movement = self._build_movement_grid()
        info = self._build_information_panel()

        distance_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        distance_box.set_hexpand(True)
        distance_label = Gtk.Label(label=_("Move Distance (mm)"))
        distance_box.pack_start(distance_label, False, False, 0)
        distance_grid = Gtk.Grid()
        distance_grid.set_hexpand(True)
        self.distance_buttons = {}
        for index, distance in enumerate(self.DISTANCES):
            button = self._gtk.Button(label=distance, lines=1)
            button.set_direction(Gtk.TextDirection.LTR)
            context = button.get_style_context()
            if (
                (self._screen.lang_ltr and index == 0)
                or (not self._screen.lang_ltr and index == len(self.DISTANCES) - 1)
            ):
                context.add_class("distbutton_top")
            elif (
                (not self._screen.lang_ltr and index == 0)
                or (self._screen.lang_ltr and index == len(self.DISTANCES) - 1)
            ):
                context.add_class("distbutton_bottom")
            else:
                context.add_class("distbutton")
            button.connect("clicked", self._change_distance, distance)
            distance_grid.attach(button, index, 0, 1, 1)
            self.distance_buttons[distance] = button
        distance_box.pack_start(distance_grid, True, True, 0)

        body = Gtk.Grid()
        body.set_hexpand(True)
        body.set_vexpand(True)
        body.set_column_homogeneous(not self._screen.vertical_mode)
        if self._screen.vertical_mode:
            info.set_vexpand(False)
            body.attach(info, 0, 0, 1, 1)
            body.attach(movement, 0, 1, 1, 2)
            body.attach(distance_box, 0, 3, 1, 1)
        else:
            left_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            left_column.set_hexpand(True)
            left_column.set_vexpand(True)
            left_column.pack_start(movement, True, True, 0)
            left_column.pack_end(distance_box, False, False, 0)
            body.attach(left_column, 0, 0, 2, 1)
            body.attach(info, 2, 0, 1, 1)
        view.pack_start(body, True, True, 0)

        self._mark_active_distance()
        return view

    def _build_movement_grid(self):
        grid = self._gtk.HomogeneousGrid()
        grid.get_style_context().add_class("position-calibration-movement")
        self.move_buttons = {}
        specs = (
            ("y+", "arrow-up", "Y+", "Y", 1, 0, "color2"),
            ("x-", "arrow-left", "X−", "X", -1, 1, "color1"),
            ("x+", "arrow-right", "X+", "X", 1, 1, "color1"),
            ("y-", "arrow-down", "Y−", "Y", -1, 2, "color2"),
            ("z+", "z-farther", "Z+", "Z", 1, 0, "color3"),
            ("z-", "z-closer", "Z−", "Z", -1, 2, "color3"),
        )
        positions = {
            "y+": (1, 0),
            "x-": (0, 1),
            "x+": (2, 1),
            "y-": (1, 2),
            "z+": (3, 0),
            "z-": (3, 2),
        }
        for key, icon, label, axis, direction, _row, style in specs:
            button = self._gtk.Button(icon, label, style, scale=0.65, lines=1)
            button.set_can_focus(True)
            button.get_style_context().add_class("position-calibration-control")
            button.connect("clicked", self._move, axis, direction)
            column, row = positions[key]
            grid.attach(button, column, row, 1, 1)
            self.move_buttons[key] = button

        center = Gtk.Label(label="XYZ")
        center.get_style_context().add_class("position-calibration-center")
        grid.attach(center, 1, 1, 1, 1)
        return grid

    def _build_information_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_hexpand(True)
        panel.set_vexpand(True)
        panel.set_valign(Gtk.Align.FILL)
        panel.get_style_context().add_class("position-calibration-info")

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        header.get_style_context().add_class("position-calibration-info-header")
        self.coordinate_title = Gtk.Label(label=_("Saved Position"))
        self.coordinate_title.set_halign(Gtk.Align.START)
        self.coordinate_title.get_style_context().add_class("position-calibration-subheading")
        header.pack_start(self.coordinate_title, False, False, 0)

        coordinates = Gtk.Grid()
        coordinates.set_hexpand(True)
        coordinates.set_column_spacing(8)
        coordinates.set_row_spacing(8)
        coordinates.get_style_context().add_class("position-calibration-coordinate-strip")
        self.coordinate_labels = {}
        for row, axis in enumerate(("x", "y", "z")):
            axis_label = Gtk.Label(label=axis.upper())
            axis_label.set_halign(Gtk.Align.START)
            axis_label.get_style_context().add_class("position-calibration-axis")
            value_label = Gtk.Label(label="—")
            value_label.set_hexpand(True)
            value_label.set_halign(Gtk.Align.END)
            value_label.get_style_context().add_class("position-calibration-coordinate")
            coordinates.attach(axis_label, 0, row, 1, 1)
            coordinates.attach(value_label, 1, row, 1, 1)
            self.coordinate_labels[axis] = value_label
        header.pack_start(coordinates, False, False, 0)
        panel.pack_start(header, False, False, 0)

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        status_box.set_hexpand(True)
        status_box.set_vexpand(True)
        status_box.set_valign(Gtk.Align.CENTER)
        status_box.get_style_context().add_class("position-calibration-status")
        self.status_spinner = Gtk.Spinner()
        self.status_spinner.set_no_show_all(True)
        self.status_spinner.set_halign(Gtk.Align.CENTER)
        self.status_label = Gtk.Label()
        self.status_label.set_halign(Gtk.Align.CENTER)
        self.status_label.set_justify(Gtk.Justification.CENTER)
        self.status_label.set_line_wrap(True)
        self.status_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        status_box.pack_start(self.status_spinner, False, False, 0)
        status_box.pack_start(self.status_label, False, False, 0)
        panel.pack_start(status_box, True, True, 0)

        self.start_button = self._gtk.Button(
            "start", _("Start Calibration"), "color1", scale=0.42, position=Gtk.PositionType.LEFT, lines=1
        )
        self.start_button.set_vexpand(False)
        self.start_button.set_can_focus(True)
        self.start_button.get_style_context().add_class("position-calibration-control")
        self.start_button.get_style_context().add_class("position-calibration-primary-action")
        self.start_button.connect("clicked", self._start_clicked)
        panel.pack_start(self.start_button, False, False, 0)

        actions = self._gtk.HomogeneousGrid()
        actions.set_vexpand(False)
        actions.get_style_context().add_class("position-calibration-actions")
        self.cancel_button = self._gtk.Button(
            "cancel", _("Cancel"), scale=0.4, position=Gtk.PositionType.LEFT, lines=1
        )
        self.cancel_button.set_vexpand(False)
        self.cancel_button.set_can_focus(True)
        self.cancel_button.get_style_context().add_class("position-calibration-control")
        self.cancel_button.connect("clicked", self._cancel_clicked)
        self.save_button = self._gtk.Button(
            "complete", _("Save Position"), "color4", scale=0.4, position=Gtk.PositionType.LEFT, lines=1
        )
        self.save_button.set_vexpand(False)
        self.save_button.set_can_focus(True)
        self.save_button.get_style_context().add_class("position-calibration-control")
        self.save_button.connect("clicked", self._save_clicked)
        actions.attach(self.cancel_button, 0, 0, 1, 1)
        actions.attach(self.save_button, 1, 0, 1, 1)
        panel.pack_end(actions, False, False, 0)
        return panel

    def _printer_is_busy(self):
        print_state = self._printer.get_stat("print_stats", "state")
        return (
            str(print_state or "").lower() in {"printing", "paused"}
            or self._printer.state in {"printing", "paused"}
        )

    @staticmethod
    def _target_label(target):
        return {
            "PROBE_DEPLOY": _("Probe Deploy"),
            "PROBE_STOW": _("Probe Stow"),
            "CLEAN": _("Nozzle Clean"),
            "FINE_CLEAN": _("Fine Clean"),
        }[target]

    @staticmethod
    def _error_message(code):
        messages = {
            "PRINTER_BUSY": _("Printing is active. End the print before calibrating."),
            "ACTIVE_SESSION": _("Another position calibration is active. Use Safe Exit first."),
            "NO_ACTIVE_SESSION": _("The calibration session ended. Return and start again."),
            "POSITION_MISMATCH": _("The selected calibration changed. Return and start again."),
            "INVALID_POSITION": _("This calibration position is not supported."),
            "INVALID_ACTION": _("This calibration action is not supported."),
            "NOT_READY": _("Position calibration is still initializing. Wait a moment and retry."),
            "INVALID_COORDINATES": _("The current coordinates are invalid. Adjust the position and retry."),
            "OUT_OF_RANGE": _("The current position is outside the configured movement range."),
            "SAVE_FAILED": _("The position could not be saved. Check the Klipper log and retry."),
            "CONNECTION_LOST": _("Connection was lost. Reconnect, then use Safe Exit."),
            "MOTION_ERROR": _("Movement failed. Check the machine, then use Safe Exit."),
            "PROTOCOL_ERROR": _("The printer returned an invalid calibration response."),
        }
        return messages.get(code, messages["PROTOCOL_ERROR"])

    def _update_target_availability(self):
        busy = self._printer_is_busy()
        connected = self._printer.state not in {"disconnected", "startup", "shutdown", "error"}
        for button in self.target_buttons.values():
            button.set_sensitive(connected and not busy)
        context = self.selection_status.get_style_context()
        context.remove_class("position-calibration-error")
        if not connected:
            self.selection_status.set_text(_("Printer connection is unavailable."))
            context.add_class("position-calibration-error")
        elif busy:
            self.selection_status.set_text(_("Printing is active. End the print before calibrating."))
            context.add_class("position-calibration-error")
        else:
            self.selection_status.set_text(
                _("The toolhead will move automatically. Clear the movement area before starting.")
            )

    def _select_target(self, _widget, target, label):
        if self._printer_is_busy():
            self._update_target_availability()
            return
        self.selected_target = target
        self.selected_label = label
        self.saved_position = None
        self.session_started = False
        self.pending_back = False
        self.error_code = None
        self._screen.base_panel.set_title(f"{self.title} | {self.selected_label}")
        self.stack.set_visible_child_name("detail")
        self._set_emergency_controls(True)
        self._set_state("querying", _("Loading saved position…"))
        self._send_protocol("QUERY")

    def _show_selection(self, saved=False):
        self.stack.set_visible_child_name("selection")
        self._screen.base_panel.set_title(self.title)
        self._set_emergency_controls(False)
        self.state = "idle"
        self.selected_target = None
        self.selected_label = None
        self.session_started = False
        self.pending_back = False
        self._update_target_availability()
        if saved:
            self.selection_status.set_text(_("Position saved."))
            if self.saved_feedback_timeout is not None:
                GLib.source_remove(self.saved_feedback_timeout)
            self.saved_feedback_timeout = GLib.timeout_add_seconds(3, self._clear_saved_feedback)

    def _clear_saved_feedback(self):
        self.saved_feedback_timeout = None
        if self.state == "idle":
            self._update_target_availability()
        return False

    def _set_emergency_controls(self, calibrating):
        controls = getattr(self._screen.base_panel, "control", {})
        estop = controls.get("estop")
        shutdown = controls.get("shutdown")
        printing = self._printer.state in {"printing", "paused"}
        if estop is not None:
            estop.set_visible(calibrating or printing)
        if shutdown is not None:
            shutdown.set_visible(not (calibrating or printing))

    def _set_state(self, state, status_text=None):
        self.state = state
        if state != "error":
            self.status_label.get_style_context().remove_class("position-calibration-error")
        controls = controls_for_state(state, self.session_started)
        for button in self.move_buttons.values():
            button.set_sensitive(controls["move"])
        for button in self.distance_buttons.values():
            button.set_sensitive(controls["distance"])
        self.save_button.set_sensitive(controls["save"])
        self.cancel_button.set_sensitive(controls["cancel"])
        self.start_button.set_sensitive(controls["start"])

        busy = state in {"querying", "moving", "saving", "cancelling"}
        if busy:
            self.status_spinner.start()
            self.status_spinner.show()
        else:
            self.status_spinner.stop()
            self.status_spinner.hide()

        if state == "ready_to_start":
            self.start_button.set_label(_("Start Calibration"))
            self.coordinate_title.set_text(_("Saved Position"))
        elif state == "moving":
            self.start_button.set_label(_("Moving…"))
        elif state == "adjusting":
            self.coordinate_title.set_text(_("Current Position"))
            self.start_button.set_label(_("Adjust Position"))
        elif state == "saving":
            self.start_button.set_label(_("Saving…"))
        elif state == "cancelling":
            self.start_button.set_label(_("Moving to safety…"))
        elif state == "error":
            self.start_button.set_label(_("Safe Exit") if self.session_started else _("Retry"))

        if status_text is not None:
            self.status_label.set_text(status_text)
        self._update_step_indicator()

    def _update_step_indicator(self):
        if self.state in {"querying", "ready_to_start", "moving"}:
            active = 0
        elif self.state in {"adjusting", "error"} and self.session_started:
            active = 1
        else:
            active = 2
        for index, label in enumerate(self.step_labels):
            context = label.get_style_context()
            context.remove_class("position-calibration-step-active")
            context.remove_class("position-calibration-step-complete")
            if index < active:
                context.add_class("position-calibration-step-complete")
            elif index == active:
                context.add_class("position-calibration-step-active")

    def _mark_active_distance(self):
        for distance, button in self.distance_buttons.items():
            context = button.get_style_context()
            context.remove_class("distbutton_active")
            if distance == self.distance:
                context.add_class("distbutton_active")

    def _change_distance(self, _widget, distance):
        if self.state != "adjusting":
            return
        self.distance = distance
        self._mark_active_distance()

    def _start_clicked(self, _widget):
        if self.state == "error":
            if self.session_started:
                self._cancel_session()
            elif self.error_from_state == "querying":
                self._set_state("querying", _("Loading saved position…"))
                self._send_protocol("QUERY")
            else:
                self._show_start_confirmation()
            return
        if self.state == "ready_to_start":
            self._show_start_confirmation()

    def _show_start_confirmation(self):
        if self._printer_is_busy():
            self._set_error("PRINTER_BUSY")
            return
        message = Gtk.Label()
        message.set_text(
            _(
                "The printer may home and move the toolhead. "
                "Remove printed parts and clear the movement area before continuing."
            )
        )
        message.set_line_wrap(True)
        message.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        message.set_hexpand(True)
        message.set_vexpand(True)
        message.set_halign(Gtk.Align.CENTER)
        message.set_valign(Gtk.Align.CENTER)
        message.set_justify(Gtk.Justification.CENTER)
        buttons = [
            {"name": _("Start Moving"), "response": Gtk.ResponseType.OK},
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL},
        ]
        self._gtk.Dialog(_("Start Position Calibration"), buttons, message, self._start_confirmation_response)

    def _start_confirmation_response(self, dialog, response):
        self._gtk.remove_dialog(dialog)
        if response != Gtk.ResponseType.OK:
            return
        self.session_started = True
        self._set_state("moving", _("Moving to the calibration position. Do not touch the machine."))
        self._send_protocol("START")

    def _move(self, _widget, axis, direction):
        if self.state != "adjusting":
            return
        if self._config.get_config()["main"].getboolean(f"invert_{axis.lower()}", False):
            direction *= -1
        delta = float(self.distance) * direction
        axis_key = axis.lower()
        candidate = dict(self.position)
        candidate[axis_key] = candidate[axis_key] + delta
        valid, reason = validate_coordinates(candidate, self.bounds)
        if not valid:
            logging.warning("Position calibration movement blocked: %s", reason)
            self._show_adjustment_error(_("Movement blocked: the target is outside the configured range."))
            return
        self._clear_adjustment_error()
        self.position = candidate
        self._update_coordinates(candidate)
        self._screen._ws.klippy.gcode_script(f"G91\nG0 {axis}{delta:.3f} F600\nG90")

    def _show_adjustment_error(self, message):
        self.status_label.set_text(message)
        self.status_label.get_style_context().add_class("position-calibration-error")

    def _clear_adjustment_error(self):
        self.status_label.get_style_context().remove_class("position-calibration-error")
        if self.state == "adjusting":
            self.status_label.set_text(_("Use the controls to align the toolhead, then save the position."))

    def _save_clicked(self, _widget):
        if self.state != "adjusting":
            return
        valid, reason = validate_coordinates(self.position, self.bounds)
        if not valid:
            logging.warning("Position calibration save blocked: %s", reason)
            self._show_adjustment_error(_("Cannot save: the current position is outside the configured range."))
            return
        self._set_state("saving", _("Saving and moving the toolhead to safety…"))
        self._send_protocol("SAVE", self.position)

    def _cancel_clicked(self, _widget):
        if self.session_started:
            self._cancel_session()
        else:
            self._show_selection()

    def _cancel_session(self):
        if self.state == "cancelling":
            return
        self._set_state("cancelling", _("Moving the toolhead to safety…"))
        self._send_protocol("CANCEL")

    def _send_protocol(self, action, coordinates=None):
        if self.selected_target is None:
            return
        try:
            command = build_position_command(action, self.selected_target, coordinates)
        except ValueError as error:
            logging.error("Unable to build position calibration command: %s", error)
            self._set_error("PROTOCOL_ERROR")
            return
        self._screen._ws.klippy.gcode_script(command)

    def _update_coordinates(self, coordinates):
        if not coordinates:
            return
        self.position = {axis: float(coordinates[axis]) for axis in ("x", "y", "z")}
        for axis in ("x", "y", "z"):
            self.coordinate_labels[axis].set_text(f"{self.position[axis]:.3f}")

    def _handle_protocol_response(self, response):
        if self.selected_target is None or response["POSITION"] != self.selected_target:
            return
        status = response["STATUS"]
        if status == "ERROR":
            if response.get("CODE") in {"PRINTER_BUSY", "INVALID_POSITION", "INVALID_ACTION"}:
                self.session_started = False
            self._set_error(response.get("CODE", "PROTOCOL_ERROR"))
            return

        next_state = next_state_for_status(self.state, status)
        if next_state == self.state:
            return
        coordinates = response.get("coordinates")
        if coordinates:
            self._update_coordinates(coordinates)
        if status == "POSITION":
            self.saved_position = dict(self.position)
            self._set_state(next_state, _("Review the saved position, then start calibration."))
        elif status == "READY":
            self.session_started = True
            self._set_state(next_state, _("Use the controls to align the toolhead, then save the position."))
        elif status == "SAVED":
            self.session_started = False
            self._set_state(next_state, _("Position saved."))
            GLib.timeout_add(600, self._finish_detail, True)
        elif status == "CANCELLED":
            self.session_started = False
            self._set_state(next_state, _("Calibration cancelled."))
            GLib.timeout_add(250, self._finish_detail, False)

    def _set_error(self, code, message=None):
        self.error_from_state = self.state
        self.error_code = code
        text = message or self._error_message(code)
        self.status_label.get_style_context().add_class("position-calibration-error")
        self._set_state("error", text)

    def _finish_detail(self, saved):
        self.status_label.get_style_context().remove_class("position-calibration-error")
        self._show_selection(saved=saved)
        return False

    def process_update(self, action, data):
        if action == "notify_gcode_response":
            response = parse_protocol_response(data)
            if response is not None:
                self._handle_protocol_response(response)
                return
            if self.session_started and (
                str(data).startswith("!!")
                or "out of range" in str(data).lower()
                or "must home" in str(data).lower()
            ):
                self._set_error("MOTION_ERROR")
            return

        if action != "notify_status_update":
            return
        if self.stack.get_visible_child_name() == "selection":
            self._update_target_availability()
        if self.state == "adjusting" and "gcode_move" in data:
            position = data["gcode_move"].get("gcode_position")
            if position and len(position) >= 3:
                self._update_coordinates({"x": position[0], "y": position[1], "z": position[2]})

    def activate(self):
        if self.stack.get_visible_child_name() == "detail":
            self._set_emergency_controls(True)
        else:
            self._update_target_availability()

    def deactivate(self):
        if self.session_started and self.selected_target and self.state != "cancelling":
            try:
                self._screen._ws.klippy.gcode_script(
                    build_position_command("CANCEL", self.selected_target)
                )
            except ValueError:
                logging.exception("Unable to cancel position calibration while leaving the panel")
        self.session_started = False
        self.selected_target = None
        self.selected_label = None
        self.state = "idle"
        self.stack.set_visible_child_name("selection")
        self._set_emergency_controls(False)

    def back(self):
        if self.stack.get_visible_child_name() != "detail":
            return False
        if self.session_started:
            self.pending_back = True
            self._cancel_session()
        else:
            self._show_selection()
        return True
