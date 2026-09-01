import math


AXES = ("x", "y", "z")
RETREAT_DISTANCE = 20.0


def normalize_coordinates(coordinates):
    if not isinstance(coordinates, dict):
        raise ValueError("Cutter coordinates must be a mapping")

    normalized = {}
    for axis in AXES:
        try:
            value = float(coordinates[axis])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid {axis.upper()} cutter coordinate") from error
        if not math.isfinite(value):
            raise ValueError(f"Invalid {axis.upper()} cutter coordinate")
        normalized[axis] = value
    return normalized


def build_cutter_test_script(coordinates, restore_relative=False):
    position = normalize_coordinates(coordinates)
    retreat_x = position["x"] - RETREAT_DISTANCE
    commands = [
        "G90",
        f"G1 X{retreat_x:.2f} F3000",
        "M400",
        f"G1 X{position['x']:.2f} F1200",
        "M400",
        f"G1 X{retreat_x:.2f} F3000",
    ]
    if restore_relative:
        commands.append("G91")
    return "\n".join(commands)


def build_cutter_save_script(coordinates):
    position = normalize_coordinates(coordinates)
    retreat_x = position["x"] - RETREAT_DISTANCE
    return "\n".join(
        [
            "G90",
            f"G1 X{retreat_x:.2f} F3000",
            "M400",
            f"SAVE_VARIABLE VARIABLE=cutter_xpos VALUE={position['x']:.2f}",
            f"SAVE_VARIABLE VARIABLE=cutter_ypos VALUE={position['y']:.2f}",
            f"SAVE_VARIABLE VARIABLE=cutter_zpos VALUE={position['z']:.2f}",
        ]
    )
