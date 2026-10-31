"""USB storage discovery. Paths are discovered from the kernel, not supplied by callers."""

import json
import os
from pathlib import Path
import re
import stat
import uuid

from .common import UpdateError, command, no_links


def volumes(tree, usb_parent=False):
    for row in tree:
        usb = usb_parent or row.get('tran') == 'usb'
        mounts = row.get('mountpoints') or ([row.get('mountpoint')] if row.get('mountpoint') else [])
        if usb and row.get('type') in ('part', 'disk') and (row.get('fstype') or mounts):
            yield row
        yield from volumes(row.get('children', []), usb)


def discover(mount_missing=True, run=command):
    data = json.loads(run(['lsblk', '--json', '--paths', '--output', 'NAME,TYPE,TRAN,MOUNTPOINTS,FSTYPE,MAJ:MIN']))
    packages, warnings = [], []
    for volume in volumes(data.get('blockdevices', [])):
        device = Path(volume['name'])
        number = volume.get('maj:min', '')
        if not re.fullmatch(r'\d+:\d+', number) or not str(device).startswith('/dev/'):
            continue
        try:
            if not stat.S_ISBLK(device.stat().st_mode):
                continue
            # Confirm sysfs ancestry; a misleading filesystem label is not USB identity.
            sys_path = (Path('/sys/dev/block') / number).resolve(strict=True)
            if not any(re.fullmatch(r'usb\d+', part) for part in sys_path.parts):
                continue
            mounts = [Path(x) for x in volume.get('mountpoints', []) if x]
            if not mounts and mount_missing:
                if volume.get('fstype') not in ('vfat', 'exfat', 'ntfs', 'ext4'):
                    warnings.append('Unsupported USB filesystem: ' + str(volume.get('fstype')))
                    continue
                target = Path('/run/klipperscreen-update/usb') / number.replace(':', '-')
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                run(['mount', '-o', 'ro,nosuid,nodev,noexec', device, target], timeout=15)
                mounts = [target]
            for mount in mounts:
                if not mount.is_mount():
                    continue
                for folder in (mount, mount / 'MINGDA-Update'):
                    if folder.is_symlink() or not folder.is_dir():
                        continue
                    for path in sorted(folder.glob('*.mdu')):
                        if len(packages) >= 128:
                            break
                        no_links(path, mount)
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        packages.append({'id': uuid.uuid4().hex, 'name': path.name, 'size': info.st_size,
                                         'mount': str(mount), 'path': str(path), 'device': number,
                                         'st_dev': info.st_dev, 'st_ino': info.st_ino, 'mtime_ns': info.st_mtime_ns})
        except (OSError, UpdateError, ValueError):
            warnings.append('USB volume unavailable: ' + number)
    return packages, warnings


def open_package(record):
    path = no_links(record['path'], record['mount'])
    if not Path(record['mount']).is_mount():
        raise UpdateError('USB_REMOVED')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != record['st_dev'] or info.st_ino != record['st_ino'] or
                info.st_size != record['size'] or info.st_mtime_ns != record['mtime_ns']):
            raise UpdateError('USB_PACKAGE_CHANGED')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
