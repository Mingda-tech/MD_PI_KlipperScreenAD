"""Validate a complete offline runtime release without executing its ARM binary.

The manifest is a content inventory, not a signature of the screen checkout. The
screen checkout is already the trusted source of executable update code. All
bytes returned here are read once and checked before a privileged install uses
them, so changing the checkout later cannot change the installation snapshot.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType

from .common import UpdateError, identifier, no_links
from .swu_package import json_object, require_rsa_key
from .swupdate import ENABLED, SOURCE_COMMIT, VERSION


TARGET = {'arch': 'aarch64'}
DEVICE_FIELDS = {'arch'}
FORBIDDEN = ('LUA', 'LUAEXTERNAL', 'LUASCRIPTHANDLER', 'EMBEDDED_LUA_HANDLER',
             'DOWNLOAD', 'SURICATTA', 'WEBSERVER', 'MTD', 'UBIVOL', 'UBOOT',
             'BOOTLOADER_GRUB', 'BOOTLOADER_EBG', 'BOOTLOADER_CBOOT', 'BOOTLOADERHANDLER',
             'RAW', 'ARCHIVE', 'COPY', 'DISKPART', 'DISKFORMAT_HANDLER', 'REMOTE_HANDLER',
             'UCFWHANDLER', 'ENCRYPTED_IMAGES', 'ENCRYPTED_SW_DESCRIPTION',
             'SIGALG_RSAPSS', 'SIGALG_CMS', 'SIGALG_GPG', 'SSL_IMPL_WOLFSSL',
             'SSL_IMPL_MBEDTLS', 'SSL_IMPL_GPGME', 'NOCLEANUP')
MAX_FILES = 512
MAX_TOTAL = 512 * 1024 * 1024
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+~-]{0,127}\Z')


def relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or
            any(part in ('.', '..') or not PART.fullmatch(part) for part in value.split('/'))):
        raise UpdateError('RUNTIME_BUNDLE_PATH')
    return value


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise UpdateError('RUNTIME_BUNDLE_SCHEMA')


def validate_inventory(value, require_evidence=False):
    if not isinstance(value, dict):
        raise UpdateError('RUNTIME_BASELINE_INVALID')
    for name, row in value.items():
        identifier(name)
        if (not isinstance(row, dict) or set(row) - {'version', 'sequence', 'releaseId', 'packageSha256', 'evidence'} or
                not isinstance(row.get('version'), str) or not row['version'] or
                type(row.get('sequence')) is not int or row['sequence'] < 1):
            raise UpdateError('RUNTIME_BASELINE_INVALID')
        if 'releaseId' in row:
            identifier(row['releaseId'])
        if 'packageSha256' in row and (
                not isinstance(row['packageSha256'], str) or not SHA256.fullmatch(row['packageSha256'])):
            raise UpdateError('RUNTIME_BASELINE_INVALID')
        evidence = row.get('evidence')
        if require_evidence or evidence is not None:
            if not isinstance(evidence, dict):
                raise UpdateError('RUNTIME_BASELINE_INVALID')
            kind = evidence.get('kind')
            fields = {'git': {'kind', 'path', 'commit'}, 'json': {'kind', 'path', 'field', 'value'},
                      'text': {'kind', 'path', 'value'}}.get(kind)
            if fields is None or set(evidence) != fields:
                raise UpdateError('RUNTIME_BASELINE_INVALID')
            path = evidence['path']
            if (not isinstance(path, str) or not path.startswith('/') or '\\' in path or
                    any(part in ('.', '..') for part in path.split('/'))):
                raise UpdateError('RUNTIME_BASELINE_INVALID')
            if kind == 'git':
                if not isinstance(evidence['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', evidence['commit']):
                    raise UpdateError('RUNTIME_BASELINE_INVALID')
            elif evidence['value'] != row['version']:
                raise UpdateError('RUNTIME_BASELINE_INVALID')
            if kind == 'json' and (not isinstance(evidence['field'], str) or not PART.fullmatch(evidence['field'])):
                raise UpdateError('RUNTIME_BASELINE_INVALID')
    return value


def validate_policy(value, device):
    if (value.get('schemaVersion') != 2 or value.get('engine') != 'swupdate' or
            value.get('device') != device or not isinstance(value.get('swupdate'), dict) or
            value['swupdate'].get('version') != VERSION):
        raise UpdateError('RUNTIME_POLICY_INVALID')
    return value


def _elf(data):
    if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01' or int.from_bytes(data[18:20], 'little') != 183:
        raise UpdateError('SWUPDATE_ARCHITECTURE_MISMATCH')


def _engine(manifest, files):
    engine = manifest['engine']
    _shape(engine, ('binary', 'receipt', 'config'))
    binary, receipt, config = (files[relative_path(engine[key])] for key in ('binary', 'receipt', 'config'))
    _elf(binary)
    receipt = json_object(receipt)
    if (receipt.get('schemaVersion') != 1 or receipt.get('version') != VERSION or
            receipt.get('sourceCommit') != SOURCE_COMMIT or receipt.get('arch') != TARGET['arch'] or
            receipt.get('developmentBuild') is not False or
            receipt.get('socketPaths') != {'control': '', 'progress': ''} or
            receipt.get('binarySha256') != hashlib.sha256(binary).hexdigest() or
            receipt.get('configSha256') != hashlib.sha256(config).hexdigest()):
        raise UpdateError('SWUPDATE_BUILD_MISMATCH')
    features = receipt.get('features', {})
    if (not isinstance(features, dict) or any(features.get(key) is not True for key in ENABLED) or
            any(features.get(key) is not False for key in FORBIDDEN)):
        raise UpdateError('SWUPDATE_BUILD_UNSAFE')
    try:
        values = {}
        for line in config.decode('utf-8').splitlines():
            if line.startswith('CONFIG_') and '=' in line:
                key, value = line.split('=', 1)
                if key[7:] in values:
                    raise UpdateError('SWUPDATE_BUILD_UNSAFE')
                values[key[7:]] = value
    except UnicodeError as exc:
        raise UpdateError('SWUPDATE_CONFIG_MISMATCH') from exc
    if (any(values.get(key) != 'y' for key in ENABLED) or
            any(values.get(key, 'n') != 'n' for key in FORBIDDEN) or
            any(values.get(key) != '""' for key in ('SOCKET_CTRL_PATH', 'SOCKET_PROGRESS_PATH'))):
        raise UpdateError('SWUPDATE_BUILD_UNSAFE')


def load_bundle(root):
    root = Path(root).absolute()
    if not root.is_dir():
        raise UpdateError('RUNTIME_BUNDLE_MISSING')
    no_links(root, root.anchor)
    path = root / 'manifest.json'
    if not path.is_file():
        raise UpdateError('RUNTIME_BUNDLE_MISSING')
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise UpdateError('RUNTIME_BUNDLE_SCHEMA')
    manifest = json_object(path.read_bytes())
    _shape(manifest, ('schemaVersion', 'target', 'assets', 'profiles', 'engine', 'publicKey'), ('libraries',))
    if type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 1 or manifest['target'] != TARGET:
        raise UpdateError('RUNTIME_BUNDLE_TARGET')
    assets = manifest['assets']
    if not isinstance(assets, dict) or not 1 <= len(assets) <= MAX_FILES:
        raise UpdateError('RUNTIME_BUNDLE_SCHEMA')
    names = set()
    for name, digest in assets.items():
        relative_path(name)
        if (name == 'manifest.json' or name.casefold() in names or not isinstance(digest, str) or
                not SHA256.fullmatch(digest)):
            raise UpdateError('RUNTIME_BUNDLE_SCHEMA')
        names.add(name.casefold())
    actual = set()
    for directory, directories, entries in os.walk(root, followlinks=False):
        for name in directories + entries:
            item = Path(directory) / name
            if item.is_symlink():
                raise UpdateError('RUNTIME_BUNDLE_PATH')
        for name in entries:
            item = Path(directory) / name
            if not stat.S_ISREG(item.stat().st_mode):
                raise UpdateError('RUNTIME_BUNDLE_PATH')
            actual.add(item.relative_to(root).as_posix())
    if actual != set(assets) | {'manifest.json'}:
        raise UpdateError('RUNTIME_BUNDLE_UNLISTED_FILE')
    files, hashes, size = {}, {}, 0
    for name, expected in assets.items():
        item = root / name
        size += item.stat().st_size
        if size > MAX_TOTAL:
            raise UpdateError('RUNTIME_BUNDLE_TOO_LARGE')
        data = item.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected or data.startswith(b'version https://git-lfs.github.com/spec/'):
            raise UpdateError('RUNTIME_BUNDLE_CHANGED')
        files[name], hashes[name] = data, digest
    try:
        _engine(manifest, files)
        key_name = relative_path(manifest['publicKey'])
        key = files[key_name]
        if b'TEST_ONLY' in key or b'PRIVATE KEY' in key:
            raise UpdateError('RUNTIME_RELEASE_KEY_INVALID')
        # Verify the frozen bytes, never reopen the potentially changing checkout.
        with tempfile.TemporaryDirectory(prefix='mingda-runtime-key-') as temporary:
            key_path = Path(temporary) / 'public.pem'
            key_path.write_bytes(key)
            require_rsa_key(key_path)
        libraries = manifest.get('libraries', [])
        if not isinstance(libraries, list) or len(libraries) != len(set(libraries)):
            raise UpdateError('RUNTIME_BUNDLE_SCHEMA')
        for name in libraries:
            relative_path(name)
            if not name.startswith('lib/') or '/' in name[4:] or '.so' not in name:
                raise UpdateError('RUNTIME_BUNDLE_PATH')
            _elf(files[name])
        profiles = manifest['profiles']
        if not isinstance(profiles, list) or not profiles:
            raise UpdateError('RUNTIME_BUNDLE_SCHEMA')
        seen = set()
        for profile in profiles:
            _shape(profile, ('device', 'policy', 'baseline'))
            device = profile['device']
            _shape(device, DEVICE_FIELDS)
            if any(not isinstance(device[key], str) or not PART.fullmatch(device[key]) for key in DEVICE_FIELDS):
                raise UpdateError('RUNTIME_BUNDLE_TARGET')
            if any(device[key] != TARGET[key] for key in TARGET):
                raise UpdateError('RUNTIME_BUNDLE_TARGET')
            identity = tuple(device[key] for key in sorted(DEVICE_FIELDS))
            if identity in seen:
                raise UpdateError('RUNTIME_PROFILE_DUPLICATE')
            seen.add(identity)
            validate_policy(json_object(files[relative_path(profile['policy'])]), device)
            validate_inventory(json_object(files[relative_path(profile['baseline'])]), require_evidence=True)
    except (KeyError, TypeError) as exc:
        raise UpdateError('RUNTIME_BUNDLE_SCHEMA') from exc
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return {'manifest': manifest, 'files': MappingProxyType(files), 'hashes': MappingProxyType(hashes),
            'runtimeVersion': hashlib.sha256(encoded).hexdigest()}
