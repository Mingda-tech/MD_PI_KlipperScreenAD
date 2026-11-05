"""Read and unpack the publisher's SDK/AI archive without running its scripts.

The enclosing signed MDU authenticates the original compressed archive. This
module validates paths, metadata and release layout; it deliberately does not
restore the source package's disabled per-file/model digest gates.
"""
from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tarfile
import gzip
import selectors
import stat
import subprocess
import sys
import tempfile
import time

MAX_ENTRIES = 500000
MAX_REGULAR_BYTES = 8 * 1024 ** 3
MAX_EXPANDED_BYTES = 32 * 1024 ** 3
MAX_MEMBER_BYTES = 2 * 1024 ** 3
MAX_JSON_BYTES = 2 * 1024 ** 2
MAX_METADATA_CACHE = 64 * 1024 ** 2
MODEL_NAMES = {'spaghetti', 'nozzle_blob', 'foreign_object', 'under_extrusion',
               'over_extrusion', 'warping'}


class ArchiveError(ValueError):
    pass


def safe_name(name):
    if (not isinstance(name, str) or not name or len(name) > 4096
            or name.startswith('/') or '\\' in name or ':' in name or '\0' in name
            or any(p in ('', '.', '..') for p in name.split('/'))
            or str(PurePosixPath(name)) != name):
        raise ArchiveError('Unsafe SDK archive path')
    return name


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('An archive or extraction path is a symbolic link')
    return path


def _mtime(member):
    try:
        value = Decimal(str(member.pax_headers.get('mtime', member.mtime)))
        if not value.is_finite():
            raise ValueError()
        return int(value * 1000000000)
    except (ValueError, OverflowError):
        raise ArchiveError('Invalid archive modification time')


def _row(member):
    kind = ('file' if member.isfile() else 'directory' if member.isdir()
            else 'hardlink' if member.islnk() else 'symlink' if member.issym() else None)
    if kind is None:
        raise ArchiveError('Special archive entries are unsupported')
    if member.mode & ~0o777:
        raise ArchiveError('Special archive permission bits are unsupported')
    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
        raise ArchiveError('Archive member size exceeds the limit')
    name = member.name.rstrip('/') if kind == 'directory' else member.name
    safe_name(name)
    return name, {'kind': kind, 'bytes': member.size, 'mode': member.mode,
                  'mtimeNs': _mtime(member), 'link': member.linkname if kind in ('hardlink', 'symlink') else ''}


