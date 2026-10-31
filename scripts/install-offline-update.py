#!/usr/bin/python3
"""Compatibility name for automatic runtime synchronization; no legacy gates."""

import argparse
from pathlib import Path
import runpy
import sys


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    entry = Path(__file__).resolve().with_name('ensure-offline-update.py')
    sys.argv = [str(entry), 'ensure']
    runpy.run_path(str(entry), run_name='__main__')
