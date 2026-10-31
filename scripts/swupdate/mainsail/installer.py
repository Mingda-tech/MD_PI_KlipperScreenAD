#!/usr/bin/env python3
"""Signed-package Mainsail static-site replacement, with no service changes."""
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import time
import urllib.parse
import urllib.request

TARGET = '/home/mingda/mainsail'
MARKER = '.mdu-mainsail-task.json'


class UpdateFailure(RuntimeError):
    pass


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise UpdateFailure('A site, task or material path is a symbolic link')
    return path


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value, mode=0o600):
    path = no_links(path)
    partial = path.with_name('.' + path.name + '.partial')
    fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write((json.dumps(value, sort_keys=True) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(partial, mode)
    os.replace(partial, path)
    sync_dir(path.parent)


def safe_name(name):
    if (not isinstance(name, str) or not name or '\\' in name or '\x00' in name
            or name.startswith('/') or ':' in name
            or any(part in ('', '.', '..') for part in name.split('/'))
            or str(PurePosixPath(name)) != name):
        raise UpdateFailure('Unsafe static-site archive path')
    return name


def inventory(root):
    """Record content and permissions; reject links and special files."""
    root = no_links(root)
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise UpdateFailure('The existing site contains a link or special file')
        key = '.' if path == root else path.relative_to(root).as_posix()
        row = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
               'type': 'directory' if path.is_dir() else 'file'}
        if path.is_file():
            row.update(bytes=info.st_size, sha256=digest(path))
        result[key] = row
    return result


def exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = libc.renameat2
    except AttributeError as error:
        raise UpdateFailure('Atomic directory exchange is unavailable') from error
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        code = ctypes.get_errno()
        raise UpdateFailure('Atomic directory exchange failed: ' + os.strerror(code))
    sync_dir(Path(left).parent)
    sync_dir(Path(right).parent)


class Installer:
    def __init__(self, materials=None, job=None, require_root=True, http=None, api=None, swap=exchange):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise UpdateFailure('Linux root execution is required')
        self.materials = no_links(materials or os.environ['TMPDIR'])
        self.job = no_links(job or os.environ['MINGDA_JOB_DIR'])
        if not self.materials.is_dir() or not self.job.is_dir():
            raise UpdateFailure('Missing private update workspace')
        self.spec = json.loads((self.materials / 'release.json').read_text(encoding='utf-8'))
        if (self.spec.get('schemaVersion') != 1 or self.spec.get('component') != 'mainsail'
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', self.spec.get('releaseId', ''))
                or not isinstance(self.spec.get('files'), dict) or not self.spec['files']
                or not isinstance(self.spec.get('materials'), dict)):
            raise UpdateFailure('Invalid Mainsail release declaration')
        self.target = no_links(self.spec.get('targetPath', ''))
        if require_root and str(self.target) != TARGET:
            raise UpdateFailure('This package targets a different Mainsail path')
        self.token = hashlib.sha256(str(self.job.resolve()).encode()).hexdigest()
        self.container = self.target.parent / ('.mdu-mainsail-' + self.token[:24])
        self.stage = self.container / 'site'
        self.state_path = self.job / 'mainsail-state.json'
        no_links(self.state_path)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        self.identity = {'releaseId': self.spec['releaseId'], 'token': self.token,
                         'releaseSha256': digest(self.materials / 'release.json'),
                         'targetPath': str(self.target), 'backupPath': str(self.stage)}
        if self.state and any(self.state.get(k) != v for k, v in self.identity.items()):
            raise UpdateFailure('Recovery state belongs to different materials or target')
        self.http = http or self.http_bytes
        self.api = api or self.http_json
        self.swap = swap

    @staticmethod
    def http_bytes(path):
        request = urllib.request.Request('http://127.0.0.1' + path,
                                         headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            if response.status != 200:
                raise UpdateFailure('The local web server did not return HTTP 200')
            return response.read(32 * 1024 * 1024)

    @staticmethod
    def http_json(path):
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                'http://127.0.0.1:7125' + path, timeout=10) as response:
            return json.load(response)

    def idle(self):
        names = ('print_stats', 'pause_resume', 'webhooks', 'manual_probe', 'idle_timeout')
        data = self.api('/printer/objects/query?' + '&'.join(names))['result']['status']
        if (data.get('webhooks', {}).get('state') != 'ready'
                or data.get('print_stats', {}).get('state') not in ('standby', 'complete', 'cancelled', 'error')
                or data.get('pause_resume', {}).get('is_paused') is not False
                or data.get('manual_probe', {}).get('is_active') is not False
                or data.get('idle_timeout', {}).get('state') not in ('Idle', 'Ready')):
            raise UpdateFailure('Printer is printing, paused, calibrating or its idle state is unknown')

    def save(self, phase, **fields):
        self.state.update(fields, phase=phase, updatedAt=time.time())
        write_json(self.state_path, self.state)

    def progress(self, percent, message):
        write_json(self.job / 'script-progress.json', {'phase': 'INSTALLING', 'component': 'mainsail',
                   'progress': max(0, min(99, int(percent))), 'message': message}, mode=0o644)

    def owns(self, root):
        path = no_links(root) / MARKER
        if not path.is_file() or path.is_symlink():
            return False
        try:
            return json.loads(path.read_text()) == self.identity
        except (ValueError, OSError):
            return False

    def archive(self):
        files = self.spec['files']
        for name, row in files.items():
            safe_name(name)
            if name == MARKER or not isinstance(row, dict) or not re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', '')):
                raise UpdateFailure('Invalid static file declaration')
            if type(row.get('bytes')) is not int or row['bytes'] < 0:
                raise UpdateFailure('Invalid static file size')
        seen = set()
        with tarfile.open(self.materials / 'mainsail-dist.tar.gz', mode='r:gz') as archive:
            for member in archive:
                name = safe_name(member.name)
                if name in seen or not member.isfile() or name not in files or member.size != files[name]['bytes']:
                    raise UpdateFailure('Unexpected, linked or duplicate static archive member')
                seen.add(name)
                stream = archive.extractfile(member)
                value = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(block)
                if value.hexdigest() != files[name]['sha256']:
                    raise UpdateFailure('Static archive content digest mismatch')
        if seen != set(files):
            raise UpdateFailure('Static archive is incomplete')

    def check(self):
        for name, expected in self.spec['materials'].items():
            safe_name(name)
            if '/' in name or not no_links(self.materials / name).is_file() or digest(self.materials / name) != expected:
                raise UpdateFailure('Update material digest mismatch: ' + name)
        for required in ('mainsail-dist.tar.gz', 'mainsail-source.tar.gz', 'installer.py', 'COPYING.mainsail'):
            if required not in self.spec['materials']:
                raise UpdateFailure('Release is missing a required signed material')
        if not self.target.is_dir() or not (self.target / 'index.html').is_file():
            raise UpdateFailure('The configured Mainsail static site is missing')
        current = inventory(self.target)
        if 'config.json' not in current or current['config.json']['type'] != 'file':
            raise UpdateFailure('Current Mainsail config.json must be a regular file')
        for name in ('index.html', 'config.json', '.version', 'release_info.json', 'sw.js'):
            if name not in self.spec['files']:
                raise UpdateFailure('Release is missing a required static file')
        self.archive()
        required = sum(row['bytes'] for row in self.spec['files'].values()) * 2 + 1024 * 1024
        if shutil.disk_usage(self.target.parent).free < required:
            raise UpdateFailure('Insufficient space for the static-site staging directory')
        self.idle()
        # Prove nginx currently serves this exact directory, rather than merely
        # accepting an HTTP 200 SPA fallback or a different virtual host.
        if hashlib.sha256(self.http('/index.html')).hexdigest() != current['index.html']['sha256']:
            raise UpdateFailure('The local HTTP site does not match the configured target')
        return current

    def verify_site(self):
        if not self.owns(self.target):
            raise UpdateFailure('The installed site is not this task snapshot')
        actual = inventory(self.target)
        expected = self.spec['files']
        if set(actual) != set(self.state['staged']):
            raise UpdateFailure('Installed static-site tree changed')
        for name, row in self.state['staged'].items():
            if actual.get(name) != row:
                raise UpdateFailure('Installed static-site digest or permissions changed: ' + name)
        for name in self.spec['httpFiles']:
            safe_name(name)
            want = self.state['staged'][name]['sha256']
            path = '/' + urllib.parse.quote(name, safe='/')
            if hashlib.sha256(self.http(path)).hexdigest() != want:
                raise UpdateFailure('Local HTTP verification failed: ' + name)
        if (self.target / '.version').read_text().strip() != self.spec['targetVersion']:
            raise UpdateFailure('Installed Mainsail version is incorrect')
        release = json.loads((self.target / 'release_info.json').read_text())
        if release != {'project_name': 'mainsail', 'project_owner': 'Mingda-tech', 'version': self.spec['targetVersion']}:
            raise UpdateFailure('Installed Mainsail release metadata is incorrect')

    def prepare(self, original):
        self.state = dict(self.identity, phase='PREPARING', original=original, startedAt=time.time())
        write_json(self.state_path, self.state)
        no_links(self.container)
        if self.container.exists():
            raise UpdateFailure('Unexpected existing static-site staging container')
        self.container.mkdir(mode=0o700)
        sync_dir(self.container.parent)
        self.stage.mkdir(mode=0o700)
        root_meta = original['.']
        with tarfile.open(self.materials / 'mainsail-dist.tar.gz', 'r:gz') as archive:
            for member in archive:
                name = safe_name(member.name)
                path = self.stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as output, archive.extractfile(member) as source:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
        shutil.copy2(self.target / 'config.json', self.stage / 'config.json')
        write_json(self.stage / MARKER, self.identity, 0o644)
        for path in [self.stage, *sorted(self.stage.rglob('*'))]:
            key = '.' if path == self.stage else path.relative_to(self.stage).as_posix()
            info = original.get(key, root_meta)
            mode = info['mode'] if key in original else (0o755 if path.is_dir() else 0o644)
            os.chown(path, info['uid'], info['gid'])
            os.chmod(path, mode)
            if path.is_file():
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        for path in sorted((p for p in self.stage.rglob('*') if p.is_dir()), reverse=True):
            sync_dir(path)
        sync_dir(self.stage)
        # Exercise actual RENAME_EXCHANGE on empty private siblings before the
        # real site is touched; an unsupported filesystem fails at this point.
        left, right = self.container / 'probe-a', self.container / 'probe-b'
        left.mkdir(); right.mkdir()
        self.swap(left, right)
        left.rmdir(); right.rmdir()
        self.save('STAGED', staged=inventory(self.stage))

    def rollback(self):
        if self.state is None:
            return
        if self.owns(self.target):
            no_links(self.stage)
            if not self.stage.is_dir() or inventory(self.stage) != self.state['original']:
                raise UpdateFailure('Original static-site backup cannot be verified')
            self.save('ROLLBACK_INTENT')
            self.swap(self.target, self.stage)
        if inventory(self.target) != self.state['original']:
            raise UpdateFailure('Original static-site restore could not be verified')
        if hashlib.sha256(self.http('/index.html')).hexdigest() != self.state['original']['index.html']['sha256']:
            raise UpdateFailure('Restored static site failed HTTP verification')
        self.save('ROLLED_BACK')

    def install(self):
        self.check()
        if self.state and self.state.get('phase') == 'COMPLETE':
            self.verify_site()
            return
        if self.state and self.state.get('phase') == 'ROLLED_BACK':
            # A resumed task can retry the same validated release; its failed
            # candidate remains private until this explicit recovery action.
            if inventory(self.target) != self.state['original']:
                raise UpdateFailure('Original site changed after rollback')
            if self.container.exists():
                no_links(self.container)
                shutil.rmtree(self.container)
            self.prepare(self.state['original'])
        elif self.state is None:
            self.progress(10, 'Preparing Mainsail static-site files')
            self.prepare(inventory(self.target))
        elif self.state.get('phase') == 'PREPARING' and not self.owns(self.target):
            if inventory(self.target) != self.state['original']:
                raise UpdateFailure('Original site changed during interrupted staging')
            if self.container.exists():
                no_links(self.container)
                shutil.rmtree(self.container)
            self.prepare(self.state['original'])
        try:
            if not self.owns(self.target):
                if (not self.owns(self.stage) or inventory(self.target) != self.state['original']
                        or inventory(self.stage) != self.state['staged']):
                    raise UpdateFailure('A site or staged snapshot changed before directory exchange')
                self.idle()
                if inventory(self.target) != self.state['original'] or inventory(self.stage) != self.state['staged']:
                    raise UpdateFailure('Static-site files changed during the final idle check')
                self.progress(60, 'Replacing Mainsail static-site directory')
                self.save('SWAP_INTENT')
                self.swap(self.target, self.stage)
            self.save('VERIFYING')
            self.progress(85, 'Verifying Mainsail HTTP assets')
            self.verify_site()
            self.save('COMPLETE')
            self.progress(99, 'Mainsail verified; services and printer configuration preserved')
        except Exception:
            self.rollback()
            raise

    def failure(self):
        if self.state and self.state.get('phase') != 'COMPLETE':
            self.rollback()


if __name__ == '__main__':
    try:
        runner = Installer()
        action = sys.argv[1] if len(sys.argv) == 2 else ''
        if action not in ('check', 'install', 'failure'):
            raise UpdateFailure('Unknown Mainsail update action')
        getattr(runner, action)()
    except Exception as error:
        print('Mainsail update failed: ' + str(error)[:600], file=sys.stderr)
        sys.exit(1)
