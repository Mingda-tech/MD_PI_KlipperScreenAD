# Network in KlipperScreen is a connection in NetworkManager
# Interface in KlipperScreen is a device in NetworkManager

import logging
import queue
import threading
import uuid
from contextlib import suppress
from typing import Callable, Optional

import dbus
import gi
from dbus.mainloop.glib import DBusGMainLoop

from ks_includes import NetworkManager
from ks_includes.wifi import WifiChannels

gi.require_version('Gdk', '3.0')
from gi.repository import GLib


class WorkerTask:
    def __init__(self, task_type: str, args=(), callback: Optional[Callable] = None):
        self.task_type = task_type
        self.args = args
        self.callback = callback


class WifiManager:
    """Asynchronous NetworkManager backend.

    All potentially blocking D-Bus operations and all mutable Wi-Fi state are
    owned by one worker thread. Public callbacks always run on the GLib main
    loop and receive ``(result, error)``.
    """

    _STOP_TASK = object()

    def __init__(self, interface_name, *args, **kwargs):
        super().__init__(*args, **kwargs)
        DBusGMainLoop(set_as_default=True)

        self.interface_name = interface_name
        self.wifi_dev = None

        self.known_networks = {}
        self.visible_networks = {}
        self.ssid_by_path = {}
        self.path_by_ssid = {}
        self.hidden_ssid_index = 0

        self._callbacks = {
            "initialized": [],
            "connected": [],
            "connecting_status": [],
            "scan_results": [],
            "popup": [],
        }
        self._callback_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()

        self._connected = False
        self._connected_ssid = None
        self.initialized = False
        self.initialization_error = None
        self._initialization_complete = False

        self._task_queue = queue.Queue()
        self._shutdown_event = threading.Event()
        self._worker_thread = None
        self._start_worker_thread()
        self._submit_task("initialize")

    @property
    def connected(self):
        with self._state_lock:
            return self._connected

    @property
    def connected_ssid(self):
        with self._state_lock:
            return self._connected_ssid

    def _set_connection_state(self, connected, ssid=None):
        with self._state_lock:
            previous_ssid = self._connected_ssid
            self._connected = connected
            self._connected_ssid = ssid if connected else None
        return previous_ssid

    def _start_worker_thread(self):
        with self._lifecycle_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._shutdown_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_thread_func,
                name=f"wifi-{self.interface_name}",
                daemon=True,
            )
            self._worker_thread.start()
            logging.debug("WiFi worker thread started for %s", self.interface_name)

    def _worker_thread_func(self):
        while True:
            task = self._task_queue.get()
            if task is self._STOP_TASK:
                self._task_queue.task_done()
                break

            result = None
            error = None
            try:
                if (
                    task.task_type != "initialize"
                    and not self.initialized
                ):
                    raise RuntimeError(self.initialization_error or "WiFi manager is not initialized")
                result = self._execute_task(task)
            except Exception as exc:
                error = str(exc)
                if task.task_type == "initialize":
                    logging.exception("WiFi initialization failed")
                else:
                    logging.error("WiFi task %s failed: %s", task.task_type, error)

            if task.task_type == "initialize":
                self._finish_initialization(error)

            if task.callback is not None:
                self._schedule_callback(task.callback, result if error is None else None, error)
            self._task_queue.task_done()

        logging.debug("WiFi worker thread stopped for %s", self.interface_name)

    def _execute_task(self, task):
        handlers = {
            "initialize": self._worker_initialize,
            "rescan": self._worker_rescan,
            "get_networks": self._worker_get_networks,
            "get_network_info": self._worker_get_network_info,
            "get_snapshot": self._worker_get_snapshot,
            "connect": self._worker_connect,
            "add_network": self._worker_add_network,
            "delete_network": self._worker_delete_network,
            "get_connected_ssid": self._worker_get_connected_ssid,
            "get_supplicant_networks": self._worker_get_supplicant_networks,
            "update_known_connections": self._worker_update_known_connections,
            "ap_added": self._worker_ap_added,
            "ap_removed": self._worker_ap_removed,
            "state_changed": self._worker_state_changed,
        }
        handler = handlers.get(task.task_type)
        if handler is None:
            raise ValueError(f"Unknown WiFi task: {task.task_type}")
        return handler(*task.args)

    def _finish_initialization(self, error):
        with self._state_lock:
            self.initialized = error is None
            self.initialization_error = error
            self._initialization_complete = True
            with self._callback_lock:
                callbacks = list(self._callbacks["initialized"])
        for callback in callbacks:
            self._schedule_callback(callback, self.initialized, error)

    def _submit_task(self, task_type, callback=None, *args):
        if self._shutdown_event.is_set():
            if callback is not None:
                self._schedule_callback(callback, None, "WiFi manager is shut down")
            return False
        self._task_queue.put(WorkerTask(task_type, args, callback))
        return True

    def shutdown(self):
        with self._lifecycle_lock:
            if self._shutdown_event.is_set():
                return
            self._shutdown_event.set()
            self._task_queue.put(self._STOP_TASK)
            worker = self._worker_thread

        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=5.0)
            if worker.is_alive():
                logging.warning("WiFi worker thread did not stop in time for %s", self.interface_name)

    @staticmethod
    def _invoke_callback(callback, *args):
        try:
            callback(*args)
        except Exception:
            logging.exception("Exception in WiFi callback")
        return False

    def _schedule_callback(self, callback, *args):
        try:
            GLib.idle_add(self._invoke_callback, callback, *args)
        except Exception:
            logging.exception("Unable to schedule WiFi callback on the GLib main loop")

    def _emit(self, name, *args):
        with self._callback_lock:
            callbacks = list(self._callbacks.get(name, ()))
        for callback in callbacks:
            self._schedule_callback(callback, *args)

    def add_callback(self, name, callback):
        if name == "initialized":
            with self._state_lock:
                complete = self._initialization_complete
                initialized = self.initialized
                error = self.initialization_error
                with self._callback_lock:
                    callbacks = self._callbacks[name]
                    if callback in callbacks:
                        return
                    callbacks.append(callback)
            if complete:
                self._schedule_callback(callback, initialized, error)
            return

        with self._callback_lock:
            callbacks = self._callbacks.get(name)
            if callbacks is None or callback in callbacks:
                return
            callbacks.append(callback)

    def remove_callback(self, name, callback):
        with self._callback_lock:
            callbacks = self._callbacks.get(name)
            if callbacks is not None and callback in callbacks:
                callbacks.remove(callback)

    def callback(self, callback_type, message):
        self._emit(callback_type, message)

    # Public asynchronous API. Callbacks receive (result, error).

    def rescan(self, callback=None):
        return self._submit_task("rescan", callback)

    def get_networks(self, callback=None):
        return self._submit_task("get_networks", callback)

    def get_network_info(self, ssid, callback=None):
        return self._submit_task("get_network_info", callback, ssid)

    def get_snapshot(self, callback=None):
        return self._submit_task("get_snapshot", callback)

    def connect(self, ssid, callback=None):
        return self._submit_task("connect", callback, ssid)

    def add_network(self, ssid, psk, callback=None):
        return self._submit_task("add_network", callback, ssid, psk)

    def delete_network(self, ssid, callback=None):
        return self._submit_task("delete_network", callback, ssid)

    def get_connected_ssid(self, callback=None):
        return self._submit_task("get_connected_ssid", callback)

    def get_supplicant_networks(self, callback=None):
        return self._submit_task("get_supplicant_networks", callback)

    def update_known_connections(self, callback=None):
        return self._submit_task("update_known_connections", callback)

    # Worker-only D-Bus operations and state mutation.

    def _worker_initialize(self):
        self.wifi_dev = NetworkManager.NetworkManager.GetDeviceByIpIface(self.interface_name)
        self.wifi_dev.OnAccessPointAdded(self._ap_added)
        self.wifi_dev.OnAccessPointRemoved(self._ap_removed)
        self.wifi_dev.OnStateChanged(self._ap_state_changed)

        self.known_networks = {}
        self.visible_networks = {}
        self.ssid_by_path = {}
        self.path_by_ssid = {}
        for access_point in self.wifi_dev.GetAccessPoints():
            with suppress(NetworkManager.ObjectVanished):
                self._add_ap(access_point)

        self._worker_update_known_connections()
        connected_ssid = self._read_connected_ssid()
        self._set_connection_state(connected_ssid is not None, connected_ssid)
        logging.info(
            "WiFi manager initialized for %s with %d access points",
            self.interface_name,
            len(self.visible_networks),
        )
        return True

    def _worker_update_known_connections(self):
        known_networks = {}
        for connection in NetworkManager.Settings.ListConnections():
            settings = connection.GetSettings()
            if "802-11-wireless" in settings:
                known_networks[settings["802-11-wireless"]["ssid"]] = connection
        self.known_networks = known_networks
        return list(known_networks)

    def _worker_rescan(self):
        self.wifi_dev.RequestScan({})
        return True

    def _worker_get_networks(self):
        return list(set(self.known_networks).union(self.ssid_by_path.values()))

    def _worker_get_network_info(self, ssid, connected_ssid=None):
        netinfo = {
            "ssid": ssid,
            "configured": ssid in self.known_networks,
            "connected": False,
        }
        if connected_ssid is None:
            connected_ssid = self.connected_ssid
        netinfo["connected"] = connected_ssid == ssid

        connection = self.known_networks.get(ssid)
        if connection is not None:
            with suppress(NetworkManager.ObjectVanished):
                settings = connection.GetSettings()
                if settings and "802-11-wireless" in settings:
                    netinfo["ssid"] = settings["802-11-wireless"]["ssid"]

        path = self.path_by_ssid.get(ssid)
        access_point = self.visible_networks.get(path)
        if access_point is None:
            return netinfo

        with suppress(NetworkManager.ObjectVanished):
            frequency = getattr(access_point, "Frequency", None)
            channel = ""
            if frequency:
                with suppress(Exception):
                    channel = WifiChannels.lookup(str(frequency))[1]
            netinfo.update({
                "mac": getattr(access_point, "HwAddress", ""),
                "channel": channel,
                "frequency": str(frequency) if frequency else "",
                "flags": getattr(access_point, "Flags", 0),
                "encryption": self._get_encryption(getattr(access_point, "RsnFlags", 0)),
                "signal_level_dBm": str(getattr(access_point, "Strength", 0)),
            })
        return netinfo

    def _worker_get_snapshot(self):
        connected_ssid = self._read_connected_ssid()
        self._set_connection_state(connected_ssid is not None, connected_ssid)
        networks = self._worker_get_networks()
        return {
            "networks": networks,
            "network_info": {
                ssid: self._worker_get_network_info(ssid, connected_ssid)
                for ssid in networks
            },
            "connected_ssid": connected_ssid,
        }

    def _worker_connect(self, ssid):
        connection = self.known_networks.get(ssid)
        if connection is None:
            return False
        self._emit("connecting_status", f"Connecting to: {ssid}")
        NetworkManager.NetworkManager.ActivateConnection(connection, self.wifi_dev, "/")
        return True

    def _worker_add_network(self, ssid, psk):
        new_connection = {
            "802-11-wireless": {
                "mode": "infrastructure",
                "security": "802-11-wireless-security",
                "ssid": ssid,
            },
            "802-11-wireless-security": {
                "auth-alg": "open",
                "key-mgmt": "wpa-psk",
                "psk": psk,
            },
            "connection": {
                "id": ssid,
                "type": "802-11-wireless",
                "uuid": str(uuid.uuid4()),
            },
            "ipv4": {"method": "auto"},
            "ipv6": {"method": "auto"},
        }
        try:
            NetworkManager.Settings.AddConnection(new_connection)
        except dbus.exceptions.DBusException as exc:
            error = str(exc)
            if "802-11-wireless-security.psk" in error:
                raise ValueError(_("Invalid password")) from exc
            raise
        self._worker_update_known_connections()
        return True

    def _worker_delete_network(self, ssid):
        connection = self.known_networks.get(ssid)
        if connection is None:
            return False
        connection.Delete()
        self._worker_update_known_connections()
        return True

    def _read_connected_ssid(self):
        if self.wifi_dev is None:
            return None
        access_point = self.wifi_dev.SpecificDevice().ActiveAccessPoint
        return access_point.Ssid if access_point else None

    def _worker_get_connected_ssid(self):
        connected_ssid = self._read_connected_ssid()
        self._set_connection_state(connected_ssid is not None, connected_ssid)
        return connected_ssid

    def _worker_get_supplicant_networks(self):
        return {ssid: {"ssid": ssid} for ssid in self.known_networks}

    # D-Bus signal handlers only enqueue work; they never mutate shared state.

    def _ap_added(self, nm, interface, signal, access_point):
        self._submit_task("ap_added", None, access_point)

    def _ap_removed(self, dev, interface, signal, access_point):
        self._submit_task("ap_removed", None, access_point.object_path)

    def _ap_state_changed(self, nm, interface, signal, new_state, old_state, reason):
        self._submit_task("state_changed", None, old_state, new_state, reason)

    def _worker_ap_added(self, access_point):
        with suppress(NetworkManager.ObjectVanished):
            ssid = self._add_ap(access_point)
            self._emit("scan_results", [ssid], [])
            return ssid
        return None

    def _worker_ap_removed(self, path):
        ssid = self.ssid_by_path.get(path)
        if ssid is None:
            return None
        self._remove_ap(path)
        if ssid not in self.path_by_ssid:
            self._emit("scan_results", [], [ssid])
        return ssid

    def _worker_state_changed(self, old_state, new_state, reason):
        messages = {
            NetworkManager.NM_DEVICE_STATE_UNKNOWN: "State is unknown",
            NetworkManager.NM_DEVICE_STATE_UNMANAGED: "Error: Not managed by NetworkManager",
            NetworkManager.NM_DEVICE_STATE_UNAVAILABLE: "Error: Device is unavailable",
            NetworkManager.NM_DEVICE_STATE_DISCONNECTED: "Currently disconnected",
            NetworkManager.NM_DEVICE_STATE_PREPARE: "Preparing the connection",
            NetworkManager.NM_DEVICE_STATE_CONFIG: "Configuring the connection",
            NetworkManager.NM_DEVICE_STATE_NEED_AUTH: "Authorizing",
            NetworkManager.NM_DEVICE_STATE_IP_CONFIG: "Requesting an IP address",
            NetworkManager.NM_DEVICE_STATE_IP_CHECK: "Checking the connection",
            NetworkManager.NM_DEVICE_STATE_SECONDARIES: "Waiting for secondary connections",
            NetworkManager.NM_DEVICE_STATE_ACTIVATED: "Connected",
            NetworkManager.NM_DEVICE_STATE_DEACTIVATING: "Disconnecting",
            NetworkManager.NM_DEVICE_STATE_FAILED: _("Connection failed"),
        }
        message = messages.get(new_state, f"State {new_state}")

        if new_state == NetworkManager.NM_DEVICE_STATE_ACTIVATED:
            connected_ssid = self._read_connected_ssid()
            previous_ssid = self._set_connection_state(True, connected_ssid)
            self._emit("connected", connected_ssid, previous_ssid)
        else:
            self._set_connection_state(False)
            if new_state == NetworkManager.NM_DEVICE_STATE_FAILED:
                dependency_failed = getattr(
                    NetworkManager, "NM_DEVICE_STATE_REASON_DEPENDENCY_FAILED", None
                )
                carrier_reason = getattr(NetworkManager, "NM_DEVICE_STATE_REASON_CARRIER", None)
                if reason == dependency_failed:
                    message = "Connection dependency failed"
                elif reason == carrier_reason:
                    message = "Connection carrier was lost"
                self._emit("popup", message)

        if message:
            self._emit("connecting_status", message)
        return new_state

    def _add_ap(self, access_point):
        ssid = access_point.Ssid
        if not ssid:
            ssid = _("Hidden") + f" {self.hidden_ssid_index}"
            self.hidden_ssid_index += 1
        self.ssid_by_path[access_point.object_path] = ssid
        self.path_by_ssid[ssid] = access_point.object_path
        self.visible_networks[access_point.object_path] = access_point
        return ssid

    def _remove_ap(self, path):
        ssid = self.ssid_by_path.pop(path, None)
        self.visible_networks.pop(path, None)
        if ssid is None or self.path_by_ssid.get(ssid) != path:
            return

        replacement_path = next(
            (ap_path for ap_path, ap_ssid in self.ssid_by_path.items() if ap_ssid == ssid),
            None,
        )
        if replacement_path is None:
            self.path_by_ssid.pop(ssid, None)
        else:
            self.path_by_ssid[ssid] = replacement_path

    @staticmethod
    def _get_encryption(flags):
        encryption = ""
        if (
            flags & NetworkManager.NM_802_11_AP_SEC_PAIR_WEP40
            or flags & NetworkManager.NM_802_11_AP_SEC_PAIR_WEP104
            or flags & NetworkManager.NM_802_11_AP_SEC_GROUP_WEP40
            or flags & NetworkManager.NM_802_11_AP_SEC_GROUP_WEP104
        ):
            encryption += "WEP "
        if (
            flags & NetworkManager.NM_802_11_AP_SEC_PAIR_TKIP
            or flags & NetworkManager.NM_802_11_AP_SEC_GROUP_TKIP
        ):
            encryption += "TKIP "
        if (
            flags & NetworkManager.NM_802_11_AP_SEC_PAIR_CCMP
            or flags & NetworkManager.NM_802_11_AP_SEC_GROUP_CCMP
        ):
            encryption += "AES "
        if flags & NetworkManager.NM_802_11_AP_SEC_KEY_MGMT_PSK:
            encryption += "WPA-PSK "
        if flags & NetworkManager.NM_802_11_AP_SEC_KEY_MGMT_802_1X:
            encryption += "802.1x "
        return encryption.strip()

    def __del__(self):
        with suppress(Exception):
            self.shutdown()
