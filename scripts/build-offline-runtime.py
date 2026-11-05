#!/usr/bin/env python3
"""Assemble an architecture-specific runtime; packages check OS and MCU compatibility."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ks_includes'))
from offline_update.bundle import DEVICE_FIELDS, TARGET, load_bundle, relative_path
from offline_update.common import UpdateError, no_links, regular
from offline_update.swu_package import json_object


RELEASE_FILES = ('COPYING.swupdate', 'COPYING', 'LICENSE', 'build-environment.json',
                 'abi-validation.json', 'runtime-libraries.txt', 'source-provenance.json',
                 'license-manifest.json', 'swupdate-source-2026.05.tar.gz', 'SHA256SUMS',
                 'build-inputs.json', 'dependency-sources.json', 'README.release.txt', 'release-files.json', 'version.txt')
RELEASE_TREES = ('LICENSES', 'sources', 'validation', 'dependency-sources', 'dependency-licenses', 'reproducibility')


def build(recipe_path, output):
    recipe_path = regular(recipe_path).absolute()
    recipe = json_object(recipe_path.read_bytes())
    required = {'schemaVersion', 'engineDirectory', 'publicKey', 'profiles'}
    if (set(recipe) - required - {'target', 'libraries'} or required - set(recipe) or
            type(recipe['schemaVersion']) is not int or recipe['schemaVersion'] != 1 or
            recipe.get('target', TARGET) != TARGET or not isinstance(recipe['profiles'], list)):
        raise UpdateError('RUNTIME_RECIPE_INVALID')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise UpdateError('RUNTIME_OUTPUT_EXISTS')
    if not output.parent.is_dir():
        raise UpdateError('RUNTIME_OUTPUT_PARENT_MISSING')

    def source(value):
        if not isinstance(value, str) or not value:
            raise UpdateError('RUNTIME_RECIPE_INVALID')
        path = Path(value)
        if not path.is_absolute():
            path = recipe_path.parent / path
        path = Path(os.path.abspath(path))
        no_links(path, path.anchor)
        return path

    assets = {}

    def add(name, path):
        relative_path(name)
        if name.casefold() in {item.casefold() for item in assets}:
            raise UpdateError('RUNTIME_RECIPE_DUPLICATE')
        regular(path)
        data = path.read_bytes()
        assets[name] = data

    engine = source(recipe['engineDirectory'])
    for name in ('swupdate', 'build-info.json', 'swupdate.config'):
        add('engine/' + name, source(str(engine / name)))
    # Preserve redistributable source, licenses and validation evidence in Git.
    # Runtime installation intentionally deploys only its selected executable assets.
    for name in RELEASE_FILES:
        path = engine / name
        if path.exists() or path.is_symlink():
            add('engine/' + name, source(str(path)))
    for name in RELEASE_TREES:
        directory = engine / name
        if directory.exists() or directory.is_symlink():
            no_links(directory, directory.anchor)
            if not directory.is_dir():
                raise UpdateError('RUNTIME_RECIPE_INVALID')
            for parent, directories, entries in os.walk(directory, followlinks=False):
                for entry in directories + entries:
                    no_links(Path(parent) / entry, directory)
                for entry in sorted(entries):
                    path = Path(parent) / entry
                    add('engine/' + path.relative_to(engine).as_posix(), path)
    add('release-public.pem', source(recipe['publicKey']))
    profiles = []
    for index, profile in enumerate(recipe['profiles']):
        if (not isinstance(profile, dict) or set(profile) != {'device', 'policy', 'baseline'} or
                not isinstance(profile['device'], dict) or set(profile['device']) != DEVICE_FIELDS):
            raise UpdateError('RUNTIME_RECIPE_INVALID')
        folder = 'profiles/%d/' % index
        add(folder + 'policy.json', source(profile['policy']))
        add(folder + 'baseline.json', source(profile['baseline']))
        profiles.append({'device': profile['device'], 'policy': folder + 'policy.json',
                         'baseline': folder + 'baseline.json'})
    libraries = recipe.get('libraries', [])
    if not isinstance(libraries, list):
        raise UpdateError('RUNTIME_RECIPE_INVALID')
    libraries = [source(item) for item in libraries]
    libdir = engine / 'lib'
    if libdir.is_symlink():
        raise UpdateError('SYMLINK_PATH')
    if libdir.is_dir():
        libraries += sorted(libdir.iterdir())
    library_names = []
    for path in libraries:
        no_links(path, path.anchor)
        name = 'lib/' + path.name
        add(name, path)
        library_names.append(name)
    manifest = {'schemaVersion': 1, 'target': TARGET,
                'assets': {name: hashlib.sha256(data).hexdigest() for name, data in assets.items()},
                'profiles': profiles,
                'engine': {'binary': 'engine/swupdate', 'receipt': 'engine/build-info.json',
                           'config': 'engine/swupdate.config'},
                'publicKey': 'release-public.pem', 'libraries': library_names}
    # Publish only after all hashes, keys, build claims and profiles pass validation.
    with tempfile.TemporaryDirectory(prefix='.runtime-release-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'bundle'
        staging.mkdir()
        for name, data in assets.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n',
                                             encoding='utf-8')
        checked = load_bundle(staging)
        # mkdir preserves the no-overwrite promise even if another publisher wins a race.
        output.mkdir()
        try:
            for path in staging.iterdir():
                shutil.move(str(path), str(output / path.name))
        except Exception:
            # Remove only the new output owned by this invocation.
            shutil.rmtree(output)
            raise
    return checked['runtimeVersion']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', help='Production material recipe; paths relative to this JSON')
    parser.add_argument('--output', help='New output directory; never overwrite an existing directory')
    parser.add_argument('--check-only', metavar='DIRECTORY', help='Validate an existing Git delivery directory')
    args = parser.parse_args()
    if args.check_only:
        if args.recipe or args.output:
            parser.error('--check-only cannot be combined with --recipe or --output')
        version = load_bundle(args.check_only)['runtimeVersion']
    else:
        if not args.recipe or not args.output:
            parser.error('--recipe and --output are required')
        version = build(args.recipe, args.output)
    print('Verified production offline runtime: ' + version)


if __name__ == '__main__':
    try:
        main()
    except (UpdateError, OSError) as exc:
        print('Runtime release failed: ' + (exc.code if isinstance(exc, UpdateError) else type(exc).__name__),
              file=sys.stderr)
        raise SystemExit(1)
