#!/usr/bin/env python3
"""Build a signed Moonraker package with complete offline Python dependencies."""
import argparse
from email.parser import BytesParser
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tarfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://github.com/Mingda-tech/moonraker.git'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def project_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('Invalid dependency project name')
    return re.sub(r'[-_.]+', '-', value).lower()


def requirements(path):
    """Accept an explicit complete lock, never requirement URLs or pip options."""
    values = {}
    for number, raw in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        row = raw.strip()
        if not row or row.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)', row)
        if not match:
            raise ValueError('Only fully pinned name==version requirements are supported: line ' + str(number))
        name, version = project_name(match[1]), match[2]
        if name in values or name in ('pip', 'setuptools', 'wheel'):
            raise ValueError('Duplicate or environment bootstrap dependency: ' + name)
        values[name] = version
    if not values:
        raise ValueError('The offline dependency lock is empty')
    return dict(sorted(values.items()))


def wheel_inventory(directory, dependencies):
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('The wheelhouse must be a real directory')
    files, projects = {}, {}
    for path in sorted(directory.iterdir()):
        if (path.is_symlink() or not path.is_file()
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,240}\.whl', path.name)):
            raise ValueError('The wheelhouse may contain only regular wheel files')
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 4 * 1024 * 1024:
                raise ValueError('A wheel has invalid distribution metadata: ' + path.name)
            metadata = BytesParser().parsebytes(archive.read(names[0]))
        name = project_name(metadata.get('Name', ''))
        version = metadata.get('Version', '')
        if name not in dependencies or dependencies[name] != version or name in projects:
            raise ValueError('Wheel identity differs from the complete dependency lock: ' + path.name)
        files[path.name] = digest(path)
        projects[name] = path.name
    if set(projects) != set(dependencies):
        missing = ', '.join(sorted(set(dependencies) - set(projects)))
        raise ValueError('The offline wheelhouse is incomplete: ' + missing)
    return files


