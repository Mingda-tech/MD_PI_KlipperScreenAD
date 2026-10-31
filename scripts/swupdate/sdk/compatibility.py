"""Read-only compatibility checks for the SDK and matching NoSHA AI release."""
import ast
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


class CompatibilityError(ValueError):
    pass


SERIAL_PATTERN = re.compile(r'^([A-Z0-9]{3})(\d{2})(\d{2})([A-Z])(\d{2})(\d{5})$')
STANDARD_CODES = {'M4D', 'MF1', 'MF4', 'A4M', 'A4R', 'A6R', 'MC2', 'MR4', 'MR8', 'MR1', 'MM1', 'MM3'}
PROFILE_FIELDS = ('serialNumber', 'skuKey', 'hardwareRevision', 'boardType', 'npuType', 'arch', 'os', 'osVersion')
PUBLIC_MODELS = {'SPAGHETTI': 'spaghetti', 'NOZZLE_BLOB': 'nozzle_blob',
                 'FOREIGN_OBJECT': 'foreign_object', 'UNDER_EXTRUSION': 'under_extrusion',
                 'OVER_EXTRUSION': 'over_extrusion', 'WARPING': 'warping'}
RUNTIME_IMPORTS = 'import apscheduler, cv2, flask, flask_inputs, numpy, PIL, psutil, requests, wtforms'


def object_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise CompatibilityError('Release metadata must be a bounded regular file: ' + path.name)
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise CompatibilityError('Release metadata must be a JSON object: ' + path.name)
    return value


def resolve_sku(serial):
    match = SERIAL_PATTERN.fullmatch(serial) if isinstance(serial, str) else None
    if not match:
        raise CompatibilityError('Invalid machine serial number')
    model, edition = match.group(1), match.group(4)
    if model in ('M1D', 'M6D'):
        return model + ('_ULTRA' if edition == 'I' else '_STANDARD')
    if model in ('M1P', 'M6P'):
        return model + ('_PLUS' if edition == 'C' else '_STANDARD')
    if model in STANDARD_CODES:
        return model + '_STANDARD'
    raise CompatibilityError('Unsupported machine model')


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get('schemaVersion') != 1:
        raise CompatibilityError('Invalid device profile schema')
    for name in PROFILE_FIELDS:
        value = profile.get(name)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
            raise CompatibilityError('Invalid device profile field: ' + name)
    if resolve_sku(profile['serialNumber']) != profile['skuKey']:
        raise CompatibilityError('Device profile SKU conflicts with its serial number')
    return profile


def relative_name(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or '\0' in value
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/'))
            or str(PurePosixPath(value)) != value):
        raise CompatibilityError('Unsafe release-relative path')
    return value


def inside_file(root, relative, *, executable=False, link=False):
    relative = relative_name(relative)
    root = Path(root).resolve(strict=True)
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise CompatibilityError('Release file is missing or escapes its root: ' + relative) from exc
    # Internal hard links remain regular files. Symlinks are permitted only for
    # explicitly declared entry points, never for model or metadata files.
    if (path.is_symlink() and not link) or not resolved.is_file():
        raise CompatibilityError('Release entry must be a regular file: ' + relative)
    if executable and ((os.name == 'posix' and not path.stat().st_mode & 0o111)
                       or not os.access(path, os.X_OK)):
        raise CompatibilityError('Release entry is not executable: ' + relative)
    return path


def arm64_elf(path):
    with Path(path).open('rb') as stream:
        header = stream.read(64)
    if (len(header) < 20 or header[:7] != b'\x7fELF\x02\x01\x01'
            or int.from_bytes(header[16:18], 'little') not in (2, 3)
            or int.from_bytes(header[18:20], 'little') != 183):
        raise CompatibilityError('Release requires a Linux ARM64 ELF executable: ' + Path(path).name)


