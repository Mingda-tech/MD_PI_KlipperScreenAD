import math
import shlex


PROTOCOL_PREFIX = "MD_POSITION_CALIBRATE"
VALID_ACTIONS = {"QUERY", "START", "SAVE", "CANCEL"}
VALID_POSITIONS = {
    "PROBE_DEPLOY",
    "PROBE_STOW",
    "CLEAN",
    "FINE_CLEAN",
}
VALID_STATUSES = {
    "POSITION",
    "READY",
    "SAVED",
    "CANCELLED",
    "ERROR",
}

STATUS_TRANSITIONS = {
    ("querying", "POSITION"): "ready_to_start",
    ("moving", "READY"): "adjusting",
    ("saving", "SAVED"): "complete",
    ("cancelling", "CANCELLED"): "complete",
}


def build_position_command(action, position, coordinates=None):
    action = str(action).upper()
    position = str(position).upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported position calibration action: {action}")
    if position not in VALID_POSITIONS:
        raise ValueError(f"Unsupported position calibration target: {position}")

    command = f"{PROTOCOL_PREFIX} ACTION={action} POSITION={position}"
    if action != "SAVE":
        if coordinates is not None:
            raise ValueError("Coordinates are only valid for SAVE")
        return command

    valid, reason = validate_coordinates(coordinates)
    if not valid:
        raise ValueError(reason)
    return (
        f"{command}"
        f" X={float(coordinates['x']):.3f}"
        f" Y={float(coordinates['y']):.3f}"
        f" Z={float(coordinates['z']):.3f}"
    )


def parse_protocol_response(message):
    if not isinstance(message, str):
        return None
    start = message.find(PROTOCOL_PREFIX)
    if start < 0:
        return None

    try:
        parts = shlex.split(message[start:])
    except ValueError:
        return None
    if not parts or parts[0] != PROTOCOL_PREFIX:
        return None

    result = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.upper()] = value

    status = result.get("STATUS", "").upper()
    position = result.get("POSITION", "").upper()
    if status not in VALID_STATUSES or position not in VALID_POSITIONS:
        return None
    result["STATUS"] = status
    result["POSITION"] = position

    coordinate_values = {}
    for axis in ("X", "Y", "Z"):
        if axis not in result:
            continue
        try:
            value = float(result[axis])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        coordinate_values[axis.lower()] = value
    if coordinate_values:
        if len(coordinate_values) != 3:
            return None
        result["coordinates"] = coordinate_values
    return result


def validate_coordinates(coordinates, bounds=None):
    if not isinstance(coordinates, dict):
        return False, "Coordinates must be a mapping"
    for axis in ("x", "y", "z"):
        if axis not in coordinates:
            return False, f"Missing {axis.upper()} coordinate"
        try:
            value = float(coordinates[axis])
        except (TypeError, ValueError):
            return False, f"Invalid {axis.upper()} coordinate"
        if not math.isfinite(value):
            return False, f"Invalid {axis.upper()} coordinate"
        if bounds and axis in bounds:
            minimum, maximum = bounds[axis]
            if value < minimum or value > maximum:
                return False, f"{axis.upper()} coordinate is outside the configured range"
    return True, None


def next_state_for_status(current_state, status):
    status = str(status).upper()
    if status == "ERROR":
        return "error"
    return STATUS_TRANSITIONS.get((current_state, status), current_state)


def controls_for_state(state, session_started=False):
    controls = {
        "start": False,
        "move": False,
        "distance": False,
        "save": False,
        "cancel": False,
    }
    if state == "ready_to_start":
        controls.update(start=True, cancel=True)
    elif state == "adjusting":
        controls.update(move=True, distance=True, save=True, cancel=True)
    elif state == "error":
        controls.update(start=True, cancel=not session_started)
    return controls
