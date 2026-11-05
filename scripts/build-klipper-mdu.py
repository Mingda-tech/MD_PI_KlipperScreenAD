#!/usr/bin/env python3
"""Create an offline Klipper host-software package without MCU firmware actions."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://github.com/Mingda-tech/MD_PI_klipper.git'
LEGACY_ORIGIN = 'https://github.com/Mingda-tech/KlipperAD.git'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(args):
    source, output = Path(args.source).absolute(), Path(args.output).absolute()
    materials = output.with_suffix('.materials')
    if output.suffix != '.mdu' or output.exists() or materials.exists():
        raise ValueError('The output package and materials folder must not exist')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        raise ValueError('A complete source commit is required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', args.version) or args.sequence < 1:
        raise ValueError('Invalid package version or sequence')
    def git(*parts):
        result = subprocess.run(['git', *map(str, parts)], cwd=source, capture_output=True, check=False)
        if result.returncode:
            raise ValueError('Source Git command failed: ' + result.stderr.decode('utf-8', 'replace')[:500])
        return result.stdout
    remote = git('remote', 'get-url', 'origin').decode().strip().rstrip('/')
    if remote.removesuffix('.git').lower() != ORIGIN.removesuffix('.git').lower():
        raise ValueError('The source checkout is not the requested MD_PI_klipper repository')
    commit = git('rev-parse', args.commit + '^{commit}').decode().strip()
    target_version = git('describe', '--tags', '--always', commit).decode().strip()
    inventory = module('host_git_materials', ROOT / 'scripts/build-klipperscreen-mdu.py')
    inventory.ROOT = source
    files, size = inventory.release_files(commit)
    materials.mkdir(parents=True)
    reference = 'refs/heads/mdu-klipper-' + uuid.uuid4().hex
    created = False
    try:
        git('update-ref', reference, commit, '0' * 40)
        created = True
        bundle = materials / 'klipper.bundle'
        git('bundle', 'create', bundle, reference)
        git('bundle', 'verify', bundle)
        scripts = ROOT / 'scripts/swupdate/klipper_host'
        for name in ('installer.py', 'update.sh'):
            (materials / name).write_bytes((scripts / name).read_bytes().replace(b'\r\n', b'\n'))
        # This shared helper only stages and exchanges Git directories. No CAN
        # transport or physical firmware payload is part of the host package.
        (materials / 'host.py').write_bytes((ROOT / 'scripts/swupdate/klipper600du/host.py').read_bytes().replace(b'\r\n', b'\n'))
        (materials / 'README-Klipper.txt').write_bytes((scripts / 'README.md').read_bytes().replace(b'\r\n', b'\n'))
        (materials / 'COPYING.klipper').write_bytes(git('show', commit + ':COPYING'))
        (materials / 'klippy-requirements.txt').write_bytes(git('show', commit + ':scripts/klippy-requirements.txt'))
        sources = {p.name: str(p) for p in materials.iterdir() if p.is_file() and p.name != 'update.sh'}
        release_id = 'klipper-host-' + args.version
        spec = {'schemaVersion': 1, 'releaseId': release_id, 'service': 'klipper.service',
                'mainConfigurationPath': '/home/mingda/printer_data/config/printer.cfg',
                'host': {'targetPath': '/home/mingda/klipper', 'pythonPath': '/home/mingda/klippy-env/bin/python',
                         'targetCommit': commit, 'targetVersion': target_version, 'bundleRef': reference,
                         'bundleSha256': digest(bundle), 'targetFiles': files, 'targetBytes': size,
                         'allowedAncestors': git('rev-list', commit).decode().splitlines(),
                         'expectedRemote': ORIGIN, 'allowedSourceRemotes': [ORIGIN, LEGACY_ORIGIN],
                         'migrateRemote': True,
                         'dependencies': {'cffi': '1.14.6', 'pyserial': '3.4', 'Jinja2': '2.11.3',
                                          'python-can': '3.3.4', 'MarkupSafe': '1.1.1'}},
                'materials': {name: digest(path) for name, path in sources.items()}}
        release = materials / 'release.json'
        release.write_bytes(canonical(spec))
        sources['release.json'] = str(release)
        metadata = {'schemaVersion': 2, 'scriptApi': 1, 'releaseId': release_id, 'version': args.version,
                    'description': 'Klipper host software only; preserves printer configuration and installed MCU applications',
                    'components': [{'id': 'klipper', 'version': target_version, 'sequence': args.sequence}],
                    'firmware': [], 'configChanges': [], 'touchesFirmware': False, 'touchesScreen': False,
                    'bootRescue': False, 'recoveryMode': 'replay'}
        recipe = materials / 'recipe.json'
        recipe.write_bytes(canonical({'metadata': metadata, 'script': str(materials / 'update.sh'), 'sources': sources}))
        native = module('host_native_builder', ROOT / 'scripts/build-swu.py')
        native.build(recipe, args.key, output, openssl=args.openssl)
        checksum = digest(output)
        output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
        receipt = {'package': output.name, 'packageBytes': output.stat().st_size, 'packageSha256': checksum,
                   'version': args.version, 'sequence': args.sequence, 'releaseId': release_id,
                   'hostRepository': ORIGIN, 'hostCommit': commit, 'hostVersion': target_version,
                   'targetFiles': len(files), 'targetBytes': size, 'firmwareComponents': [],
                   'touchesFirmware': False, 'touchesScreen': False, 'requiresRegisteredComponents': [],
                   'unknownPreviousVersionAllowed': True, 'sourceMigrationExplicit': True,
                   'physicalPrinterAccessed': False, 'privateKeyIncluded': False}
        (materials / 'build-info.json').write_bytes(canonical(receipt))
        return receipt
    finally:
        if created:
            git('update-ref', '-d', reference, commit)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--key', required=True)
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args()), indent=2))
