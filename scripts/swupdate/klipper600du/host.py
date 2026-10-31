"""Package-owned Klipper Git staging; never starts services or talks to an MCU.

The controller must stop Klipper before switch(), and must only request rollback
after proving that no firmware write could have begun. All transfer is offline.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid


SHA = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
JOB = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
MARKER = '600du-host-release.json'
SOURCE_REMOTES = frozenset(('https://github.com/mingda-tech/md_pi_klipper',
                            'https://github.com/mingda-tech/klipperad'))


class HostUpdateError(RuntimeError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def relative(name):
    if (not isinstance(name, str) or not name or '\\' in name or '\x00' in name
            or name.startswith('/') or any(p in ('', '.', '..', '.git') for p in name.split('/'))):
        raise HostUpdateError('Invalid managed host path')
    return name


def no_links(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise HostUpdateError('Linked host update workspace')
    return path


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.partial')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(value, output, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    sync_dir(path.parent)


def remote_identity(value):
    # The package supports the supplied HTTPS origin, never an executable Git URL.
    value = value.rstrip('/')
    return (value[:-4] if value.endswith('.git') else value).lower()


def semantic_version(value):
    # Git describe suffixes do not order releases, but a clear three-part
    # version can still prove a newer major/minor/patch without that history.
    match = re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
                         r'(?:[-+][0-9A-Za-z.-]+)?', value)
    return tuple(int(part) for part in match.groups()) if match else None


class HostUpdate:
    def __init__(self, spec, materials, job, progress=None, run=subprocess.run, require_root=True):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise HostUpdateError('Host installation requires Linux root execution')
        self.spec = spec
        self.run = run
        self.progress = progress or (lambda value, message: None)
        self.materials = no_links(materials)
        self.job = no_links(job)
        self._validate(require_root)
        self.target = no_links(spec['targetPath'])
        self.bundle = no_links(self.materials / 'klipper.bundle')
        stem = '.' + self.target.name + '.mdu-' + self.job.name
        self.stage = self.target.with_name(stem + '.stage')
        self.backup = self.target.with_name(stem + '.backup')
        self.failed = self.target.with_name(stem + '.failed')
        self.checkpoint = self.job / 'host-state.json'
        self.names = set(spec['targetFiles'])
        self.parents = {str(parent) for name in self.names for parent in Path(name).parents if str(parent) != '.'}
        self.state = self._load()

    def _validate(self, strict):
        s = self.spec
        if (not isinstance(s, dict) or not isinstance(s.get('targetPath'), str)
                or not Path(s['targetPath']).is_absolute() or Path(s['targetPath']).name != 'klipper'
                or not isinstance(s.get('pythonPath'), str) or not Path(s['pythonPath']).is_absolute()
                or any(c.isspace() or c in '\"\x00' for c in s['targetPath'] + s['pythonPath'])
                or not COMMIT.fullmatch(str(s.get('targetCommit', '')))
                or not SHA.fullmatch(str(s.get('bundleSha256', '')))
                or not re.fullmatch(r'refs/heads/[A-Za-z0-9_.-]+', str(s.get('bundleRef', '')))
                or not isinstance(s.get('targetVersion'), str) or not s['targetVersion']
                or type(s.get('targetBytes')) is not int or s['targetBytes'] < 1
                or not isinstance(s.get('expectedRemote'), str)
                or remote_identity(s['expectedRemote']) != 'https://github.com/mingda-tech/md_pi_klipper'):
            raise HostUpdateError('Invalid host release declaration')
        if strict and (s['targetPath'] != '/home/mingda/klipper'
                       or s['pythonPath'] != '/home/mingda/klippy-env/bin/python'):
            raise HostUpdateError('Host installation paths do not match this package')
        remotes = s.get('allowedSourceRemotes', [s['expectedRemote']])
        if (not isinstance(remotes, list) or not remotes or len(remotes) > len(SOURCE_REMOTES)
                or not all(isinstance(value, str) and remote_identity(value) in SOURCE_REMOTES for value in remotes)
                or remote_identity(s['expectedRemote']) not in {remote_identity(value) for value in remotes}
                or type(s.get('migrateRemote', False)) is not bool):
            raise HostUpdateError('Invalid source remote migration declaration')
        files, ancestors, dependencies = s.get('targetFiles'), s.get('allowedAncestors'), s.get('dependencies', {})
        if not isinstance(files, dict) or not files or len(files) > 50000:
            raise HostUpdateError('Missing exact host release inventory')
        for name, checksum in files.items():
            relative(name)
            if not SHA.fullmatch(str(checksum)) or self._generated(name):
                raise HostUpdateError('Invalid host file digest or generated release file')
        if (not isinstance(ancestors, list) or not ancestors or len(ancestors) > 50000
                or not all(COMMIT.fullmatch(str(value)) for value in ancestors)
                or s['targetCommit'] not in ancestors):
            raise HostUpdateError('Missing verified host ancestry')
        if (not isinstance(dependencies, dict) or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', str(name))
                or not isinstance(version, str) or not version for name, version in dependencies.items())):
            raise HostUpdateError('Invalid dependency declaration')
        if (not self.materials.is_dir() or not self.job.is_dir() or not JOB.fullmatch(self.job.name)):
            raise HostUpdateError('Invalid durable host workspace')

    def _load(self):
        if not self.checkpoint.exists():
            return None
        value = json.loads(no_links(self.checkpoint).read_text())
        expected = {'schemaVersion': 1, 'jobId': self.job.name, 'targetPath': str(self.target),
                    'stagePath': str(self.stage), 'backupPath': str(self.backup),
                    'targetCommit': self.spec['targetCommit'], 'bundleSha256': self.spec['bundleSha256']}
        if (not isinstance(value, dict) or any(value.get(k) != v for k, v in expected.items())
                or value.get('phase') not in ('PREPARING', 'STAGED', 'SWITCHING', 'SWITCHED',
                                             'ROLLBACK_INTENT', 'ROLLED_BACK')
                or not COMMIT.fullmatch(str(value.get('originalHead', '')))
                or not isinstance(value.get('originalRemote', self.spec['expectedRemote']), str)
                or remote_identity(value.get('originalRemote', self.spec['expectedRemote'])) not in {
                    remote_identity(value) for value in self.spec.get('allowedSourceRemotes', [self.spec['expectedRemote']])}):
            raise HostUpdateError('Host recovery materials do not match this task')
        return value

    def _save(self, phase, **fields):
        self.state.update(fields, phase=phase)
        atomic_json(self.checkpoint, self.state)

    def _execute(self, args, timeout=120, check=True, env=None):
        value = self.run([str(a) for a in args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='surrogateescape',
                         timeout=timeout, check=False, env=env)
        if check and value.returncode:
            raise HostUpdateError('Host command failed: ' + str(args[0]) + ': ' + value.stderr[:400])
        return value

    def _git(self, directory, *args, check=True):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
        path = Path(directory)
        return self._execute(['git', '--no-optional-locks', '-c', 'safe.directory=' + str(path),
                              '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                              '-c', 'protocol.allow=never', '-c', 'protocol.file.allow=always',
                              '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', '-c', 'core.autocrlf=false',
                              '-c', 'core.eol=lf', '--git-dir=' + str(path / '.git'),
                              '--work-tree=' + str(path), *args], env=env, check=check)

    def _head(self, directory):
        return self._git(directory, 'rev-parse', 'HEAD').stdout.strip()

    def _repository(self, directory):
        directory = no_links(directory)
        git = directory / '.git'
        if (not git.is_dir() or git.is_symlink() or (git / 'objects/info/alternates').exists()
                or (git / 'commondir').exists() or (git / 'worktrees').exists()
                or any(item.is_symlink() for item in git.rglob('*')) or any(git.glob('**/*.lock'))):
            raise HostUpdateError('Linked, shared or busy Git metadata is not supported')
        flags = self._git(directory, 'ls-files', '-v', '-z').stdout.split('\0')
        if any(row and (row[0].islower() or row[0] == 'S') for row in flags):
            raise HostUpdateError('Hidden local source flags are not supported')
        for expression in ('^filter\\.', '^core\\.worktree$'):
            if self._git(directory, 'config', '--local', '--get-regexp', expression, check=False).stdout.strip():
                raise HostUpdateError('External Git checkout configuration is not supported')
        remote = self._git(directory, 'remote', 'get-url', 'origin').stdout.strip()
        allowed = self.spec.get('allowedSourceRemotes', [self.spec['expectedRemote']])
        if remote_identity(remote) not in {remote_identity(value) for value in allowed}:
            raise HostUpdateError('Installed Klipper origin differs from the release')
        return remote

    def _others(self, directory, ignored=False):
        args = ['ls-files', '--others', '--exclude-standard', '-z']
        if ignored:
            args.append('--ignored')
        return [relative(value) for value in self._git(directory, *args).stdout.split('\0') if value]

    def _overlap(self, name):
        return name in self.names or name in self.parents or any(str(parent) in self.names for parent in Path(name).parents)

    @staticmethod
    def _generated(name):
        return name == 'klippy/chelper/c_helper.so' or name.endswith('.pyc') or '__pycache__' in Path(name).parts

    def _original(self, directory):
        remote = self._repository(directory)
        head = self._head(directory)
        if self.state and head != self.state['originalHead']:
            raise HostUpdateError('Original host revision changed during the task')
        if self.state and remote_identity(remote) != remote_identity(self.state.get('originalRemote', self.spec['expectedRemote'])):
            raise HostUpdateError('Original host origin changed during the task')
        # The published ancestry proves known older/equal releases. An unlisted
        # field commit may be a fork or lack enough history to order versions;
        # preserve its real HEAD for recovery instead of rejecting it as unknown.
        target = self.spec['targetCommit']
        if head != target and head not in self.spec['allowedAncestors']:
            current_version = self._git(directory, 'describe', '--tags', '--always', check=False)
            current_base = semantic_version(current_version.stdout.strip()) if current_version.returncode == 0 else None
            target_base = semantic_version(self.spec['targetVersion'])
            if current_base is not None and target_base is not None and current_base > target_base:
                raise HostUpdateError('Installed host is newer than the target release; downgrade is not allowed')
            present = self._git(directory, 'rev-parse', '--verify', '--quiet',
                                target + '^{commit}', check=False)
            if present.returncode == 0:
                relation = self._git(directory, 'merge-base', '--is-ancestor', target, head, check=False)
                if relation.returncode == 0:
                    raise HostUpdateError('Installed host is newer than the target release; downgrade is not allowed')
        if self._git(directory, 'status', '--porcelain=v1', '--untracked-files=no').stdout.strip():
            raise HostUpdateError('Tracked host source has local modifications')
        for name in set(self._others(directory) + self._others(directory, ignored=True)):
            if self._overlap(name):
                raise HostUpdateError('A local user file conflicts with the host release: ' + name)
        return {'head': head, 'remote': remote}

    def _material(self):
        if not self.bundle.is_file() or digest(self.bundle) != self.spec['bundleSha256']:
            raise HostUpdateError('Host Git bundle changed')
        expected = self.spec['targetCommit'] + ' ' + self.spec['bundleRef']
        if expected not in self._execute(['git', 'bundle', 'list-heads', self.bundle]).stdout.splitlines():
            raise HostUpdateError('Host bundle lacks the declared release')

    def _dependencies(self):
        python = Path(self.spec['pythonPath'])
        if not python.is_file() or not os.access(python, os.X_OK):
            raise HostUpdateError('Existing Klipper Python interpreter is unavailable')
        declared = self.spec.get('dependencies', {})
        names = list(declared)
        if names and 'greenlet' not in names:
            names.append('greenlet')
        code = ('import importlib,importlib.metadata as m,json,sys; '
                'names=json.loads(sys.argv[1]); '
                'modules={"cffi":"cffi","pyserial":"serial","greenlet":"greenlet",'
                '"Jinja2":"jinja2","python-can":"can","MarkupSafe":"markupsafe"}; '
                '[importlib.import_module(modules[n]) for n in names if n in modules]; '
                'print(json.dumps({"packages":{n:m.version(n) for n in names},"python":list(sys.version_info[:2])}))')
        result = self._execute([python, '-I', '-B', '-c', code, json.dumps(names)], timeout=30)
        actual = json.loads(result.stdout)
        version = actual.get('python', [])
        if len(version) != 2 or version[0] != 3 or version[1] < 8:
            raise HostUpdateError('Existing Klipper Python version is unsupported')
        expected = dict(declared)
        if names:
            expected['greenlet'] = '2.0.2' if version[1] < 12 else '3.0.3'
        if any(actual.get('packages', {}).get(name) != wanted for name, wanted in expected.items()):
            raise HostUpdateError('Existing Klipper dependencies differ from the release')
        self._execute(['gcc', '--version'], timeout=10)

    def _new(self, directory):
        path = Path(directory) / '.git' / MARKER
        if not path.is_file() or path.is_symlink():
            return False
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        return value.get('jobId') == self.job.name and value.get('commit') == self.spec['targetCommit']

    def _artifact(self, path):
        no_links(path)
        if path not in (self.stage, self.backup, self.failed) or path.parent != self.target.parent:
            raise HostUpdateError('Invalid host task artifact')
        return path

    def check(self):
        self._material()
        if not self.target.is_dir():
            raise HostUpdateError('Installed Klipper directory is missing')
        if self.state and self._new(self.target):
            self.verify()
            return {'head': self.state['originalHead'],
                    'remote': self.state.get('originalRemote', self.spec['expectedRemote'])}
        baseline = self._original(self.target)
        self._git(self.target, 'bundle', 'verify', self.bundle)
        self._dependencies()
        required = self.spec['targetBytes'] + self.bundle.stat().st_size + 64 * 1024**2
        for parent, dirs, files in os.walk(self.target, followlinks=False):
            dirs[:] = [n for n in dirs if not (Path(parent) / n).is_symlink()]
            required += sum((Path(parent) / n).stat().st_size for n in files if not (Path(parent) / n).is_symlink())
        if shutil.disk_usage(self.target.parent).free < required:
            raise HostUpdateError('Insufficient staging and backup space')
        for path in (self.stage, self.backup, self.failed):
            self._artifact(path)
            if path.exists() and self.state is None:
                raise HostUpdateError('A foreign host task artifact already exists')
        return baseline

    def _preserve_users(self, source, destination):
        files = sorted(set(self._others(source) + self._others(source, ignored=True)))
        for name in files:
            if self._generated(name) or self._overlap(name):
                continue
            origin, copied = Path(source) / name, Path(destination) / name
            parent = Path()
            for part in Path(name).parts[:-1]:
                parent /= part
                before, after = Path(source) / parent, Path(destination) / parent
                if before.is_symlink() or not before.is_dir() or after.is_symlink() or after.is_file():
                    raise HostUpdateError('User directory changed while staging')
                after.mkdir(exist_ok=True)
                shutil.copystat(before, after)
                info = before.stat(); os.chown(after, info.st_uid, info.st_gid)
            if copied.is_dir() and not copied.is_symlink():
                raise HostUpdateError('User file type changed while staging')
            if copied.exists() or copied.is_symlink():
                copied.unlink()
            if origin.is_symlink():
                os.symlink(os.readlink(origin), copied)
            else:
                shutil.copy2(origin, copied, follow_symlinks=False)
                if digest(origin) != digest(copied):
                    raise HostUpdateError('User file changed while staging')
            info = origin.lstat(); os.chown(copied, info.st_uid, info.st_gid, follow_symlinks=False)
        for parent, dirs, _ in os.walk(source, followlinks=False):
            origin = Path(parent)
            dirs[:] = [n for n in dirs if n != '.git' and not (origin / n).is_symlink()]
            name = origin.relative_to(source)
            copied = Path(destination) / name
            if not copied.exists() and not any(origin.iterdir()) and not self._overlap(str(name)):
                copied.mkdir(parents=True)
            if copied.is_dir() and not copied.is_symlink():
                shutil.copystat(origin, copied)
                info = origin.stat(); os.chown(copied, info.st_uid, info.st_gid)

    def _sync_tree(self, directory):
        folders = []
        for parent, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = [n for n in dirs if not (Path(parent) / n).is_symlink()]
            folders.append(Path(parent))
            for name in files:
                path = Path(parent) / name
                if path.is_file() and not path.is_symlink():
                    with path.open('rb') as stored: os.fsync(stored.fileno())
        for path in reversed(folders): sync_dir(path)

    def _finish_exchange(self):
        if not self._new(self.target):
            raise HostUpdateError('Target is not this task release')
        if self.stage.exists():
            if self.backup.exists() or self._new(self.stage):
                raise HostUpdateError('Ambiguous original host backup')
            self._original(self.stage)
            os.replace(self.stage, self.backup)
            sync_dir(self.target.parent)
        if not self.backup.is_dir() or self._new(self.backup):
            raise HostUpdateError('Original host backup is unavailable')
        self._original(self.backup)
        self._save('SWITCHED')

    def _probe_exchange(self):
        # Test the actual target filesystem before the controller can stop
        # Klipper or enter any bootloader. Never reuse or glob-delete a probe.
        probe = Path(tempfile.mkdtemp(prefix='.' + self.target.name + '.mdu-' + self.job.name
                                      + '.exchange-probe-', dir=self.target.parent))
        no_links(probe)
        owner = {'jobId': self.job.name, 'targetPath': str(self.target),
                 'bundleSha256': self.spec['bundleSha256'], 'nonce': uuid.uuid4().hex}
        ownership = probe / 'owner.json'
        left, right = probe / 'left', probe / 'right'
        created = False
        try:
            atomic_json(ownership, owner)
            left.mkdir(mode=0o700); right.mkdir(mode=0o700)
            atomic_json(left / 'identity.json', {'side': 'left', 'nonce': owner['nonce']})
            atomic_json(right / 'identity.json', {'side': 'right', 'nonce': owner['nonce']})
            created = True
            sync_dir(probe)
            if not self._exchange(left, right):
                raise HostUpdateError('Atomic directory exchange is unavailable on the Klipper filesystem')
            sync_dir(probe)
            if (json.loads((left / 'identity.json').read_text()) != {'side': 'right', 'nonce': owner['nonce']}
                    or json.loads((right / 'identity.json').read_text()) != {'side': 'left', 'nonce': owner['nonce']}):
                raise HostUpdateError('Atomic directory exchange did not preserve the probe identities')
        finally:
            # A replaced ownership marker or unfamiliar contents are retained,
            # never recursively deleted. The private directory is only this
            # invocation's mkdtemp result, on the same filesystem as Klipper.
            trusted = (not probe.is_symlink() and probe.parent == self.target.parent
                       and probe.stat().st_uid == os.geteuid() and ownership.is_file()
                       and not ownership.is_symlink() and json.loads(ownership.read_text()) == owner)
            expected = {'owner.json', 'left', 'right'}
            if trusted and created and {p.name for p in probe.iterdir()} == expected:
                for path in (left, right):
                    trusted = trusted and path.is_dir() and not path.is_symlink()
                    if trusted:
                        trusted = {p.name for p in path.iterdir()} == {'identity.json'}
                    identity = path / 'identity.json'
                    if trusted:
                        trusted = (not identity.is_symlink() and json.loads(identity.read_text())
                                   in ({'side': 'left', 'nonce': owner['nonce']},
                                       {'side': 'right', 'nonce': owner['nonce']}))
                if trusted:
                    for path in (left, right):
                        (path / 'identity.json').unlink(); path.rmdir()
                    ownership.unlink(); probe.rmdir(); sync_dir(probe.parent)
                else:
                    raise HostUpdateError('Exchange probe ownership changed; probe retained')
            elif trusted and not created:
                # Partial initialization can only remove known empty folders
                # and our completed marker; unfamiliar partial files remain.
                for path in (left, right):
                    if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                        path.rmdir()
                if {p.name for p in probe.iterdir()} == {'owner.json'}:
                    ownership.unlink(); probe.rmdir(); sync_dir(probe.parent)
            else:
                raise HostUpdateError('Exchange probe ownership changed; probe retained')

    def prepare(self):
        baseline = self.check()
        if self.state and self._new(self.target):
            self._finish_exchange()
            return baseline
        self._probe_exchange()
        if self.state is None:
            owner = self.target.stat()
            self.state = {'schemaVersion': 1, 'jobId': self.job.name, 'targetPath': str(self.target),
                          'stagePath': str(self.stage), 'backupPath': str(self.backup),
                          'targetCommit': self.spec['targetCommit'], 'bundleSha256': self.spec['bundleSha256'],
                          'originalHead': baseline['head'], 'originalRemote': baseline['remote'],
                          'uid': owner.st_uid, 'gid': owner.st_gid,
                          'mode': stat.S_IMODE(owner.st_mode)}
        self._save('PREPARING')
        if self.backup.exists():
            raise HostUpdateError('Original backup prevents a fresh staging pass')
        if self.stage.exists():
            self._artifact(self.stage)
            shutil.rmtree(self.stage)
            sync_dir(self.stage.parent)
        self.progress(10, 'Staging the Klipper host release')
        shutil.copytree(self.target, self.stage, symlinks=True)
        index = self.stage / '.git/index'
        if index.exists(): index.unlink()
        self._git(self.stage, 'fetch', '--no-tags', self.bundle, self.spec['bundleRef'])
        self._git(self.stage, 'reset', '--hard', self.spec['targetCommit'])
        if self.spec.get('migrateRemote', False):
            self._git(self.stage, 'remote', 'set-url', 'origin', self.spec['expectedRemote'])
        for row in self._git(self.stage, 'ls-tree', '-r', '-z', self.spec['targetCommit']).stdout.split('\0'):
            if not row: continue
            header, name = row.split('\t', 1)
            mode = header.split()[0]
            if mode not in ('100644', '100755'):
                raise HostUpdateError('Host release contains links or submodules')
            os.chmod(no_links(self.stage / relative(name)), 0o755 if mode == '100755' else 0o644)
        for parent, dirs, files in os.walk(self.stage, followlinks=False):
            dirs[:] = [n for n in dirs if n != '.git' and not (Path(parent) / n).is_symlink()]
            for name in files:
                path = Path(parent) / name
                if (path.name == 'c_helper.so' and path.parent == self.stage / 'klippy/chelper') or path.suffix == '.pyc':
                    path.unlink()
        for parent, dirs, files in os.walk(self.stage, followlinks=False):
            os.chown(parent, self.state['uid'], self.state['gid'])
            dirs[:] = [n for n in dirs if not (Path(parent) / n).is_symlink()]
            for name in dirs + files:
                os.chown(Path(parent) / name, self.state['uid'], self.state['gid'], follow_symlinks=False)
        self._preserve_users(self.target, self.stage)
        self._verify_tree(self.stage)
        code = 'import sys; sys.path.insert(0,sys.argv[1]); import chelper; chelper.get_ffi()'
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        self._execute([self.spec['pythonPath'], '-I', '-B', '-c', code, str(self.stage / 'klippy')], timeout=180, env=env)
        library = no_links(self.stage / 'klippy/chelper/c_helper.so')
        if not library.is_file() or library.stat().st_size < 1:
            raise HostUpdateError('Staged host helper library was not built')
        os.chown(library, self.state['uid'], self.state['gid'])
        marker = self.stage / '.git' / MARKER
        atomic_json(marker, {'schemaVersion': 1, 'jobId': self.job.name, 'commit': self.spec['targetCommit'],
                             'version': self.spec['targetVersion']})
        os.chown(marker, self.state['uid'], self.state['gid'])
        os.chmod(self.stage, self.state['mode'])
        self._verify_tree(self.stage)
        self._sync_tree(self.stage)
        self._save('STAGED', helperSha256=digest(library))
        return baseline

    def _verify_tree(self, directory):
        remote = self._repository(directory)
        if self.spec.get('migrateRemote', False) and remote_identity(remote) != remote_identity(self.spec['expectedRemote']):
            raise HostUpdateError('Migrated host origin differs from the target release')
        if self._head(directory) != self.spec['targetCommit']:
            raise HostUpdateError('Host release commit differs')
        listed = set()
        for row in self._git(directory, 'ls-tree', '-r', '-z', self.spec['targetCommit']).stdout.split('\0'):
            if not row: continue
            header, name = row.split('\t', 1); relative(name)
            mode = header.split()[0]
            if mode not in ('100644', '100755'):
                raise HostUpdateError('Host release contains links or submodules')
            path = no_links(Path(directory) / name)
            if stat.S_IMODE(path.stat().st_mode) != (0o755 if mode == '100755' else 0o644):
                raise HostUpdateError('Host executable mode differs from the release')
            listed.add(name)
        if listed != self.names:
            raise HostUpdateError('Host release inventory differs from its Git tree')
        for name, checksum in self.spec['targetFiles'].items():
            path = no_links(Path(directory) / name)
            if digest(path) != checksum:
                raise HostUpdateError('Host release file differs: ' + name)
            # The repository also ships legacy Python 2 analysis utilities;
            # compile the actual Klippy runtime, not unrelated host tools.
            if name.startswith('klippy/') and name.endswith('.py'):
                compile(path.read_bytes(), name, 'exec')
        if self._git(directory, 'status', '--porcelain=v1', '--untracked-files=no').stdout.strip():
            raise HostUpdateError('Installed host tracked files are not clean')

    @staticmethod
    def _exchange(left, right):
        if os.name != 'posix': return False
        library = ctypes.CDLL(None, use_errno=True)
        function = getattr(library, 'renameat2', None)
        if function is None: return False
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(-100, os.fsencode(left), -100, os.fsencode(right), 2): return False
        return True

    def switch(self):
        if not self.state:
            raise HostUpdateError('Host was not staged')
        if self._new(self.target):
            self._finish_exchange()
            self.verify()
            return
        self._original(self.target)
        if self.state['phase'] != 'STAGED' or self.backup.exists() or not self._new(self.stage):
            raise HostUpdateError('Host staging state cannot be confirmed')
        self._preserve_users(self.target, self.stage)
        self._verify_tree(self.stage)
        if digest(self.stage / 'klippy/chelper/c_helper.so') != self.state['helperSha256']:
            raise HostUpdateError('Staged helper library changed')
        self._sync_tree(self.stage)
        self._save('SWITCHING')
        if not self._exchange(self.target, self.stage):
            raise HostUpdateError('Atomic directory exchange is required by this package')
        sync_dir(self.target.parent)
        self._finish_exchange()
        self.verify()

    def verify(self):
        if not self.state or not self._new(self.target):
            raise HostUpdateError('This task has not installed the host release')
        self._verify_tree(self.target)
        library = no_links(self.target / 'klippy/chelper/c_helper.so')
        if digest(library) != self.state.get('helperSha256'):
            raise HostUpdateError('Installed helper library differs from the staged build')
        return {'commit': self.spec['targetCommit'], 'helperSha256': self.state['helperSha256']}

    def rollback_before_firmware(self, *, firmware_write_started=None):
        if firmware_write_started is not False:
            raise HostUpdateError('Rollback requires proof that firmware writes never began')
        if not self.state:
            return
        if self._new(self.target):
            self._finish_exchange()
            self._save('ROLLBACK_INTENT')
            if not self._exchange(self.target, self.backup):
                raise HostUpdateError('Atomic directory exchange is required for rollback')
            sync_dir(self.target.parent)
        self._original(self.target)
        if self.backup.exists():
            if not self._new(self.backup):
                raise HostUpdateError('Unexpected directory at the failed host backup path')
            if self.failed.exists():
                self._artifact(self.failed)
                previous = self.failed.with_name(self.failed.name + '-' + str(time.time_ns()))
                os.replace(self.failed, previous)
            os.replace(self.backup, self.failed)
            sync_dir(self.target.parent)
        self._save('ROLLED_BACK')
