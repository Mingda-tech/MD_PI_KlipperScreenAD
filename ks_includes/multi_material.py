import logging


SWITCH_DATA_OBJECT = "gcode_macro _SWITCH_TOOLS_DATA"
FEEDER_SENSOR_PREFIX = "gcode_button enter_button_"
MAX_SUPPORTED_CHANNELS = 10
MATERIAL_FIELDS = ("vendor", "type", "min_temp", "max_temp", "color")


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_channel_count(value, default=1):
    count = _as_int(value, default)
    return max(1, min(count, MAX_SUPPORTED_CHANNELS))


def get_switch_data(printer):
    data = printer.get_stat(SWITCH_DATA_OBJECT)
    return data if isinstance(data, dict) else {}


def get_saved_variables(printer):
    data = printer.get_stat("save_variables")
    if not isinstance(data, dict):
        return {}
    variables = data.get("variables", {})
    return variables if isinstance(variables, dict) else {}


def get_channel_count(printer):
    feeder_sections = printer.get_config_section_list("manual_stepper motor_")
    feeder_channels = set()
    for section in feeder_sections:
        channel = _as_int(section.rsplit("_", 1)[-1])
        if channel is not None:
            feeder_channels.add(channel)
    if feeder_channels:
        contiguous_count = 0
        for channel in range(1, MAX_SUPPORTED_CHANNELS + 1):
            if channel not in feeder_channels:
                break
            contiguous_count = channel
        if contiguous_count:
            return contiguous_count

    count = _as_int(get_saved_variables(printer).get("feed_system_max_filament"))
    if count is not None:
        return clamp_channel_count(count)

    switch_data = get_switch_data(printer)
    count = _as_int(switch_data.get("max_filament"))
    if count is not None:
        return clamp_channel_count(count)

    section = printer.get_config_section(SWITCH_DATA_OBJECT)
    if isinstance(section, dict):
        count = _as_int(section.get("variable_max_filament"))
        if count is not None:
            return clamp_channel_count(count)

    commands = printer.available_commands or {}
    sequential_tools = 0
    for tool in range(MAX_SUPPORTED_CHANNELS):
        if f"T{tool}" not in commands:
            break
        sequential_tools += 1
    return clamp_channel_count(sequential_tools or 1)


def get_active_channel(printer, default=0):
    active = _as_int(get_switch_data(printer).get("active_tools"))
    if active is None:
        active = _as_int(get_saved_variables(printer).get("feed_system_active_tool"), default)
    channel_count = get_channel_count(printer)
    return active if active is not None and 0 <= active <= channel_count else default


def get_filament_mask(printer, default=None):
    return _as_int(get_switch_data(printer).get("filament_index"), default)


def get_feeder_sensors(printer):
    sensors = {}
    for section in printer.get_config_section_list(FEEDER_SENSOR_PREFIX):
        channel = _as_int(section.rsplit("_", 1)[-1])
        if channel is not None and 1 <= channel <= MAX_SUPPORTED_CHANNELS:
            sensors[channel] = section
    return dict(sorted(sensors.items()))


def get_channel_presence(printer, channel, fallback_mask=None, feeder_sensors=None):
    channel = _as_int(channel)
    if channel is None or channel < 1:
        return None
    sensors = feeder_sensors if feeder_sensors is not None else get_feeder_sensors(printer)
    sensor = sensors.get(channel)
    if sensor is not None:
        state = printer.get_stat(sensor, "state")
        if isinstance(state, str):
            state = state.strip().upper()
            if state == "PRESSED":
                return True
            if state == "RELEASED":
                return False
    return is_channel_loaded(fallback_mask, channel)


def is_channel_loaded(mask, channel):
    if mask is None or channel < 1:
        return None
    return bool(mask & (1 << (channel - 1)))


def empty_material_record():
    return {
        "name": None,
        "vendor": None,
        "type": None,
        "min_temp": None,
        "max_temp": None,
        "color": None,
    }


def parse_material_records(raw_value, channel_count):
    channel_count = clamp_channel_count(channel_count)
    raw = "" if raw_value is None else str(raw_value).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]

    serialized_records = raw.split("/") if raw else []
    records = []
    for index in range(channel_count + 1):
        values = serialized_records[index].split(",") if index < len(serialized_records) else []
        values = values[:len(MATERIAL_FIELDS)]
        values.extend([None] * (len(MATERIAL_FIELDS) - len(values)))
        normalized = [None if value in (None, "", "None") else value for value in values]
        record = dict(zip(MATERIAL_FIELDS, normalized))
        record["name"] = (
            f"{record['vendor']} {record['type']}"
            if record["vendor"] is not None and record["type"] is not None
            else None
        )
        records.append(record)

    if len(serialized_records) > channel_count + 1:
        logging.warning(
            "Ignoring %s material records above the configured %s channels",
            len(serialized_records) - channel_count - 1,
            channel_count,
        )
    return records


def get_material_record(printer, channel, fallback_records=None):
    record = get_switch_data(printer).get(f"filament_info_{channel}")
    if isinstance(record, dict):
        result = empty_material_record()
        for field in MATERIAL_FIELDS:
            value = record.get(field)
            result[field] = None if value in (None, "", "None") else value
        if result["vendor"] is not None and result["type"] is not None:
            result["name"] = f"{result['vendor']} {result['type']}"
        return result

    if fallback_records is not None and 0 <= channel < len(fallback_records):
        return fallback_records[channel]
    return empty_material_record()
