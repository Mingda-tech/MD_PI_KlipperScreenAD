#!/usr/bin/env python3
"""Validate the pinned engine profile and produce a binary/config receipt."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

VERSION = '2026.05'
COMMIT = '602edf8e57f940791034096c9e24206097b4b7a6'
REQUIRED = ('SCRIPTS', 'HW_COMPATIBILITY', 'SHELLSCRIPTHANDLER', 'SSL_IMPL_OPENSSL',
            'SIGNED_IMAGES', 'SIGALG_RAWRSA', 'HASH_VERIFY', 'BOOTLOADER_NONE',
            'BOOTLOADER_DEFAULT_NONE', 'BOOTLOADER_STATIC_LINKED')
FORBIDDEN = ('LUA', 'LUAEXTERNAL', 'LUASCRIPTHANDLER', 'EMBEDDED_LUA_HANDLER',
             'DOWNLOAD', 'SURICATTA', 'WEBSERVER', 'MTD', 'UBIVOL', 'UBOOT',
             'BOOTLOADER_GRUB', 'BOOTLOADER_EBG', 'BOOTLOADER_CBOOT', 'BOOTLOADERHANDLER',
             'RAW', 'ARCHIVE', 'COPY', 'DISKPART', 'DISKFORMAT_HANDLER', 'REMOTE_HANDLER',
             'UCFWHANDLER', 'ENCRYPTED_IMAGES', 'ENCRYPTED_SW_DESCRIPTION',
             'SIGALG_RSAPSS', 'SIGALG_CMS', 'SIGALG_GPG', 'SSL_IMPL_WOLFSSL',
             'SSL_IMPL_MBEDTLS', 'SSL_IMPL_GPGME', 'NOCLEANUP')


def check_config(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            name, value = line.split('=', 1)
            values[name[7:]] = value
    for name in REQUIRED:
        if values.get(name) != 'y':
            raise ValueError('Required engine option missing: CONFIG_' + name)
    for name in FORBIDDEN:
        if values.get(name, 'n') != 'n':
            raise ValueError('Forbidden engine option enabled: CONFIG_' + name)
    for name in ('SOCKET_CTRL_PATH', 'SOCKET_PROGRESS_PATH'):
        if values.get(name) != '""':
            raise ValueError('Runtime-private socket option must be empty: CONFIG_' + name)
    return {name: values.get(name) == 'y' for name in REQUIRED + FORBIDDEN}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def receipt(binary, config, arch, development):
    features = check_config(config)
    with Path(binary).open('rb') as source:
        header = source.read(20)
    machines = {'aarch64': 183, 'x86_64': 62}
    if (arch not in machines or header[:6] != b'\x7fELF\x02\x01' or
            len(header) != 20 or struct.unpack('<H', header[18:20])[0] != machines[arch]):
        raise ValueError('Engine ELF architecture mismatch')
    if not development and arch != 'aarch64':
        raise ValueError('Only aarch64 is a production engine target')
    completed = subprocess.run([str(binary), '--version'], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, check=True, timeout=10)
    if not re.fullmatch(r'SWUpdate v2026\.05(?:\.0)?', completed.stdout.strip()):
        raise ValueError('Unexpected SWUpdate engine version: ' + completed.stdout.strip())
    return {'schemaVersion': 1, 'version': VERSION, 'sourceCommit': COMMIT,
            'configSha256': sha256(config), 'binarySha256': sha256(binary), 'arch': arch,
            'features': features, 'socketPaths': {'control': '', 'progress': ''},
            'developmentBuild': development}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-config')
    parser.add_argument('--binary')
    parser.add_argument('--config')
    parser.add_argument('--output')
    parser.add_argument('--arch', choices=('aarch64', 'x86_64'))
    parser.add_argument('--development-build', choices=('true', 'false'), default='false')
    args = parser.parse_args()
    if args.check_config:
        check_config(args.check_config)
        print('Pinned SWUpdate feature profile verified')
        return
    if not all((args.binary, args.config, args.output, args.arch)):
        parser.error('--binary, --config, --output and --arch are required')
    result = receipt(args.binary, args.config, args.arch, args.development_build == 'true')
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
