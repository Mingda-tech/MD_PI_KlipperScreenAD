#!/usr/bin/env python3
"""Package-owned, offline Git update of KlipperScreen with durable file rescue.

This module never changes Klipper, Linux MCU or Moonraker services. The signed
screen-release.json describes the exact release and permitted source baselines.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time


SERVICE = 'KlipperScreen.service'
SHA = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
JOB = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
MARKER = 'klipperscreen-offline-deployment.json'


class ScreenPackageError(RuntimeError):
    pass


def checksum(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or value.startswith('/') or any(part in ('', '.', '..', '.git') for part in value.split('/'))):
        raise ScreenPackageError('Invalid managed release path')
    return value


def sync_dir(path):
    if os.name != 'posix':
        return
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.partial')
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    sync_dir(path.parent)


def ensure_no_link(path):
    path = Path(path).absolute()
    for item in (path,) + tuple(path.parents):
        if item.is_symlink():
            raise ScreenPackageError('Symbolic link in update workspace')
    return path


class ScreenInstaller:
    def __init__(self, temporary=None, job=None, run=subprocess.run, require_root=True,
                 units=Path('/run/systemd/system'), run_root=Path('/run')):
        if require_root and (os.name != 'posix' or os.geteuid() != 0):
            raise ScreenPackageError('Installer requires Linux root execution')
        self.tmp = ensure_no_link(temporary or os.environ['TMPDIR'])
        self.job = ensure_no_link(job or os.environ['MINGDA_JOB_DIR'])
        if not self.tmp.is_dir() or not self.job.is_dir() or not JOB.fullmatch(self.job.name):
            raise ScreenPackageError('Invalid persistent task workspace')
        self.run = run
        self.spec = json.loads((self.tmp / 'screen-release.json').read_text(encoding='utf-8'))
        self._validate_spec()
        self.target = ensure_no_link(Path(self.spec['targetPath']))
        self.stage = self.target.with_name('.' + self.target.name + '.mdu-' + self.job.name + '.stage')
        self.backup = self.target.with_name('.' + self.target.name + '.mdu-' + self.job.name + '.backup')
        self.failed = self.target.with_name('.' + self.target.name + '.mdu-' + self.job.name + '.failed')
        self.checkpoint = self.job / 'klipperscreen-state.json'
        self.bundle = ensure_no_link(self.tmp / 'screen.bundle')
        self.units = Path(units)
        self.runtime = Path(run_root) / ('klipperscreen-mdu-' + self.job.name)
        self.override = self.units / (SERVICE + '.d/90-klipperscreen-mdu.conf')
        self.state = self._load_state()

    def _validate_spec(self):
        value = self.spec
        if (not isinstance(value, dict) or value.get('schemaVersion') != 1 or value.get('service') != SERVICE
                or not isinstance(value.get('targetPath'), str) or not Path(value['targetPath']).is_absolute()
                or Path(value['targetPath']).name != 'KlipperScreen'
                or any(character.isspace() or character in '\"\x00' for character in value['targetPath'])
                or not COMMIT.fullmatch(str(value.get('targetCommit', '')))
                or not SHA.fullmatch(str(value.get('bundleSha256', '')))
                or not isinstance(value.get('targetVersion'), str) or not value['targetVersion']
                or len(value['targetVersion']) > 127 or type(value.get('targetBytes')) is not int
                or value['targetBytes'] < 1 or not re.fullmatch(r'refs/heads/[A-Za-z0-9_.-]+', str(value.get('bundleRef', '')))):
            raise ScreenPackageError('Invalid screen release declaration')
        files = value.get('targetFiles')
        if not isinstance(files, dict) or not files or len(files) > 20000:
            raise ScreenPackageError('Missing exact release file inventory')
        for name, digest in files.items():
            relative(name)
            if not SHA.fullmatch(str(digest)):
                raise ScreenPackageError('Invalid release file digest')
        baselines = value.get('allowedBaselines')
        if not isinstance(baselines, list) or not baselines:
            raise ScreenPackageError('Missing verified screen baseline')
        for baseline in baselines:
            if (not isinstance(baseline, dict) or not COMMIT.fullmatch(str(baseline.get('head', '')))
                    or not isinstance(baseline.get('files'), dict)):
                raise ScreenPackageError('Invalid screen baseline')
            for name, digest in baseline['files'].items():
                relative(name)
                if digest is not None and not SHA.fullmatch(str(digest)):
                    raise ScreenPackageError('Invalid baseline file digest')

    def _load_state(self):
        if not self.checkpoint.exists():
            return None
        value = json.loads(ensure_no_link(self.checkpoint).read_text(encoding='utf-8'))
        expected = {'schemaVersion': 1, 'jobId': self.job.name, 'targetPath': str(self.target),
                    'stagePath': str(self.stage), 'backupPath': str(self.backup),
                    'targetCommit': self.spec['targetCommit'], 'bundleSha256': self.spec['bundleSha256']}
        if not isinstance(value, dict) or any(value.get(key) != field for key, field in expected.items()):
            raise ScreenPackageError('Recovery materials do not match this task')
        return value

    def _save(self, phase, **fields):
        if self.state is None:
            raise ScreenPackageError('No original installation checkpoint')
        self.state.update(fields, phase=phase)
        atomic_json(self.checkpoint, self.state)

    def _execute(self, args, timeout=120, env=None, check=True):
        result = self.run([str(item) for item in args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='surrogateescape',
                          timeout=timeout, check=False, env=env)
        if check and result.returncode:
            raise ScreenPackageError('Command failed: ' + str(args[0]) + ': ' + result.stderr[:300])
        return result

    def _git(self, directory, *args, check=True):
        directory = Path(directory)
        environment = dict(os.environ)
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
        command = ['git', '--no-optional-locks', '-c', 'safe.directory=' + str(directory),
                   '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                   '-c', 'protocol.allow=never', '-c', 'protocol.file.allow=always',
                   '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
                   '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                   '--git-dir=' + str(directory / '.git'), '--work-tree=' + str(directory), *args]
        return self._execute(command, env=environment, check=check)

    def _service(self, action):
        return self._execute(['systemctl', action, SERVICE], timeout=30).stdout.strip()

    def _properties(self):
        result = self._execute(['systemctl', 'show', SERVICE, '-p', 'User', '-p', 'WorkingDirectory',
                                '-p', 'Environment', '-p', 'ExecStart', '-p', 'ActiveState'])
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)

    def _launch_info(self):
        import pwd
        properties = self._properties()
        if Path(properties.get('WorkingDirectory', '')).absolute() != self.target:
            raise ScreenPackageError('Screen service working directory does not match the package')
        if str(self.target / 'scripts/KlipperScreen-start.sh') not in properties.get('ExecStart', ''):
            raise ScreenPackageError('Unsupported screen service launcher')
        if (self.target / 'scripts/launch_KlipperScreen.sh').exists():
            raise ScreenPackageError('A custom screen launcher needs a dedicated update package')
        launcher = self.target / 'scripts/KlipperScreen-start.sh'
        if not launcher.is_file() or 'KS_XCLIENT' not in launcher.read_text(encoding='utf-8'):
            raise ScreenPackageError('Screen launcher does not use KS_XCLIENT')
        environment = {}
        for field in shlex.split(properties.get('Environment', '')):
            if '=' in field:
                key, value = field.split('=', 1)
                environment[key] = value
        client = shlex.split(environment.get('KS_XCLIENT', ''))
        normal = len(client) == 2 and Path(client[1]).absolute() == self.target / 'screen.py'
        recovering = (self.state and len(client) == 2 and client[0] == self.state.get('python')
                      and Path(client[1]) == self.runtime / 'start.py' and self._own_override())
        if not normal and not recovering:
            raise ScreenPackageError('Unsupported screen client command')
        interpreter = Path(client[0])
        if (any(character.isspace() or character in '\"\x00' for character in str(interpreter))
                or not interpreter.is_file() or not os.access(interpreter, os.X_OK)):
            raise ScreenPackageError('Screen Python interpreter is unavailable')
        version = self._execute([interpreter, '-c', 'import sys; print("%d.%d" % sys.version_info[:2])']).stdout.strip()
        if not re.fullmatch(r'3\.[0-9]+', version) or int(version.split('.')[1]) < 8:
            raise ScreenPackageError('Screen Python is not supported by this package')
        account = pwd.getpwnam(properties.get('User') or 'root')
        owner = self.target.stat()
        if account.pw_uid != owner.st_uid:
            raise ScreenPackageError('Screen repository owner differs from its service user')
        return {'python': str(interpreter), 'uid': owner.st_uid, 'gid': owner.st_gid,
                'mode': stat.S_IMODE(owner.st_mode), 'serviceWasActive': properties.get('ActiveState') == 'active'}

    def _material(self):
        if not self.bundle.is_file() or checksum(self.bundle) != self.spec['bundleSha256']:
            raise ScreenPackageError('Screen Git bundle has changed')
        result = self._execute(['git', 'bundle', 'list-heads', self.bundle]).stdout.splitlines()
        expected = self.spec['targetCommit'] + ' ' + self.spec['bundleRef']
        if expected not in result:
            raise ScreenPackageError('Git bundle does not contain the declared release')

    def _repository(self, directory):
        ensure_no_link(directory)
        gitdir = Path(directory) / '.git'
        if gitdir.is_symlink() or not gitdir.is_dir() or (gitdir / 'objects/info/alternates').exists():
            raise ScreenPackageError('Linked or alternate Git installations need a dedicated package')
        if any(item.is_symlink() for item in gitdir.rglob('*')) or (gitdir / 'worktrees').exists():
            raise ScreenPackageError('Shared or linked Git metadata is not supported')
        if any(gitdir.glob('**/*.lock')):
            raise ScreenPackageError('Another Git operation is in progress')
        flags = self._git(directory, 'ls-files', '-v', '-z').stdout.split('\0')
        if any(row and (row[0].islower() or row[0] == 'S') for row in flags):
            raise ScreenPackageError('Hidden local Git source flags are not supported')
        top = self._git(directory, 'rev-parse', '--show-toplevel').stdout.strip()
        if Path(top).resolve() != Path(directory).resolve():
            raise ScreenPackageError('Git worktree does not match the installed screen directory')
        if self._git(directory, 'config', '--local', '--get-regexp', '^filter\\.', check=False).stdout.strip():
            raise ScreenPackageError('External Git checkout filters are not supported')

    def _head(self, directory):
        return self._git(directory, 'rev-parse', 'HEAD').stdout.strip()

    def _new(self, directory):
        path = Path(directory) / '.git' / MARKER
        if not path.is_file():
            return False
        try:
            marker = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return False
        return marker.get('jobId') == self.job.name and marker.get('commit') == self.spec['targetCommit']

    def _status(self, directory):
        result = self._git(directory, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout.split('\0')
        rows, index = [], 0
        while index < len(result):
            row = result[index]
            index += 1
            if not row:
                continue
            if len(row) < 4:
                raise ScreenPackageError('Invalid Git status response')
            code, name = row[:2], relative(row[3:])
            rows.append((code, name))
            if 'R' in code or 'C' in code:
                if index >= len(result) or not result[index]:
                    raise ScreenPackageError('Invalid Git rename response')
                rows.append((code, relative(result[index])))
                index += 1
        return rows

    def _other_paths(self, directory, ignored=False):
        args = ['ls-files', '--others', '--exclude-standard', '-z']
        if ignored:
            args.append('--ignored')
        return [relative(name) for name in self._git(directory, *args).stdout.split('\0') if name]

    def _overlap(self, name):
        return any(name == target or name.startswith(target + '/') or target.startswith(name + '/')
                   for target in self.spec['targetFiles'])

    def _file_digest(self, directory, name):
        path = Path(directory) / relative(name)
        ensure_no_link(path)
        if not path.exists():
            return None
        if not path.is_file():
            raise ScreenPackageError('Managed baseline path is not a regular file')
        return checksum(path)

    def _baseline(self, directory):
        self._repository(directory)
        head = self._head(directory)
        if head == self.spec['targetCommit']:
            self._verify_tree(directory)
            self._git(directory, 'bundle', 'verify', self.bundle)
            return {'head': head, 'files': {}}
        candidates = [item for item in self.spec['allowedBaselines'] if item['head'] == head]
        for baseline in candidates:
            if all(self._file_digest(directory, name) == digest for name, digest in baseline['files'].items()):
                break
        else:
            raise ScreenPackageError('Installed source files do not match a verified package baseline')
        for code, name in self._status(directory):
            if name in baseline['files']:
                continue
            if code == '??' and not self._overlap(name):
                continue  # Preserve non-conflicting user files.
            raise ScreenPackageError('Unexpected local source modification: ' + name)
        for name in self._other_paths(directory, ignored=True):
            if self._overlap(name):
                raise ScreenPackageError('A local ignored file conflicts with the incoming release: ' + name)
        self._git(directory, 'bundle', 'verify', self.bundle)
        return baseline

    def _tree_bytes(self, directory):
        result = 0
        for parent, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
            for name in files:
                path = Path(parent) / name
                if path.is_file() and not path.is_symlink():
                    result += path.stat().st_size
        return result

    def check(self):
        self._material()
        if not self.target.exists():
            if not self.state or not self.backup.is_dir() or self._new(self.backup):
                raise ScreenPackageError('Installed screen directory is missing')
            self._baseline(self.backup)
            return
        if self._new(self.target):
            self._verify_tree(self.target)
            self._launch_info()
            return
        self._baseline(self.target)
        self._launch_info()
        required = self._tree_bytes(self.target) + self.spec['targetBytes'] + self.bundle.stat().st_size + 64 * 1024**2
        if shutil.disk_usage(self.target.parent).free < required:
            raise ScreenPackageError('Not enough free space for a staged screen update and backup')
        if self.override.exists() and not self._own_override():
            raise ScreenPackageError('Another screen startup override is present')
        for path in (self.stage, self.backup, self.failed):
            if path.exists() and self.state is None:
                raise ScreenPackageError('A foreign screen staging or backup path already exists')

    def _progress(self, value, message):
        atomic_json(self.job / 'script-progress.json', {'phase': 'INSTALLING', 'progress': value,
                                                      'component': 'klipperscreen', 'message': message}, 0o644)

    def _assert_artifact(self, path):
        path = ensure_no_link(path)
        if path not in (self.stage, self.backup, self.failed) or path.parent != self.target.parent or path == self.target:
            raise ScreenPackageError('Invalid screen task artifact path')
        return path

    def _remove_stage(self):
        self._assert_artifact(self.stage)
        if self.stage.exists():
            if not self.target.is_dir() or self._new(self.target):
                raise ScreenPackageError('An original backup may still occupy the staging path')
            shutil.rmtree(self.stage)
            sync_dir(self.stage.parent)

    def _chown(self, directory, uid, gid):
        for parent, directories, files in os.walk(directory, followlinks=False):
            os.chown(parent, uid, gid)
            for name in directories + files:
                os.chown(Path(parent) / name, uid, gid, follow_symlinks=False)

    def _copy_user_file(self, source, destination):
        source, destination = Path(source), Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_dir() and not destination.is_symlink():
            raise ScreenPackageError('User data changed type while staging')
        if source.is_symlink():
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            os.symlink(os.readlink(source), destination)
        else:
            if destination.is_symlink():
                destination.unlink()
            shutil.copy2(source, destination, follow_symlinks=False)
        owner = source.lstat()
        os.chown(destination, owner.st_uid, owner.st_gid, follow_symlinks=False)
        if source.is_symlink():
            if not destination.is_symlink() or os.readlink(source) != os.readlink(destination):
                raise ScreenPackageError('User data changed while staging')
        elif checksum(source) != checksum(destination):
            raise ScreenPackageError('User data changed while staging')

    def _user_files(self, directory):
        return sorted(set(self._other_paths(directory) + self._other_paths(directory, ignored=True)))

    def _preserve_directory_metadata(self, source, destination):
        for parent, directories, _ in os.walk(source, followlinks=False):
            original = Path(parent)
            relative_name = original.relative_to(source)
            directories[:] = [name for name in directories if name != '.git'
                              and not (original / name).is_symlink()]
            copied = Path(destination) / relative_name
            if copied.is_dir() and not copied.is_symlink():
                shutil.copystat(original, copied, follow_symlinks=False)
                owner = original.stat()
                os.chown(copied, owner.st_uid, owner.st_gid)

    def _preserve_user_files(self, source, destination):
        retired = self.state['baseline']['files'] if self.state else {}
        files = [name for name in self._user_files(source) if not self._overlap(name) and name not in retired]
        for name in files:
            parent = Path()
            for part in Path(name).parts[:-1]:
                parent /= part
                original = Path(source) / parent
                copied = Path(destination) / parent
                if original.is_symlink() or not original.is_dir():
                    raise ScreenPackageError('User data has changed while staging')
                if copied.is_symlink() or copied.is_file():
                    copied.unlink()
                copied.mkdir(exist_ok=True)
                shutil.copystat(original, copied, follow_symlinks=False)
                owner = original.stat()
                os.chown(copied, owner.st_uid, owner.st_gid)
            self._copy_user_file(Path(source) / name, Path(destination) / name)

    def _sync_tree(self, directory):
        folders = []
        for parent, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
            folders.append(Path(parent))
            for name in files:
                path = Path(parent) / name
                if not path.is_symlink() and path.is_file():
                    with path.open('rb') as source:
                        os.fsync(source.fileno())
        for folder in reversed(folders):
            sync_dir(folder)

    def _verify_tree(self, directory):
        self._repository(directory)
        if self._head(directory) != self.spec['targetCommit']:
            raise ScreenPackageError('Installed screen Git revision is incorrect')
        tree = self._git(directory, 'ls-tree', '-r', '-z', self.spec['targetCommit']).stdout.split('\0')
        listed = set()
        for row in tree:
            if not row:
                continue
            header, name = row.split('\t', 1)
            if header.split()[0] not in ('100644', '100755'):
                raise ScreenPackageError('Screen release contains symlinks or submodules')
            expected_mode = 0o755 if header.split()[0] == '100755' else 0o644
            if stat.S_IMODE((Path(directory) / name).stat().st_mode) != expected_mode:
                raise ScreenPackageError('Installed screen file mode differs from the release: ' + name)
            listed.add(relative(name))
        if listed != set(self.spec['targetFiles']):
            raise ScreenPackageError('Screen release inventory differs from its Git tree')
        for name, digest in self.spec['targetFiles'].items():
            if self._file_digest(directory, name) != digest:
                raise ScreenPackageError('Installed screen file differs from the release: ' + name)
            if name.endswith('.py'):
                compile((Path(directory) / name).read_bytes(), name, 'exec')
        if self._git(directory, 'status', '--porcelain=v1', '--untracked-files=no').stdout.strip():
            raise ScreenPackageError('Installed tracked screen files are not clean')

    def _build_stage(self):
        self._progress(10, 'Staging the screen Git release')
        self._remove_stage()
        shutil.copytree(self.target, self.stage, symlinks=True)
        self._save('PREPARING')
        index = self.stage / '.git/index'
        if index.exists():
            index.unlink()
        self._git(self.stage, 'config', '--local', '--unset-all', 'core.worktree', check=False)
        self._git(self.stage, 'fetch', '--no-tags', self.bundle, self.spec['bundleRef'])
        self._git(self.stage, 'reset', '--hard', self.spec['targetCommit'])
        for row in self._git(self.stage, 'ls-tree', '-r', '-z', self.spec['targetCommit']).stdout.split('\0'):
            if row:
                header, name = row.split('\t', 1)
                mode = header.split()[0]
                if mode not in ('100644', '100755'):
                    raise ScreenPackageError('Screen release contains symlinks or submodules')
                os.chmod(ensure_no_link(self.stage / relative(name)), 0o755 if mode == '100755' else 0o644)
        for name in self.state['baseline']['files']:
            if name not in self.spec['targetFiles']:
                path = self.stage / name
                if path.is_file() or path.is_symlink():
                    path.unlink()
        self._chown(self.stage, self.state['uid'], self.state['gid'])
        self._preserve_directory_metadata(self.target, self.stage)
        self._preserve_user_files(self.target, self.stage)
        atomic_json(self.stage / '.git' / MARKER, {'schemaVersion': 1, 'jobId': self.job.name,
                    'commit': self.spec['targetCommit'], 'version': self.spec['targetVersion']}, 0o644)
        os.chown(self.stage / '.git' / MARKER, self.state['uid'], self.state['gid'])
        os.chmod(self.stage, self.state['mode'])
        self._verify_tree(self.stage)
        self._sync_tree(self.stage)
        self._save('STAGED')

    @staticmethod
    def _exchange(left, right):
        if os.name != 'posix':
            return False
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, 'renameat2', None)
        if function is None:
            return False
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(-100, os.fsencode(left), -100, os.fsencode(right), 2) == 0:
            return True
        failure = ctypes.get_errno()
        if failure in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            return False
        raise OSError(failure, os.strerror(failure))

    def _finish_exchange_backup(self):
        if self.stage.exists() and not self._new(self.stage):
            if self.backup.exists():
                raise ScreenPackageError('Original backup paths conflict')
            if self._head(self.stage) != self.state['originalHead']:
                raise ScreenPackageError('The exchanged original backup is not the expected screen')
            self._baseline(self.stage)
            os.replace(self.stage, self.backup)
            sync_dir(self.target.parent)

    def _switch(self):
        if self.backup.exists():
            raise ScreenPackageError('The original backup must not be overwritten')
        self._save('SWITCHING')
        if self._exchange(self.target, self.stage):
            sync_dir(self.target.parent)
            self._finish_exchange_backup()
        else:
            os.replace(self.target, self.backup)
            sync_dir(self.target.parent)
            os.replace(self.stage, self.target)
            sync_dir(self.target.parent)
        self._save('INSTALLED')

    def _own_override(self):
        if not self.override.is_file() or self.override.is_symlink():
            return False
        return self.override.read_text(encoding='utf-8').startswith('# KlipperScreen offline update ' + self.job.name + '\n')

    def _clear_override(self):
        if self.override.exists():
            if not self._own_override():
                raise ScreenPackageError('A foreign screen override must not be removed')
            self.override.unlink()
            sync_dir(self.override.parent)
            self._execute(['systemctl', 'daemon-reload'])
        # Retain the tiny /run wrapper until reboot: an old manager environment
        # may still reference it if daemon-reload failed after unlinking.

    def _start_screen(self):
        self.runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
        ensure_no_link(self.runtime)
        # The supervisor deliberately uses UMask=0077; the screen's ordinary
        # service user must still traverse this one-process launch directory.
        os.chmod(self.runtime, 0o755)
        wrapper = self.runtime / 'start.py'
        text = ('import sys\nsys.path.insert(0, ' + repr(str(self.target)) + ')\n'
                'import screen\nscreen.KlipperScreen.auto_check = False\nscreen.main()\n')
        temporary = wrapper.with_suffix('.partial')
        temporary.write_text(text, encoding='utf-8')
        temporary.chmod(0o644)
        with temporary.open('rb') as source:
            os.fsync(source.fileno())
        os.replace(temporary, wrapper)
        sync_dir(self.runtime)
        self.override.parent.mkdir(parents=True, exist_ok=True)
        ensure_no_link(self.override.parent)
        content = ('# KlipperScreen offline update ' + self.job.name + '\n[Service]\nEnvironment="KS_XCLIENT='
                   + self.state['python'] + ' ' + str(wrapper) + '"\n')
        if self.override.exists() and not self._own_override():
            raise ScreenPackageError('A foreign screen startup override is present')
        pending = self.override.with_suffix('.partial')
        pending.write_text(content, encoding='utf-8')
        pending.chmod(0o644)
        with pending.open('rb') as source:
            os.fsync(source.fileno())
        os.replace(pending, self.override)
        sync_dir(self.override.parent)
        self._execute(['systemctl', 'daemon-reload'])
        self._service('start')
        self._clear_override()

    def install(self):
        self.check()
        if self.state and self._new(self.target):
            self._finish_exchange_backup()
            self._verify_tree(self.target)
            self._start_screen()
            self.verify()
            return
        if not self.target.exists() and self.state and self.backup.exists():
            if self.stage.exists() and self._new(self.stage):
                self._verify_tree(self.stage)
                os.replace(self.stage, self.target)
                sync_dir(self.target.parent)
                self._save('INSTALLED')
                self._start_screen()
                self.verify()
                return
            os.replace(self.backup, self.target)
            sync_dir(self.target.parent)
            self._save('RESCUED')
        if self.state is None:
            launch = self._launch_info()
            baseline = self._baseline(self.target)
            self.state = {'schemaVersion': 1, 'jobId': self.job.name, 'targetPath': str(self.target),
                          'stagePath': str(self.stage), 'backupPath': str(self.backup),
                          'targetCommit': self.spec['targetCommit'], 'bundleSha256': self.spec['bundleSha256'],
                          'baseline': baseline, 'originalHead': self._head(self.target), **launch}
            self._save('PREPARING')
        self._build_stage()
        self._baseline(self.target)  # Detect an online checkout racing with staging.
        self._save('STOPPING')
        self._progress(60, 'Restarting the screen with the verified release')
        self._service('stop')
        self._baseline(self.target)
        self._preserve_user_files(self.target, self.stage)
        self._verify_tree(self.stage)
        self._sync_tree(self.stage)
        self._switch()
        self._start_screen()
        self.verify()

    def verify(self):
        if not self.state or not self._new(self.target):
            raise ScreenPackageError('This task has not installed the screen')
        self._verify_tree(self.target)
        if self._properties().get('ActiveState') != 'active':
            raise ScreenPackageError('The updated screen service is not active')
        self._save('VERIFIED')
        self._progress(99, 'Screen files verified; waiting for the display handshake')

    def rescue(self, boot=False):
        self._material()
        if self.state is None:
            return  # A read-only preinst rejection has no screen files to restore.
        if self._new(self.target):
            self._finish_exchange_backup()
        if self.backup.exists():
            if self._new(self.backup) or self._head(self.backup) != self.state['originalHead']:
                raise ScreenPackageError('The original screen backup cannot be confirmed')
            if not boot:
                self._service('stop')
            if self.target.exists():
                if not self._new(self.target):
                    raise ScreenPackageError('Refusing to replace an unknown installed screen')
                self._preserve_user_files(self.target, self.backup)
                if self.failed.exists():
                    self._assert_artifact(self.failed)
                    # Preserve prior attempts; never recursively delete a backup.
                    previous = self.failed.with_name(self.failed.name + '-' + str(time.time_ns()))
                    os.replace(self.failed, previous)
                os.replace(self.target, self.failed)
                sync_dir(self.target.parent)
            os.replace(self.backup, self.target)
            sync_dir(self.target.parent)
        elif not self.target.exists() or self._new(self.target) or self._head(self.target) != self.state['originalHead']:
            raise ScreenPackageError('The original screen is unavailable for rescue')
        self._save('RESCUED')
        if not boot:
            self._clear_override()
            if self.state.get('serviceWasActive'):
                self._start_screen()

    def failure(self):
        atomic_json(self.job / 'klipperscreen-failure.json', {'jobId': self.job.name,
                    'phase': self.state.get('phase') if self.state else 'PRECHECK',
                    'recoveryRequired': True})


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('check', 'install', 'recover', 'verify', 'failure', 'rescue'):
        raise SystemExit(2)
    installer = ScreenInstaller()
    action = sys.argv[1]
    if action == 'rescue':
        flag = os.environ.get('MINGDA_BOOT_RECOVERY', '0')
        if flag not in ('0', '1'):
            raise ScreenPackageError('Invalid boot rescue mode')
        installer.rescue(boot=flag == '1')
    elif action in ('install', 'recover'):
        installer.install()
    else:
        getattr(installer, action)()


if __name__ == '__main__':
    main()
