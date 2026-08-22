import json
import os
import socket


DEFAULT_PUBLIC_STATE_PATH = "/run/mingda-device-lock/public-state.json"
DEFAULT_SOCKET_PATH = "/run/mingda-device-lock/control.sock"
DEFAULT_LOCK_MARKER_PATH = "/var/lib/mingda-device-lock/locked"
MAX_MESSAGE_BYTES = 8192


class DeviceLockState:
    def __init__(self, locked=False, desired="UNLOCKED", actual="UNLOCKED",
                 phase="STABLE", policy_version=0, generation=0,
                 capable=False):
        self.locked = bool(locked)
        self.desired = str(desired or "UNKNOWN").upper()
        self.actual = str(actual or "UNKNOWN").upper()
        self.phase = str(phase or "UNKNOWN").upper()
        self.policy_version = _non_negative_int(policy_version)
        self.generation = _non_negative_int(generation)
        self.capable = bool(capable)

    @classmethod
    def from_mapping(cls, value, marker_exists=False):
        if not isinstance(value, dict):
            value = {}
        actual = str(
            value.get("actual")
            or value.get("lockState")
            or value.get("state")
            or "UNKNOWN"
        ).upper()
        desired = str(value.get("desired") or "").upper()
        explicit_locked = value.get("locked")
        if explicit_locked is None:
            explicit_locked = value.get("machineLocked")
        if explicit_locked is None:
            locked = (
                marker_exists
                or actual == "LOCKED"
                or desired == "LOCKED"
                or str(value.get("phase") or "").upper()
                in ("LOCKING", "APPLYING", "ERROR")
            )
        else:
            locked = bool(explicit_locked)
        if marker_exists:
            locked = True
        return cls(
            locked=locked,
            desired=desired,
            actual=actual,
            phase=value.get("phase"),
            policy_version=value.get("policyVersion"),
            generation=value.get("generation"),
            capable=value.get("capable", True),
        )

    @property
    def signature(self):
        return (
            self.locked,
            self.desired,
            self.actual,
            self.phase,
            self.policy_version,
            self.generation,
            self.capable,
        )


class DeviceLockStateReader:
    def __init__(self, public_state_path=DEFAULT_PUBLIC_STATE_PATH,
                 marker_path=DEFAULT_LOCK_MARKER_PATH):
        self.public_state_path = public_state_path
        self.marker_path = marker_path

    def read(self):
        marker_exists = os.path.exists(self.marker_path)
        try:
            with open(self.public_state_path, "r", encoding="utf-8") as source:
                payload = json.load(source)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            payload = {}
        return DeviceLockState.from_mapping(payload, marker_exists=marker_exists)


class DeviceLockSocketClient:
    def __init__(self, socket_path=DEFAULT_SOCKET_PATH, timeout=0.5):
        self.socket_path = socket_path
        self.timeout = timeout

    def acknowledge_ui(self, generation, actual):
        return self.request({
            "v": 1,
            "op": "ui.ack",
            "generation": _non_negative_int(generation),
            "actual": str(actual or "UNKNOWN").upper(),
        })

    def request(self, payload):
        encoded = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError("device lock request exceeds size limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(encoded)
            response = _read_line(client)
        decoded = json.loads(response.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("device lock response must be an object")
        return decoded


def _read_line(client):
    chunks = []
    total = 0
    while True:
        chunk = client.recv(min(1024, MAX_MESSAGE_BYTES + 1 - total))
        if not chunk:
            raise ConnectionError("device lock socket closed without a response")
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_MESSAGE_BYTES:
            raise ValueError("device lock response exceeds size limit")
    response = b"".join(chunks)
    if not response:
        raise ValueError("device lock response is empty")
    return response


def _non_negative_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)
