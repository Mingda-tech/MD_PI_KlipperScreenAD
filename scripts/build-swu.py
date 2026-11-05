#!/usr/bin/env python3
"""Create native SWUpdate2026.05 CPIO packages with signed package-owned scripts."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ks_includes'))
from offline_update.common import UpdateError, regular
from offline_update.swu_package import (
    RESERVED, flat_name, hardware_revisions, read_archive, require_rsa_key, validate_metadata,
)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_member(output, name, content, inode, mode=0o644):
    data = content if isinstance(content, bytes) else None
    size = len(data) if data is not None else content.stat().st_size
    if size > 0xffffffff:
        raise UpdateError('SWU_MEMBER_TOO_LARGE', name)
    encoded = name.encode('ascii') + b'\0'
    fields = (inode, stat.S_IFREG | mode, 0, 0, 1, 0, size, 0, 0, 0, 0, len(encoded), 0)
    header = b'070701' + b''.join(('%08x' % value).encode('ascii') for value in fields)
    output.write(header)
    output.write(encoded)
    output.write(b'\0' * ((-len(header) - len(encoded)) % 4))
    if data is not None:
        output.write(data)
    else:
        copied = 0
        with content.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                if copied + len(block) > size:
                    raise UpdateError('SOURCE_CHANGED', name)
                output.write(block)
                copied += len(block)
        if copied != size:
            raise UpdateError('SOURCE_CHANGED', name)
    output.write(b'\0' * ((-size) % 4))


def write_cpio(path, members):
    with Path(path).open('wb') as output:
        for inode, (name, content) in enumerate(members, 1):
            write_member(output, name, content, inode, 0o755 if name in ('update.sh', 'rescue.sh') else 0o644)
        write_member(output, 'TRAILER!!!', b'', len(members) + 1)
        output.write(b'\0' * ((-output.tell()) % 512))
        output.flush()
        os.fsync(output.fileno())


def build(recipe_path, key, output, openssl='openssl'):
    recipe_path = Path(recipe_path).resolve()
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    metadata = validate_metadata(copy.deepcopy(recipe['metadata']))
    sources = recipe.get('sources', {})
    if not isinstance(sources, dict):
        raise UpdateError('SWU_RECIPE_SOURCES')
    assets = {}
    for name, source in sources.items():
        flat_name(name)
        if name in RESERVED or not isinstance(source, str):
            raise UpdateError('SWU_RESERVED_NAME', name)
        assets[name] = regular(recipe_path.parent / source)
    script = regular(recipe_path.parent / recipe['script'])
    with script.open('rb') as stream:
        script_prefix = stream.read(128)
    if not script_prefix.startswith(b'#!/bin/sh\n'):
        raise UpdateError('SWU_SHELL_SCRIPT_REQUIRED', 'update.sh must start with LF-terminated #!/bin/sh')
    rescue = None
    if metadata.get('bootRescue'):
        if not recipe.get('rescueScript'):
            raise UpdateError('SWU_SCREEN_RESCUE_REQUIRED')
        rescue = regular(recipe_path.parent / recipe['rescueScript'])
        with rescue.open('rb') as stream:
            prefix = stream.read(128)
        if not prefix.startswith(b'#!/bin/sh\n'):
            raise UpdateError('SWU_SHELL_SCRIPT_REQUIRED', 'rescue.sh must start with LF-terminated #!/bin/sh')
    elif recipe.get('rescueScript'):
        raise UpdateError('SWU_SCREEN_RESCUE_DECLARATION')
    bits = require_rsa_key(key, private=True, openssl=openssl)
    metadata_raw = canonical(metadata)
    rows = [{'filename': 'mingda-meta.json', 'type': 'dummy',
             'sha256': hashlib.sha256(metadata_raw).hexdigest()}]
    rows += [{'filename': name, 'type': 'dummy', 'sha256': file_digest(path)} for name, path in sorted(assets.items())]
    description = {'software': {
        'version': metadata['version'], 'description': metadata.get('description', metadata['releaseId']),
        'hardware-compatibility': hardware_revisions(),
        'bootloader_transaction_marker': False, 'bootloader_state_marker': False, 'reboot': False,
        'stable': {'main': {
            'images': rows,
            'scripts': [{'filename': 'update.sh', 'type': 'shellscript', 'sha256': file_digest(script)}],
        }},
    }}
    if rescue:
        description['software']['stable']['rescue'] = {
            'images': rows,
            'scripts': [{'filename': 'rescue.sh', 'type': 'shellscript', 'sha256': file_digest(rescue)}],
        }
    description_raw = canonical(description)
    output = Path(output)
    if output.suffix != '.mdu' or output.exists():
        raise UpdateError('SWU_OUTPUT_EXISTS_OR_EXTENSION')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='swu-sign-') as temporary:
        root = Path(temporary)
        (root / 'sw-description').write_bytes(description_raw)
        subprocess.run([openssl, 'dgst', '-sha256', '-sign', str(key),
                        '-out', str(root / 'sw-description.sig'), str(root / 'sw-description')],
                       check=True, timeout=30, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        signature = (root / 'sw-description.sig').read_bytes()
        if len(signature) != (bits + 7) // 8:
            raise UpdateError('SWU_SIGNATURE_SIZE')
        descriptor, partial_name = tempfile.mkstemp(
            prefix='.' + output.name + '-', suffix='.partial', dir=output.parent)
        os.close(descriptor)
        partial = Path(partial_name)
        try:
            members = [('sw-description', description_raw), ('sw-description.sig', signature),
                       ('mingda-meta.json', metadata_raw), ('update.sh', script)] + sorted(assets.items())
            if rescue:
                members.append(('rescue.sh', rescue))
            write_cpio(partial, members)
            read_archive(partial)  # Also catches changing source files between hash and write.
            # Publish without replacing another simultaneous publisher's output.
            if os.name == 'nt':
                os.rename(partial, output)  # Windows rename refuses an existing destination.
            else:
                os.link(partial, output)
        finally:
            partial.unlink(missing_ok=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recipe')
    parser.add_argument('--key', required=True, help='RSA private PEM, at least2048 bits; not included in the SWU')
    parser.add_argument('--output', required=True)
    parser.add_argument('--openssl', default='openssl')
    args = parser.parse_args()
    print(build(args.recipe, args.key, args.output, args.openssl))