def _scan(path, *, progress=None, expected_entries=None, single_pass=False, stream_bytes=False):
    path = no_links(path)
    if not path.is_file():
        raise ArchiveError('The source archive is missing')
    nodes, payloads, kinds, roots = {}, {}, Counter(), set()
    regular_bytes = metadata_bytes = tar_bytes = 0
    last_report = time.monotonic()
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            name, row = _row(member)
            if name in nodes:
                raise ArchiveError('Duplicate archive entry')
            nodes[name] = row
            if len(nodes) > MAX_ENTRIES:
                raise ArchiveError('The SDK archive contains too many entries')
            roots.add(name.split('/')[0])
            kinds[row['kind']] += 1
            if row['kind'] == 'file':
                regular_bytes += row['bytes']
                if regular_bytes > MAX_REGULAR_BYTES:
                    raise ArchiveError('The expanded SDK archive exceeds the limit')
                required_metadata = name.endswith(('/deployment-catalog.json', '/component.json',
                                                   '/model-manifest.json', '/version.json'))
                if required_metadata:
                    if row['bytes'] > MAX_JSON_BYTES:
                        raise ArchiveError('Release metadata exceeds the limit')
                    payloads[name] = archive.extractfile(member).read()
                    metadata_bytes += len(payloads[name])
                elif single_pass and row['bytes'] <= MAX_JSON_BYTES:
                    # A required JSON file can be a hardlink to a differently
                    # named regular member. Retain small JSON metadata during
                    # this pass instead of reopening the gzip archive later.
                    with archive.extractfile(member) as stream:
                        prefix = stream.read(min(4096, row['bytes']))
                        if prefix.lstrip(b' \t\r\n\xef\xbb\xbf').startswith(b'{'):
                            raw = prefix + stream.read()
                            try:
                                value = json.loads(raw)
                            except (ValueError, UnicodeError):
                                value = None
                            if isinstance(value, dict) and any(key in value for key in (
                                    'version', 'componentId', 'models', 'bundles')):
                                payloads[name] = raw
                                metadata_bytes += len(raw)
                if single_pass and metadata_bytes > MAX_METADATA_CACHE:
                    raise ArchiveError('Release metadata cache exceeds the limit')
            if expected_entries is not None and len(nodes) > expected_entries:
                raise ArchiveError('The source archive exceeds its declared entry count')
            if progress and expected_entries and time.monotonic() - last_report >= 1:
                progress(min(len(nodes), expected_entries - 1), expected_entries)
                last_report = time.monotonic()
            if single_pass:
                # tarfile's stream mode still caches every TarInfo on Python
                # 3.10. We own the compact index and never resolve members by
                # TarFile.getmember, so release that duplicate object cache.
                archive.members.clear()
        if stream_bytes:
            # Publication records the complete decompressed tar stream, including
            # its zero trailer/record padding, without reopening the gzip file.
            while True:
                tail = archive.fileobj.read(1024 * 1024)
                if not tail:
                    break
                if any(tail):
                    raise ArchiveError('Unexpected data after the archive trailer')
                if archive.fileobj.tell() > MAX_EXPANDED_BYTES:
                    raise ArchiveError('The tar stream exceeds the publication limit')
            tar_bytes = archive.fileobj.tell()
            if tar_bytes % 512 or tar_bytes > MAX_EXPANDED_BYTES:
                raise ArchiveError('Invalid complete tar stream size')
    if len(roots) != 1:
        raise ArchiveError('The SDK archive must contain one top-level directory')
    root = next(iter(roots))
    if nodes.get(root, {}).get('kind') != 'directory':
        raise ArchiveError('The archive root is not a declared directory')
    resolved = {}
    for name, row in nodes.items():
        parent = posixpath.dirname(name)
        while parent:
            if parent in nodes and nodes[parent]['kind'] != 'directory':
                raise ArchiveError('An archive parent is not a directory')
            parent = posixpath.dirname(parent)
        if row['kind'] == 'file':
            resolved[name] = name
        elif row['kind'] == 'symlink':
            expected = root + '/releases/sdk-common/bin/application'
            if name != expected or row['link'] != '../mingda-printer' or row['mode'] != 0o777:
                raise ArchiveError('Unsupported archive symbolic link')
            target = root + '/releases/sdk-common/mingda-printer'
            if nodes.get(target, {}).get('kind') != 'file':
                raise ArchiveError('The SDK launcher does not select a regular executable')
    expanded_bytes = 0
    for name, row in nodes.items():
        if row['kind'] == 'hardlink':
            target, visited = name, set()
            while target not in resolved:
                if target in visited or len(visited) > 1024:
                    raise ArchiveError('Cyclic or excessive archive hard links')
                visited.add(target)
                entry = nodes.get(target)
                if entry is None or entry['kind'] != 'hardlink':
                    raise ArchiveError('An archive hard link does not select a regular file')
                target = safe_name(entry['link'])
                if target.split('/')[0] != root:
                    raise ArchiveError('An archive hard link escapes its root')
            canonical = resolved[target]
            final = nodes[canonical]
            if row['mode'] != final['mode'] or row['mtimeNs'] != final['mtimeNs']:
                raise ArchiveError('Hard-linked entries declare conflicting file metadata')
            for alias in visited:
                resolved[alias] = canonical
        if row['kind'] in ('file', 'hardlink'):
            expanded_bytes += nodes[resolved[name]]['bytes']
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise ArchiveError('Logical expanded archive size exceeds the limit')

    def metadata(relative):
        name = root + '/' + safe_name(relative)
        if name not in resolved:
            raise ArchiveError('Required release metadata is missing')
        canonical = resolved[name]
        if nodes[canonical]['bytes'] > MAX_JSON_BYTES:
            raise ArchiveError('Release metadata exceeds the limit')
        if canonical not in payloads:
            if single_pass:
                raise ArchiveError('Required release JSON was not available in the single archive audit')
            with tarfile.open(path, 'r|gz') as archive:
                for member in archive:
                    if member.name == canonical:
                        payloads[canonical] = archive.extractfile(member).read()
                        break
        try:
            value = json.loads(payloads[canonical])
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (KeyError, ValueError, UnicodeError) as exc:
            raise ArchiveError('Invalid release JSON metadata') from exc

    catalog = metadata('deployment-catalog.json')
    if catalog.get('schemaVersion') != 1 or not isinstance(catalog.get('bundles'), list) or not catalog['bundles']:
        raise ArchiveError('Invalid deployment catalog')
    sdk = metadata('releases/sdk-common/version.json')
    sdk_version = sdk.get('version')
    if not isinstance(sdk_version, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9_.-]+)?', sdk_version):
        raise ArchiveError('Invalid SDK version declaration')
    variants, seen = [], set()
    for bundle in catalog['bundles']:
        if not isinstance(bundle, dict) or bundle.get('sdkRelease') != 'releases/sdk-common':
            raise ArchiveError('The catalog selects an unexpected SDK source')
        source = safe_name(bundle.get('aiRelease'))
        if not source.startswith('releases/runtime/') or source.count('/') != 2:
            raise ArchiveError('Invalid AI Runtime source path')
        if source in seen:
            continue
        seen.add(source)
        component = metadata(source + '/component.json')
        manifest = metadata(source + '/model-manifest.json')
        if (component.get('componentId') != 'ai-runtime'
                or not isinstance(component.get('version'), str) or not component['version']
                or not isinstance(component.get('variantId'), str) or not component['variantId']):
            raise ArchiveError('Invalid AI Runtime identity')
        models = manifest.get('models')
        if not isinstance(models, dict) or set(models) != MODEL_NAMES:
            raise ArchiveError('An AI Runtime must declare six canonical models')
        for model in models.values():
            if not isinstance(model, dict):
                raise ArchiveError('Invalid model declaration')
            relative = safe_name(model.get('file'))
            if root + '/' + source + '/' + relative not in resolved:
                raise ArchiveError('A declared model is missing or not a regular file')
        variants.append({'sourcePath': source, 'version': component['version'],
                         'variantId': component['variantId'], 'modelsCount': len(models),
                         'modelBundleVersion': manifest.get('modelBundleVersion', ''),
                         'selector': bundle.get('selector', {}),
                         'modelFiles': {name: row['file'] for name, row in models.items()}})
    public = {'root': root, 'entries': len(nodes), 'entryCount': len(nodes),
              'regularBytes': regular_bytes, 'expandedBytes': expanded_bytes,
              'kinds': dict(kinds), 'sdkVersion': sdk_version,
              'catalog': catalog, 'runtimeVariants': variants,
              'sdkDescriptor': metadata('releases/sdk-common/component.json'),
              'variantDescriptors': {row['sourcePath']: metadata(row['sourcePath'] + '/component.json')
                                     for row in variants}}
    if stream_bytes:
        public['tarBytes'] = tar_bytes
    return public, nodes, resolved


def audit_archive(path):
    """Return JSON-serializable layout facts, without changing the source archive."""
    public, _, _ = _scan(path)
    return public


