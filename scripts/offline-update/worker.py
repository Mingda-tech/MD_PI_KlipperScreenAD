#!/usr/bin/python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_update.launcher import worker
from offline_update.common import identifier
if len(sys.argv) != 3 or sys.argv[1] not in ('prepare', 'start', 'recover'):
    raise SystemExit(2)
worker(sys.argv[1], identifier(sys.argv[2]))
