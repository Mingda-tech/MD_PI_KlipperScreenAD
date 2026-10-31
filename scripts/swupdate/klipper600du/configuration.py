"""Plan a conservative 600DU load-cell configuration migration in memory.

The caller owns file provenance, backups and atomic replacement. This module
never writes a printer file or runs a macro. Existing macro bodies, tuning and
calibration values remain authoritative; only the sensor protocol is upgraded.
"""
import hashlib
import math
import re


SENSOR = 'load_cell load_cell_head_l'
PINS = {'dout_pin': '600D_HB:CS_DOUT', 'sclk_pin': '600D_HB:CS_SCLK'}
UUID = '4d4400000a02'
CALIBRATION = frozenset(('z_offset', 'reference_tare_counts', 'counts_per_gram',
                         'sensor_orientation', 'variable_mfe_offset'))
HEADER = re.compile(r'^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[#;][^\r\n]*)?(?:\r?\n)?$')
OPTION = re.compile(r'^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)[ \t]*[:=][ \t]*([^\r\n]*)(\r?\n)?$')
DEPENDENCIES = frozenset(('gcode_macro _client_variable', 'gcode_macro _debug_info'))


class ConfigurationError(ValueError):
    pass


def _name(value):
    return ' '.join(value.split()).casefold()


def _decode(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 1024 or b'\x00' in raw:
        raise ConfigurationError('Invalid configuration input')
    bom = raw.startswith(b'\xef\xbb\xbf')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ConfigurationError('Configuration must use UTF-8') from exc
    lines = text.splitlines(keepends=True)
    marker = next((i for i, line in enumerate(lines) if line.lstrip().startswith('#*#')), len(lines))
    return bom, lines[:marker], ''.join(lines[marker:])


def _sections(lines):
    sections = []
    seen = set()
    for index, line in enumerate(lines):
        match = HEADER.fullmatch(line)
        if match is None:
            continue
        name = _name(match[1])
        if name in seen:
            raise ConfigurationError('Duplicate configuration section: ' + name)
        if sections:
            sections[-1]['end'] = index
        seen.add(name)
        sections.append({'name': name, 'start': index, 'end': len(lines)})
    return sections


def _options(lines, section):
    result = {}
    for index in range(section['start'] + 1, section['end']):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith(('#', ';')):
            continue
        match = OPTION.fullmatch(line)
        if match is None:
            raise ConfigurationError('Unsupported multiline sensor option')
        name = match[2].casefold()
        if name in result:
            raise ConfigurationError('Duplicate sensor option: ' + name)
        value = re.split(r'[ \t]+[#;]', match[3], maxsplit=1)[0].strip()
        result[name] = {'value': value, 'index': index, 'match': match}
    return result


def _version(value):
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError('Unsupported load-cell protocol version') from exc
    if not math.isfinite(number) or number not in (1.0, 2.0):
        raise ConfigurationError('Unsupported load-cell protocol version')
    return number


def _active(config):
    if not isinstance(config, dict):
        raise ConfigurationError('Active configuration snapshot is required')
    result = {}
    for key, options in config.items():
        if not isinstance(key, str) or not isinstance(options, dict):
            raise ConfigurationError('Invalid active configuration section')
        name = _name(key)
        if name in result:
            raise ConfigurationError('Duplicate active configuration section: ' + name)
        values = {}
        for option, value in options.items():
            if not isinstance(option, str) or option.casefold() in values:
                raise ConfigurationError('Invalid active configuration option')
            values[option.casefold()] = str(value).strip()
        result[name] = values
    return result


def _sensor(options):
    if options.get('sensor_type', '').casefold() != 'cs1237':
        raise ConfigurationError('This package requires the existing CS1237 sensor')
    if any(options.get(name) != pin for name, pin in PINS.items()):
        raise ConfigurationError('Load-cell pins do not match the 600DU target')
    _version(options.get('version', '1.0'))
    if 'start_move_wait_time' in options:
        try:
            delay = float(options['start_move_wait_time'])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError('Invalid load-cell start timing') from exc
        if not math.isfinite(delay) or delay <= 0:
            raise ConfigurationError('Invalid load-cell start timing')


def plan_configuration(original, template, active_config):
    """Return migrated bytes and a value-free summary; perform no file I/O.

    ``active_config`` is Moonraker's complete configfile.config mapping. The
    caller must also prove that ``original`` is the active target file and
    retain its fingerprint until replacement. Missing protected calibration
    options are not filled from example defaults, including when SAVE_CONFIG
    supplies their values outside the editable section.
    """
    bom, lines, saved = _decode(original)
    _, reference, template_saved = _decode(template)
    if template_saved:
        raise ConfigurationError('A release template cannot contain SAVE_CONFIG data')
    sections = _sections(lines)
    expected = _sections(reference)
    sensor = next((row for row in sections if row['name'] == SENSOR), None)
    example = next((row for row in expected if row['name'] == SENSOR), None)
    if sensor is None or example is None:
        raise ConfigurationError('The existing target load-cell section is required')
    active = _active(active_config)
    if SENSOR not in active:
        raise ConfigurationError('The target load-cell section is not active')
    current = _options(lines, sensor)
    defaults = _options(reference, example)
    _sensor({key: row['value'] for key, row in current.items()})
    _sensor(active[SENSOR])
    _sensor({key: row['value'] for key, row in defaults.items()})
    if _version(defaults.get('version', {}).get('value')) != 2.0 or 'start_move_wait_time' not in defaults:
        raise ConfigurationError('The release template must use load-cell protocol 2.0')
    board = active.get('mcu 600d_hb')
    if board is not None and board.get('canbus_uuid', '').lower() != UUID:
        raise ConfigurationError('The active 600DU board CAN identity differs')

    newline = '\r\n' if sum(line.endswith('\r\n') for line in lines) > sum(
        line.endswith('\n') and not line.endswith('\r\n') for line in lines) else '\n'
    updated = list(lines)
    changed = []
    additions = []
    for name, row in defaults.items():
        if name in current:
            if name != 'version' or _version(current[name]['value']) == 2.0:
                continue
            position = current[name]['index']
            old = lines[position]
            # Preserve the original separator, whitespace, comment and newline.
            match = re.match(r'^([ \t]*version[ \t]*[:=][ \t]*)([^#;\r\n]*)(.*?)(\r?\n)?$', old, re.I)
            trailing = re.search(r'[ \t]*$', match[2])[0]
            updated[position] = match[1] + '2.0' + trailing + match[3] + (match[4] or '')
            operation = 'replace'
        else:
            if name in CALIBRATION or name.startswith('variable_'):
                continue
            additions.append(name + ': ' + row['value'] + newline)
            operation = 'add'
        changed.append({'section': SENSOR, 'option': name, 'operation': operation})
    if additions:
        at = sensor['end']
        if at and updated[at - 1] and not updated[at - 1].endswith('\n'):
            additions.insert(0, newline)
        updated[at:at] = additions

    local = {row['name'] for row in sections}
    macros = [row for row in expected if row['name'].startswith('gcode_macro ')
              and row['name'] not in local and row['name'] not in active]
    if macros and not DEPENDENCIES.issubset(active):
        raise ConfigurationError('Required existing load-cell helper macros are missing')
    body = ''.join(updated)
    for section in macros:
        if body and not body.endswith('\n'):
            body += newline
        if body and not body.endswith(newline + newline):
            body += newline
        block = ''.join(reference[section['start']:section['end']])
        body += re.sub(r'\r?\n', newline, block)
        if not body.endswith('\n'):
            body += newline
    if saved and body and not body.endswith('\n'):
        body += newline
    content = (b'\xef\xbb\xbf' if bom else b'') + (body + saved).encode('utf-8')
    return {'content': content, 'changedOptions': changed,
            'addedSections': [section['name'] for section in macros],
            'originalSha256': hashlib.sha256(original).hexdigest(),
            'resultSha256': hashlib.sha256(content).hexdigest()}
