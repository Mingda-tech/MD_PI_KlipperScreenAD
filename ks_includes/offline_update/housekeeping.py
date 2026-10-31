"""Remove committed tasks' caches/backups; preserve unfinished recovery and live code.

Call only while holding control.lock and the offline-update task lock. Small
journals remain available for diagnosis. Component backups survive until commit.
"""

import logging
import hashlib
import os
from pathlib import Path
import re
import shutil
import time

from .common import UpdateError, atomic_json, file_lock, no_links, read_json, sync_dir, trusted_path, worker_running

CACHE_NAMES = ('package.partial', 'package.mdu', 'runner', 'native-tmp', 'native-run')
VERSION = re.compile(r'[0-9a-f]{64}\Z')
APPLICATIONS = Path('/home/mingda')


def _remove(path, root):
    root = Path(root).absolute()
    path = no_links(Path(path).absolute(), root)
    if path == root or root.resolve() not in path.resolve().parents:
        raise UpdateError('PATH_OUTSIDE_ROOT')
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()
    elif path.exists():
        raise UpdateError('NOT_REGULAR_FILE')
    sync_dir(path.parent)


def in_use(path):
    """An old process can still have its cwd or libraries in a renamed backup."""
    prefix = str(path) + '/'
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            for name in ('cwd', 'exe'):
                try:
                    value = os.readlink(proc / name).removesuffix(' (deleted)')
                    if value == str(path) or value.startswith(prefix):
                        return True
                except FileNotFoundError:
                    pass
            with (proc / 'maps').open() as stream:
                if any(prefix in line for line in stream):
                    return True
        except FileNotFoundError:
            continue
        except (PermissionError, OSError):
            return True
    return False


def _state(job, name):
    path = job / name
    if not path.exists():
        return None
    value = read_json(no_links(path, job))
    if not isinstance(value, dict):
        raise UpdateError('INVALID_STATE')
    return value


def external_backups(job, store):
    """Derive only the shipped installers' task-owned backup paths.

    Never delete arbitrary paths listed in a state file. Every candidate has a
    fixed component root or an exact task-specific name beside a recorded target.
    """
    paths = []
    for filename, component in (('klipperscreen-state.json', 'KlipperScreen'), ('host-state.json', 'klipper')):
        saved = _state(job, filename)
        if saved is None:
            continue
        target = Path(saved['targetPath'])
        if saved.get('jobId') != job.name or not target.is_absolute() or target.name != component:
            raise UpdateError('INVALID_STATE')
        no_links(target, target.anchor)
        stem = '.' + component + '.mdu-' + job.name
        for field, suffix in (('stagePath', '.stage'), ('backupPath', '.backup')):
            path = target.with_name(stem + suffix)
            if str(path) != saved.get(field):
                raise UpdateError('INVALID_STATE')
            paths.append(path)
        paths.extend(path for path in target.parent.glob(stem + '.failed*')
                     if re.fullmatch(re.escape(stem) + r'\.failed(?:-[0-9]+)?', path.name))
    saved = _state(job, 'moonraker-state.json')
    if saved is not None:
        if saved.get('jobId') != job.name or saved.get('phase') != 'COMPLETE':
            raise UpdateError('INVALID_STATE')
        paths.extend(APPLICATIONS / ('.' + name + '-mdu-' + job.name) for name in ('moonraker', 'moonraker-env'))
    saved = _state(job, 'mainsail-state.json')
    if saved is not None:
        token = hashlib.sha256(str(job.resolve()).encode()).hexdigest()
        target = Path(saved['targetPath'])
        container = target.with_name('.mdu-mainsail-' + token[:24])
        if (not target.is_absolute() or target.name != 'mainsail' or saved.get('token') != token
                or saved.get('backupPath') != str(container / 'site') or saved.get('phase') != 'COMPLETE'):
            raise UpdateError('INVALID_STATE')
        no_links(target, target.anchor)
        paths.append(container)
    saved = _state(job, 'sdk-ai-state.json')
    if saved is not None:
        if saved.get('jobId') != job.name or saved.get('phase') != 'COMPLETE':
            raise UpdateError('INVALID_STATE')
        if saved.get('retentionScheduled'):
            report = _state(job, 'retention-report.json') or {}
            if (report.get('phase') != 'COMPLETE'
                    or any(report.get('identity', {}).get(k) != saved.get(k)
                           for k in ('schemaVersion', 'jobId', 'releaseId', 'specSha256'))):
                raise UpdateError('UPDATE_BUSY')
        # A later interrupted SDK task may depend on an earlier release, even
        # after the operator dismissed its task. Retain shared SDK baselines.
        for other in store.jobs.iterdir():
            if other == job:
                continue
            tx = store.load(other.name)
            if (other / 'sdk-ai-state.json').exists() and (
                    tx.get('phase') not in ('COMPLETE', 'FAILED', 'CANCELLED')
                    or tx.get('recoveryArtifactsRetained') or tx.get('phase') != 'COMPLETE'
                    and tx.get('sideEffectsMayHaveStarted') is not False):
                raise UpdateError('UPDATE_BUSY')
        components = saved['components']
        if set(components) != {'sdk', 'ai-runtime'}:
            raise UpdateError('INVALID_STATE')
        for name, row in components.items():
            target = Path(row['target'])
            root = target.parents[2]
            if not target.is_absolute() or root.name != name or target.parents[1].name != 'releases':
                raise UpdateError('INVALID_STATE')
            no_links(root, root.anchor)
            current = (root / 'current').resolve(strict=True)
            if len(current.relative_to(root / 'releases').parts) != 2:
                raise UpdateError('INVALID_STATE')
            no_links(current, root)
            stage = target.with_name('.' + target.name + '.mdu-' + job.name)
            baseline = Path(saved['baseline'][name]['target'])
            if str(stage) != row['stage'] or len(baseline.relative_to(root / 'releases').parts) != 2:
                raise UpdateError('INVALID_STATE')
            for path in (stage, baseline):
                if path != current:
                    paths.append(path)
    return paths