def _matches(selector, profile):
    if not isinstance(selector, dict):
        raise CompatibilityError('Invalid deployment selector')
    for name in ('boardType', 'npuType', 'arch', 'os'):
        expected = selector.get(name)
        if expected is not None and (not isinstance(expected, str) or not expected):
            raise CompatibilityError('Invalid deployment selector field: ' + name)
        actual = profile.get(name, '')
        if expected and (expected.lower() != actual.lower() if name in ('arch', 'os') else expected != actual):
            return False
    for name, field in (('skuKeys', 'skuKey'), ('hardwareRevisions', 'hardwareRevision'), ('osVersions', 'osVersion')):
        values = selector.get(name)
        if values is not None and (not isinstance(values, list) or not values or
                                   any(not isinstance(v, str) or not v for v in values)):
            raise CompatibilityError('Invalid deployment selector list: ' + name)
        if values and profile.get(field) not in values:
            return False
    return True


def select_variant(catalog, profile):
    validate_profile(profile)
    if not isinstance(catalog, dict) or catalog.get('schemaVersion') != 1 or not isinstance(catalog.get('bundles'), list):
        raise CompatibilityError('Invalid deployment catalog')
    rows = []
    for row in catalog['bundles']:
        if not isinstance(row, dict) or not isinstance(row.get('selector'), dict):
            raise CompatibilityError('Invalid deployment bundle')
        for field in ('sdkRelease', 'aiRelease'):
            relative_name(row.get(field))
        if _matches(row['selector'], profile):
            rows.append(row)
    if len(rows) != 1:
        raise CompatibilityError('Expected exactly one matching SDK/AI bundle; found ' + str(len(rows)))
    return copy.deepcopy(rows[0])


def _component(root, name):
    descriptor = object_file(inside_file(root, 'component.json'))
    if descriptor.get('schemaVersion') != 1 or descriptor.get('componentId') != name:
        raise CompatibilityError('Invalid ' + name + ' component identity')
    for field in ('version', 'variantId'):
        value = descriptor.get(field)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
            raise CompatibilityError('Invalid ' + name + ' release identity')
    return descriptor


def _inspection_mapping(sdk_root, sku, manifest, factory, source, template):
    inspection = source.get('inspection')
    if inspection is None:
        versions = template.get('defectModelVersions') or {}
        if not isinstance(versions, dict):
            raise CompatibilityError('Invalid SDK defect model versions')
        for public, model in PUBLIC_MODELS.items():
            row = manifest['models'].get(model)
            if not isinstance(row, dict) or versions.get(public) != PurePosixPath(row['file']).name:
                raise CompatibilityError('SDK and AI Runtime model identities differ: ' + public)
        return {'requiresKlipperInspection': False, 'profileDigest': None, 'inspectionBindings': []}
    if not isinstance(inspection, dict):
        raise CompatibilityError('Invalid SDK inspection profile')
    contract = object_file(inside_file(sdk_root, 'inspection-contract.json'))
    expected = (contract.get('profiles') or {}).get(sku, {})
    if (contract.get('capability') != 'TARGET_INSPECTION_V1'
            or contract.get('factoryBundleChecksum') != factory.get('bundleChecksum')
            or not isinstance(expected, dict) or not expected.get('profileDigest')
            or expected.get('requiresKlipperInspection') is not True):
        raise CompatibilityError('SDK inspection contract differs from its factory profile')
    captures, bindings = inspection.get('captures'), inspection.get('bindings')
    if not isinstance(captures, dict) or not isinstance(bindings, list):
        raise CompatibilityError('Invalid SDK inspection binding inventory')
    active = []
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get('id'), str):
            raise CompatibilityError('Invalid inspection binding')
        capture = captures.get(binding.get('captureId'))
        if not isinstance(capture, dict):
            raise CompatibilityError('Inspection capture is missing')
        if binding.get('enabled', True) and capture.get('calibrated'):
            active.append(binding)
    compiled = expected.get('bindings')
    if (not isinstance(compiled, list) or any(not isinstance(v, dict) for v in compiled)
            or len({v.get('id') for v in compiled}) != len(compiled)
            or {v.get('id') for v in compiled} != {v['id'] for v in active}
            or len({v['id'] for v in active}) != len(active)):
        raise CompatibilityError('SDK inspection binding inventory differs from its contract')
    for binding in active:
        explicit = binding.get('model') or {}
        if not isinstance(explicit, dict) or binding.get('defectType') not in PUBLIC_MODELS:
            raise CompatibilityError('Invalid inspection model reference')
        key = explicit.get('modelId') or binding['defectType'].lower()
        row = manifest['models'].get(key)
        if not isinstance(row, dict) or str(row.get('defectType') or key).upper() != binding['defectType']:
            raise CompatibilityError('SDK inspection model type differs from the Runtime')
        version = explicit.get('modelVersion') or (template.get('defectModelVersions') or {}).get(binding['defectType'])
        if PurePosixPath(row['file']).name != version:
            raise CompatibilityError('SDK inspection model identity differs from the Runtime')
        if explicit and 'MODEL_REF_V1' not in manifest.get('capabilities', []):
            raise CompatibilityError('Runtime does not support explicit inspection model references')
    for relative in ('klipper_components/ai_inspection.py', 'scripts/install_inspection_support.py',
                     'moonraker_components/ai_detection.py'):
        inside_file(sdk_root, relative)
    return {'requiresKlipperInspection': True, 'profileDigest': expected['profileDigest'],
            'inspectionBindings': [v['id'] for v in active]}


