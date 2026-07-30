import logging
import gi

gi.require_version("Gtk", "3.0")
from math import pi
from gi.repository import Gtk
from ks_includes.multi_material import (
    SWITCH_DATA_OBJECT,
    get_active_channel,
    get_channel_count,
    get_filament_mask,
    get_saved_variables,
    is_channel_loaded,
    parse_material_records,
)
from ks_includes.screen_panel import ScreenPanel

class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)

        logging.info("Init Feed")

        self.max_filament = get_channel_count(self._printer)
        feed_filament_info_var = get_saved_variables(self._printer).get("feed_filament_info")
        if feed_filament_info_var is None and self._screen.klippy_config is not None:
            feed_filament_info_var = self._screen.klippy_config.get(
                "Variables", "feed_filament_info", fallback=""
            )
        self._screen.feed_filament_allinfo = parse_material_records(
            feed_filament_info_var,
            self.max_filament,
        )
        self.da_size = self._gtk.img_scale * 2
        logging.info("%s"%(feed_filament_info_var))

        self.filament_name = [
            f"filament_{channel}"
            for channel in range(1, self.max_filament + 1)
        ]
        self.filament_index = get_filament_mask(self._printer)
        active_channel = get_active_channel(self._printer, default=0)
        first_loaded = next(
            (
                channel for channel in range(1, self.max_filament + 1)
                if is_channel_loaded(self.filament_index, channel)
            ),
            1,
        )
        self.select_filament = self._screen.feed_filament_index = (
            active_channel if active_channel > 0 else first_loaded
        )
        self.filament_device_info = {}

        logging.info(f"feed_filament:{self._screen.feed_filament_index}==>{self._screen.feed_filament_allinfo}")
        self.popover_timeout = None
        self.left_panel = None
        self.popover_device = None
        self.h = self.f = 0
        # self.show_preheat = False
        # self.preheat_options = self._screen._config.get_preheat_options()
        self.labels['filament_box'] = Gtk.Grid()
        self.grid = self._gtk.HomogeneousGrid()
        self._gtk.reset_temp_color()
        self.grid.attach(self.create_left_panel(), 0, 0, 3, 1)
        self.right_buttons = {
            'edit': self._gtk.Button("fine-tune", _("Edit"), "color1"),
            'none': self._gtk.Button(),
        }

        # When printing start in temp_delta mode and only select tools
        # if self._printer.state not in ["printing", "paused"]:
        #     self.show_preheat = True
        #     selection.extend(self._printer.get_temp_devices())
        # elif extra:
        #     selection.append(extra)

        # Select heaters
        # for h in selection:
        #     if h.startswith("temperature_sensor "):
        #         continue
        #     name = h.split()[1] if len(h.split()) > 1 else h
        #     # Support for hiding devices by name
        #     if name.startswith("_"):
        #         continue
        #     if h not in self.active_heaters:
        #         self.select_heater(None, h)

        if self._screen.vertical_mode:
            self.grid.attach(self.create_right_panel(), 0, 3, 1, 1)
        else:
            self.grid.attach(self.create_right_panel(), 3, 0, 1, 1)

        self.content.add(self.grid)

    def activate(self):
        logging.info("feed_filament_box active")
        self.filament_index = get_filament_mask(self._printer, self.filament_index)
        for d in self.filament_name:
            self.update_device(d)
            self.update_channel_presence(d)

    def process_update(self, action, data):
        if action != "notify_status_update" or SWITCH_DATA_OBJECT not in data:
            return
        if "filament_index" in data[SWITCH_DATA_OBJECT]:
            self.filament_index = int(data[SWITCH_DATA_OBJECT]["filament_index"])
            for device in self.filament_name:
                self.update_channel_presence(device)

    def update_channel_presence(self, device):
        if device not in self.filament_device_info:
            return
        channel = self.filament_device_info[device]["index"]
        button = self.filament_device_info[device]["button"]
        context = button.get_style_context()
        context.remove_class("filament_sensor_detected")
        context.remove_class("filament_sensor_empty")
        loaded = is_channel_loaded(self.filament_index, channel)
        if loaded is True:
            context.add_class("filament_sensor_detected")
        elif loaded is False:
            context.add_class("filament_sensor_empty")

    def edit_event(self, widget, event):
        if self.select_filament is not None:
            logging.info(f"edit filament_{self.select_filament}")
            self._screen.feed_filament_index = self.select_filament
            filament_info = self._screen.feed_filament_allinfo[self.select_filament]
            logging.info(f"Passing filament info: {filament_info}")
            self._screen.show_panel("feed_filament_info", _("Filament Info"))

    def load_event(self, widget, event):
        if self.select_filament is not None:
            logging.info(f"load filament_{self.select_filament}")
        the_str = "ACTIVE_FIALMENT S=%d" % self._screen.feed_filament_index
        # self._screen._ws.klippy.gcode_script(the_str)
        logging.info(the_str)

    def unload_event(self, widget, event):
        if self.select_filament is not None:
            logging.info(f"unload filament_{self.select_filament}")
        self._screen._ws.klippy.gcode_script("_FEEDSYS_RETRACT_FILAMENT")
        logging.info("")

    def select_filament_event(self, widget, event, device):
        device_index = device[9:] if device[9:] else None
        if device_index is None:
            return
        if device in self.filament_device_info:
            if int(self.select_filament) != int(self.filament_device_info[device]["index"]):
                self.filament_device_info[device]["button"].get_style_context().add_class("button_active")
                if self.select_filament is not None:
                    previous = self.filament_device_info[f"filament_{self.select_filament}"]["button"]
                    previous.get_style_context().remove_class("button_active")
                else:
                    self.right_buttons['edit'].set_sensitive(True)
                    self.labels['filament_box'].show_all()
                    
                self.select_filament = self._screen.feed_filament_index = int(device_index)
            else:
                logging.info(f"The same {device}")
        else:
            logging.info(f"Not found {device}")

    def update_device(self, device):
        logging.info(f"update device:{device}")
        device_index = device[9:] if device[9:] else None
        filament_msg = self._screen.feed_filament_allinfo[int(device_index)]
        tool_name = f"T{int(device_index) - 1}"
        label_text = "  %s  %s: %s-%s (°C)" % (
            tool_name,
            filament_msg['name'] if filament_msg['name'] is not None else "-",
            filament_msg['min_temp'] if filament_msg['min_temp'] is not None else "?",
            filament_msg['max_temp'] if filament_msg['max_temp'] is not None else "?",
        )
        try:
            color = [
                int(filament_msg['color'][0:2], 16) / 255,
                int(filament_msg['color'][2:4], 16) / 255,
                int(filament_msg['color'][4:6], 16) / 255,
                0.0,
            ]
        except (TypeError, ValueError):
            color = [0,0,0,0]
        logging.info(f"l={label_text}, c={color}")
        if device in self.filament_device_info:
            self.filament_device_info[device]['button'].set_label(label_text)
            self.filament_device_info[device]['color'][:] = color
            self.filament_device_info[device]['color_area'].queue_draw()
        self.labels['filament_box'].show_all()
        return True

    def add_device(self, device):
        logging.info(f"Feed adding device: {device}")
        device_index = device[9:] if device[9:] else None
        if device_index is None:
            logging.info(_("Not found") + f" {device}")
            return
        if device.startswith("filament"):
            image = f"filament-{int(device_index)-1}" if device_index is not None else "filament-0"
            devname = None #f"{device}"
            class_name = "graph_label_heater_bed"
            dev_type = "extrude"
        else:
            return

        device_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        name = self._gtk.Button(image, self.prettify(devname), None, self.bts, Gtk.PositionType.LEFT, 1)
        name.set_alignment(0, .5)
        name.get_style_context().add_class(class_name)
        name.connect('button-release-event', self.select_filament_event, device)
        if int(self.select_filament) == int(device_index):
            name.get_style_context().add_class("button_active")
        else:
            name.get_style_context().remove_class("button_active")

        color_area = Gtk.DrawingArea(width_request=self.da_size, height_request=self.da_size)
        color_box = Gtk.Box()
        color_box.add(color_area)
        
        device_box.set_valign(Gtk.Align.CENTER)
        device_box.add(color_box)
        device_box.add(name)
        self.filament_device_info[device] = {
            'index': int(device_index),
            'vendor': "",
            'type': "",
            'temp': "",
            'button': name,
            'color': [0,0,0,0],
            'color_area': color_area,
        }
        color_area.connect("draw", self.on_draw, self.filament_device_info[device]['color'])
        color_area.queue_draw()
        pos = self.filament_name.index(device)
        self.labels['filament_box'].attach(device_box, 0, pos, 1, 1)
        self.update_channel_presence(device)
        self.labels['filament_box'].show_all()

    def create_left_panel(self):
        scroll = self._gtk.ScrolledWindow(steppers=False)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class('heater-list')
        scroll.add(self.labels['filament_box'])

        self.left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.left_panel.add(scroll)

        for d in self.filament_name:
            self.add_device(d)

        return self.left_panel

    def create_right_panel(self):
        right = self._gtk.HomogeneousGrid()
        right.attach(self.right_buttons['edit'], 0, 0, 1, 1)
        right.attach(self.right_buttons['none'], 0, 1, 1, 1)
        
        if self.select_filament is None:
            self.right_buttons['edit'].set_sensitive(False)
        self.right_buttons['none'].set_sensitive(False)

        self.right_buttons['edit'].connect('button-release-event', self.edit_event)
        self.labels['filament_box'].show_all()
        
        return right


    def on_draw(self, da, ctx, color=None):
        if color is None:
            # color = self.color_data
            logging.info("on_draw, None")
            return
        logging.info(f"on_draw, {color}")
        ctx.set_source_rgb(*self.rgbw_to_rgb(color))
        # Set the size of the rectangle
        width = height = da.get_allocated_width() * .9
        x = da.get_allocated_width() * .05
        # Set the radius of the corners
        radius = width / 2 * 0.2
        ctx.arc(x + radius, radius, radius, pi, 3 * pi / 2)
        ctx.arc(x + width - radius, radius, radius, 3 * pi / 2, 0)
        ctx.arc(x + width - radius, height - radius, radius, 0, pi / 2)
        ctx.arc(x + radius, height - radius, radius, pi / 2, pi)
        ctx.close_path()
        ctx.fill()

    @staticmethod
    def rgb_to_hex(color):
        hex_color = ''
        for value in color:
            int_value = round(value * 255)
            hex_color += hex(int_value)[2:].zfill(2)
        return hex_color.upper()

    @staticmethod
    def rgbw_to_rgb(color):
        # The idea here is to use the white channel as a saturation control
        # The white channel 'washes' the color
        return (
            [color[3] for i in range(3)]  # Special case of only white channel
            if color[0] == 0 and color[1] == 0 and color[2] == 0
            else [color[i] + (1 - color[i]) * color[3] / 3 for i in range(3)]
        )
