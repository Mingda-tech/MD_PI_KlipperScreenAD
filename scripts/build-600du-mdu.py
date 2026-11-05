#!/usr/bin/env python3
"""Build the signed, guarded 600DU Klipper and head-firmware update package."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(args):
    source, output = Path(args.source).absolute(), Path(args.output).absolute()
    materials = output.with_suffix('.materials')
    if output.suffix != '.mdu' or output.exists() or materials.exists():
        raise ValueError('Output package and its materials directory must not exist')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', args.version) or args.sequence < 1:
        raise ValueError('Invalid release version or sequence')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        raise ValueError('A complete, verified source commit is required')
    def git(*command):
        result = subprocess.run(['git', *map(str, command)], cwd=source, capture_output=True, check=False)
        if result.returncode:
            raise ValueError('Source Git command failed: ' + result.stderr.decode('utf-8', 'replace')[:500])
        return result.stdout
    remote = git('remote', 'get-url', 'origin').decode().strip().rstrip('/')
    if remote.removesuffix('.git').lower() != 'https://github.com/mingda-tech/md_pi_klipper':
        raise ValueError('The source checkout is not the requested MD_PI_klipper repository')
    commit = git('rev-parse', args.commit + '^{commit}').decode().strip()
    host_version = git('describe', '--always', '--tags', commit).decode().strip()
    ancestors = git('rev-list', commit).decode().splitlines()
    shared = load_module('screen_git_materials', ROOT / 'scripts/build-klipperscreen-mdu.py')
    shared.ROOT = source
    files, size = shared.release_files(commit)
    materials.mkdir(parents=True)
    reference = 'refs/heads/mdu-600du-' + uuid.uuid4().hex
    created = False
    try:
        git('update-ref', reference, commit, '0' * 40)
        created = True
        bundle = materials / 'klipper.bundle'
        git('bundle', 'create', bundle, reference)
        git('bundle', 'verify', bundle)
        firmware_path = materials / 'head.bin'
        firmware_path.write_bytes(Path(args.firmware).read_bytes())
        template = materials / 'load_cell.cfg'
        template.write_bytes(Path(args.configuration).read_bytes())
        tool = materials / 'flash_can.py'
        tool.write_bytes(git('show', commit + ':lib/canboot/flash_can.py'))
        (materials / 'COPYING.klipper').write_bytes(git('show', commit + ':COPYING'))
        scripts = ROOT / 'scripts/swupdate/klipper600du'
        for name in ('installer.py', 'host.py', 'firmware.py', 'configuration.py', 'update.sh'):
            (materials / name).write_bytes((scripts / name).read_bytes().replace(b'\r\n', b'\n'))
        (materials / 'README-600DU.txt').write_bytes((scripts / 'README.md').read_bytes().replace(b'\r\n', b'\n'))
        firmware = {'canbusUuid': '4d4400000a02', 'canInterface': 'can0', 'mcuType': 'stm32f072xb',
                    'applicationStart': 0x08002000, 'flashEnd': 0x08020000,
                    'imageBytes': firmware_path.stat().st_size, 'imageSha256': sha(firmware_path),
                    'toolSha256': sha(tool),
                    'firmwareVersion': 'v0.13.1-34-g0770141c-dirty-20260715_224036-MINGDA'}
        if firmware['imageSha256'] != '62f476a00b1bddcfbcd02bac045c69ca30358916bc97189d4b57412d4ea3d08a':
            raise ValueError('This recipe is limited to the verified, supplied 600DU head image')
        guard = load_module('head_firmware_guard', scripts / 'firmware.py')
        firmware_receipt = guard.validate_image(firmware_path, firmware)
        (materials / 'firmware-receipt.json').write_bytes(canonical(firmware_receipt))
        (materials / 'klippy-requirements.txt').write_bytes(git('show', commit + ':scripts/klippy-requirements.txt'))
        if args.guide:
            (materials / 'operation-guide.txt').write_bytes(Path(args.guide).read_bytes())
        sources = {path.name: str(path) for path in materials.iterdir() if path.is_file() and path.name != 'update.sh'}
        release_id = '600du-klipper-head-' + args.version
        spec = {'schemaVersion': 1, 'releaseId': release_id, 'service': 'klipper.service',
                'mcuSection': 'mcu 600D_HB', 'firmware': firmware,
                'configurationPath': '/home/mingda/printer_data/config/load_cell.cfg',
                'mainConfigurationPath': '/home/mingda/printer_data/config/printer.cfg',
                'configurationTemplateSha256': sha(template),
                'host': {'targetPath': '/home/mingda/klipper', 'pythonPath': '/home/mingda/klippy-env/bin/python',
                         'targetCommit': commit, 'targetVersion': host_version, 'bundleRef': reference,
                         'bundleSha256': sha(bundle), 'targetFiles': files, 'targetBytes': size,
                         'allowedAncestors': ancestors, 'expectedRemote': 'https://github.com/Mingda-tech/MD_PI_klipper.git',
                         'dependencies': {'cffi': '1.14.6', 'pyserial': '3.4', 'Jinja2': '2.11.3',
                                          'python-can': '3.3.4', 'MarkupSafe': '1.1.1'}},
                'materials': {name: sha(path) for name, path in sources.items()}}
        release = materials / 'release.json'
        release.write_bytes(canonical(spec)); sources['release.json'] = str(release)
        metadata = {'schemaVersion': 2, 'scriptApi': 1, 'releaseId': release_id, 'version': args.version,
                    'description': '600DU only: matched Klipper host, CAN head application and pressure-probe configuration',
                    'components': [{'id': 'klipper', 'version': host_version, 'sequence': args.sequence}],
                    'firmware': [{'id': 'headboard-600du', 'version': firmware['firmwareVersion'],
                                  'buildVersion': firmware['firmwareVersion'], 'sequence': args.sequence}],
                    'configChanges': [{'path': spec['configurationPath'], 'op': 'merge',
                                       'description': 'Enable CS1237 v2 while preserving calibration and existing macros'}],
                    'touchesFirmware': True, 'touchesScreen': False, 'bootRescue': False, 'recoveryMode': 'replay',
                    'hardware': {'device': '600DU', 'canbusUuid': firmware['canbusUuid'], 'mcu': firmware['mcuType']}}
        recipe_path = materials / 'recipe.json'
        recipe_path.write_bytes(canonical({'metadata': metadata, 'script': str(materials / 'update.sh'), 'sources': sources}))
        builder = load_module('native_600du_builder', ROOT / 'scripts/build-swu.py')
        builder.build(recipe_path, args.key, output, openssl=args.openssl)
        checksum = sha(output)
        output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
        receipt = {'package': output.name, 'packageBytes': output.stat().st_size, 'packageSha256': checksum,
                   'releaseId': release_id, 'version': args.version, 'sequence': args.sequence,
                   'hostRepository': remote, 'hostCommit': commit, 'hostVersion': host_version,
                   'targetFiles': len(files), 'targetBytes': size, 'firmwareSha256': firmware['imageSha256'],
                   'firmwareVersion': firmware['firmwareVersion'], 'canbusUuid': firmware['canbusUuid'],
                   'requiresRegisteredComponents': [], 'unknownPreviousVersionAllowed': True,
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
    parser.add_argument('--firmware', required=True)
    parser.add_argument('--configuration', required=True)
    parser.add_argument('--guide')
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--key', required=True)
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args()), indent=2))
