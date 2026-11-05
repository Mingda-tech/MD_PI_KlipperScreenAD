#!/usr/bin/env python3
"""Build a signed, self-contained Mainsail static-site MDU from a fixed Git source."""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tarfile
import io

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://github.com/Mingda-tech/mainsail.git'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def safe_name(name):
    if (not name or name.startswith('/') or '\\' in name or ':' in name
            or any(p in ('', '.', '..') for p in name.split('/'))):
        raise ValueError('Invalid static file path')
    return name


def tar_files(path, source, names):
    with Path(path).open('xb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as archive:
            for name in sorted(names):
                safe_name(name)
                source_path = source / name
                if not source_path.is_file() or source_path.is_symlink():
                    raise ValueError('Static output contains a link or non-file')
                data = source_path.read_bytes()
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.mtime = len(data), 0o644, 0
                archive.addfile(member, io.BytesIO(data))


def build(args):
    source, dist, output = Path(args.source).absolute(), Path(args.dist).absolute(), Path(args.output).absolute()
    materials = output.with_suffix('.materials')
    if output.suffix != '.mdu' or output.exists() or materials.exists():
        raise ValueError('Output package and materials must not exist')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        raise ValueError('A full source Git commit is required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', args.version) or args.sequence < 1:
        raise ValueError('Invalid version or sequence')
    def git(*parts):
        result = subprocess.run(['git', *map(str, parts)], cwd=source, capture_output=True)
        if result.returncode:
            raise ValueError('Source Git command failed: ' + result.stderr.decode('utf-8', 'replace')[:400])
        return result.stdout
    origin = git('remote', 'get-url', 'origin').decode().strip().rstrip('/')
    if origin.removesuffix('.git').lower() != ORIGIN.removesuffix('.git').lower():
        raise ValueError('Unexpected Mainsail source repository')
    commit = git('rev-parse', 'HEAD').decode().strip()
    if commit != args.commit or git('status', '--porcelain', '--untracked-files=no').strip():
        raise ValueError('Build source must be the exact clean requested commit')
    package = json.loads(git('show', commit + ':package.json'))
    target_version = 'v' + package['version']
    if (dist / '.version').read_text().strip() != target_version:
        raise ValueError('Built static version does not match source')
    release = json.loads((dist / 'release_info.json').read_text())
    if release != {'project_name': 'mainsail', 'project_owner': 'Mingda-tech', 'version': target_version}:
        raise ValueError('Built release metadata does not match source')
    files = {}
    for path in sorted(dist.rglob('*')):
        if path.is_symlink():
            raise ValueError('Static output contains a symbolic link')
        if path.is_file():
            name = safe_name(path.relative_to(dist).as_posix())
            if name == 'mainsail.zip':
                continue
            files[name] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
    html = (dist / 'index.html').read_text(encoding='utf-8')
    urls = re.findall(r'(?:src|href)=["\'](/[^"\']+)["\']', html)
    http_files = {'index.html', '.version', 'release_info.json', 'manifest.webmanifest', 'sw.js'}
    for value in urls:
        name = value.split('?', 1)[0].lstrip('/')
        if name:
            safe_name(name)
            http_files.add(name)
    http_files.update(name for name in files if re.fullmatch(r'workbox-[0-9a-f]+\.js', name))
    if not http_files <= set(files) or not any(name.startswith('assets/') for name in http_files):
        raise ValueError('Built index references absent or unversioned entry resources')
    materials.mkdir(parents=True)
    scripts = ROOT / 'scripts/swupdate/mainsail'
    for name in ('installer.py', 'update.sh'):
        (materials / name).write_bytes((scripts / name).read_bytes().replace(b'\r\n', b'\n'))
    (materials / 'README-Mainsail.txt').write_bytes((scripts / 'README.md').read_bytes().replace(b'\r\n', b'\n'))
    (materials / 'COPYING.mainsail').write_bytes(git('show', commit + ':LICENSE'))
    archive = materials / 'mainsail-dist.tar.gz'
    tar_files(archive, dist, files)
    # git archive reads only the specified commit and excludes ignored local
    # build outputs, credentials, Git metadata, and node_modules.
    raw_source = git('archive', '--format=tar', commit)
    with (materials / 'mainsail-source.tar.gz').open('xb') as stream:
        with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as zipped:
            zipped.write(raw_source)
    build_notes = ('Repository: ' + ORIGIN + '\nCommit: ' + commit + '\nVersion: ' + target_version
                   + '\nNode: ' + args.node_version + '\nNpm: ' + args.npm_version
                   + '\nLockfile SHA256: ' + hashlib.sha256(git('show', commit + ':package-lock.json')).hexdigest()
                   + '\nCommands: npm ci --no-audit --no-fund; npx --no-install vite build\n'
                   + 'CYPRESS_INSTALL_BINARY=0 skips only the browser test binary download.\n'
                   + 'No source changes, no runtime network access, no node_modules included.\n')
    (materials / 'BUILD-Mainsail.txt').write_text(build_notes, encoding='utf-8')
    sources = {p.name: str(p) for p in materials.iterdir() if p.is_file() and p.name != 'update.sh'}
    release_id = 'mainsail-' + args.version
    spec = {'schemaVersion': 1, 'component': 'mainsail', 'releaseId': release_id,
            'targetPath': '/home/mingda/mainsail', 'targetVersion': target_version,
            'sourceRepository': ORIGIN, 'sourceCommit': commit,
            'files': files, 'httpFiles': sorted(http_files),
            'materials': {name: digest(path) for name, path in sources.items()}}
    (materials / 'release.json').write_bytes(canonical(spec))
    sources['release.json'] = str(materials / 'release.json')
    metadata = {'schemaVersion': 2, 'scriptApi': 1, 'releaseId': release_id, 'version': args.version,
                'description': 'Mainsail static web interface; preserves config.json and existing services',
                'components': [{'id': 'mainsail', 'version': target_version, 'sequence': args.sequence}],
                'firmware': [], 'configChanges': [], 'touchesFirmware': False, 'touchesScreen': False,
                'bootRescue': False, 'recoveryMode': 'replay'}
    recipe = materials / 'recipe.json'
    recipe.write_bytes(canonical({'metadata': metadata, 'script': str(materials / 'update.sh'), 'sources': sources}))
    loader = importlib.util.spec_from_file_location('mainsail_native_builder', ROOT / 'scripts/build-swu.py')
    native = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(native)
    native.build(recipe, args.key, output, openssl=args.openssl)
    checksum = digest(output)
    output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
    receipt = {'package': output.name, 'packageBytes': output.stat().st_size, 'packageSha256': checksum,
               'version': args.version, 'sequence': args.sequence, 'releaseId': release_id,
               'sourceRepository': ORIGIN, 'sourceCommit': commit, 'targetVersion': target_version,
               'nodeVersion': args.node_version, 'npmVersion': args.npm_version,
               'files': len(files), 'staticBytes': sum(row['bytes'] for row in files.values()),
               'httpFiles': sorted(http_files), 'payloadSha256': digest(archive),
               'sourceArchiveSha256': digest(materials / 'mainsail-source.tar.gz'),
               'sameVersionReinstallSupported': True, 'preservedPaths': ['config.json'],
               'serviceRestarts': [], 'firmwareComponents': [], 'privateKeyIncluded': False}
    (materials / 'build-info.json').write_bytes(canonical(receipt))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--dist', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--node-version', required=True)
    parser.add_argument('--npm-version', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args()), indent=2))
