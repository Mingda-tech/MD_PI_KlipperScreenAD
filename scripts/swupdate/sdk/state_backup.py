"""Fixed, private SQLite baselines for stopped managed SDK applications."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


class StateBackupError(RuntimeError):
    pass


SIDECARS = ('', '-wal', '-shm', '-journal')
EXTENSIONS = ('.db', '.sqlite', '.sqlite3')
EXCLUDED = {'models', 'model', 'cache', 'caches', 'output', 'temp', 'tmp', 'keys',
            'config', 'configs', 'ai-evidence', 'inspection-evidence', 'device-lock'}
LEGACY_STATE_ROOTS = (Path('/home/mingda/printer_data/state'),
                      Path('/home/mingda/MINGDA-3DPrinting-SDK/state'))
MAX_FILE_BYTES = 2 * 1024 ** 3


def _safe(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise StateBackupError('A database or baseline path contains a symbolic link')
    return path


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _excluded(path):
    return any(part.lower() in EXCLUDED or part.lower().startswith('.cache') for part in path.parts)


def _protected(path, data_root):
    if _within(path, data_root):
        return _excluded(path.relative_to(data_root))
    for root in LEGACY_STATE_ROOTS:
        if _within(path, root):
            return _excluded(path.relative_to(root))
    release = Path('/opt/mingda/apps/sdk/releases')
    if _within(path, release):
        return _excluded(Path(*path.relative_to(release).parts[3:]))
    return True


def _database_path(path, data_root, extra=(), *, inspect=True):
    path = _safe(path)
    if _protected(path, data_root) or path.name in ('lock.db', 'device-lock.db'):
        raise StateBackupError('Only SDK state databases may be captured')
    if not _within(path, data_root) and path not in extra:
        raise StateBackupError('Database path is outside the declared SDK state')
    if any(path.name.endswith(suffix) and path.name[:-len(suffix)].endswith(EXTENSIONS)
           for suffix in SIDECARS[1:]):
        raise StateBackupError('An explicit database path must name the main database')
    if inspect and path.exists():
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise StateBackupError('Database must be a bounded regular file')
        with path.open('rb') as stream:
            header = stream.read(16)
        if header and header != b'SQLite format 3\0':
            raise StateBackupError('The selected state file is not SQLite')
    return path


def _request(data_root, additional_paths):
    data_root = _safe(data_root)
    if _excluded(Path(data_root.name)):
        raise StateBackupError('SDK state root must not be a model, cache or configuration directory')
    extras = []
    for raw in additional_paths:
        path = _safe(raw)
        allowed = _within(path, data_root) or any(_within(path, _safe(root)) for root in LEGACY_STATE_ROOTS)
        # A legacy release may have used its own state/ command journal.
        release = Path('/opt/mingda/apps/sdk/releases')
        relative = path.relative_to(release) if _within(path, release) else None
        allowed = allowed or (relative is not None and len(relative.parts) >= 4 and relative.parts[2] == 'state')
        if not allowed:
            raise StateBackupError('Explicit database path is outside supported SDK state directories')
        _database_path(path, data_root, (path,), inspect=False)
        extras.append(str(path))
    return {'schemaVersion': 1, 'dataRoot': str(data_root), 'additionalPaths': sorted(set(extras))}


def _sync(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path, value):
    path = _safe(path)
    temporary = path.with_name('.' + path.name + '.partial')
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write((json.dumps(value, sort_keys=True) + '\n').encode())
        stream.flush(); os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    _sync(path.parent)


def _read_json(path):
    path = _safe(path)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise StateBackupError('Database baseline metadata is missing or too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise StateBackupError('Invalid database baseline metadata')
    return value


def _stopped(run):
    result = run(['systemctl', 'show', 'mingda-app@sdk.service', '-p', 'MainPID'],
                 stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=15)
    if result.returncode or result.stdout.strip() not in ('MainPID=0', '0'):
        raise StateBackupError('The managed SDK must be stopped before database capture or restoration')


def _digest(path):
    value = hashlib.sha256()
    with _safe(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _discover(request):
    root = Path(request['dataRoot'])
    bases = {root / 'command_journal.db', root / 'ai-agent.db'}
    bases.update(Path(value) for value in request['additionalPaths'])
    if root.exists():
        if not root.is_dir():
            raise StateBackupError('SDK state root must be a directory')
        for parent, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if not _excluded(Path(name))]
            if any((Path(parent) / name).is_symlink() for name in directories):
                raise StateBackupError('SDK database discovery cannot traverse linked directories')
            for name in files:
                base = name
                for suffix in SIDECARS[1:]:
                    if base.endswith(suffix):
                        base = base[:-len(suffix)]
                        break
                if base.endswith(EXTENSIONS) and base not in ('lock.db', 'device-lock.db'):
                    bases.add(Path(parent) / base)
    if len(bases) > 512:
        raise StateBackupError('Too many SDK state databases')
    return sorted(_database_path(p, root, tuple(Path(v) for v in request['additionalPaths'])) for p in bases)


def _file_record(path):
    path = _safe(path)
    if not path.exists():
        return {'path': str(path), 'present': False}
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
        raise StateBackupError('SDK database state must be a bounded regular file')
    return {'path': str(path), 'present': True, 'bytes': before.st_size,
            'sha256': _digest(path), 'mode': stat.S_IMODE(before.st_mode),
            'uid': before.st_uid, 'gid': before.st_gid,
            'mtimeNs': before.st_mtime_ns, 'atimeNs': before.st_atime_ns}


def _records_equal(left, right):
    # Reading a source file may change atime. Data, ownership and modification
    # evidence must remain stable while the SDK is stopped.
    return {k: v for k, v in left.items() if k != 'atimeNs'} == {k: v for k, v in right.items() if k != 'atimeNs'}


def inspect_state(dataRoot, additionalPaths=()):
    """Check live state paths and storage needs without copying or hashing data."""
    request = _request(dataRoot, additionalPaths)
    bases = _discover(request)
    total = 0
    for base in bases:
        for suffix in SIDECARS:
            path = _safe(str(base) + suffix)
            if path.exists():
                if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    raise StateBackupError('SDK database sidecar must be a bounded regular file')
                total += path.stat().st_size
    return {'databases': len(bases), 'bytes': total}


def capture_state(dataRoot, backupRoot, additionalPaths=(), run=subprocess.run):
    _stopped(run)
    request = _request(dataRoot, additionalPaths)
    backup = _safe(backupRoot)
    if _within(backup, Path(request['dataRoot'])):
        raise StateBackupError('Database baseline must be outside the live SDK state')
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    intent_path, manifest_path, files = backup / 'capture-intent.json', backup / 'manifest.json', backup / 'files'
    allowed = {'capture-intent.json', 'manifest.json', 'files', '.capture-intent.json.partial', '.manifest.json.partial'}
    if any(p.name not in allowed for p in backup.iterdir()):
        raise StateBackupError('Unexpected files in the private database baseline')
    os.chmod(backup, 0o700)
    os.chown(backup, os.geteuid(), os.getegid())
    if intent_path.exists():
        if _read_json(intent_path) != request:
            raise StateBackupError('Database baseline belongs to different state paths')
    else:
        if manifest_path.exists() or files.exists():
            raise StateBackupError('Unidentified database backup artifacts')
        _write_json(intent_path, request)
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        if not verify_baseline(backup, manifest):
            raise StateBackupError('Saved database baseline cannot be verified')
        return manifest
    if files.exists():
        files = _safe(files)
        if not _within(files.resolve(), backup.resolve()) or any(p.is_symlink() for p in files.rglob('*')):
            raise StateBackupError('Unsafe partial database baseline')
        shutil.rmtree(files)
    files.mkdir(mode=0o700)
    rows, parents = [], {}
    bases = _discover(request)
    for base in bases:
        if base.parent.is_dir():
            meta = base.parent.stat()
            parents[str(base.parent)] = {'mode': stat.S_IMODE(meta.st_mode), 'uid': meta.st_uid, 'gid': meta.st_gid}
        for suffix in SIDECARS:
            path = Path(str(base) + suffix)
            row = _file_record(path)
            if row['present']:
                name = '{:06d}.bin'.format(len(rows))
                destination = files / name
                descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with path.open('rb') as source, os.fdopen(descriptor, 'wb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush(); os.fsync(output.fileno())
                if not _records_equal(row, _file_record(path)) or _digest(destination) != row['sha256']:
                    raise StateBackupError('A stopped SDK database changed during capture')
                row['backup'] = 'files/' + name
            rows.append(row)
    _sync(files)
    manifest = {**request, 'databases': [str(p) for p in bases], 'files': rows, 'parents': parents}
    _write_json(manifest_path, manifest)
    _sync(backup.parent)
    if not verify_baseline(backup, manifest):
        raise StateBackupError('New database baseline verification failed')
    return manifest


def _validate(backup, manifest):
    if not isinstance(manifest, dict):
        raise StateBackupError('Invalid database manifest')
    request = _request(manifest.get('dataRoot', ''), manifest.get('additionalPaths', ()))
    if _read_json(backup / 'capture-intent.json') != request:
        raise StateBackupError('Database manifest and capture intent differ')
    if _read_json(backup / 'manifest.json') != manifest:
        raise StateBackupError('Supplied database manifest differs from the saved baseline')
    bases = manifest.get('databases')
    if not isinstance(bases, list) or len(bases) > 512 or len(set(bases)) != len(bases):
        raise StateBackupError('Invalid database path inventory')
    root, extras = Path(request['dataRoot']), tuple(Path(p) for p in request['additionalPaths'])
    # Do not inspect the current database format here: failed startup may have
    # replaced it with a different schema or left it absent.
    for value in bases:
        path = _safe(value)
        if _protected(path, root) or (not _within(path, root) and path not in extras):
            raise StateBackupError('Database path is outside the saved scope')
    expected = {str(base) + suffix for base in bases for suffix in SIDECARS}
    rows = manifest.get('files')
    if not isinstance(rows, list) or {row.get('path') for row in rows if isinstance(row, dict)} != expected or len(rows) != len(expected):
        raise StateBackupError('Database sidecar inventory is incomplete')
    backup_names = set()
    for row in rows:
        _safe(row['path'])
        if type(row.get('present')) is not bool:
            raise StateBackupError('Invalid database presence record')
        if row['present']:
            name = row.get('backup', '')
            if not re.fullmatch(r'files/[0-9]{6}\.bin', name) or name in backup_names:
                raise StateBackupError('Invalid database backup member')
            backup_names.add(name)
            path = _safe(backup / name)
            if (not path.is_file() or path.stat().st_size != row.get('bytes')
                    or _digest(path) != row.get('sha256')):
                raise StateBackupError('Saved database bytes changed')
            for field in ('mode', 'uid', 'gid', 'mtimeNs', 'atimeNs'):
                if type(row.get(field)) is not int or row[field] < 0:
                    raise StateBackupError('Invalid database ownership or permission metadata')
            if row['mode'] > 0o777:
                raise StateBackupError('Special database permissions are unsupported')
    if {p.relative_to(backup).as_posix() for p in _safe(backup / 'files').iterdir()} != backup_names:
        raise StateBackupError('Private database backup file list changed')
    return request


def verify_baseline(backupRoot, manifest):
    try:
        _validate(_safe(backupRoot), manifest)
        return True
    except (OSError, ValueError, TypeError, KeyError, StateBackupError):
        return False


def restore_state(backupRoot, manifest, *, run=subprocess.run):
    _stopped(run)
    backup = _safe(backupRoot)
    _validate(backup, manifest)
    rows = manifest['files']
    # Remove journal files first. Never let an interrupted new-schema WAL be
    # replayed over the restored original main database.
    for row in sorted(rows, key=lambda r: 0 if r['path'].endswith(SIDECARS[1:]) else 1):
        path = _safe(row['path'])
        if path.exists() and not path.is_file():
            raise StateBackupError('Database restoration target is not a regular file')
        if not row['present'] or path.name.endswith(SIDECARS[1:]):
            if path.exists():
                path.unlink(); _sync(path.parent)
    # The SDK remains stopped until the caller finishes the whole restoration.
    # Every replacement comes from the fixed baseline, so replay is idempotent.
    for row in rows:
        if not row['present']:
            continue
        path = _safe(row['path'])
        missing_parent = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if missing_parent:
            info = manifest['parents'][str(path.parent)]
            os.chown(path.parent, info['uid'], info['gid']); os.chmod(path.parent, info['mode'])
        temporary = _safe(path.with_name('.' + path.name + '.mdu-restore'))
        if temporary.exists() and not temporary.is_file():
            raise StateBackupError('Unexpected database restoration workspace')
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with (backup / row['backup']).open('rb') as source, os.fdopen(descriptor, 'wb') as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
            output.flush(); os.fsync(output.fileno())
        os.chown(temporary, row['uid'], row['gid']); os.chmod(temporary, row['mode'])
        os.utime(temporary, ns=(row['atimeNs'], row['mtimeNs']))
        descriptor = os.open(str(temporary), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _sync(path.parent)
    if any(row['present'] and _digest(row['path']) != row['sha256'] for row in rows):
        raise StateBackupError('Restored SDK database bytes differ from the fixed baseline')
