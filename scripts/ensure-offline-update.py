#!/usr/bin/python3 -I
"""Fixed internal entry for the screen's automatic runtime synchronization."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ks_includes'))

if __name__ == '__main__':
    args = sys.argv[1:]
    cancelling = len(args) == 2 and args[0] == 'cancel'
    if args != ['ensure'] and not cancelling:
        print(json.dumps({'ok': False, 'error': 'INVALID_ACTION'}))
        raise SystemExit(2)
    try:
        if cancelling:
            # A pending task deliberately blocks runtime replacement. Use the
            # screen's current fixed cancellation code without touching its runner.
            from offline_update.launcher import dispatch
            print(json.dumps(dispatch(args), ensure_ascii=False))
        else:
            from offline_update.runtime_install import ensure_runtime
            print(json.dumps(ensure_runtime(ROOT), ensure_ascii=False))
    except Exception as error:
        if cancelling:
            from offline_update.common import UpdateError
            print(json.dumps({'ok': False, 'error': error.code if isinstance(error, UpdateError) else 'INSTALLER_ERROR'}))
            raise SystemExit(1)
        # Raw tracebacks may contain local paths/config; GTK receives fixed codes.
        print(json.dumps({'ok': True, 'runtimeState': 'BLOCKED', 'runtimeReason': 'RUNTIME_SETUP_FAILED',
                          'runtimeVersion': '', 'changed': False}))
