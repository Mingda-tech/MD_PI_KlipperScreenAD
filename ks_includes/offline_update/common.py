"""Small, dependency-free primitives shared by the privileged installer."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from functools import lru_cache


DEFAULT_LOCK = '/run/lock/klipperscreen-offline-update.lock'


class UpdateError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise UpdateError('INVALID_ID')
    return value


def regular(path):
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise UpdateError('NOT_REGULAR_FILE', str(path))
    return path


def no_links(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if '..' in path.parts or '..' in root.parts:
        raise UpdateError('PATH_OUTSIDE_ROOT')
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise UpdateError('PATH_OUTSIDE_ROOT') from exc
    current = Path(path.anchor)
    for part in (None,) + root.parts[1:] + parts:
        if part is not None:
            current /= part
        if current.is_symlink():
            raise UpdateError('SYMLINK_PATH', str(current))
    return path


@lru_cache(maxsize=1)
def root_group_is_privileged():
    """Group zero is safe to trust only when it contains root-equivalent users."""
    import grp
    import pwd
    try:
        group = grp.getgrgid(0)
        return (all(user.pw_uid == 0 for user in pwd.getpwall() if user.pw_gid == 0)
                and all(pwd.getpwnam(name).pw_uid == 0 for name in group.gr_mem))
    except (KeyError, OSError):
        return False


def trusted_path(path):
    """Reject writable files and unprivileged directory writers, including ancestors."""
    path = Path(path).absolute()
    no_links(path, path.anchor)
    if os.name == 'posix':
        for item in (path,) + tuple(path.parents):
            info = item.lstat()
            if info.st_uid != 0 or info.st_mode & 0o002:
                raise UpdateError('UNTRUSTED_INSTALLATION')
            if info.st_mode & 0o020:
                # Some vendor images ship /etc as root:root 0775. Root-only
                # directory writers do not introduce an unprivileged writer.
                if not stat.S_ISDIR(info.st_mode) or info.st_gid != 0 or not root_group_is_privileged():
                    raise UpdateError('UNTRUSTED_INSTALLATION')
    return path


def digest(path):
    value = hashlib.sha256()
    with regular(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def sync_dir(directory):
    if os.name == 'posix':
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise UpdateError('SYMLINK_PATH', str(path))
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value, mode=0o600):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'), mode)


def read_json(path):
    try:
        with regular(path).open(encoding='utf-8') as source:
            return json.load(source)
    except (OSError, ValueError) as exc:
        raise UpdateError('INVALID_STATE', str(path)) from exc


def command(argv, timeout=60):
    result = subprocess.run([str(x) for x in argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                            env={'PATH': os.environ.get('PATH', '') if os.name == 'nt'
                                 else '/usr/sbin:/usr/bin:/sbin:/bin',
                                 **({'SYSTEMROOT': os.environ['SYSTEMROOT']} if 'SYSTEMROOT' in os.environ else {}),
                                 'HOME': '/root', 'LANG': 'C.UTF-8',
                                 'PYTHONNOUSERSITE': '1'})
    if result.returncode:
        # Do not put arbitrary subprocess output (possibly config/credentials) in public status.
        raise UpdateError('COMMAND_FAILED', '%s exited %d' % (Path(str(argv[0])).name, result.returncode))
    return result.stdout.decode('utf-8', errors='replace').strip()


def worker_running(job_id):
    """False means confirmed inactive, including a collected transient unit."""
    try:
        result = subprocess.run(
            ['/usr/bin/systemctl', 'show', 'klipperscreen-update-' + identifier(job_id) + '.service',
             '--property=ActiveState', '--value'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=False)
        state = result.stdout.decode('utf-8', errors='replace').strip()
        if state in ('inactive', 'failed'):
            return False
        if result.returncode == 0 and state in ('active', 'activating', 'reloading', 'deactivating'):
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


@contextlib.contextmanager
def file_lock(path):
    """All participants use this inode; never remove it, including on failure."""
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags, 0o660)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (os.geteuid() == 0 and info.st_uid != 0)):
            raise UpdateError('INVALID_LOCK')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError('UPDATE_BUSY') from exc
        yield
    finally:
        os.close(fd)
