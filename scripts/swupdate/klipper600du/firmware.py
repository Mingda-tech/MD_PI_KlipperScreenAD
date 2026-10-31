"""Package-owned CanBoot transport guard for the supplied 600DU head image.

``validate_image`` only reads files. ``FirmwareUpdate.run`` is intentionally a
post-install operation: even an upstream ``-q`` query resets bootloader nodes.
The record callback must persist each checkpoint synchronously or raise.
"""
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import re
import struct
import types
import zlib


class FirmwareError(RuntimeError):
    pass


RESET_HANDLER = bytes.fromhex('72b6024b024a9d4610477047')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{12}\Z')


def _regular_bytes(path):
    path = Path(path).absolute()
    if not path.is_file() or any(item.is_symlink() for item in (path, *path.parents)):
        raise FirmwareError('Firmware material must be a regular, unlinked file')
    return path.read_bytes()


def _specification(spec):
    if (not isinstance(spec, dict) or not UUID.fullmatch(str(spec.get('canbusUuid', '')))
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', str(spec.get('canInterface', '')))
            or spec.get('mcuType') != 'stm32f072xb'
            or type(spec.get('applicationStart')) is not int or spec['applicationStart'] != 0x08002000
            or type(spec.get('flashEnd')) is not int or spec['flashEnd'] != 0x08020000
            or type(spec.get('imageBytes')) is not int or not 32 <= spec['imageBytes'] <= 0x1e000
            or not SHA256.fullmatch(str(spec.get('imageSha256', '')))
            or not SHA256.fullmatch(str(spec.get('toolSha256', '')))
            or not isinstance(spec.get('firmwareVersion'), str) or not spec['firmwareVersion']):
        raise FirmwareError('Invalid 600DU firmware declaration')


def _identify(raw):
    matches = []
    for offset, value in enumerate(raw):
        if value != 0x78:
            continue
        try:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(raw[offset:], 1024 * 1024 + 1)
            if len(decoded) > 1024 * 1024 or not decoder.eof:
                continue
            item = json.loads(decoded)
            if isinstance(item, dict) and item.get('app') == 'Klipper' and 'version' in item and 'config' in item:
                matches.append((offset, item))
        except (ValueError, UnicodeError, zlib.error):
            continue
    if len(matches) != 1:
        raise FirmwareError('Firmware must contain one unambiguous Klipper identity')
    return matches[0]


def _validate_bytes(raw, spec):
    _specification(spec)
    checksum = hashlib.sha256(raw).hexdigest()
    if len(raw) != spec['imageBytes'] or checksum != spec['imageSha256']:
        raise FirmwareError('Firmware image size or SHA256 differs from its signed declaration')
    stack, reset, default = struct.unpack_from('<III', raw)
    if raw.count(RESET_HANDLER) != 1 or not reset & 1 or not default & 1:
        raise FirmwareError('Firmware reset vectors are not recognized')
    offset = raw.index(RESET_HANDLER)
    base = (reset & ~1) - offset
    if offset + 20 > len(raw):
        raise FirmwareError('Truncated reset handler')
    literal_stack, stage2 = struct.unpack_from('<II', raw, offset + 12)
    default_offset = (default & ~1) - base
    if (base != spec['applicationStart'] or stack != 0x20004000 or literal_stack != stack
            or not stage2 & 1 or not base <= (stage2 & ~1) < base + len(raw)
            or default_offset < 0 or raw[default_offset:default_offset + 2] != b'\xfe\xe7'
            or base + len(raw) > spec['flashEnd']):
        raise FirmwareError('Firmware link address, stack, or reset code is inconsistent')
    identity_offset, identity = _identify(raw)
    constants = identity['config']
    if (not isinstance(constants, dict) or identity['version'] != spec['firmwareVersion']
            or constants.get('MCU') != spec['mcuType'] or constants.get('CLOCK_FREQ') != 48000000
            or constants.get('CANBUS_FREQUENCY') != 1000000
            or constants.get('RESERVE_PINS_CAN') != 'PB8,PB9'):
        raise FirmwareError('Firmware identity does not match the 600DU board declaration')
    commands = identity.get('commands', {})
    if (not isinstance(commands, dict) or not any(name.startswith('cs1237_home_v2 ') for name in commands)
            or not any(name.startswith('config_cs1237 ') for name in commands)):
        raise FirmwareError('Firmware does not provide the required CS1237 v2 protocol')
    return {'imageSha256': checksum, 'imageBytes': len(raw), 'applicationStart': base,
            'mcuType': constants['MCU'], 'firmwareVersion': identity['version'],
            'resetHandlerOffset': offset, 'identityOffset': identity_offset}


def validate_image(image, spec):
    """Return verified metadata without opening CAN or executing the tool."""
    return _validate_bytes(_regular_bytes(image), spec)