def validate_releases(sdkPath, aiPath, profile, *, probe=None):
    validate_profile(profile)
    if profile['arch'].lower() != 'aarch64':
        raise CompatibilityError('This SDK/AI dependency release requires ARM64')
    sdk, ai = Path(sdkPath).resolve(strict=True), Path(aiPath).resolve(strict=True)
    sdk_meta, ai_meta = _component(sdk, 'sdk'), _component(ai, 'ai-runtime')
    sdk_binary = inside_file(sdk, 'mingda-printer', executable=True)
    arm64_elf(sdk_binary)
    application = inside_file(sdk, 'bin/application', executable=True, link=True)
    if (not application.is_symlink() or os.readlink(application) != '../mingda-printer'
            or application.resolve() != sdk_binary.resolve()):
        raise CompatibilityError('SDK application must be the packaged relative binary link')
    version = object_file(inside_file(sdk, 'version.json'))
    if version.get('version') != sdk_meta['version']:
        raise CompatibilityError('SDK component and binary version metadata differ')
    launcher = inside_file(ai, 'bin/application', executable=True).read_text(encoding='utf-8')
    if (not launcher.startswith('#!/bin/sh\n') or 'release_root=' not in launcher
            or 'exec "$release_root/venv/bin/ai-detection-runtime" "$@"' not in launcher):
        raise CompatibilityError('AI Runtime requires its self-locating launcher')
    python = inside_file(ai, 'venv/bin/python', executable=True, link=True)
    arm64_elf(python)
    entry = inside_file(ai, 'venv/bin/ai-detection-runtime', executable=True)
    arm64_elf(entry)
    venv = inside_file(ai, 'venv/pyvenv.cfg').read_text(encoding='utf-8')
    properties = dict(line.strip().split('=', 1) for line in venv.splitlines() if '=' in line)
    properties = {name.strip(): value.strip() for name, value in properties.items()}
    if properties.get('version', '').split('.')[:2] != ['3', '10']:
        raise CompatibilityError('AI Runtime virtual environment must use Python 3.10')
    manifest = object_file(inside_file(ai, 'model-manifest.json'))
    if manifest.get('variantId') != ai_meta['variantId']:
        raise CompatibilityError('Runtime component and model variants differ')
    selector = manifest.get('deviceProfile')
    if not isinstance(selector, dict):
        raise CompatibilityError('Runtime model profile is missing')
    for key, field in (('skuKeys', 'skuKey'), ('hardwareRevisions', 'hardwareRevision')):
        allowed = selector.get(key)
        if not isinstance(allowed, list) or profile[field] not in allowed:
            raise CompatibilityError('Runtime model profile does not support ' + field)
    for field in ('boardType', 'npuType', 'arch'):
        if str(selector.get(field) or '').lower() != profile[field].lower():
            raise CompatibilityError('Runtime model profile differs in ' + field)
    models = manifest.get('models')
    if (not isinstance(models, dict) or len(models) != 6
            or (manifest.get('schemaVersion') == 1 and set(models) != set(PUBLIC_MODELS.values()))):
        raise CompatibilityError('Runtime must contain six declared model entries')
    referenced = set()
    for key, row in models.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            raise CompatibilityError('Invalid Runtime model entry')
        name = relative_name(row.get('file'))
        if not name.startswith('models/') or not name.endswith('.nb'):
            raise CompatibilityError('Runtime model must be declared below models/')
        path = inside_file(ai, name)
        if not path.stat().st_size:
            raise CompatibilityError('Runtime model file is empty')
        referenced.add(path.resolve())
    actual = set()
    models_root = ai / 'models'
    if models_root.is_symlink() or not models_root.is_dir():
        raise CompatibilityError('Runtime models directory is missing or linked')
    for path in models_root.rglob('*.nb'):
        inside_file(ai, path.relative_to(ai).as_posix())
        actual.add(path.resolve())
    if actual != referenced:
        raise CompatibilityError('Runtime contains unreferenced or missing model files')
    factory = object_file(inside_file(sdk, 'ai_factory_profiles.json'))
    source = (factory.get('skuProfiles') or {}).get(profile['skuKey'])
    if not isinstance(source, dict):
        raise CompatibilityError('SDK factory profile does not support this SKU')
    template = (factory.get('profileTemplates') or {}).get(source.get('templateId'))
    if not isinstance(template, dict):
        raise CompatibilityError('SDK factory profile template is missing')
    inspection = _inspection_mapping(sdk, profile['skuKey'], manifest, factory, source, template)
    result = {'sdkVersion': sdk_meta['version'], 'sdkVariant': sdk_meta['variantId'],
              'aiVersion': ai_meta['version'], 'aiVariant': ai_meta['variantId'],
              'modelBundleVersion': manifest.get('modelBundleVersion', 'unspecified'),
              'modelsCount': len(models), **inspection}
    if probe is not None:
        probe(sdk, ai)
    return result


