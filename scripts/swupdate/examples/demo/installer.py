#!/usr/bin/env python3
"""Harmless example: each package owns its actions and durable recovery logic."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


def atomic(path, data):
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(action):
    job = Path(os.environ['MINGDA_JOB_DIR'])
    source = Path(os.environ['TMPDIR']) / 'demo.txt'
    if not job.is_absolute() or job.is_symlink() or not job.is_dir() or not source.is_file():
        raise RuntimeError('Invalid task workspace')
    target, checkpoint = job / 'demo-installed.txt', job / 'demo-checkpoint.json'
    if action == 'check':
        # This demo needs only Python, a valid task directory and a readable
        # staged payload, on any OS/board. A production package checks its own
        # system capabilities, target boards and firmware here, without writes
        # or service stops. Record a durable write intent before each later
        # physical MCU write; recovery must use this task and the same payload.
        source.read_bytes()
        return
    if action in ('install', 'recover'):
        # Both actions are idempotent; a power loss can safely replay the same package.
        state = {'phase': 'write-intent', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        atomic(checkpoint, json.dumps(state).encode())
        atomic(target, source.read_bytes())
        state['phase'] = 'installed'
        atomic(checkpoint, json.dumps(state).encode())
    elif action == 'verify':
        if target.read_bytes() != source.read_bytes():
            raise RuntimeError('Demo verification failed')
    elif action == 'failure':
        # Failure notification does not claim rollback or erase the recovery record.
        print('Demo update failed; retain task state and use explicit recovery.', flush=True)
    else:
        raise RuntimeError('Unsupported action')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(2)
    run(sys.argv[1])