def prune_jobs(store):
    """Delete every confirmed success cache/backup; return False to retry later.

    Incomplete, unconfirmed and cancelled-after-write jobs are never eligible.
    Failed preparations lose only disposable caches. JSON records survive.
    """
    if store.marker.exists():
        return False
    if not store.jobs.exists():
        return True
    finished = True
    try:
        trusted_path(store.jobs)
        for directory in store.jobs.iterdir():
            try:
                no_links(directory, store.jobs)
                tx = store.load(directory.name)
                if not isinstance(tx, dict) or tx.get('jobId') != directory.name or tx.get('recoveryArtifactsRetained'):
                    continue
                complete = (tx.get('phase') == 'COMPLETE' and tx.get('nativeSuccess') is True
                            and tx.get('commitIntent') is True and not tx.get('rescueCompleted'))
                untouched = (tx.get('phase') in ('FAILED', 'CANCELLED')
                             and tx.get('sideEffectsMayHaveStarted') is False)
                if not (complete or untouched):
                    continue
                cleaned = _state(directory, 'cleanup-result.json') or {}
                if cleaned.get('complete') is True and cleaned.get('jobId') == directory.name:
                    continue
                if worker_running(directory.name) is not False:
                    finished = False
                    continue
                with file_lock(directory / 'worker.lock'):
                    external = external_backups(directory, store) if complete else []
                    internal = ([p for p in directory.iterdir() if p.name != 'worker.lock' and p.suffix != '.json']
                                if complete else [directory / name for name in CACHE_NAMES])
                    candidates = [(p, p.parent) for p in external] + [(p, store.jobs) for p in internal]
                    # Validate the whole plan before deleting its first member.
                    candidates = [(no_links(p, root), root) for p, root in candidates if p.exists() or p.is_symlink()]
                    if any(in_use(p) for p, _ in candidates):
                        raise UpdateError('UPDATE_BUSY')
                    for path, root in candidates:
                        _remove(path, root)
                    atomic_json(directory / 'cleanup-result.json', {
                        'jobId': directory.name, 'complete': True, 'completedAt': time.time(),
                        'removed': [str(p) for p, _ in candidates]})
            except (OSError, ValueError, KeyError, IndexError, TypeError, UpdateError):
                finished = False
                logging.warning('Offline update cache cleanup deferred for task %s', directory.name)
    except (OSError, UpdateError):
        logging.warning('Offline update cache cleanup deferred')
        return False
    return finished


def prune_releases(runtime):
    """Keep the active runtime and one previous version, after a complete switch."""
    runtime = Path(runtime)
    try:
        releases = trusted_path(runtime / 'releases')
        # Synchronization owns both locks: no staging directory can be in use.
        # A killed setup may have left one before writing runtime.json.
        for path in releases.iterdir():
            if re.fullmatch(r'[0-9a-f]{64}\.staging-[0-9a-f]{32}', path.name):
                _remove(path, releases)
        pointer = read_json(trusted_path(runtime / 'current.json'))
        if not isinstance(pointer, dict):
            return
        current = pointer.get('runtimeVersion', '')
        if pointer.get('schemaVersion') != 1 or not isinstance(current, str) or not VERSION.fullmatch(current):
            return
        active = trusted_path(releases / current)
        if not active.is_dir():
            return
        previous = []
        for path in releases.iterdir():
            if path.name != current and VERSION.fullmatch(path.name):
                no_links(path, releases)
                if (path.is_dir()
                        and read_json(path / 'runtime.json') == {'schemaVersion': 1, 'runtimeVersion': path.name}):
                    previous.append(path)
        previous.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for path in previous[1:]:
            _remove(path, releases)
    except (OSError, TypeError, UpdateError):
        logging.warning('Offline update runtime cleanup deferred')