def extract_archive(path, destination):
    """Unpack a validated archive, preserving hard-link deduplication and times."""
    if os.name != 'posix':
        raise ArchiveError('SDK extraction requires Linux filesystem semantics')
    path, destination = no_links(path), no_links(destination)
    if not destination.is_dir() or any(destination.iterdir()):
        raise ArchiveError('Extraction requires an existing empty directory')
    audit, nodes, resolved = _scan(path)
    pending, directories, symlinks = [], [], []
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            name, row = _row(member)
            if nodes.get(name) != row:
                raise ArchiveError('The source archive changed during extraction')
            target = destination / name
            no_links(target.parent)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if row['kind'] == 'directory':
                target.mkdir(exist_ok=True, mode=0o700)
                directories.append((target, row))
            elif row['kind'] == 'file':
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output, archive.extractfile(member) as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    if output.tell() != row['bytes']:
                        raise ArchiveError('An archived file was truncated')
                os.chmod(target, row['mode'])
                os.utime(target, ns=(row['mtimeNs'], row['mtimeNs']))
            elif row['kind'] == 'hardlink':
                pending.append((target, destination / resolved[name]))
            else:
                symlinks.append((target, row))
    for target, source in pending:
        no_links(source)
        if not source.is_file() or target.exists() or target.is_symlink():
            raise ArchiveError('Invalid extracted hard-link target')
        os.link(source, target, follow_symlinks=False)
    for target, row in symlinks:
        os.symlink(row['link'], target)
        os.utime(target, ns=(row['mtimeNs'], row['mtimeNs']), follow_symlinks=False)
    for target, row in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(target, row['mode'])
        os.utime(target, ns=(row['mtimeNs'], row['mtimeNs']))
    return destination / audit['root']


_PREPARED_TOKEN = object()


def _archive_identity(path):
    path = no_links(path)
    value = path.lstat()
    if not path.is_file() or not stat.S_ISREG(value.st_mode):
        raise ArchiveError('The source archive is not a regular file')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode, value.st_uid, value.st_gid)


def _archive_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class PreparedArchive:
    """Immutable, process-local proof of a complete archive safety audit."""
    __slots__ = ('_path', '_identity', '_sha256', '_facts_json', '_names', '_deferred', '_directories', '_token')

    def __init__(self, token, path, identity, checksum, facts, nodes, resolved):
        if token is not _PREPARED_TOKEN:
            raise ArchiveError('Prepared archives must be created by the archive auditor')
        available, deferred = set(), []
        for name, row in nodes.items():
            if row['kind'] == 'file':
                available.add(name)
            elif row['kind'] == 'hardlink':
                if row['link'] in available:
                    available.add(name)
                else:
                    deferred.append((name, resolved[name]))
        fields = {
            '_path': path, '_identity': identity, '_sha256': checksum,
            '_facts_json': json.dumps(facts, sort_keys=True), '_names': tuple(nodes),
            '_deferred': tuple(deferred),
            '_directories': tuple((name, row['mode'], row['mtimeNs']) for name, row in nodes.items()
                                  if row['kind'] == 'directory'),
            '_token': token,
        }
        for name, value in fields.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError('Prepared archive snapshots are immutable')

    @property
    def facts(self):
        # Callers may inspect/edit their own copy without changing the proof
        # subsequently consumed by native extraction.
        return json.loads(self._facts_json)


def prepare_archive(path, expected_sha256=None, expected_entries=None, progress=None):
    """Audit gzip members exactly once and retain a bound native extraction plan."""
    path = no_links(path)
    if expected_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', str(expected_sha256)):
        raise ArchiveError('Invalid source archive digest')
    if expected_entries is not None and (type(expected_entries) is not int
                                         or not 0 < expected_entries <= MAX_ENTRIES):
        raise ArchiveError('Invalid source archive entry count')
    identity = _archive_identity(path)
    checksum = _archive_digest(path)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ArchiveError('The source archive digest differs from its signed declaration')
    if progress:
        progress(0, expected_entries or 0)
    facts, nodes, resolved = _scan(path, progress=progress, expected_entries=expected_entries, single_pass=True)
    if expected_entries is not None and facts['entryCount'] != expected_entries:
        raise ArchiveError('The source archive entry count differs from its signed declaration')
    if _archive_identity(path) != identity:
        raise ArchiveError('The source archive changed during its safety audit')
    snapshot = PreparedArchive(_PREPARED_TOKEN, path, identity, checksum, facts, nodes, resolved)
    if progress:
        progress(facts['entryCount'], facts['entryCount'])
    return snapshot


def _check_prepared(snapshot):
    if type(snapshot) is not PreparedArchive or snapshot._token is not _PREPARED_TOKEN:
        raise ArchiveError('A complete prepared archive audit is required')
    if (_archive_identity(snapshot._path) != snapshot._identity
            or _archive_digest(snapshot._path) != snapshot._sha256
            or _archive_identity(snapshot._path) != snapshot._identity):
        raise ArchiveError('The source archive changed after its safety audit')


def _native_environment():
    value = dict(os.environ)
    for name in ('TAR_OPTIONS', 'GZIP', 'GZIP_OPT', 'POSIXLY_CORRECT', 'TAPE',
                 'ENV', 'BASH_ENV', 'SHELLOPTS'):
        value.pop(name, None)
    value.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL='C')
    return value


