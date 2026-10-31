"""Constrained native SWUpdate package structure, without executing package code.

This module is NOT the signature verifier or installer. The caller must run the
pinned, SIGNED_IMAGES-enabled SWUpdate check before trusting the returned data.
Only JSON descriptions, dummy payload handlers and one shellscript are accepted,
so metadata parsing/check-only cannot execute embedded Lua or install streamed data.
"""

import hashlib
import json
from pathlib import Path
import re
import stat
import tempfile

from .common import UpdateError, command, identifier, regular


MAX_ARCHIVE = 16 * 1024**3
MAX_METADATA = 1024 * 1024
MAX_FILES = 10000
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
RESERVED = {'sw-description', 'sw-description.sig', 'mingda-meta.json', 'update.sh', 'rescue.sh', 'TRAILER!!!'}
ENGINE_VERSION = '2026.05'
SCRIPT_COMPATIBILITY = 'mdu-script-api-1'


def flat_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value) or value in ('.', '..'):
        raise UpdateError('SWU_UNSAFE_NAME')
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateError('SWU_DUPLICATE_JSON_KEY', key)
        value[key] = item
    return value


def json_object(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError('SWU_INVALID_JSON') from exc
    if not isinstance(value, dict):
        raise UpdateError('SWU_INVALID_JSON')
    return value


def hardware_revisions(hardware=None):
    """The native compatibility field identifies the script protocol, not a device."""
    if hardware is not None and not isinstance(hardware, dict):
        raise UpdateError('SWU_HARDWARE')
    return [SCRIPT_COMPATIBILITY]


def validate_metadata(metadata):
    if type(metadata.get('schemaVersion')) is not int or metadata['schemaVersion'] != 2:
        raise UpdateError('SWU_METADATA_SCHEMA')
    if type(metadata.get('scriptApi')) is not int or metadata['scriptApi'] != 1:
        raise UpdateError('SWU_SCRIPT_API')
    identifier(metadata.get('releaseId'))
    if not isinstance(metadata.get('version'), str) or not 0 < len(metadata['version']) <= 127:
        raise UpdateError('SWU_VERSION')
    if ('description' in metadata and
            (not isinstance(metadata['description'], str) or len(metadata['description']) > 4096)):
        raise UpdateError('SWU_METADATA_FIELD', 'description')
    # Applicability belongs to authenticated preinst. These optional notes must
    # never become a second platform/board allowlist in the screen or builder.
    if 'hardware' in metadata and not isinstance(metadata['hardware'], dict):
        raise UpdateError('SWU_HARDWARE')
    if 'boardProfiles' in metadata:
        profiles = metadata['boardProfiles']
        if not isinstance(profiles, dict) or any(not isinstance(value, dict) for value in profiles.values()):
            raise UpdateError('SWU_BOARD_PROFILES')
    for name in ('touchesFirmware', 'touchesScreen'):
        if type(metadata.get(name)) is not bool:
            raise UpdateError('SWU_METADATA_FIELD', name)
    if metadata.get('recoveryMode') not in ('replay', 'none'):
        raise UpdateError('SWU_RECOVERY_MODE')
    rescue = metadata.get('bootRescue', False)
    if type(rescue) is not bool:
        raise UpdateError('SWU_METADATA_FIELD', 'bootRescue')
    if metadata['touchesScreen'] and (not rescue or metadata['recoveryMode'] != 'replay'):
        raise UpdateError('SWU_SCREEN_RESCUE_REQUIRED')
    if rescue and not metadata['touchesScreen']:
        raise UpdateError('SWU_SCREEN_RESCUE_DECLARATION')
    seen = set()
    for group in ('components', 'firmware'):
        rows = metadata.get(group, [])
        if not isinstance(rows, list):
            raise UpdateError('SWU_COMPONENTS')
        for row in rows:
            if not isinstance(row, dict):
                raise UpdateError('SWU_COMPONENTS')
            name = identifier(row.get('id'))
            if name in seen or not isinstance(row.get('version'), str) or not row['version']:
                raise UpdateError('SWU_COMPONENTS')
            seen.add(name)
            if type(row.get('sequence')) is not int or row['sequence'] < 1:
                raise UpdateError('SWU_SEQUENCE')
            if group == 'firmware' and not row.get('buildVersion'):
                raise UpdateError('SWU_FIRMWARE_IDENTITY')
    if not seen:
        raise UpdateError('SWU_EMPTY_UPDATE')
    if metadata.get('firmware') and not metadata['touchesFirmware']:
        raise UpdateError('SWU_FIRMWARE_DECLARATION')
    if metadata['touchesFirmware'] and not metadata.get('firmware'):
        raise UpdateError('SWU_FIRMWARE_DECLARATION')
    changes = metadata.get('configChanges', [])
    if not isinstance(changes, list):
        raise UpdateError('SWU_CONFIG_SUMMARY')
    for row in changes:
        if (not isinstance(row, dict) or set(row) - {'path', 'description', 'op'} or
                not all(isinstance(value, str) for value in row.values())):
            raise UpdateError('SWU_CONFIG_SUMMARY')
    return metadata


def _der_item(data, offset=0):
    if offset + 2 > len(data):
        raise UpdateError('RSA_KEY_REQUIRED')
    tag, length = data[offset], data[offset + 1]
    cursor = offset + 2
    if length & 0x80:
        count = length & 0x7f
        if not 1 <= count <= 4 or cursor + count > len(data):
            raise UpdateError('RSA_KEY_REQUIRED')
        length = int.from_bytes(data[cursor:cursor + count], 'big')
        cursor += count
    end = cursor + length
    if end > len(data):
        raise UpdateError('RSA_KEY_REQUIRED')
    return tag, data[cursor:end], end


def require_rsa_key(key, private=False, openssl='openssl'):
    """Return RSA modulus bits; raw RSA/SHA256 signing is SWUpdate's native mode."""
    args = [openssl, 'pkey', '-in', regular(key), '-pubout', '-outform', 'DER']
    if not private:
        args.append('-pubin')
    # A file also avoids text-mode/binary-pipe quirks in Windows OpenSSL builds.
    # Only public SubjectPublicKeyInfo is written here, never private key data.
    with tempfile.TemporaryDirectory(prefix='swu-public-key-') as temporary:
        output = Path(temporary) / 'public.der'
        command(args + ['-out', output])
        der = output.read_bytes()
    tag, sequence, end = _der_item(der)
    if tag != 0x30 or end != len(der):
        raise UpdateError('RSA_KEY_REQUIRED')
    tag, algorithm, offset = _der_item(sequence)
    # rsaEncryption OID 1.2.840.113549.1.1.1, NULL parameters; not RSA-PSS or EC.
    if tag != 0x30 or algorithm != bytes.fromhex('06092a864886f70d0101010500'):
        raise UpdateError('RSA_KEY_REQUIRED')
    tag, bits, end = _der_item(sequence, offset)
    if tag != 0x03 or end != len(sequence) or not bits or bits[0] != 0:
        raise UpdateError('RSA_KEY_REQUIRED')
    tag, public, end = _der_item(bits, 1)
    if tag != 0x30 or end != len(bits):
        raise UpdateError('RSA_KEY_REQUIRED')
    tag, modulus, offset = _der_item(public)
    exponent_tag, exponent, end = _der_item(public, offset)
    if tag != 2 or exponent_tag != 2 or end != len(public) or not modulus or modulus[0] & 0x80:
        raise UpdateError('RSA_KEY_REQUIRED')
    size = int.from_bytes(modulus, 'big').bit_length()
    if not 2048 <= size <= 8192 or int.from_bytes(exponent, 'big') < 3:
        raise UpdateError('RSA_KEY_TOO_SMALL_OR_INVALID')
    return size


def _read_exact(source, size):
    data = source.read(size)
    if len(data) != size:
        raise UpdateError('SWU_TRUNCATED')
    return data


def _padding(source, count):
    if count and any(_read_exact(source, count)):
        raise UpdateError('SWU_INVALID_PADDING')


def read_archive(path):
    """Check the inert envelope before invoking SWUpdate's authenticated check."""
    path = regular(path)
    if path.stat().st_size > MAX_ARCHIVE:
        raise UpdateError('SWU_TOO_LARGE')
    members, metadata_bytes, description_bytes = {}, None, None
    with path.open('rb') as source:
        for index in range(MAX_FILES + 1):
            header = _read_exact(source, 110)
            if header[:6] not in (b'070701', b'070702'):
                raise UpdateError('SWU_CPIO_FORMAT')
            try:
                fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
            except ValueError as exc:
                raise UpdateError('SWU_CPIO_HEADER') from exc
            mode, nlink, size, name_size, checksum = fields[1], fields[4], fields[6], fields[11], fields[12]
            if not 1 <= name_size <= 128:
                raise UpdateError('SWU_UNSAFE_NAME')
            encoded = _read_exact(source, name_size)
            if encoded[-1:] != b'\0' or b'\0' in encoded[:-1]:
                raise UpdateError('SWU_UNSAFE_NAME')
            try:
                name = encoded[:-1].decode('ascii')
            except UnicodeError as exc:
                raise UpdateError('SWU_UNSAFE_NAME') from exc
            _padding(source, (-(110 + name_size)) % 4)
            if name == 'TRAILER!!!':
                if size != 0:
                    raise UpdateError('SWU_INVALID_TRAILER')
                rest = source.read(512)
                if any(rest) or source.read(1):
                    raise UpdateError('SWU_TRAILING_DATA')
                break
            flat_name(name)
            if name in members or nlink != 1 or stat.S_IFMT(mode) != stat.S_IFREG or mode & 0o7000:
                raise UpdateError('SWU_UNSAFE_MEMBER')
            if index < 2 and name != ('sw-description', 'sw-description.sig')[index]:
                raise UpdateError('SWU_METADATA_ORDER')
            if name in ('sw-description', 'mingda-meta.json') and size > MAX_METADATA:
                raise UpdateError('SWU_METADATA_TOO_LARGE')
            if name == 'sw-description.sig' and not 256 <= size <= 1024:
                raise UpdateError('SWU_SIGNATURE_SIZE')
            offset = source.tell()
            remaining, crc, hasher = size, 0, hashlib.sha256()
            keep = [] if name in ('sw-description', 'mingda-meta.json') else None
            while remaining:
                block = _read_exact(source, min(remaining, 1024 * 1024))
                remaining -= len(block)
                hasher.update(block)
                if header[:6] == b'070702':
                    crc = (crc + sum(block)) & 0xffffffff
                if keep is not None:
                    keep.append(block)
            if (header[:6] == b'070702' and crc != checksum) or (header[:6] == b'070701' and checksum != 0):
                raise UpdateError('SWU_CPIO_CHECKSUM')
            _padding(source, (-size) % 4)
            members[name] = {'size': size, 'sha256': hasher.hexdigest(), 'offset': offset}
            if name == 'sw-description':
                description_bytes = b''.join(keep)
            elif name == 'mingda-meta.json':
                metadata_bytes = b''.join(keep)
        else:
            raise UpdateError('SWU_TOO_MANY_FILES')
    if description_bytes is None or metadata_bytes is None or 'update.sh' not in members:
        raise UpdateError('SWU_METADATA_MISSING')
    description = json_object(description_bytes)
    metadata = validate_metadata(json_object(metadata_bytes))
    _validate_description(description, metadata, members)
    return {'description': description, 'metadata': metadata, 'members': members}


def _validate_description(description, metadata, members):
    if set(description) != {'software'} or not isinstance(description['software'], dict):
        raise UpdateError('SWU_DESCRIPTION_SCHEMA')
    software = description['software']
    allowed = {'version', 'description', 'hardware-compatibility', 'bootloader_transaction_marker',
               'bootloader_state_marker', 'reboot', 'stable'}
    if set(software) - allowed:
        raise UpdateError('SWU_UNSUPPORTED_DESCRIPTION')
    if software.get('version') != metadata['version']:
        raise UpdateError('SWU_METADATA_MISMATCH')
    if software.get('description') != metadata.get('description', metadata['releaseId']):
        raise UpdateError('SWU_METADATA_MISMATCH')
    for flag in ('bootloader_transaction_marker', 'bootloader_state_marker', 'reboot'):
        if software.get(flag) is not False:
            raise UpdateError('SWU_UNSUPPORTED_DESCRIPTION')
    if software.get('hardware-compatibility') != hardware_revisions():
        raise UpdateError('SWU_HARDWARE_MISMATCH')
    selections = software.get('stable')
    expected_selections = {'main', 'rescue'} if metadata.get('bootRescue') else {'main'}
    if not isinstance(selections, dict) or set(selections) != expected_selections:
        raise UpdateError('SWU_SELECTION_CONTRACT')
    seen = set()
    expected_images = set(members) - {'sw-description', 'sw-description.sig', 'update.sh', 'rescue.sh'}
    for selection, contents in selections.items():
        if not isinstance(contents, dict) or set(contents) != {'scripts', 'images'}:
            raise UpdateError('SWU_HANDLER_CONTRACT')
        scripts = contents['scripts']
        images = contents['images']
        if not isinstance(scripts, list) or len(scripts) != 1 or not isinstance(images, list):
            raise UpdateError('SWU_HANDLER_CONTRACT')
        selection_files, image_files = set(), set()
        for row in scripts + images:
            if not isinstance(row, dict) or set(row) != {'filename', 'type', 'sha256'}:
                raise UpdateError('SWU_HANDLER_CONTRACT')
            name = flat_name(row['filename'])
            if name in selection_files or name not in members or name in ('sw-description', 'sw-description.sig'):
                raise UpdateError('SWU_DESCRIPTION_FILES')
            selection_files.add(name)
            expected = 'shellscript' if row is scripts[0] else 'dummy'
            script_name = 'update.sh' if selection == 'main' else 'rescue.sh'
            if row['type'] != expected or (expected == 'shellscript' and name != script_name):
                raise UpdateError('SWU_HANDLER_CONTRACT')
            if expected == 'dummy':
                image_files.add(name)
            if not isinstance(row['sha256'], str) or not SHA256.fullmatch(row['sha256']):
                raise UpdateError('SWU_HASH_REQUIRED')
            if row['sha256'] != members[name]['sha256']:
                raise UpdateError('SWU_HASH_MISMATCH', name)
        if image_files != expected_images:
            raise UpdateError('SWU_DESCRIPTION_FILES')
        seen.update(selection_files)
    if seen != set(members) - {'sw-description', 'sw-description.sig'} or 'mingda-meta.json' not in seen:
        raise UpdateError('SWU_UNDECLARED_FILES')


def read_preview(path):
    """Unauthenticated data until SWUpdate -c -k succeeds; never executes a script."""
    return read_archive(path)['metadata']


inspect_swu = read_preview
validate_structure = read_preview