class FirmwareUpdate:
    def __init__(self, spec, image, tool, record, progress):
        _specification(spec)
        if not callable(record) or not callable(progress):
            raise FirmwareError('Persistent record and progress callbacks are required')
        self.spec = dict(spec)
        self.image, self.tool = Path(image).absolute(), Path(tool).absolute()
        self.record, self.progress = record, progress
        self.write_started = self.verified = self.completed = False
        self._module = None
        self._attempted = False

    def _record(self, stage, **fields):
        result = self.record(stage, canbusUuid=self.spec['canbusUuid'], imageSha256=self.spec['imageSha256'],
                             writeMayHaveStarted=self.write_started, flashVerified=self.verified,
                             flashDone=self.completed, **fields)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise FirmwareError('The checkpoint callback must finish synchronously')

    def _load_tool(self):
        source = _regular_bytes(self.tool)
        if hashlib.sha256(source).hexdigest() != self.spec['toolSha256']:
            raise FirmwareError('CanBoot tool differs from its signed declaration')
        module = types.ModuleType('_600du_canboot_' + self.spec['toolSha256'][:16])
        module.__file__ = str(self.tool)
        # Execute the already verified bytes, avoiding a second path-based load.
        exec(compile(source, str(self.tool), 'exec'), module.__dict__)
        if not isinstance(getattr(module, 'CanFlasher', None), type) or not isinstance(getattr(module, 'CanSocket', None), type):
            raise FirmwareError('The declared CanBoot tool has no supported CAN transport')
        return module

    def _guarded_flasher(self, module, raw):
        owner = self
        expected_uuid = int(self.spec['canbusUuid'], 16)

        class GuardedFlasher(module.CanFlasher):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.target_verified = self.connected = self.readback_verified = self.finished = False

            async def connect_btl(self):
                reply = await self.send_command('CONNECT')
                if not isinstance(reply, (bytes, bytearray)) or not 13 <= len(reply) <= 256:
                    raise FirmwareError('Invalid CanBoot CONNECT reply')
                _, address, block = struct.unpack('<4sII', reply[:12])
                try:
                    kind = bytes(reply[12:]).rstrip(b'\x00').decode('ascii')
                except UnicodeError as error:
                    raise FirmwareError('Invalid CanBoot MCU identity') from error
                padded_size = ((len(raw) + block - 1) // block) * block if block else 0
                if (address != owner.spec['applicationStart'] or kind != owner.spec['mcuType']
                        or block not in (64, 128, 256, 512) or address + padded_size > owner.spec['flashEnd']):
                    raise FirmwareError('CanBoot MCU, application address, or writable range does not match')
                self.app_start_addr, self.block_size = address, block
                self.connected = True
                owner._record('BOOTLOADER_CONNECTED', mcuType=kind, applicationStart=address, blockSize=block)

            async def verify_canbus_uuid(self, uuid):
                if not self.connected or uuid != expected_uuid:
                    raise FirmwareError('Unexpected CAN target')
                reply = await self.send_command('GET_CANBUS_ID')
                if not isinstance(reply, (bytes, bytearray)) or len(reply) < 6 or int.from_bytes(reply[:6], 'big') != expected_uuid:
                    raise FirmwareError('CanBoot CAN UUID does not match the declared head board')
                self.target_verified = True
                owner._record('TARGET_VERIFIED', uuidVerified=True)

            async def send_command(self, cmdname, payload=b'', tries=5):
                if cmdname == 'SEND_BLOCK':
                    if not self.connected or not self.target_verified:
                        raise FirmwareError('Firmware write attempted before target verification')
                    if not owner.write_started:
                        # The callback must finish its durable write before any
                        # SEND_BLOCK packet, including upstream packet retries.
                        owner.write_started = True
                        owner._record('WRITE_INTENT', writeBoundaryIntent=True)
                        owner.progress(10, 'Writing the verified 600DU head firmware')
                elif cmdname == 'COMPLETE' and not self.readback_verified:
                    raise FirmwareError('Unverified firmware must remain in the bootloader')
                return await super().send_command(cmdname, payload, tries)

            async def send_file(self):
                if not self.connected or not self.target_verified:
                    raise FirmwareError('Firmware target is not verified')
                # Refuse a replaced payload before starting physical writes.
                _validate_bytes(_regular_bytes(owner.image), owner.spec)
                await super().send_file()
                padded = raw + b'\xff' * (-len(raw) % self.block_size)
                if self.block_count != len(padded) // self.block_size or self.fw_sha.digest() != hashlib.sha1(padded).digest():
                    raise FirmwareError('Transferred bytes differ from the verified firmware image')

            async def verify_file(self):
                owner.progress(70, 'Reading back the head firmware for verification')
                await super().verify_file()
                self.readback_verified = owner.verified = True
                owner._record('FLASH_VERIFIED', blockSize=self.block_size, blocks=self.block_count)

            async def finish(self):
                # Upstream calls finish in finally even after a failed write.
                # Returning without COMPLETE leaves that board recoverable.
                if not self.readback_verified or self.finished:
                    return
                owner._record('APPLICATION_START_INTENT')
                await self.send_command('COMPLETE')
                self.finished = owner.completed = True
                owner._record('FLASH_COMPLETE')
                owner.progress(100, 'Head firmware readback verified')

        return GuardedFlasher

    async def _run(self, module):
        connection = None
        try:
            connection = module.CanSocket(asyncio.get_running_loop())
            await connection.run(self.spec['canInterface'], int(self.spec['canbusUuid'], 16), self.image)
            if not self.verified or not self.completed:
                raise FirmwareError('CanBoot did not confirm verified firmware completion')
        except BaseException as error:
            try:
                self._record('RECOVERY_REQUIRED', errorType=type(error).__name__)
            except Exception:
                pass  # Preserve the original transport/persistence failure.
            raise
        finally:
            if connection is not None:
                connection.close()

    def run(self):
        """Flash only the declared target; invoke solely after explicit install."""
        if self._attempted:
            raise FirmwareError('Use a fresh FirmwareUpdate instance for each recovery attempt')
        self._attempted = True
        raw = _regular_bytes(self.image)
        receipt = _validate_bytes(raw, self.spec)
        module = self._load_tool()
        self._module = module
        module.CanFlasher = self._guarded_flasher(module, raw)
        self._record('BOOTLOADER_INTENT', bootloaderMayHaveStarted=True)
        self.progress(0, 'Entering the declared 600DU head bootloader')
        asyncio.run(self._run(module))
        return {**receipt, 'canbusUuid': self.spec['canbusUuid'], 'verified': True, 'complete': True}
