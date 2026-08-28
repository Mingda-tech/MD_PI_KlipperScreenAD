import logging
import json
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
from jinja2 import Template
from ks_includes.screen_panel import ScreenPanel


def get_grid_attachments(item_count, columns, expand_last=False):
    """Return (column, row, width, height) tuples for visible menu items."""
    if item_count == 5 and columns in (3, 4):
        # Use six half-width tracks to center a 3 + 2 card layout.
        return [
            (0, 0, 2, 1),
            (2, 0, 2, 1),
            (4, 0, 2, 1),
            (1, 1, 2, 1),
            (3, 1, 2, 1),
        ]

    if columns == 4:
        if item_count <= 4:
            columns = 2
        elif item_count <= 6:
            columns = 3

    attachments = []
    for index in range(item_count):
        column = index % columns
        row = index // columns
        width = 2 if expand_last and index + 1 == item_count and item_count % 2 == 1 else 1
        attachments.append((column, row, width, 1))
    return attachments


class Panel(ScreenPanel):

    def __init__(self, screen, title, items=None):
        super().__init__(screen, title)
        self.items = items
        self.j2_data = self._printer.get_printer_status_data()
        self.create_menu_items()
        self.grid = self._gtk.HomogeneousGrid()
        self.scroll = self._gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    def activate(self):
        self.j2_data = self._printer.get_printer_status_data()
        self.add_content()

    def add_content(self):
        for child in self.scroll.get_children():
            self.scroll.remove(child)
        if self._screen.vertical_mode:
            self.scroll.add(self.arrangeMenuItems(self.items, 3))
        else:
            self.scroll.add(self.arrangeMenuItems(self.items, 4))
        if not self.content.get_children():
            self.content.add(self.scroll)

    def arrangeMenuItems(self, items, columns, expand_last=False):
        for child in self.grid.get_children():
            self.grid.remove(child)

        visible_items = []
        for item in items:
            key = list(item)[0]
            if not self.evaluate_enable(item[key]['enable']):
                logging.debug(f"X > {key}")
                continue
            visible_items.append(key)

        attachments = get_grid_attachments(len(visible_items), columns, expand_last)
        for key, (column, row, width, height) in zip(visible_items, attachments):
            self.grid.attach(self.labels[key], column, row, width, height)
        return self.grid

    def create_menu_items(self):
        count = sum(bool(self.evaluate_enable(i[next(iter(i))]['enable'])) for i in self.items)
        scale = 1.1 if 12 < count <= 16 else None  # hack to fit a 4th row
        for i in range(len(self.items)):
            key = list(self.items[i])[0]
            item = self.items[i][key]

            name = self._screen.env.from_string(item['name']).render(self.j2_data)
            icon = self._screen.env.from_string(item['icon']).render(self.j2_data) if item['icon'] else None
            style = self._screen.env.from_string(item['style']).render(self.j2_data) if item['style'] else None

            b = self._gtk.Button(icon, name, style or f"color{i % 4 + 1}", scale=scale)

            if item['panel']:
                b.connect("clicked", self.menu_item_clicked, item)
            elif item['method']:
                params = {}

                if item['params'] is not False:
                    try:
                        p = self._screen.env.from_string(item['params']).render(self.j2_data)
                        params = json.loads(p)
                    except Exception as e:
                        logging.exception(f"Unable to parse parameters for [{name}]:\n{e}")
                        params = {}

                if item['confirm'] is not None:
                    b.connect("clicked", self._screen._confirm_send_action, item['confirm'], item['method'], params)
                else:
                    b.connect("clicked", self._screen._send_action, item['method'], params)
            else:
                b.connect("clicked", self._screen._go_to_submenu, key)
            self.labels[key] = b

    def evaluate_enable(self, enable):
        if enable == "{{ moonraker_connected }}":
            logging.info(f"moonraker connected {self._screen._ws.connected}")
            return self._screen._ws.connected
        try:
            j2_temp = Template(enable, autoescape=True)
            result = j2_temp.render(self.j2_data)
            return result == 'True'
        except Exception as e:
            logging.debug(f"Error evaluating enable statement: {enable}\n{e}")
            return False
