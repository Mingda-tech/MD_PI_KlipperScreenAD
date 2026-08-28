import logging
import re
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk, Pango
from ks_includes.KlippyGcodes import KlippyGcodes
from ks_includes.KlippyGtk import find_widget
from ks_includes.icon_tint import resolve_theme_color, tint_pixel_data
from ks_includes.multi_material import (
    SWITCH_DATA_OBJECT,
    get_active_channel,
    get_channel_presence,
    get_channel_count,
    get_feeder_sensors,
    get_filament_mask,
    get_material_record,
    parse_material_records,
)
from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.current_extruder = self._printer.get_stat("toolhead", "extruder")
        self.channel_count = get_channel_count(self._printer)
        self.current_tool = get_active_channel(self._printer, default=0)
        self.filament_mask = get_filament_mask(self._printer)
        self.feeder_sensors = get_feeder_sensors(self._printer)
        self.tool_icon_sources = {}
        self.tool_icon_states = {}
        self.tool_icon_colors = {
            True: resolve_theme_color(self._gtk.themedir, "echo", (54, 117, 84)),
            False: resolve_theme_color(self._gtk.themedir, "error", (152, 30, 31)),
        }
        self.multi_material_enabled = True if self.current_tool > 0 else False
        self.previous_tool = self.current_tool if self.current_tool > 0 else 1

        # Tool commands are zero-based (T0..T9); feeder channels are one-based.
        available_commands = self._printer.available_commands or {}
        self.available_tools = [
            tool for tool in range(self.channel_count)
            if f"T{tool}" in available_commands
        ]
        
        if not self.available_tools and "extruder" in self._printer.get_tools():
            self.available_tools.append(0)
        
        macros = self._printer.get_config_section_list("gcode_macro ")
        self.load_filament = any("LOAD_FILAMENT" in macro.upper() for macro in macros)
        self.unload_filament = any("UNLOAD_FILAMENT" in macro.upper() for macro in macros)

        self.speeds = ['2', '5']
        self.distances = ['10', '25', '50', '100']
        if self.ks_printer_cfg is not None:
            dis = self.ks_printer_cfg.get("extrude_distances", '10, 25, 50, 100')
            if re.match(r'^[0-9,\s]+$', dis):
                dis = [str(i.strip()) for i in dis.split(',')]
                if 1 < len(dis) < 5:
                    self.distances = dis
            vel = self.ks_printer_cfg.get("extrude_speeds", '2, 5')
            if re.match(r'^[0-9,\s]+$', vel):
                vel = [str(i.strip()) for i in vel.split(',')]
                if 1 < len(vel) < 5:
                    self.speeds = vel

        self.distance = int(self.distances[1])
        self.speed = int(self.speeds[1])
        self.buttons = {
            'extrude': self._gtk.Button("extrude", _("Load"), "color4"),
            'load': self._gtk.Button("arrow-down", _("Load"), "color3"),
            'unload': self._gtk.Button("retract", _("Unload"), "color2"),
            'retract': self._gtk.Button("retract", _("Unload"), "color1"),
            'temperature': self._gtk.Button("heat-up", _("Preheat"), "color4"),
            'spoolman': self._gtk.Button("spool", _("Filament Settings"), "color3"),
            'multi_material': self._gtk.Button(
                "multi_material_enabled" if self.current_tool > 0 else "multi_material_disable",
                _("Multi-Material Box"),
                "color3"
            ),
        }
        self.buttons['extrude'].connect("clicked", self.extrude, "+")
        self.buttons['load'].connect("clicked", self.load_unload, "+")
        self.buttons['unload'].connect("clicked", self.extrude, "-")
        self.buttons['retract'].connect("clicked", self.extrude, "-")
        self.buttons['temperature'].connect("clicked", self.menu_item_clicked, {
            "name": "Temperature",
            "panel": "temperature"
        })
        self.buttons['spoolman'].connect("clicked", self.menu_item_clicked, {
            "name": "Filament Settings",
            "panel": "feed_filament_box",
            "params": {"current_tool": self.current_tool}
        })
        self.buttons['multi_material'].connect("clicked", self.toggle_multi_material)
        self.buttons['spoolman'].set_sensitive(self.multi_material_enabled > 0)
        
        # 初始化时设置多材料按钮的文本限制
        self._apply_multi_material_text_limit()

        extgrid = self._gtk.HomogeneousGrid()
        tool_columns = min(len(self.available_tools), 5)
        for position, tool_num in enumerate(self.available_tools):
            extruder = f"extruder{tool_num}"
            self.labels[extruder] = self._gtk.Button(f"filament-{tool_num}", f"T{tool_num}")
            self.labels[extruder].get_style_context().add_class("extrude-tool-status")
            image = find_widget(self.labels[extruder], Gtk.Image)
            if image is not None and image.get_pixbuf() is not None:
                self.tool_icon_sources[tool_num] = image.get_pixbuf().copy()
            self.labels[extruder].connect("clicked", self.change_extruder, tool_num + 1)
            extgrid.attach(
                self.labels[extruder],
                position % tool_columns,
                position // tool_columns,
                1,
                1,
            )
        
        distgrid = Gtk.Grid()
        for j, i in enumerate(self.distances):
            self.labels[f"dist{i}"] = self._gtk.Button(label=i)
            self.labels[f"dist{i}"].connect("clicked", self.change_distance, int(i))
            ctx = self.labels[f"dist{i}"].get_style_context()
            if ((self._screen.lang_ltr is True and j == 0) or
                    (self._screen.lang_ltr is False and j == len(self.distances) - 1)):
                ctx.add_class("distbutton_top")
            elif ((self._screen.lang_ltr is False and j == 0) or
                  (self._screen.lang_ltr is True and j == len(self.distances) - 1)):
                ctx.add_class("distbutton_bottom")
            else:
                ctx.add_class("distbutton")
            if int(i) == self.distance:
                ctx.add_class("distbutton_active")
            distgrid.attach(self.labels[f"dist{i}"], j, 0, 1, 1)

        speedgrid = Gtk.Grid()
        for j, i in enumerate(self.speeds):
            self.labels[f"speed{i}"] = self._gtk.Button(label=i)
            self.labels[f"speed{i}"].connect("clicked", self.change_speed, int(i))
            ctx = self.labels[f"speed{i}"].get_style_context()
            if ((self._screen.lang_ltr is True and j == 0) or
                    (self._screen.lang_ltr is False and j == len(self.speeds) - 1)):
                ctx.add_class("distbutton_top")
            elif ((self._screen.lang_ltr is False and j == 0) or
                  (self._screen.lang_ltr is True and j == len(self.speeds) - 1)):
                ctx.add_class("distbutton_bottom")
            else:
                ctx.add_class("distbutton")
            if int(i) == self.speed:
                ctx.add_class("distbutton_active")
            speedgrid.attach(self.labels[f"speed{i}"], j, 0, 1, 1)

        distbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.labels['extrude_dist'] = Gtk.Label(_("Distance (mm)"))
        distbox.pack_start(self.labels['extrude_dist'], True, True, 0)
        distbox.add(distgrid)
        speedbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.labels['extrude_speed'] = Gtk.Label(_("Speed (mm/s)"))
        speedbox.pack_start(self.labels['extrude_speed'], True, True, 0)
        speedbox.add(speedgrid)

        filament_sensors = self._printer.get_filament_sensors()
        sensors = Gtk.Grid()
        sensors.set_size_request(self._gtk.content_width - 30, -1)
        if len(filament_sensors) > 0:
            sensors.set_column_spacing(5)
            sensors.set_row_spacing(5)
            sensors.set_halign(Gtk.Align.CENTER)
            sensors.set_valign(Gtk.Align.CENTER)
            for s, x in enumerate(filament_sensors):
                name = x[23:].strip()
                self.labels[x] = {
                    'label': Gtk.Label(self.prettify(name)),
                    'switch': Gtk.Switch(),
                    'box': Gtk.Box()
                }
                self.labels[x]['label'].set_halign(Gtk.Align.CENTER)
                self.labels[x]['label'].set_hexpand(True)
                self.labels[x]['label'].set_ellipsize(Pango.EllipsizeMode.END)
                self.labels[x]['switch'].set_property("width-request", round(self._gtk.font_size * 2))
                self.labels[x]['switch'].set_property("height-request", round(self._gtk.font_size))
                self.labels[x]['switch'].connect("notify::active", self.enable_disable_fs, name, x)
                self.labels[x]['box'].pack_start(self.labels[x]['label'], True, True, 10)
                self.labels[x]['box'].pack_start(self.labels[x]['switch'], False, False, 0)
                self.labels[x]['box'].get_style_context().add_class("filament_sensor")
                sensors.attach(self.labels[x]['box'], s % 5, s // 5, 1, 1)

        grid = Gtk.Grid()
        grid.set_column_homogeneous(True)
        grid.attach(extgrid, 0, 0, 4, 1)

        if self._screen.vertical_mode:
            grid.attach(self.buttons['extrude'], 0, 1, 2, 1)
            grid.attach(self.buttons['retract'], 2, 1, 2, 1)
            grid.attach(self.buttons['load'], 0, 2, 2, 1)
            grid.attach(self.buttons['unload'], 2, 2, 2, 1)
            grid.attach(distbox, 0, 3, 4, 1)
            grid.attach(speedbox, 0, 4, 4, 1)
            grid.attach(sensors, 0, 5, 4, 1)
            grid.attach(self.buttons['multi_material'], 0, 6, 4, 1)  # 添加按钮到布局
        else:
            grid.attach(self.buttons['extrude'], 0, 2, 1, 1)
            # grid.attach(self.buttons['load'], 1, 2, 1, 1)
            grid.attach(self.buttons['unload'], 1, 2, 1, 1)
            grid.attach(self.buttons['multi_material'], 2, 2, 1, 1)  # 添加按钮到布局
            # grid.attach(self.buttons['spoolman'], 2, 2, 1, 1)
            grid.attach(self.buttons['temperature'], 3, 2, 1, 1)
            grid.attach(distbox, 0, 3, 2, 1)
            grid.attach(speedbox, 2, 3, 2, 1)
            grid.attach(sensors, 0, 4, 4, 1)

                # 更新所有按钮状态
        self._update_tool_buttons()
        self._update_tool_filament_status()
        self.content.add(grid)

    def _update_tool_buttons(self):
        allow_selection = self.multi_material_enabled and self._printer.state != "printing"
        for tool_num in self.available_tools:
            button = self.labels.get(f"extruder{tool_num}")
            if button is None:
                continue
            button.get_style_context().remove_class("button_active")
            button.set_sensitive(allow_selection)
            if self.current_tool == tool_num + 1:
                button.get_style_context().add_class("button_active")

    def _update_tool_filament_status(self):
        for tool_num in self.available_tools:
            button = self.labels.get(f"extruder{tool_num}")
            if button is None:
                continue
            context = button.get_style_context()
            context.remove_class("extrude-tool-loaded")
            context.remove_class("extrude-tool-empty")
            loaded = get_channel_presence(
                self._printer,
                tool_num + 1,
                self.filament_mask,
                self.feeder_sensors,
            )
            if loaded is True:
                context.add_class("extrude-tool-loaded")
            elif loaded is False:
                context.add_class("extrude-tool-empty")
            self._update_tool_icon(button, tool_num, loaded)

    def _update_tool_icon(self, button, tool_num, loaded):
        if self.tool_icon_states.get(tool_num) is loaded:
            return
        image = find_widget(button, Gtk.Image)
        source = self.tool_icon_sources.get(tool_num)
        if image is None or source is None:
            return
        if loaded is None:
            image.set_from_pixbuf(source)
            self.tool_icon_states[tool_num] = None
            return
        try:
            pixels = tint_pixel_data(
                bytes(source.get_pixels()),
                source.get_width(),
                source.get_height(),
                source.get_rowstride(),
                source.get_n_channels(),
                self.tool_icon_colors[loaded],
            )
            tinted = GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(pixels),
                GdkPixbuf.Colorspace.RGB,
                source.get_has_alpha(),
                source.get_bits_per_sample(),
                source.get_width(),
                source.get_height(),
                source.get_rowstride(),
            )
        except (TypeError, ValueError, GLib.Error) as error:
            logging.warning("Unable to tint T%s filament icon: %s", tool_num, error)
            return
        image.set_from_pixbuf(tinted)
        self.tool_icon_states[tool_num] = loaded

    def _set_filament_mask(self, filament_mask):
        try:
            filament_mask = int(filament_mask)
        except (TypeError, ValueError):
            self.filament_mask = None
        else:
            self.filament_mask = filament_mask if filament_mask >= 0 else None
        self._update_tool_filament_status()

    def _set_active_tool(self, active_tool):
        try:
            active_tool = int(active_tool)
        except (TypeError, ValueError):
            return
        if not 0 <= active_tool <= self.channel_count:
            logging.warning("Ignoring invalid active feeder channel: %s", active_tool)
            return

        if self.current_tool > 0 and active_tool != self.current_tool:
            self.previous_tool = self.current_tool
        self.current_tool = active_tool
        self.multi_material_enabled = active_tool > 0
        self._update_multi_material_icon(self.multi_material_enabled)
        self.buttons['spoolman'].set_sensitive(self.multi_material_enabled)
        self._update_tool_buttons()

    def _get_saved_material_records(self):
        raw_value = None
        save_variables = self._printer.get_stat("save_variables", "variables")
        if isinstance(save_variables, dict):
            raw_value = save_variables.get("feed_filament_info")
        if raw_value is None and self._screen.klippy_config is not None:
            raw_value = self._screen.klippy_config.get(
                "Variables", "feed_filament_info", fallback=""
            )
        return parse_material_records(raw_value, self.channel_count)

    def _get_extrusion_temperatures(self):
        profile = get_material_record(
            self._printer,
            self.current_tool,
            self._get_saved_material_records(),
        )
        try:
            minimum = float(profile["min_temp"])
        except (TypeError, ValueError):
            minimum = 190
        try:
            target = float(profile["max_temp"])
        except (TypeError, ValueError):
            target = 240

        extruder_config = self._printer.get_config_section(self.current_extruder)
        if isinstance(extruder_config, dict):
            try:
                minimum = max(minimum, float(extruder_config.get("min_extrude_temp", minimum)))
            except (TypeError, ValueError):
                pass
            try:
                target = min(target, float(extruder_config.get("max_temp", target + 1)) - 1)
            except (TypeError, ValueError):
                pass
        return round(minimum), round(max(minimum, target))

    def _apply_multi_material_text_limit(self):
        """为横屏模式下的multi_material按钮设置文本限制"""
        if not self._screen.vertical_mode and 'multi_material' in self.buttons:
            label = find_widget(self.buttons['multi_material'], Gtk.Label)
            if label:
                # 对所有语言统一限制为12字符，确保界面一致性
                label.set_max_width_chars(12)
                label.set_ellipsize(Pango.EllipsizeMode.END)
    
    def _update_multi_material_icon(self, enabled):
        """更新多材料按钮的图标，不破坏按钮结构"""
        image = find_widget(self.buttons['multi_material'], Gtk.Image)
        if image:
            icon_name = "multi_material_enabled" if enabled else "multi_material_disable"
            # 直接更新图片的pixbuf，而不是替换整个图片对象
            new_pixbuf = self._gtk.Image(icon_name, self._gtk.img_scale * self._gtk.button_image_scale, 
                                         self._gtk.img_scale * self._gtk.button_image_scale).get_pixbuf()
            if new_pixbuf:
                image.set_from_pixbuf(new_pixbuf)

    def enable_buttons(self, enable):
        for button in self.buttons:
            if button in ("temperature", "spoolman"):
                continue
            self.buttons[button].set_sensitive(enable)

    def activate(self):
        self._set_active_tool(get_active_channel(self._printer, default=self.current_tool))
        self._set_filament_mask(get_filament_mask(self._printer, self.filament_mask))
        if self._printer.state == "printing":
            self.enable_buttons(False)
            for tool_num in self.available_tools:
                self.labels[f"extruder{tool_num}"].set_sensitive(False)

    def deactivate(self):
        # Firmware persists the channel only after a successful switch.
        pass

    def process_update(self, action, data):
        if action != "notify_status_update":
            return
            
        for x in self._printer.get_tools():
            if x in data:
                self.update_temp(
                    x,
                    self._printer.get_dev_stat(x, "temperature"),
                    self._printer.get_dev_stat(x, "target"),
                    self._printer.get_dev_stat(x, "power"),
                    lines=2,
                )

        if SWITCH_DATA_OBJECT in data:
            switch_data = data[SWITCH_DATA_OBJECT]
            if "active_tools" in switch_data:
                self._set_active_tool(switch_data["active_tools"])
            if "filament_index" in switch_data:
                self._set_filament_mask(switch_data["filament_index"])
        if any(sensor in data for sensor in self.feeder_sensors.values()):
            self._update_tool_filament_status()
        if "save_variables" in data:
            variables = data["save_variables"].get("variables", {})
            if "feed_system_active_tool" in variables:
                self._set_active_tool(variables["feed_system_active_tool"])

        for x in self._printer.get_filament_sensors():
            if x in data:
                if 'enabled' in data[x]:
                    self._printer.set_dev_stat(x, "enabled", data[x]['enabled'])
                    self.labels[x]['switch'].set_active(data[x]['enabled'])
                if 'filament_detected' in data[x]:
                    self._printer.set_dev_stat(x, "filament_detected", data[x]['filament_detected'])
                    if self._printer.get_stat(x, "enabled"):
                        if data[x]['filament_detected']:
                            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_empty")
                            self.labels[x]['box'].get_style_context().add_class("filament_sensor_detected")
                        else:
                            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_detected")
                            self.labels[x]['box'].get_style_context().add_class("filament_sensor_empty")
                logging.info(f"{x}: {self._printer.get_stat(x)}")

    def change_distance(self, widget, distance):
        logging.info(f"### Distance {distance}")
        self.labels[f"dist{self.distance}"].get_style_context().remove_class("distbutton_active")
        self.labels[f"dist{distance}"].get_style_context().add_class("distbutton_active")
        self.distance = distance

    def change_extruder(self, widget, tool_num):
        if 0 < tool_num <= self.channel_count:
            logging.info("Requesting feeder channel %s", tool_num)
            if self.current_tool > 0:
                self.previous_tool = self.current_tool
            self._screen._send_action(widget, "printer.gcode.script",
                                      {"script": f"T{tool_num - 1}"})

    def change_speed(self, widget, speed):
        logging.info(f"### Speed {speed}")
        self.labels[f"speed{self.speed}"].get_style_context().remove_class("distbutton_active")
        self.labels[f"speed{speed}"].get_style_context().add_class("distbutton_active")
        self.speed = speed

    def extrude(self, widget, direction):
        temp = self._printer.get_dev_stat(self.current_extruder, "temperature")
        minimum_temp, target_temp = self._get_extrusion_temperatures()
        if temp is None or temp < minimum_temp:
            script = {"script": f"M104 S{target_temp}"}
            self._screen._confirm_send_action(None,
                                              _("The nozzle temperature is too low, Are you sure you want to heat it?")
                                              + f"\n\n{target_temp} °C",
                                              "printer.gcode.script", script, save_button=False)
        else:
            self._screen._ws.klippy.gcode_script(KlippyGcodes.EXTRUDE_REL)
            if direction == "-":
                self._screen._ws.klippy.gcode_script("_FEEDSYS_RETRACT_FILAMENT")
            else:
                self._screen._send_action(widget, "printer.gcode.script",
                                  {"script": f"G1 E{direction}{self.distance} F{self.speed * 60}"})


    def load_unload(self, widget, direction):
        if direction == "-":
            if not self.unload_filament:
                self._screen.show_popup_message("Macro UNLOAD_FILAMENT not found")
            else:
                self._screen._send_action(widget, "printer.gcode.script",
                                          {"script": f"UNLOAD_FILAMENT SPEED={self.speed * 60}"})
        if direction == "+":
            if not self.load_filament:
                self._screen.show_popup_message("Macro LOAD_FILAMENT not found")
            else:
                self._screen._send_action(widget, "printer.gcode.script",
                                          {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"})

    def enable_disable_fs(self, switch, gparams, name, x):
        if switch.get_active():
            self._printer.set_dev_stat(x, "enabled", True)
            self._screen._ws.klippy.gcode_script(f"SET_FILAMENT_SENSOR SENSOR={name} ENABLE=1")
            if self._printer.get_stat(x, "filament_detected"):
                self.labels[x]['box'].get_style_context().add_class("filament_sensor_detected")
            else:
                self.labels[x]['box'].get_style_context().add_class("filament_sensor_empty")
        else:
            self._printer.set_dev_stat(x, "enabled", False)
            self._screen._ws.klippy.gcode_script(f"SET_FILAMENT_SENSOR SENSOR={name} ENABLE=0")
            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_empty")
            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_detected")

    def toggle_multi_material(self, widget):
        if not self.multi_material_enabled:
            label = Gtk.Label()
            label.set_markup(_("Do you want to enable multi-material box?"))
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_halign(Gtk.Align.CENTER)
            label.set_valign(Gtk.Align.CENTER)
            
            buttons = [
                {"name": _("Yes"), "response": Gtk.ResponseType.YES},
                {"name": _("No"), "response": Gtk.ResponseType.NO}
            ]
            
        else:
            # 如果当前是启用状态，显示清理喉管提示
            label = Gtk.Label()
            label.set_markup(_("Do you want to use external filament?"))
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_halign(Gtk.Align.CENTER)
            label.set_valign(Gtk.Align.CENTER)
            
            buttons = [
                {"name": _("Continue"), "response": Gtk.ResponseType.OK},
                {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL}
            ]
        
        dialog = self._gtk.Dialog(
            self._screen,
            buttons,
            label,
            self._handle_multi_material_toggle
        )
        dialog.set_title(_("Multi-Material") if not self.multi_material_enabled else _("External Filament"))

    def _handle_multi_material_toggle(self, widget, response):
        if widget:
            self._gtk.remove_dialog(widget)
            
        if self.multi_material_enabled:
            if response == Gtk.ResponseType.OK:
                if self.current_tool > 0:
                    self.previous_tool = self.current_tool
                logging.info("Requesting external filament mode")
                self._screen._ws.klippy.gcode_script("ACTIVE_FIALMENT S=0")
        else:
            if response == Gtk.ResponseType.YES:
                target_tool = (
                    self.previous_tool
                    if 0 < self.previous_tool <= self.channel_count
                    else 1
                )
                logging.info("Requesting multi-material channel %s", target_tool)
                self.change_extruder(None, target_tool)