def validate_runtime_dependencies(sdkPath, aiPath, run=subprocess.run):
    """Explicit pre-stop probes; no service launch, model load or NPU request."""
    sdk, ai = Path(sdkPath).resolve(strict=True), Path(aiPath).resolve(strict=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    commands = ([str(sdk / 'mingda-printer'), '-c', str(sdk / 'config.json'), '--self-check'],
                [str(ai / 'venv/bin/python'), '-I', '-B', '-c', RUNTIME_IMPORTS])
    for index, command in enumerate(commands):
        result = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                     check=False, timeout=120, cwd=str(sdk if index == 0 else ai), env=environment)
        if result.returncode:
            raise CompatibilityError('SDK/AI offline executable dependency probe failed')
    return {'sdkSelfCheck': True, 'runtimeImports': True}


def validate_existing_support(info, *, klipper_root, moonraker_root, config_root,
                              managed_sdk_root='/opt/mingda/apps/sdk'):
    """Require existing inspection integration without editing it or restarting."""
    if info.get('requiresKlipperInspection') is not True:
        return {'required': False}
    paths = ((Path(klipper_root) / 'klippy/extras/ai_inspection.py', 'current/klipper_components/ai_inspection.py', 'load_config'),
             (Path(moonraker_root) / 'moonraker/components/ai_detection.py', 'current/moonraker_components/ai_detection.py', 'load_component'))
    for path, relative, entry in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            raise CompatibilityError('Existing inspection loader is missing or linked')
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise CompatibilityError('Existing inspection loader is not valid Python') from exc
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry]
        if len(functions) != 1:
            raise CompatibilityError('Existing inspection loader entry point is missing')
        strings = {node.value for node in ast.walk(functions[0])
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        calls = {node.func.attr for node in ast.walk(functions[0])
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        if (managed_sdk_root not in strings or relative not in strings
                or not {'spec_from_file_location', 'exec_module', entry}.issubset(calls)):
            raise CompatibilityError('Existing inspection loader does not follow the managed SDK')
    root = Path(config_root)
    main, managed = root / 'printer.cfg', root / 'mingda_inspection.cfg'
    if not main.is_file() or not managed.is_file():
        raise CompatibilityError('Existing inspection configuration is missing')
    text = main.read_text(encoding='utf-8')
    if not re.search(r'^\s*\[include\s+mingda_inspection\.cfg\]\s*(?:[#;].*)?$', text, re.M):
        raise CompatibilityError('Existing inspection configuration is not included')
    if not re.search(r'^\s*\[ai_inspection\]\s*(?:[#;].*)?$', managed.read_text(encoding='utf-8'), re.M):
        raise CompatibilityError('Existing inspection component is not configured')
    return {'required': True, 'existingLoaders': True, 'existingConfiguration': True}