def archive_files(destination, files):
    """Store explicit regular files with stable metadata and portable paths."""
    with Path(destination).open('xb') as raw:
        with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for name, path in sorted(files.items()):
                    if (not name or '\\' in name or name.startswith('/')
                            or any(part in ('', '.', '..') for part in name.split('/'))):
                        raise ValueError('Unsafe archived material name')
                    path = Path(path)
                    if path.is_symlink() or not path.is_file():
                        raise ValueError('Archived materials must be regular files')
                    before = digest(path)
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = path.stat().st_size, 0o644
                    with path.open('rb') as stream:
                        archive.addfile(info, stream)
                    if digest(path) != before:
                        raise ValueError('Material changed while archiving: ' + name)


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
        raise ValueError('The source checkout is not the requested Moonraker repository')
    if git('status', '--porcelain=v1', '--untracked-files=no').strip():
        raise ValueError('Use a clean isolated source checkout for packaging')
    commit = git('rev-parse', args.commit + '^{commit}').decode().strip()
    target_version = git('describe', '--tags', '--always', commit).decode().strip()
    dependencies = requirements(args.requirements)
    wheelhouse = Path(args.wheelhouse).absolute()
    wheels = wheel_inventory(wheelhouse, dependencies)
    inventory = module('moonraker_git_materials', ROOT / 'scripts/build-klipperscreen-mdu.py')
    inventory.ROOT = source
    files, size = inventory.release_files(commit)
    materials.mkdir(parents=True)
    reference = 'refs/heads/mdu-moonraker-' + uuid.uuid4().hex
    created = False
    try:
        git('update-ref', reference, commit, '0' * 40)
        created = True
        bundle = materials / 'moonraker.bundle'
        # Tags are part of the application's own runtime Git version reporting.
        # Do not synthesize the printer's local v0.8.1 tag on the remote source.
        git('bundle', 'create', bundle, reference, '--tags')
        git('bundle', 'verify', bundle)
        scripts = ROOT / 'scripts/swupdate/moonraker'
        for name in ('installer.py', 'update.sh'):
            (materials / name).write_bytes((scripts / name).read_bytes().replace(b'\r\n', b'\n'))
        (materials / 'README-Moonraker.txt').write_bytes((scripts / 'README.md').read_bytes().replace(b'\r\n', b'\n'))
        (materials / 'LICENSE.moonraker').write_bytes(git('show', commit + ':LICENSE'))
        (materials / 'requirements.lock').write_text(
            ''.join(name + '==' + version + '\n' for name, version in dependencies.items()), encoding='utf-8')
        archive_files(materials / 'wheels.tar.gz', {name: wheelhouse / name for name in wheels})
        if wheel_inventory(wheelhouse, dependencies) != wheels:
            raise ValueError('The wheelhouse changed during packaging')
        license_arg = getattr(args, 'licenses', None)
        licenses = Path(license_arg).absolute() if license_arg else wheelhouse.parent / 'licenses'
        if license_arg or licenses.exists():
            if licenses.is_symlink() or not licenses.is_dir():
                raise ValueError('The dependency licenses must be a real directory')
            paths = {}
            for path in licenses.rglob('*'):
                if path.is_symlink():
                    raise ValueError('Linked dependency license material is not supported')
                if path.is_file():
                    paths[path.relative_to(licenses).as_posix()] = path
            if not paths:
                raise ValueError('The dependency license directory is empty')
            archive_files(materials / 'dependency-licenses.tar.gz', paths)
        sources = {path.name: str(path) for path in materials.iterdir() if path.is_file() and path.name != 'update.sh'}
        release_id = 'moonraker-' + args.version
        spec = {
            'schemaVersion': 1, 'releaseId': release_id, 'component': 'moonraker',
            'service': 'moonraker.service', 'targetPath': '/home/mingda/moonraker',
            'environmentPath': '/home/mingda/moonraker-env', 'dataPath': '/home/mingda/printer_data',
            'configurationPath': '/home/mingda/printer_data/config/moonraker.conf',
            'pythonVersion': [3, 10], 'architecture': 'aarch64',
            'source': {'targetCommit': commit, 'targetVersion': target_version, 'bundleRef': reference,
                       'bundleSha256': digest(bundle), 'targetFiles': files, 'targetBytes': size,
                       'expectedRemote': ORIGIN, 'allowedAncestors': git('rev-list', commit).decode().splitlines()},
            'wheels': wheels, 'dependencies': dependencies,
            'materials': {name: digest(path) for name, path in sources.items()},
        }
        release = materials / 'release.json'
        release.write_bytes(canonical(spec))
        sources['release.json'] = str(release)
        metadata = {
            'schemaVersion': 2, 'scriptApi': 1, 'releaseId': release_id, 'version': args.version,
            'description': 'Moonraker API server and complete offline dependencies; preserves printer configuration and MCU firmware',
            'components': [{'id': 'moonraker', 'version': target_version, 'sequence': args.sequence}],
            'firmware': [], 'configChanges': [], 'touchesFirmware': False, 'touchesScreen': False,
            'bootRescue': False, 'recoveryMode': 'replay',
        }
        recipe = materials / 'recipe.json'
        recipe.write_bytes(canonical({'metadata': metadata, 'script': str(materials / 'update.sh'), 'sources': sources}))
        native = module('moonraker_native_builder', ROOT / 'scripts/build-swu.py')
        native.build(recipe, args.key, output, openssl=args.openssl)
        checksum = digest(output)
        output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
        receipt = {
            'package': output.name, 'packageBytes': output.stat().st_size, 'packageSha256': checksum,
            'version': args.version, 'sequence': args.sequence, 'releaseId': release_id,
            'sourceRepository': ORIGIN, 'sourceCommit': commit, 'sourceVersion': target_version,
            'targetFiles': len(files), 'targetBytes': size, 'wheelCount': len(wheels),
            'dependencies': dependencies, 'architecture': 'aarch64', 'pythonVersion': [3, 10],
            'includesGitTags': True, 'touchesFirmware': False, 'touchesScreen': False,
            'requiresRegisteredComponents': [], 'unknownPreviousVersionAllowed': True,
            'physicalPrinterAccessed': False, 'privateKeyIncluded': False,
        }
        (materials / 'build-info.json').write_bytes(canonical(receipt))
        return receipt
    finally:
        if created:
            git('update-ref', '-d', reference, commit)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--wheelhouse', required=True)
    parser.add_argument('--requirements', required=True)
    parser.add_argument('--licenses', help='Optional dependency license directory; defaults to the wheelhouse sibling licenses/')
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--key', required=True)
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args()), indent=2))
