"""Trusted -P command: do not start package scripts before the UI monitor is attached."""

import os
from pathlib import Path
import stat
import time


def wait_for_monitor(timeout=15):
    root = Path(os.environ['RUNTIME_DIRECTORY'])
    marker = Path(os.environ['MINGDA_SWUPDATE_READY'])
    if marker.parent != root or marker.name != 'progress-ready':
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = marker.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and marker.read_bytes() == b'ACK\n':
                return 0
            return 1
        except FileNotFoundError:
            time.sleep(0.025)
    return 1


if __name__ == '__main__':
    raise SystemExit(wait_for_monitor())
