#!/usr/bin/env python3
"""Build a signed, Git-preserving KlipperScreen update for verified baselines."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def git(*args, env=None, data=None):
    result = subprocess.run(['git', *map(str, args)], cwd=ROOT, env=env, input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError('Git release build failed: ' + result.stderr.decode('utf-8', 'replace')[:600])
    return result.stdout


def stage_blob(env, name, data):
    object_id = git('hash-object', '-w', '--stdin', data=data).decode().strip()
    git('update-index', '--add', '--cacheinfo', '100644,' + object_id + ',' + name, env=env)


def release_files(commit):
    rows = git('ls-tree', '-rlz', commit).split(b'\0')
    files, objects, total = {}, [], 0
    for row in rows:
        if not row:
            continue
        metadata, path = row.split(b'\t', 1)
        mode, kind, object_id, size = metadata.split()
        name = path.decode('utf-8')
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('Linked files and submodules are not supported: ' + name)
        if name.startswith('/') or any(part in ('.', '..', '.git') for part in name.split('/')):
            raise ValueError('Unsafe release path')
        objects.append((name, object_id.decode(), int(size)))
        total += int(size)
    process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    complete = False
    try:
        for name, object_id, size in objects:
            process.stdin.write((object_id + '\n').encode()); process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b'blob' or int(header[2]) != size:
                raise RuntimeError('Git returned invalid release material')
            checksum, remaining, tail = hashlib.sha256(), size, b''
            while remaining:
                block = process.stdout.read(min(remaining, 1024 * 1024))
                if not block:
                    raise RuntimeError('Truncated Git release blob')
                # Actual PEM blocks are forbidden; source tests quoting a PEM
                # header inside a Python string are not private-key material.
                if re.search(rb'(?m)^-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----', tail + block):
                    raise ValueError('Private key found in release: ' + name)
                tail = block[-128:]
                checksum.update(block); remaining -= len(block)
            if process.stdout.read(1) != b'\n':
                raise RuntimeError('Invalid Git blob delimiter')
            files[name] = checksum.hexdigest()
        complete = True
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if not complete:
            process.kill()  # Do not wait on an unread large blob after rejecting it.
        result = process.wait(timeout=30)
        process.stdout.close()
        process.stderr.close()
        if result and complete:
            raise RuntimeError('Could not finish reading Git release objects')
    return files, total


def build(args):
    output = Path(args.output).absolute()
    if output.suffix != '.mdu' or output.exists():
        raise ValueError('Output must be a new .mdu file')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', args.version) or args.sequence < 1:
        raise ValueError('Invalid release version or sequence')
    if '\\' in args.target or not args.target.startswith('/') or '..' in args.target.split('/'):
        raise ValueError('Target must be an absolute Linux application directory')
    captured = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
    baseline = captured if 'allowedBaselines' in captured else {
        'allowedBaselines': [{'head': captured['head'], 'files': {
            name: row.get('sha256') for name, row in captured['files'].items()
            if row.get('kind') in ('file', 'deleted')}}]}
    parent = git('rev-parse', 'HEAD').decode().strip()
    for entry in baseline['allowedBaselines']:
        if not re.fullmatch(r'[0-9a-f]{40}', entry.get('head', '')) or not isinstance(entry.get('files'), dict):
            raise ValueError('Invalid verified baseline')
        # A previous offline release may be a generated child of the source
        # branch. Require related history, then retain that deployed release
        # in the new commit's parents without moving the source checkout.
        git('merge-base', entry['head'], parent)
    heads = [item['head'] for item in baseline['allowedBaselines']]
    if not heads:
        raise ValueError('At least one verified baseline is required')
    candidates = list(dict.fromkeys([parent, *heads]))
    release_parents = []
    for candidate in candidates:
        if not any(candidate != other and git('merge-base', candidate, other).decode().strip() == candidate
                   for other in candidates):
            release_parents.append(candidate)
    base = heads[0] if len(heads) == 1 else git('merge-base', '--octopus', *heads).decode().strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    materials = output.with_suffix('.materials')
    if materials.exists():
        raise ValueError('Release materials already exist')
    materials.mkdir()
    reference = 'refs/heads/mdu-build-' + uuid.uuid4().hex
    commit = None
    reference_created = False
    try:
        with tempfile.TemporaryDirectory(prefix='ks-index-', dir=materials) as temporary:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
            git('read-tree', parent, env=env)
            if args.snapshot_worktree:
                git('add', '-A', '--', '.', env=env)
            marker_name = 'resources/klipperscreen-release.json'
            marker = {'schemaVersion': 1, 'component': 'klipperscreen', 'version': args.version}
            stage_blob(env, marker_name, canonical(marker))
            baseline_name = 'resources/offline-update/profiles/generic/baseline.json'
            inventory = {'klipperscreen': {'version': args.version, 'sequence': args.sequence, 'evidence': {
                'kind': 'json', 'path': args.target.rstrip('/') + '/' + marker_name,
                'field': 'version', 'value': args.version}}}
            inventory_raw = canonical(inventory)
            stage_blob(env, baseline_name, inventory_raw)
            manifest_name = 'resources/offline-update/manifest.json'
            manifest = json.loads(git('show', ':' + manifest_name, env=env))
            manifest['assets']['profiles/generic/baseline.json'] = hashlib.sha256(inventory_raw).hexdigest()
            stage_blob(env, manifest_name, canonical(manifest))
            tree = git('write-tree', env=env).decode().strip()
            parent_args = [part for revision in release_parents for part in ('-p', revision)]
            commit = git('-c', 'user.name=MINGDA Release Builder', '-c', 'user.email=release@mingda.local',
                         'commit-tree', tree, *parent_args,
                         data=('build: package KlipperScreen ' + args.version + '\n').encode()).decode().strip()
        files, size = release_files(commit)
        for item in baseline['allowedBaselines']:
            item['files'] = {name: checksum for name, checksum in item['files'].items()
                             if name in files or git('ls-tree', item['head'], '--', name).strip()}
        git('update-ref', reference, commit, '0' * 40)
        reference_created = True
        bundle = materials / 'screen.bundle'
        full_bundle = bool(getattr(args, 'full_bundle', False))
        revisions = [reference] if full_bundle else [reference, '^' + base]
        git('bundle', 'create', bundle, *revisions)
        git('bundle', 'verify', bundle)
        release = {'schemaVersion': 1, 'targetPath': args.target, 'service': 'KlipperScreen.service',
                   'targetCommit': commit, 'targetVersion': args.version, 'bundleRef': reference,
                   'bundleSha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
                   'targetFiles': files, 'targetBytes': size, 'allowedBaselines': baseline['allowedBaselines']}
        release_path = materials / 'screen-release.json'
        release_path.write_bytes(canonical(release))
        scripts = ROOT / 'scripts/swupdate/klipperscreen'
        recipe = json.loads((scripts / 'recipe.template.json').read_text(encoding='utf-8'))
        recipe['metadata'].update(releaseId='klipperscreen-' + args.version, version=args.version)
        recipe['metadata']['components'][0].update(version=args.version, sequence=args.sequence)
        recipe['script'] = str(scripts / 'update.sh')
        recipe['rescueScript'] = str(scripts / 'rescue.sh')
        recipe['sources'] = {'installer.py': str(scripts / 'installer.py'),
                             'screen-release.json': str(release_path), 'screen.bundle': str(bundle)}
        recipe_path = materials / 'recipe.json'; recipe_path.write_bytes(canonical(recipe))
        module_spec = importlib.util.spec_from_file_location('mdu_builder', ROOT / 'scripts/build-swu.py')
        builder = importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(builder)
        builder.build(recipe_path, args.key, output, openssl=args.openssl)
        checksum = hashlib.sha256(output.read_bytes()).hexdigest()
        output.with_suffix('.mdu.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
        receipt = {'version': args.version, 'sequence': args.sequence, 'parentCommit': release_parents[0],
                   'parentCommits': release_parents, 'sourceCommit': parent,
                   'releaseCommit': commit, 'snapshotWorktree': bool(args.snapshot_worktree),
                   'bundleMode': 'full' if full_bundle else 'delta',
                   'package': output.name, 'packageSha256': checksum, 'packageBytes': output.stat().st_size,
                   'targetFiles': len(files), 'targetBytes': size, 'baselineHeads': [item['head'] for item in baseline['allowedBaselines']],
                   'branchHeadUnchanged': git('rev-parse', 'HEAD').decode().strip() == parent}
        (materials / 'build-info.json').write_bytes(canonical(receipt))
        return receipt
    finally:
        if reference_created:
            git('update-ref', '-d', reference, commit)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, help='Previously verified device source fingerprint JSON')
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', type=int, required=True)
    parser.add_argument('--target', default='/home/mingda/KlipperScreen')
    parser.add_argument('--snapshot-worktree', action='store_true', help='Include current non-ignored worktree changes')
    parser.add_argument('--full-bundle', action='store_true',
                        help='Include all release Git objects and history without prerequisite objects')
    parser.add_argument('--key', required=True)
    parser.add_argument('--openssl', default='openssl')
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args()), ensure_ascii=False, indent=2))