def _stop_tar(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _native_archive_extract(snapshot, destination, included, report):
    executable = shutil.which('tar', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not executable:
        raise ArchiveError('GNU tar is required for platform extraction')
    environment = _native_environment()
    version = subprocess.run([executable, '--version'], stdin=subprocess.DEVNULL,
                             capture_output=True, env=environment, timeout=10, check=False)
    if version.returncode or b'GNU tar' not in version.stdout[:512]:
        raise ArchiveError('GNU tar is required for platform extraction')
    expected = len(included) if included is not None else len(snapshot._names)
    with tempfile.TemporaryDirectory(prefix='.mdu-tar-list-', dir=destination.parent) as temporary:
        command = [executable, '--extract', '--gzip', '--file', str(snapshot._path),
                   '--directory', str(destination), '--no-same-owner', '--same-permissions',
                   '--delay-directory-restore', '--overwrite-dir',
                   '--verbose', '--quoting-style=escape']
        if included is not None:
            names = Path(temporary) / 'members.list'
            with names.open('wb') as output:
                for name in included:
                    output.write(os.fsencode(name) + b'\0')
            command.extend(['--no-recursion', '--null', '--verbatim-files-from', '--files-from', str(names)])
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment)
        announced, output_buffer, errors = 0, b'', b''
        try:
            with selectors.DefaultSelector() as selector:
                for stream, kind in ((process.stdout, 'output'), (process.stderr, 'error')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, kind)
                deadline = time.monotonic() + 3600
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise ArchiveError('Native tar extraction timed out')
                    for key, _ in selector.select(timeout=.5):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == 'error':
                            errors = (errors + block)[-8192:]
                            continue
                        output_buffer += block
                        while b'\n' in output_buffer:
                            row, output_buffer = output_buffer.split(b'\n', 1)
                            if row:
                                announced += 1
                                if announced > expected:
                                    raise ArchiveError('Native tar reported an unexpected member count')
                                # tar prints a name before writing its data.
                                # The preceding member is the completed one.
                                report(max(0, announced - 1))
                        if len(output_buffer) > 128 * 1024:
                            raise ArchiveError('Native tar produced an oversized progress record')
            code = process.wait(timeout=30)
            if output_buffer.strip():
                raise ArchiveError('Native tar progress stream is incomplete')
            if code:
                detail = errors.decode('utf-8', errors='replace')[-2048:]
                raise ArchiveError('Native tar extraction failed: ' + detail)
            if announced != expected:
                raise ArchiveError('Native tar did not process the complete audited member set')
        except BaseException:
            _stop_tar(process)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
    return expected


def extract_prepared_archive(snapshot, destination, progress=None):
    """Use GNU tar for an already audited original archive, without rescanning it."""
    if os.name != 'posix':
        raise ArchiveError('Native extraction requires Linux filesystem semantics')
    _check_prepared(snapshot)
    destination = no_links(destination)
    if (not destination.is_dir() or destination.stat().st_uid != os.geteuid()
            or destination.stat().st_mode & 0o077 or any(destination.iterdir())):
        raise ArchiveError('Native extraction requires an owned, empty private directory')
    total, last_report = len(snapshot._names), [float('-inf'), -1]

    def report(completed, force=False):
        now = time.monotonic()
        if progress and (force or completed != last_report[1] and now - last_report[0] >= 1):
            progress(completed, total)
            last_report[:] = [now, completed]

    report(0, force=True)
    deferred_names = {name for name, _ in snapshot._deferred}
    included = tuple(name for name in snapshot._names if name not in deferred_names) if deferred_names else None
    completed = _native_archive_extract(snapshot, destination, included, report)
    # The normal publisher archive uses backward hardlinks, all handled in C.
    # Only rare forward references or chains need a delayed canonical link.
    affected_directories = set()
    for name, _ in snapshot._deferred:
        parent = posixpath.dirname(name)
        while parent:
            affected_directories.add(parent)
            parent = posixpath.dirname(parent)
    declared = {name: (mode, timestamp) for name, mode, timestamp in snapshot._directories}
    original_modes = {}
    try:
        # Native tar has already restored directory permissions. Temporarily
        # permit the owner to traverse/write the few parents needed by rare
        # forward links, then restore their declared modes and ns timestamps.
        for name in sorted(affected_directories, key=lambda value: len(value.split('/'))):
            path = destination / name
            if path.exists():
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ArchiveError('A delayed archive hardlink parent changed')
                original_modes[name] = stat.S_IMODE(info.st_mode)
                os.chmod(path, original_modes[name] | 0o300)
        for name, canonical in snapshot._deferred:
            target, source = destination / name, destination / canonical
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() or target.is_symlink() or not stat.S_ISREG(source.lstat().st_mode):
                raise ArchiveError('A delayed archive hardlink target changed')
            os.link(source, target, follow_symlinks=False)
            completed += 1
            report(min(completed, total - 1))
    finally:
        for name in sorted(affected_directories, key=lambda value: len(value.split('/')), reverse=True):
            path = destination / name
            if name in declared and path.is_dir():
                mode, timestamp = declared[name]
                os.chmod(path, mode)
                os.utime(path, ns=(timestamp, timestamp))
            elif name in original_modes:
                os.chmod(path, original_modes[name])
    _check_prepared(snapshot)
    result = destination / snapshot.facts['root']
    if result.is_symlink() or not result.is_dir():
        raise ArchiveError('Native tar did not create the audited source root')
    report(total, force=True)
    return result


PUBLISHED_POLICY = 'sdk-platform-audit-v2'
PUBLISHED_FIELDS = {'schemaVersion', 'policy', 'archiveSha256', 'archiveBytes', 'root',
                    'entryCount', 'regularBytes', 'expandedBytes', 'tarBytes', 'kinds',
                    'sdkVersion', 'forwardHardlinkCount', 'hasScreenPatch', 'entrypoints',
                    'runtimeVariants', 'allDirectoriesTraversable'}
PUBLISHED_ENTRYPOINTS = ('install.sh', 'scripts/platform-install.sh')
_PUBLISHED_TOKEN = object()


def create_archive_certificate(path):
    """Publish the result of one complete safety audit, bound to exact bytes.

    The caller must include this certificate and the original archive in the
    authenticated MDU. It is an attestation by the already trusted publisher,
    not an independently signed assertion or a substitute for the MDU signature.
    """
    path = no_links(path)
    identity = _archive_identity(path)
    checksum = _archive_digest(path)
    facts, nodes, _ = _scan(path, single_pass=True, stream_bytes=True)
    if _archive_identity(path) != identity:
        raise ArchiveError('The source archive changed during its publication audit')
    available, forward = set(), 0
    for name, row in nodes.items():
        if row['kind'] == 'file':
            available.add(name)
        elif row['kind'] == 'hardlink':
            if row['link'] in available:
                available.add(name)
            else:
                forward += 1
    entrypoints = {}
    for relative in PUBLISHED_ENTRYPOINTS:
        value = nodes.get(facts['root'] + '/' + relative)
        if not value or value['kind'] != 'file' or value['bytes'] < 1:
            raise ArchiveError('The complete platform archive needs regular nonempty installer entry points')
        entrypoints[relative] = {'kind': 'file', 'bytes': value['bytes']}
    certificate = {
        'schemaVersion': 2, 'policy': PUBLISHED_POLICY,
        'archiveSha256': checksum, 'archiveBytes': identity[2], 'root': facts['root'],
        'entryCount': facts['entryCount'], 'regularBytes': facts['regularBytes'],
        'expandedBytes': facts['expandedBytes'], 'tarBytes': facts['tarBytes'],
        'kinds': {name: facts['kinds'].get(name, 0)
                  for name in ('directory', 'file', 'hardlink', 'symlink')},
        'sdkVersion': facts['sdkVersion'], 'forwardHardlinkCount': forward,
        'hasScreenPatch': facts['root'] + '/lock-screen/manifest.json' in nodes,
        'entrypoints': entrypoints, 'runtimeVariants': facts['runtimeVariants'],
        'allDirectoriesTraversable': all(row['mode'] & 0o555 == 0o555
                                        for row in nodes.values() if row['kind'] == 'directory'),
    }
    _validate_published_certificate(certificate, allow_unsupported=True)
    return certificate


def _validate_published_certificate(value, allow_unsupported=False):
    if (not isinstance(value, dict) or set(value) != PUBLISHED_FIELDS
            or type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 2
            or value.get('policy') != PUBLISHED_POLICY):
        raise ArchiveError('Unsupported published platform audit certificate')
    try:
        encoded = json.dumps(value, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as error:
        raise ArchiveError('Invalid published platform audit certificate') from error
    if len(encoded) > MAX_JSON_BYTES:
        raise ArchiveError('Published platform audit certificate exceeds its size limit')
    if (not isinstance(value.get('archiveSha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['archiveSha256'])
            or not isinstance(value.get('root'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value['root'])
            or not isinstance(value.get('sdkVersion'), str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', value['sdkVersion'])):
        raise ArchiveError('Invalid published platform archive identity')
    for name, maximum in (('archiveBytes', MAX_REGULAR_BYTES), ('entryCount', MAX_ENTRIES),
                          ('regularBytes', MAX_REGULAR_BYTES), ('expandedBytes', MAX_EXPANDED_BYTES),
                          ('tarBytes', MAX_EXPANDED_BYTES)):
        if type(value.get(name)) is not int or not 0 < value[name] <= maximum:
            raise ArchiveError('Invalid published platform archive bounds')
    if (value['expandedBytes'] < value['regularBytes'] or value['tarBytes'] % 512
            or value['tarBytes'] < value['regularBytes'] + value['entryCount'] * 512 + 1024):
        raise ArchiveError('Inconsistent published platform archive bounds')
    kinds = value.get('kinds')
    if not isinstance(kinds, dict) or set(kinds) != {'directory', 'file', 'hardlink', 'symlink'}:
        raise ArchiveError('Invalid published platform member types')
    if (any(type(number) is not int or not 0 <= number <= MAX_ENTRIES for number in kinds.values())
            or sum(kinds.values()) != value['entryCount'] or kinds['directory'] < 1
            or kinds['file'] < 2 or kinds['symlink'] > 1):
        raise ArchiveError('Inconsistent published platform member counts')
    forward = value.get('forwardHardlinkCount')
    if (type(forward) is not int or not 0 <= forward <= kinds['hardlink']
            or type(value.get('hasScreenPatch')) is not bool):
        raise ArchiveError('Invalid published platform extraction policy')
    if not allow_unsupported and (forward or value['hasScreenPatch']):
        raise ArchiveError('This platform archive requires the full-audit or dedicated rescue path')
    if type(value.get('allDirectoriesTraversable')) is not bool:
        raise ArchiveError('Invalid published platform directory permission policy')
    entrypoints = value.get('entrypoints')
    if not isinstance(entrypoints, dict) or set(entrypoints) != set(PUBLISHED_ENTRYPOINTS):
        raise ArchiveError('Missing published platform installer entry points')
    for row in entrypoints.values():
        if (not isinstance(row, dict) or set(row) != {'kind', 'bytes'} or row.get('kind') != 'file'
                or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= MAX_MEMBER_BYTES):
            raise ArchiveError('Invalid published platform installer entry point')
    variants = value.get('runtimeVariants')
    if (not isinstance(variants, list) or not 1 <= len(variants) <= 128
            or any(not isinstance(row, dict) for row in variants)):
        raise ArchiveError('Invalid published runtime variant summary')


class PreparedPublishedArchive:
    """Process-local proof of a signed publication certificate and source hash."""
    __slots__ = ('_path', '_identity', '_sha256', '_facts_json', '_token')
    progress_unit = 'bytes'

    def __init__(self, token, path, identity, certificate):
        if token is not _PUBLISHED_TOKEN:
            raise ArchiveError('Published archives must be loaded from an authenticated certificate')
        facts = {key: certificate[key] for key in ('root', 'entryCount', 'regularBytes',
                                                  'expandedBytes', 'tarBytes', 'kinds',
                                                  'sdkVersion', 'runtimeVariants')}
        facts['entries'] = facts['entryCount']
        facts['requiresDirectoryPermissionFixes'] = not certificate['allDirectoriesTraversable']
        for name, value in {
                '_path': path, '_identity': identity, '_sha256': certificate['archiveSha256'],
                '_facts_json': json.dumps(facts, sort_keys=True), '_token': token}.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError('Published archive snapshots are immutable')

    @property
    def facts(self):
        return json.loads(self._facts_json)


def load_published_archive(path, certificate, expected_sha256, expected_entries):
    """Load an MDU-authenticated certificate without a device-side tar scan.

    The wrapper must first verify the certificate file against the hash in its
    authenticated platform-release.json. All supplied archive bytes are still
    hashed here; extraction also checks their identity and final digest.
    """
    _validate_published_certificate(certificate)
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or type(expected_entries) is not int or not 0 < expected_entries <= MAX_ENTRIES
            or certificate['archiveSha256'] != expected_sha256
            or certificate['entryCount'] != expected_entries):
        raise ArchiveError('Published audit differs from the signed platform declaration')
    path = no_links(path)
    identity = _archive_identity(path)
    if (identity[2] != certificate['archiveBytes'] or _archive_digest(path) != expected_sha256
            or _archive_identity(path) != identity):
        raise ArchiveError('The original platform archive differs from its published audit')
    return PreparedPublishedArchive(_PUBLISHED_TOKEN, path, identity, certificate)


def _check_published(snapshot, verify_digest=False):
    if type(snapshot) is not PreparedPublishedArchive or snapshot._token is not _PUBLISHED_TOKEN:
        raise ArchiveError('A verified published archive certificate is required')
    if (_archive_identity(snapshot._path) != snapshot._identity
            or verify_digest and _archive_digest(snapshot._path) != snapshot._sha256
            or _archive_identity(snapshot._path) != snapshot._identity):
        raise ArchiveError('The source archive changed after publication verification')


def _native_published_extract(snapshot, destination, report):
    executable = shutil.which('tar', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not executable:
        raise ArchiveError('GNU tar is required for platform extraction')
    environment = _native_environment()
    version = subprocess.run([executable, '--version'], stdin=subprocess.DEVNULL,
                             capture_output=True, env=environment, timeout=10, check=False)
    if version.returncode or b'GNU tar' not in version.stdout[:512]:
        raise ArchiveError('GNU tar is required for platform extraction')
    command = [executable, '--extract', '--gzip', '--file', str(snapshot._path),
               '--directory', str(destination), '--no-same-owner', '--no-same-permissions',
               '--overwrite-dir', '--blocking-factor=20', '--checkpoint=64',
               '--checkpoint-action=echo=MDU_TAR_BYTES:%u', '--quoting-style=escape']
    process_options = {}
    if _child_umask_supported():
        # Popen performs this in its child without unsafe Python preexec_fn
        # callbacks while the wrapper's progress heartbeat thread is running.
        process_options['umask'] = 0o022
    else:
        # Python 3.8 has no Popen umask keyword. A fixed shell program receives
        # every archive/target argument positionally; none is interpolated.
        command = ['/bin/sh', '-c', 'umask 022; exec "$@"', 'mdu-native-tar', *command]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, env=environment, **process_options)
    output_buffer, errors, previous_record = b'', b'', 0
    total = snapshot.facts['tarBytes']
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stderr, selectors.EVENT_READ)
            deadline = time.monotonic() + 3600
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ArchiveError('Native tar extraction timed out')
                for key, _ in selector.select(timeout=.5):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    output_buffer += block
                    while b'\n' in output_buffer:
                        row, output_buffer = output_buffer.split(b'\n', 1)
                        match = re.search(rb'(?:^|: )MDU_TAR_BYTES:([0-9]+)$', row)
                        if match:
                            record = int(match.group(1))
                            if record < previous_record or record > (total + 10239) // 10240 + 1:
                                raise ArchiveError('Native tar reported an invalid byte checkpoint')
                            previous_record = record
                            # GNU tar emits before processing this record.
                            report(min(total - 1, max(0, (record - 1) * 10240)))
                        else:
                            errors = (errors + row + b'\n')[-8192:]
                    if len(output_buffer) > 128 * 1024:
                        raise ArchiveError('Native tar produced an oversized diagnostic record')
        code = process.wait(timeout=30)
        if output_buffer:
            errors = (errors + output_buffer)[-8192:]
        if code:
            raise ArchiveError('Native tar extraction failed: '
                               + errors.decode('utf-8', errors='replace')[-2048:])
    except BaseException:
        _stop_tar(process)
        raise
    finally:
        process.stderr.close()


def _child_umask_supported():
    return sys.version_info >= (3, 9)


def extract_published_archive(snapshot, destination, progress=None):
    """Direct GNU tar extraction after signed publication and byte verification."""
    if os.name != 'posix':
        raise ArchiveError('Native extraction requires Linux filesystem semantics')
    _check_published(snapshot)
    destination = no_links(destination)
    if (not destination.is_dir() or destination.stat().st_uid != os.geteuid()
            or destination.stat().st_mode & 0o077 or any(destination.iterdir())):
        raise ArchiveError('Native extraction requires an owned, empty private directory')
    total, last_report = snapshot.facts['tarBytes'], [float('-inf'), -1]

    def report(completed, force=False):
        now = time.monotonic()
        if progress and (force or completed != last_report[1] and now - last_report[0] >= 1):
            progress(completed, total)
            last_report[:] = [now, completed]

    report(0, force=True)
    _native_published_extract(snapshot, destination, report)
    _check_published(snapshot, verify_digest=True)
    root = destination / snapshot.facts['root']
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError('Native tar did not create the published platform root')
    for relative in PUBLISHED_ENTRYPOINTS:
        path = no_links(root / relative)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size < 1:
            raise ArchiveError('The published platform installation entry is missing')
    report(total, force=True)
    return root


def _relative_tree(nodes, resolved, prefix):
    """A release-local logical tree; content identity is the original hardlink."""
    tree = {}
    for name, row in nodes.items():
        if not name.startswith(prefix + '/'):
            continue
        relative = name[len(prefix) + 1:]
        item = dict(row)
        if row['kind'] in ('file', 'hardlink'):
            canonical = resolved[name]
            item.update(kind='file', bytes=nodes[canonical]['bytes'], link='', canonical=canonical)
        tree[relative] = item
    for name in list(tree):
        parent = posixpath.dirname(name)
        while parent:
            tree.setdefault(parent, {'kind': 'directory', 'bytes': 0, 'mode': 0o755,
                                     'mtimeNs': 0, 'link': ''})
            parent = posixpath.dirname(parent)
    root = nodes.get(prefix, {'mode': 0o755, 'mtimeNs': 0})
    return tree, {'mode': root['mode'], 'mtimeNs': root['mtimeNs']}


def _payload_plan(tree, root, base_groups=None):
    groups, members, data = dict(base_groups or {}), {}, {}
    for name in sorted(tree, key=lambda value: (len(PurePosixPath(value).parts), value)):
        row = tree[name]
        if row['kind'] == 'directory':
            members[name] = {k: row[k] for k in ('kind', 'bytes', 'mode', 'mtimeNs', 'link')}
    for name in sorted(tree):
        row = tree[name]
        if row['kind'] != 'file':
            continue
        key = row['canonical']
        item = {k: row[k] for k in ('kind', 'bytes', 'mode', 'mtimeNs', 'link')}
        if key in groups:
            item.update(kind='hardlink', bytes=0, link=groups[key])
        else:
            groups[key] = name
            data[name] = key
        members[name] = item
    for name in sorted(tree):
        row = tree[name]
        if row['kind'] == 'symlink':
            members[name] = {k: row[k] for k in ('kind', 'bytes', 'mode', 'mtimeNs', 'link')}
    return {'members': members, 'entryCount': len(members), 'rootMode': root['mode'],
            'rootMtimeNs': root['mtimeNs'], 'regularBytes': sum(r['bytes'] for r in members.values())}, data, groups


class _CountingWriter:
    def __init__(self, target):
        self.target, self.bytes = target, 0

    def write(self, raw):
        count = self.target.write(raw)
        self.bytes += len(raw)
        return count

    def flush(self):
        self.target.flush()


def _write_payload(path, declaration, data, blobs):
    with Path(path).open('xb') as output:
        with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0, compresslevel=6) as zipped:
            counter = _CountingWriter(zipped)
            with tarfile.open(fileobj=counter, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for name, row in declaration['members'].items():
                    info = tarfile.TarInfo(name)
                    info.mode, info.size = row['mode'], row['bytes']
                    info.mtime = row['mtimeNs'] // 1000000000
                    info.pax_headers = {'mtime': str(Decimal(row['mtimeNs']) / Decimal(1000000000))}
                    info.type = {'file': tarfile.REGTYPE, 'directory': tarfile.DIRTYPE,
                                 'hardlink': tarfile.LNKTYPE, 'symlink': tarfile.SYMTYPE}[row['kind']]
                    info.linkname = row['link']
                    if row['kind'] == 'file':
                        with blobs[data[name]].open('rb') as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)
            declaration['tarBytes'] = counter.bytes


def build_optimized_payloads(path, output_dir, progress=None):
    """Factor six runtime trees into one common tree and small variant overlays.

    Only the workstation reads the full publisher archive. The device extracts
    SDK, common Runtime, and one overlay directly into their final private stages.
    Content identity here uses the publisher's validated hardlinks, not new model
    digest admission rules.
    """
    path, output_dir = no_links(path), no_links(output_dir)
    if not output_dir.is_dir():
        raise ArchiveError('Payload output directory is missing')
    audit, nodes, resolved = _scan(path)
    sdk_tree, sdk_root = _relative_tree(nodes, resolved, audit['root'] + '/releases/sdk-common')
    trees = {}
    roots = {}
    for variant in audit['runtimeVariants']:
        source = variant['sourcePath']
        trees[source], roots[source] = _relative_tree(nodes, resolved, audit['root'] + '/' + source)
    first = next(iter(trees.values()))
    common = {}
    for name, row in first.items():
        if row['kind'] != 'file':
            continue
        if all(tree.get(name) == row for tree in trees.values()):
            common[name] = row
    # Common extraction needs parent directories. Every overlay repeats its
    # complete directory metadata so adding overlay files preserves final mtimes.
    for name in list(common):
        parent = posixpath.dirname(name)
        while parent:
            common[parent] = first[parent]
            parent = posixpath.dirname(parent)
    payloads, sources = {}, {}
    payloads['sdk.tar.gz'], sources['sdk.tar.gz'], _ = _payload_plan(sdk_tree, sdk_root)
    first_source = next(iter(trees))
    payloads['runtime-common.tar.gz'], sources['runtime-common.tar.gz'], base_groups = _payload_plan(common, roots[first_source])
    overlays = {}
    for index, (source, tree) in enumerate(trees.items()):
        name = 'runtime-variant-{:02d}.tar.gz'.format(index)
        overlay = {key: row for key, row in tree.items() if row['kind'] == 'directory' or key not in common}
        payloads[name], sources[name], _ = _payload_plan(overlay, roots[source], base_groups)
        overlays[source] = name
    wanted = {canonical for members in sources.values() for canonical in members.values()}
    with tempfile.TemporaryDirectory(prefix='sdk-payload-spool-', dir=output_dir.parent if os.name == 'nt' else None) as temporary:
        spool, blobs = Path(temporary), {}
        with tarfile.open(path, 'r|gz') as archive:
            for member in archive:
                if member.name not in wanted:
                    continue
                if not member.isfile():
                    raise ArchiveError('A payload source ceased to be a regular file')
                target = spool / '{:06d}'.format(len(blobs))
                with archive.extractfile(member) as stream, target.open('xb') as output:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
                if target.stat().st_size != member.size:
                    raise ArchiveError('A payload source was truncated')
                blobs[member.name] = target
        if set(blobs) != wanted:
            raise ArchiveError('A planned payload source is missing')
        for index, (name, declaration) in enumerate(payloads.items()):
            _write_payload(output_dir / name, declaration, sources[name], blobs)
            if progress:
                progress(index + 1, len(payloads), 'Built ' + name)
    layout = {'schemaVersion': 1, 'sdk': 'sdk.tar.gz', 'runtimeCommon': 'runtime-common.tar.gz',
              'variants': overlays, 'payloads': payloads,
              'sourceArchiveRoot': audit['root'], 'sdkVersion': audit['sdkVersion'],
              'catalog': audit['catalog'], 'runtimeVariants': audit['runtimeVariants'],
              'sdkDescriptor': audit['sdkDescriptor'], 'variantDescriptors': audit['variantDescriptors'],
              'sourceEntries': audit['entries'], 'sourceRegularBytes': audit['regularBytes'],
              'entryCount': sum(row['entryCount'] for row in payloads.values()),
              'regularBytes': sum(row['regularBytes'] for row in payloads.values())}
    (output_dir / 'layout.json').write_text(json.dumps(layout, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    return layout


def _validate_payload_declaration(declaration, base=None):
    if not isinstance(declaration, dict) or not isinstance(declaration.get('members'), dict):
        raise ArchiveError('Invalid optimized payload declaration')
    members = declaration['members']
    if (type(declaration.get('rootMode')) is not int or not 0 <= declaration['rootMode'] <= 0o777
            or type(declaration.get('rootMtimeNs')) is not int
            or type(declaration.get('tarBytes')) is not int or not 0 < declaration['tarBytes'] <= MAX_EXPANDED_BYTES):
        raise ArchiveError('Invalid optimized root metadata or tar size')
    if len(members) > MAX_ENTRIES or declaration.get('entryCount') != len(members):
        raise ArchiveError('Invalid optimized entry count')
    combined = dict(base.get('members', {})) if base else {}
    for name, row in members.items():
        safe_name(name)
        if not isinstance(row, dict) or set(row) != {'kind', 'bytes', 'mode', 'mtimeNs', 'link'}:
            raise ArchiveError('Invalid optimized entry metadata')
        if (row['kind'] not in ('file', 'directory', 'hardlink', 'symlink')
                or type(row['bytes']) is not int or not 0 <= row['bytes'] <= MAX_MEMBER_BYTES
                or type(row['mode']) is not int or not 0 <= row['mode'] <= 0o777
                or type(row['mtimeNs']) is not int):
            raise ArchiveError('Invalid optimized entry bounds')
        if not isinstance(row['link'], str) or len(row['link']) > 4096:
            raise ArchiveError('Invalid optimized link metadata')
        if row['kind'] != 'file' and row['bytes'] != 0:
            raise ArchiveError('Nonregular optimized entries cannot carry data')
        if name in combined and (row['kind'] != 'directory' or combined[name]['kind'] != 'directory'):
            raise ArchiveError('An overlay replaces a common file')
        combined[name] = row
    regular_bytes = sum(row['bytes'] for row in members.values() if row['kind'] == 'file')
    if (declaration.get('regularBytes') != regular_bytes or regular_bytes > MAX_REGULAR_BYTES
            or declaration['tarBytes'] < regular_bytes):
        raise ArchiveError('Invalid optimized payload data size')
    resolved = {name: name for name, row in combined.items() if row['kind'] == 'file'}
    for name, row in combined.items():
        parent = posixpath.dirname(name)
        while parent:
            if combined.get(parent, {}).get('kind') != 'directory':
                raise ArchiveError('An optimized entry parent is not a directory')
            parent = posixpath.dirname(parent)
        if row['kind'] == 'symlink':
            if (name != 'bin/application' or row['link'] != '../mingda-printer'
                    or combined.get('mingda-printer', {}).get('kind') not in ('file', 'hardlink')):
                raise ArchiveError('Unsupported optimized symbolic link')
        if row['kind'] == 'hardlink':
            target, seen = name, set()
            while target not in resolved:
                if target in seen or len(seen) >= 1024:
                    raise ArchiveError('Cyclic or excessive optimized hard link')
                seen.add(target)
                if combined.get(target, {}).get('kind') != 'hardlink':
                    raise ArchiveError('Optimized hard link does not select regular data')
                target = safe_name(combined[target]['link'])
            canonical = resolved[target]
            final = combined[canonical]
            if final.get('kind') != 'file' or any(row[key] != final[key] for key in ('mode', 'mtimeNs')):
                raise ArchiveError('Optimized hard link does not select matching regular data')
            for alias in seen:
                resolved[alias] = canonical
    return combined


def _check_existing_base(destination, base):
    expected = base['members']
    seen = set()
    pending = [(destination, '')]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                name = prefix + entry.name
                row = expected.get(name)
                if row is None or entry.is_symlink():
                    raise ArchiveError('Unexpected entry in the common runtime stage')
                if entry.is_dir(follow_symlinks=False):
                    if row['kind'] != 'directory':
                        raise ArchiveError('Common runtime entry type changed')
                    pending.append((Path(entry.path), name + '/'))
                elif not entry.is_file(follow_symlinks=False) or row['kind'] not in ('file', 'hardlink'):
                    raise ArchiveError('Common runtime entry type changed')
                seen.add(name)
    if seen != set(expected):
        raise ArchiveError('Common runtime stage is incomplete')


def extract_payload(path, destination, declaration, *, base=None, progress=None):
    """Validate a signed compact archive once, then use native tar in a private stage."""
    if os.name != 'posix':
        raise ArchiveError('Optimized extraction requires Linux filesystem semantics')
    path, destination = no_links(path), no_links(destination)
    if (not destination.is_dir() or destination.stat().st_uid != os.geteuid()
            or destination.stat().st_mode & 0o022):
        raise ArchiveError('Extraction requires an owned, non-writable private stage')
    _validate_payload_declaration(declaration, base)
    if base:
        _check_existing_base(destination, base)
    elif any(destination.iterdir()):
        raise ArchiveError('The extraction stage must be empty')
    seen = set()
    members = declaration['members']
    last = time.monotonic()
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            name, row = _row(member)
            if name in seen or members.get(name) != row:
                raise ArchiveError('Optimized archive differs from its signed declaration')
            seen.add(name)
            if progress and time.monotonic() - last >= .5:
                progress(0, declaration['tarBytes'], 'Checking ' + path.name + ': ' + str(len(seen)) + '/' + str(len(members)) + ' entries')
                last = time.monotonic()
    if seen != set(members):
        raise ArchiveError('The optimized archive is incomplete')
    executable = shutil.which('tar')
    if not executable:
        raise ArchiveError('GNU tar is required for optimized extraction')
    command = [executable, '--extract', '--gzip', '--file', str(path), '--directory', str(destination),
               '--no-same-owner', '--same-permissions', '--delay-directory-restore',
               '--checkpoint=128', '--checkpoint-action=echo=MDU_TAR_BLOCKS=%u']
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, env={**os.environ, 'LC_ALL': 'C', 'TAR_OPTIONS': ''})
    errors = []
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            while True:
                events = selector.select(timeout=1)
                if events:
                    line = process.stderr.readline()
                    if not line:
                        break
                    text = line.decode('utf-8', 'replace').strip()
                    match = re.search(r'MDU_TAR_BLOCKS=([0-9]+)$', text)
                    if match:
                        done = min(declaration['tarBytes'], int(match.group(1)) * 20 * 512)
                        if progress:
                            progress(done, declaration['tarBytes'], 'Extracting ' + path.name)
                    elif len(errors) < 8:
                        errors.append(text[:256])
                elif process.poll() is not None:
                    break
        code = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        raise
    finally:
        process.stderr.close()
    if code:
        raise ArchiveError('Native tar extraction failed: ' + '; '.join(errors))
    os.utime(destination, ns=(declaration['rootMtimeNs'], declaration['rootMtimeNs']))
    if progress:
        progress(declaration['tarBytes'], declaration['tarBytes'], 'Extracted ' + path.name)
    return destination
