import logging
import os

import gi
import netifaces

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango

from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):
    initialized = False
    show_setup_navigation = False

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.show_add = False
        self.networks = {}
        self.interface = None
        self.prev_network = None
        self.connecting_dialog = None
        self.update_timeout = None
        self._wifi_ready = False
        self._snapshot_in_progress = False
        self._snapshot_pending = False
        self._connected_ssid = None

        self.network_interfaces = netifaces.interfaces()
        self.wireless_interfaces = [iface for iface in self.network_interfaces if iface.startswith('wl')]
        self.wifi = None
        self.use_network_manager = os.system('systemctl is-active --quiet NetworkManager.service') == 0
        if self.wireless_interfaces:
            logging.info("Found wireless interfaces: %s", self.wireless_interfaces)
            if self.use_network_manager:
                logging.info("Using asynchronous NetworkManager backend")
                from ks_includes.wifi_nm import WifiManager
            else:
                logging.info("Using wpa_cli backend")
                from ks_includes.wifi import WifiManager
            self.wifi = WifiManager(self.wireless_interfaces[0])
        else:
            logging.info(_("No wireless interface has been found"))

        self._update_interface()
        self.labels['networks'] = {}
        self.labels['interface'] = Gtk.Label(hexpand=True)
        self.labels['interface'].set_text(_("Interface") + f': {self.interface}  ')
        self.labels['ip'] = Gtk.Label(hexpand=True)
        self._update_ip_label()

        reload_networks = self._gtk.Button("refresh", None, "color1", self.bts)
        reload_networks.connect("clicked", self.reload_networks)
        reload_networks.set_hexpand(False)

        sbox = Gtk.Box(hexpand=True, vexpand=False)
        if self.show_setup_navigation:
            self.labels['back'] = self._gtk.Button("arrow-left", None, "color1", .66)
            self.labels['back'].connect("clicked", self.on_back_click)
            self.labels['next'] = self._gtk.Button("complete", None, "color1", .66)
            self.labels['next'].connect("clicked", self.on_next_click)
            sbox.add(self.labels['back'])
            sbox.add(reload_networks)
            sbox.add(self.labels['next'])
        else:
            sbox.add(self.labels['interface'])
            sbox.add(self.labels['ip'])
            sbox.add(reload_networks)

        scroll = self._gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.labels['networklist'] = Gtk.Grid()

        if self.wifi is not None:
            box.pack_start(sbox, False, False, 5)
            box.pack_start(scroll, True, True, 0)
            scroll.add(self.labels['networklist'])
            self.wifi.add_callback("connected", self.connected_callback)
            self.wifi.add_callback("scan_results", self.scan_callback)
            self.wifi.add_callback("popup", self.popup_callback)
            if self.use_network_manager:
                self.wifi.add_callback("initialized", self.wifi_initialized_callback)
            else:
                self._wifi_ready = bool(self.wifi.initialized)
                if self._wifi_ready:
                    GLib.idle_add(self.request_snapshot)
            self.update_timeout = GLib.timeout_add_seconds(5, self.update_all_networks)
        else:
            self.labels['networkinfo'] = Gtk.Label()
            self.labels['networkinfo'].get_style_context().add_class('temperature_entry')
            box.pack_start(self.labels['networkinfo'], False, False, 0)
            self.update_single_network_info()
            self.update_timeout = GLib.timeout_add_seconds(5, self.update_single_network_info)

        self.content.add(box)
        self.labels['main_box'] = box
        self.initialized = True

    def _update_interface(self):
        gateways = netifaces.gateways()
        if "default" in gateways and netifaces.AF_INET in gateways["default"]:
            self.interface = gateways["default"][netifaces.AF_INET][1]
        else:
            interfaces = [iface for iface in netifaces.interfaces() if iface != 'lo']
            self.interface = interfaces[0] if interfaces else 'lo'

    def _update_ip_label(self):
        try:
            addresses = netifaces.ifaddresses(self.interface)
            ipv4 = addresses.get(netifaces.AF_INET, [])
            self.labels['ip'].set_text(f"IP: {ipv4[0]['addr']}  " if ipv4 else "IP: N/A  ")
        except (ValueError, OSError):
            self.labels['ip'].set_text("IP: N/A  ")

    def _call_wifi(self, method_name, callback, *args):
        if self.use_network_manager:
            getattr(self.wifi, method_name)(*args, callback=callback)
            return
        try:
            result = getattr(self.wifi, method_name)(*args)
            if result is None and method_name in {"connect", "delete_network", "rescan"}:
                result = True
            GLib.idle_add(callback, result, None)
        except Exception as exc:
            GLib.idle_add(callback, None, str(exc))

    def wifi_initialized_callback(self, initialized, error):
        self._wifi_ready = initialized
        if error:
            logging.error("WiFi initialization failed: %s", error)
            self._screen.show_popup_message(f"WiFi initialization failed: {error}")
            return
        self.request_snapshot()

    def _request_snapshot_backend(self, callback):
        if self.use_network_manager:
            self.wifi.get_snapshot(callback)
            return
        try:
            networks = self.wifi.get_networks()
            connected_ssid = self.wifi.get_connected_ssid()
            configured = {
                network["ssid"]
                for network in self.wifi.get_supplicant_networks().values()
            }
            network_info = {}
            for ssid in networks:
                info = dict(self.wifi.get_network_info(ssid) or {})
                info["configured"] = ssid in configured
                network_info[ssid] = info
            snapshot = {
                "networks": networks,
                "network_info": network_info,
                "connected_ssid": connected_ssid,
            }
            GLib.idle_add(callback, snapshot, None)
        except Exception as exc:
            GLib.idle_add(callback, None, str(exc))

    def request_snapshot(self, widget=None):
        if self.wifi is None or not self._wifi_ready:
            return False
        if self._snapshot_in_progress:
            self._snapshot_pending = True
            if widget:
                self._gtk.Button_busy(widget, False)
            return False

        self._snapshot_in_progress = True

        def snapshot_ready(snapshot, error):
            self._snapshot_in_progress = False
            if widget:
                self._gtk.Button_busy(widget, False)
            if error:
                logging.error("Unable to load WiFi networks: %s", error)
            else:
                self._apply_snapshot(snapshot)
            if self._snapshot_pending:
                self._snapshot_pending = False
                self.request_snapshot()

        self._request_snapshot_backend(snapshot_ready)
        return False

    def _apply_snapshot(self, snapshot):
        networks = snapshot.get("networks", [])
        network_info = snapshot.get("network_info", {})
        connected_ssid = snapshot.get("connected_ssid")

        if connected_ssid != self._connected_ssid:
            self._clear_network_rows()
        self._connected_ssid = connected_ssid

        for ssid in list(self.networks):
            if ssid not in networks:
                self.remove_network(ssid, False)

        ordered_networks = sorted(networks)
        if connected_ssid in ordered_networks:
            ordered_networks.remove(connected_ssid)
            ordered_networks.insert(0, connected_ssid)

        for ssid in ordered_networks:
            netinfo = network_info.get(ssid, {"ssid": ssid, "configured": False, "connected": False})
            existing = self.labels['networks'].get(ssid)
            if existing and existing.get("configured") != netinfo.get("configured", False):
                self.remove_network(ssid, False)
                existing = None
            if existing is None:
                self.add_network(ssid, netinfo, connected_ssid, False)
            self.update_network_info(ssid, netinfo, connected_ssid)

        self._update_interface()
        self.labels['interface'].set_text(_("Interface") + f': {self.interface}  ')
        self._update_ip_label()
        self.content.show_all()

    def _clear_network_rows(self):
        for child in list(self.labels['networklist'].get_children()):
            self.labels['networklist'].remove(child)
        self.networks = {}
        self.labels['networks'] = {}

    def add_network(self, ssid, netinfo, connected_ssid, show=True):
        if ssid is None:
            return
        ssid = ssid.strip()
        if not ssid or ssid in self.networks:
            return

        configured = bool(netinfo.get("configured"))
        connected = connected_ssid == ssid or bool(netinfo.get("connected"))
        display_name = _("Hidden") if ssid.startswith("\x00") else ssid
        name = Gtk.Label(hexpand=True, halign=Gtk.Align.START, wrap=True,
                         wrap_mode=Pango.WrapMode.WORD_CHAR)
        if connected:
            name.set_markup(f"<big><b>{display_name} ({_('Connected')})</b></big>")
        else:
            name.set_label(display_name)

        info = Gtk.Label(halign=Gtk.Align.START)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True,
                         halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        labels.add(name)
        labels.add(info)

        connect = self._gtk.Button("load", None, "color3", self.bts)
        connect.connect("clicked", self.connect_network, ssid)
        connect.set_hexpand(False)
        connect.set_halign(Gtk.Align.END)

        delete = self._gtk.Button("delete", None, "color3", self.bts)
        delete.connect("clicked", self.remove_wifi_network, ssid)
        delete.set_hexpand(False)
        delete.set_halign(Gtk.Align.END)

        network = Gtk.Box(spacing=5, hexpand=True, vexpand=False)
        network.get_style_context().add_class("frame-item")
        network.add(labels)
        buttons = Gtk.Box(spacing=5)
        buttons.pack_end(connect, False, False, 0)
        if configured or connected:
            buttons.pack_end(delete, False, False, 0)
        network.add(buttons)

        self.networks[ssid] = network
        position = len(self.networks) - 1
        self.labels['networklist'].insert_row(position)
        self.labels['networklist'].attach(network, 0, position, 1, 1)
        self.labels['networks'][ssid] = {
            "connect": connect,
            "delete": delete,
            "info": info,
            "name": name,
            "row": network,
            "configured": configured,
        }
        if show:
            self.labels['networklist'].show_all()

    def add_new_network(self, widget, ssid):
        self._screen.remove_keyboard()
        password = self.labels['network_psk'].get_text()

        def network_added(result, error):
            if error or not result:
                self._screen.show_popup_message(f"Error adding network {ssid}: {error or 'unknown error'}")
                return
            self.close_add_network()
            self.connect_network(widget, ssid, False)

        self._call_wifi("add_network", network_added, ssid, password)

    def back(self):
        if self.show_add:
            self.close_add_network()
            return True
        return False

    def check_missing_networks(self):
        self.request_snapshot()

    def close_add_network(self):
        if not self.show_add:
            return
        for child in self.content.get_children():
            self.content.remove(child)
        self.content.add(self.labels['main_box'])
        self.content.show()
        for name in ('add_network', 'network_psk'):
            self.labels.pop(name, None)
        self.show_add = False

    def popup_callback(self, message):
        self._screen.show_popup_message(message)

    def connected_callback(self, ssid, previous_ssid):
        logging.info("Connected to WiFi network %s", ssid)
        self.prev_network = previous_ssid
        GLib.timeout_add_seconds(1, self.request_snapshot)

    def _close_connecting_dialog(self):
        if self.connecting_dialog is not None:
            self._gtk.remove_dialog(self.connecting_dialog)
            self.connecting_dialog = None
        if self.wifi is not None:
            self.wifi.remove_callback("connecting_status", self.connecting_status_callback)
        return False

    def _connecting_dialog_response(self, dialog, response_id):
        self.connecting_dialog = None
        self.wifi.remove_callback("connecting_status", self.connecting_status_callback)
        self._gtk.remove_dialog(dialog)

    def connect_network(self, widget, ssid, showadd=True):
        network = self.labels['networks'].get(ssid, {})
        if not network.get("configured", False):
            if showadd:
                self.show_add_network(widget, ssid)
            return

        self.prev_network = self._connected_ssid
        buttons = [{"name": _("Close"), "response": Gtk.ResponseType.CANCEL}]
        scroll = self._gtk.ScrolledWindow()
        self.labels['connecting_info'] = Gtk.Label(
            label=_("Starting WiFi Association"), halign=Gtk.Align.START,
            valign=Gtk.Align.START, wrap=True,
        )
        scroll.add(self.labels['connecting_info'])
        self.connecting_dialog = self._gtk.Dialog(
            _("Starting WiFi Association"), buttons, scroll, self._connecting_dialog_response
        )
        self.wifi.add_callback("connecting_status", self.connecting_status_callback)

        def connect_requested(result, error):
            if error or not result:
                message = error or f"Network {ssid} is not configured"
                self._screen.show_popup_message(f"Connection failed: {message}")
                self._close_connecting_dialog()

        self._call_wifi("connect", connect_requested, ssid)

    def connecting_status_callback(self, message):
        label = self.labels.get('connecting_info')
        if label is not None:
            label.set_text(f"{label.get_text()}\n{message}")
            label.show_all()
        if message in ("Connected", _("Connected"), _("Connection failed")):
            GLib.timeout_add_seconds(2, self._close_connecting_dialog)

    def remove_network(self, ssid, show=True):
        network = self.networks.get(ssid)
        if network is None:
            return
        for row in range(len(self.labels['networklist'].get_children()) + 1):
            if network == self.labels['networklist'].get_child_at(0, row):
                self.labels['networklist'].remove_row(row)
                break
        self.networks.pop(ssid, None)
        self.labels['networks'].pop(ssid, None)
        if show:
            self.labels['networklist'].show_all()

    def remove_wifi_network(self, widget, ssid):
        def network_deleted(result, error):
            if error or not result:
                self._screen.show_popup_message(f"Failed to delete network {ssid}: {error or 'not found'}")
                return
            self.remove_network(ssid)
            self.request_snapshot()

        self._call_wifi("delete_network", network_deleted, ssid)

    def scan_callback(self, new_networks, old_networks):
        self.request_snapshot()

    def show_add_network(self, widget, ssid):
        if self.show_add:
            return
        for child in self.content.get_children():
            self.content.remove(child)
        self.labels.pop('add_network', None)

        label = Gtk.Label(label=_("PSK for") + f' {ssid}', hexpand=False)
        self.labels['network_psk'] = Gtk.Entry(hexpand=True)
        self.labels['network_psk'].connect("activate", self.add_new_network, ssid)
        self.labels['network_psk'].connect("focus-in-event", self._screen.show_keyboard)
        save = self._gtk.Button("sd", _("Save"), "color3")
        save.set_hexpand(False)
        save.connect("clicked", self.add_new_network, ssid)

        entry_box = Gtk.Box()
        entry_box.pack_start(self.labels['network_psk'], True, True, 5)
        entry_box.pack_start(save, False, False, 5)
        self.labels['add_network'] = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=5,
            valign=Gtk.Align.CENTER, hexpand=True, vexpand=True,
        )
        self.labels['add_network'].pack_start(label, True, True, 5)
        self.labels['add_network'].pack_start(entry_box, True, True, 5)
        self.content.add(self.labels['add_network'])
        self.labels['network_psk'].grab_focus_without_selecting()
        self.content.show_all()
        self.show_add = True

    def update_all_networks(self):
        self.request_snapshot()
        return True

    def update_network_info(self, ssid, netinfo, connected_ssid):
        widgets = self.labels['networks'].get(ssid)
        if widgets is None:
            return

        connected = bool(netinfo.get('connected')) or connected_ssid == ssid
        display_name = _("Hidden") if ssid.startswith("\x00") else ssid
        if connected:
            widgets['name'].set_markup(f"<big><b>{display_name} ({_('Connected')})</b></big>")
        else:
            widgets['name'].set_label(display_name)

        info = freq = encryption = channel = level = ipv4 = ipv6 = ""
        if connected:
            try:
                addresses = netifaces.ifaddresses(self.interface)
                if addresses.get(netifaces.AF_INET):
                    ipv4 = f"<b>IPv4:</b> {addresses[netifaces.AF_INET][0]['addr']}"
                if addresses.get(netifaces.AF_INET6):
                    ipv6 = f"<b>IPv6:</b> {addresses[netifaces.AF_INET6][0]['addr'].split('%')[0]}"
            except (ValueError, OSError):
                pass
            info = '<b>' + _("Hostname") + f':</b> {os.uname().nodename}\n{ipv4}\n{ipv6}'
        elif netinfo.get("configured"):
            info = _("Password saved")

        if netinfo.get("encryption") and netinfo['encryption'] != "off":
            encryption = netinfo['encryption'].upper()
        if netinfo.get("frequency"):
            freq = "2.4 GHz" if netinfo['frequency'].startswith("2") else "5 GHz"
        if netinfo.get("channel"):
            channel = _("Channel") + f' {netinfo["channel"]}'
        if "signal_level_dBm" in netinfo:
            unit = "%" if self.use_network_manager else _("dBm")
            level = f"{netinfo['signal_level_dBm']} {unit}"
            icon = self.signal_strength(int(netinfo["signal_level_dBm"]))
            old_icon = widgets.get('icon')
            if old_icon is not None:
                widgets['row'].remove(old_icon)
            widgets['row'].add(icon)
            widgets['row'].reorder_child(icon, 0)
            widgets['icon'] = icon

        widgets['info'].set_markup(f"{info}\n<small>{encryption}  {freq}  {channel}  {level}</small>")
        widgets['row'].show_all()

    def signal_strength(self, signal_level):
        excellent = 77 if self.use_network_manager else -50
        good = 60 if self.use_network_manager else -60
        fair = 35 if self.use_network_manager else -70
        if signal_level > excellent:
            return self._gtk.Image('wifi_excellent')
        if signal_level > good:
            return self._gtk.Image('wifi_good')
        if signal_level > fair:
            return self._gtk.Image('wifi_fair')
        return self._gtk.Image('wifi_weak')

    def update_single_network_info(self):
        self._update_interface()
        try:
            addresses = netifaces.ifaddresses(self.interface)
        except (ValueError, OSError):
            addresses = {}
        ipv4 = addresses.get(netifaces.AF_INET, [{}])[0].get('addr', '')
        ipv6 = addresses.get(netifaces.AF_INET6, [{}])[0].get('addr', '').split('%')[0]
        self.labels['networkinfo'].set_markup(
            f'<b>{self.interface}</b>\n\n'
            + '<b>' + _("Hostname") + f':</b> {os.uname().nodename}\n'
            f'<b>IPv4:</b> {ipv4}\n<b>IPv6:</b> {ipv6}'
        )
        self.labels['networkinfo'].show_all()
        return True

    def _snapshot_after_scan(self, widget):
        self.request_snapshot(widget)
        return False

    def reload_networks(self, widget=None):
        if self.wifi is None or not self._wifi_ready:
            return
        if widget:
            self._gtk.Button_busy(widget, True)

        def scan_complete(result, error):
            if error or not result:
                if widget:
                    self._gtk.Button_busy(widget, False)
                self._screen.show_popup_message(f"WiFi scan failed: {error or 'unknown error'}")
                return
            GLib.timeout_add_seconds(2, self._snapshot_after_scan, widget)

        self._call_wifi("rescan", scan_complete)

    def activate(self):
        if not self.initialized:
            return
        if self.wifi is not None:
            self.request_snapshot()
            if self.update_timeout is None:
                self.update_timeout = GLib.timeout_add_seconds(5, self.update_all_networks)
        elif self.update_timeout is None:
            self.update_timeout = GLib.timeout_add_seconds(5, self.update_single_network_info)

    def deactivate(self):
        if self.update_timeout is not None:
            GLib.source_remove(self.update_timeout)
            self.update_timeout = None
