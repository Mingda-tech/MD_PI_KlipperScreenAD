#!/usr/bin/env python3
"""Wrap the supplied SDK/AI release in a signed, package-owned offline update."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def build_managed(args):
    archive, output = Path(args.archive).absolute(), Path(args.output).absolute()
    materials = output.with_suffix('.materials')
    if output.suffix != '.mdu' or output.exists() or materials.exists():
        raise ValueError('The output package and materials folder must not exist')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', args.version) or args.sequence < 1:
        raise ValueError('Invalid package version or sequence')
    scripts = ROOT / 'scripts/swupdate/sdk'
    reader = module('sdk_release_archive', scripts / 'archive.py')
    archive_digest = digest(archive)
    output.parent.mkdir(parents=True, exist_ok=True)
    materials.mkdir()
    layout = reader.build_optimized_payloads(archive, materials)
    for name in ('installer.py', 'archive.py', 'compatibility.py', 'state_backup.py', 'retention.py', 'update.sh'):
        (materials / name).write_bytes((scripts / name).read_bytes().replace(b'\r\n', b'\n'))
    (materials / 'README-SDK.txt').write_bytes((scripts / 'README.md').read_bytes().replace(b'\r\n', b'\n'))
    if digest(archive) != archive_digest:
        raise ValueError('The source SDK archive changed during packaging')
    sources = {p.name: str(p) for p in materials.iterdir() if p.is_file() and p.name != 'update.sh'}
    release_id = 'sdk-ai-' + args.version
    spec = {
        'schemaVersion': 1, 'releaseId': release_id, 'component': 'sdk-ai',
        'source': {'layoutVersion': 2, 'layoutFile': 'layout.json',
                   'archiveRoot': layout['sourceArchiveRoot'],
                   'archiveBytes': archive.stat().st_size, 'archiveSha256': archive_digest,
                   'sdkVersion': layout['sdkVersion'], 'runtimeVariants': layout['runtimeVariants'],
                   'catalog': layout['catalog'], 'regularBytes': layout['regularBytes'],
                   'entryCount': layout['entryCount'], 'sourceEntryCount': layout['sourceEntries']},
        'targetRoots': {'sdk': '/opt/mingda/apps/sdk', 'ai-runtime': '/opt/mingda/apps/ai-runtime'},
        'services': {'sdk': 'mingda-app@sdk.service', 'ai-runtime': 'mingda-app@ai-runtime.service'},
        'materials': {name: digest(path) for name, path in sources.items()},
    }
    release = materials / 'release.json'
    release.write_bytes(canonical(spec))
    sources['release.json'] = str(release)
    metadata = {
        'schemaVersion': 2, 'scriptApi': 1, 'releaseId': release_id, 'version': args.version,
        'description': 'SDK and matching X527 AI runtime/models from the supplied release archive',
        'components': [{'id': 'sdk', 'version': layout['sdkVersion'], 'sequence': args.sequence},
                       {'id': 'ai-runtime', 'version': args.version, 'sequence': args.sequence}],
        'firmware': [], 'configChanges': [], 'touchesFirmware': False,
        'touchesScreen': False, 'bootRescue': False, 'recoveryMode': 'replay',
    }
    recipe = materials / 'recipe.json'
    recipe.write_bytes(canonical({'metadata': metadata, 'script': str(materials / 'update.sh'), 'sources': sources}))
    native = module('sdk_native_builder', ROOT / 'scripts/build-swu.py')
    native.build(recipe, args.key, output, openssl=args.openssl)
    checksum = digest(output)
    output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
    receipt = {
        'package': output.name, 'packageBytes': output.stat().st_size, 'packageSha256': checksum,
        'version': args.version, 'sequence': args.sequence, 'releaseId': release_id,
        'sourceArchive': archive.name, 'sourceArchiveSha256': archive_digest,
        'sourceArchiveBytes': archive.stat().st_size, 'sourceArchiveRoot': layout['sourceArchiveRoot'],
        'sourceArchiveBytesPreserved': False, 'releaseFileBytesPreserved': True, 'sdkVersion': layout['sdkVersion'],
        'runtimeVariants': layout['runtimeVariants'], 'entryCount': layout['entryCount'],
        'sourceEntryCount': layout['sourceEntries'], 'regularBytes': layout['regularBytes'],
        'sourceRegularBytes': layout['sourceRegularBytes'], 'layoutVersion': 2,
        'payloads': {name: {'entries': value['entryCount'], 'regularBytes': value['regularBytes'],
                           'compressedBytes': (materials / name).stat().st_size}
                     for name, value in layout['payloads'].items()}, 'selectedComponents': ['sdk', 'ai-runtime'],
        'installsUpdater': False, 'installsJanus': False, 'installsLockSupport': False,
        'touchesFirmware': False, 'touchesScreen': False, 'modifiesCorePrinterServices': False,
        'unknownPreviousVersionAllowed': True, 'requiresRegisteredComponents': [],
        'productFileShaPolicy': 'preserve-source-policy-no-new-file-or-model-digest-gates',
        'outerSignature': 'RSA/SHA256', 'privateKeyIncluded': False, 'physicalPrinterAccessed': False,
    }
    (materials / 'build-info.json').write_bytes(canonical(receipt))
    return receipt


def platform_archive(archive, reader):
    """Publish a complete safety audit bound to the original compressed bytes."""
    audit = reader.create_archive_certificate(archive)
    if len(audit['root']) > 128 or not re.fullmatch(r'\d+\.\d+\.\d+', audit['sdkVersion']):
        raise ValueError('Platform release requires a supported root name and three-part SDK version')
    if audit['hasScreenPatch']:
        raise ValueError('Platform bundles that replace screen files need a dedicated screen rescue package')
    if audit['forwardHardlinkCount']:
        raise ValueError('Published platform archives must order hardlinks after their targets')
    entries = audit['entrypoints']
    for name in ('install.sh', 'scripts/platform-install.sh'):
        value = entries.get(name)
        if value is None:
            raise ValueError('The complete platform archive must include install.sh and scripts/platform-install.sh')
        if value.get('kind') != 'file' or value.get('bytes', 0) < 1:
            raise ValueError('Platform installer entry points must be nonempty regular files')
    return audit


def build_platform(args):
    archive, output = Path(args.archive).absolute(), Path(args.output).absolute()
    materials = output.with_suffix('.materials')
    if output.suffix != '.mdu' or output.exists() or materials.exists():
        raise ValueError('The output package and materials folder must not exist')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', args.version) or args.sequence < 1:
        raise ValueError('Invalid package version or sequence')
    updater_version = getattr(args, 'updater_version', None)
    if not isinstance(updater_version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', updater_version):
        raise ValueError('Platform offline mode requires the source archive Updater version')
    scripts = ROOT / 'scripts/swupdate/sdk'
    reader = module('sdk_platform_archive', scripts / 'archive.py')
    archive_digest = digest(archive)
    archive_bytes = archive.stat().st_size
    audit = platform_archive(archive, reader)
    if audit['archiveSha256'] != archive_digest or audit['archiveBytes'] != archive_bytes:
        raise ValueError('The source platform archive changed during its publication audit')
    output.parent.mkdir(parents=True, exist_ok=True)
    materials.mkdir()
    payload = materials / 'platform.tar.gz'
    shutil.copyfile(archive, payload)
    if digest(payload) != archive_digest or digest(archive) != archive_digest:
        raise ValueError('The source platform archive changed during packaging')
    for name in ('platform_installer.py', 'archive.py'):
        (materials / name).write_bytes((scripts / name).read_bytes().replace(b'\r\n', b'\n'))
    (materials / 'update.sh').write_bytes((scripts / 'platform-update.sh').read_bytes().replace(b'\r\n', b'\n'))
    (materials / 'README-SDK.txt').write_bytes((scripts / 'README.md').read_bytes().replace(b'\r\n', b'\n'))
    certificate = materials / 'platform-audit.json'
    certificate.write_bytes(canonical(audit))
    release_id = 'sdk-platform-' + args.version
    release_spec = {
        'schemaVersion': 2, 'installMode': 'platform-offline', 'releaseId': release_id,
        'source': {'filename': payload.name, 'sha256': archive_digest, 'bytes': archive_bytes,
                   'root': audit['root'], 'regularBytes': audit['regularBytes'], 'entryCount': audit['entryCount']},
        'sdkVersion': audit['sdkVersion'], 'updaterVersion': updater_version,
        'audit': {'name': certificate.name, 'sha256': digest(certificate), 'bytes': certificate.stat().st_size},
    }
    release = materials / 'platform-release.json'
    release.write_bytes(canonical(release_spec))
    sources = {path.name: str(path) for path in materials.iterdir()
               if path.is_file() and path.name != 'update.sh'}
    metadata = {
        'schemaVersion': 2, 'scriptApi': 1, 'releaseId': release_id, 'version': args.version,
        'description': 'Complete SDK platform release using its original install.sh --offline workflow',
        'components': [{'id': 'sdk', 'version': audit['sdkVersion'], 'sequence': args.sequence},
                       {'id': 'ai-runtime', 'version': args.version, 'sequence': args.sequence},
                       {'id': 'updater', 'version': updater_version, 'sequence': args.sequence}],
        'firmware': [],
        'configChanges': [
            {'path': '/etc/mingda', 'op': 'update',
             'description': ('The original platform installer validates device identity '
                             'and preserves or migrates configuration.')},
            {'path': '/etc/mingda/apps/sdk', 'op': 'update',
             'description': 'SDK configuration is handled by the original platform offline installer.'},
            {'path': '/opt/mingda/janus', 'op': 'update',
             'description': 'The platform workflow may install or update the bundled Janus integration.'},
            {'path': '/usr/libexec/mingda', 'op': 'update',
             'description': 'The platform workflow may update device-lock and inspection support.'},
            {'path': '/etc/systemd/system', 'op': 'update',
             'description': 'The platform workflow may update service integration and restart Klipper or Moonraker.'},
        ],
        'touchesFirmware': False, 'touchesScreen': False, 'bootRescue': False, 'recoveryMode': 'none',
    }
    recipe = materials / 'recipe.json'
    recipe.write_bytes(canonical({'metadata': metadata, 'script': str(materials / 'update.sh'), 'sources': sources}))
    native = module('sdk_platform_native_builder', ROOT / 'scripts/build-swu.py')
    native.build(recipe, args.key, output, openssl=args.openssl)
    checksum = digest(output)
    output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
    receipt = {
        'package': output.name, 'packageBytes': output.stat().st_size, 'packageSha256': checksum,
        'version': args.version, 'sequence': args.sequence, 'releaseId': release_id,
        'installMode': 'platform-offline', 'sourceArchive': archive.name,
        'sourceArchiveSha256': archive_digest, 'sourceArchiveBytes': archive_bytes,
        'sourceArchiveRoot': audit['root'], 'sourceArchiveBytesPreserved': True,
        'releaseFileBytesPreserved': True, 'sdkVersion': audit['sdkVersion'],
        'updaterVersion': updater_version, 'runtimeVariants': audit['runtimeVariants'],
        'entryCount': audit['entryCount'], 'regularBytes': audit['regularBytes'],
        'selectedComponents': ['sdk', 'ai-runtime', 'updater'],
        'installerCommands': [['bash', 'install.sh', '--offline', '--verify-only'],
                              ['bash', 'install.sh', '--offline']],
        'installsUpdater': True, 'installsJanus': True, 'installsLockSupport': True,
        'modifiesCorePrinterServices': True, 'mayRestartCorePrinterServices': True,
        'requiresHealthySdkBaseline': False, 'touchesFirmware': False, 'touchesScreen': False,
        'recoveryMode': 'none', 'cloudReadinessPolicy': 'deferred-by-original-offline-installer',
        'archiveAuditLocation': 'publisher', 'archiveAuditPolicy': audit['policy'],
        'archiveAuditSha256': digest(certificate), 'deviceArchiveMetadataScans': 0,
        'extractionProgress': 'gnu-tar-uncompressed-bytes', 'tarBytes': audit['tarBytes'],
        'productFileShaPolicy': 'preserve-source-policy-no-new-file-or-model-digest-gates',
        'outerSignature': 'RSA/SHA256', 'privateKeyIncluded': False, 'physicalPrinterAccessed': False,
    }
    (materials / 'build-info.json').write_bytes(canonical(receipt))
    return receipt


def build(args):
    # Programmatic callers predating install_mode keep the managed contract.
    mode = getattr(args, 'install_mode', 'managed')
    if mode == 'managed':
        return build_managed(args)
    if mode == 'platform-offline':
        return build_platform(args)
    raise ValueError('Unsupported SDK install mode')


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-mode', choices=('platform-offline', 'managed'), default='platform-offline',
                        help='Use the complete platform offline installer by default; '
                             'managed is the legacy SDK/AI path')
    parser.add_argument('--archive', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--updater-version',
                        help='Updater version in the supplied platform archive; required in platform mode')
    parser.add_argument('--key', required=True)
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--output', required=True)
    return parser


if __name__ == '__main__':
    print(json.dumps(build(argument_parser().parse_args()), indent=2))
