"""PIN verification and idle-lock state, independent of GTK and printer state."""

import hashlib
import hmac
import math
import re
import secrets
import time


PIN_ITERATIONS = 600000
PIN_SALT_BYTES = 16
PIN_HASH_BYTES = 32
LOCK_TIMEOUT_OPTIONS = (0, 60, 120, 300, 900, 1800, 3600)
MAX_FAILED_ATTEMPTS = 5
COOLDOWN_SECONDS = 30
_PIN_PATTERN = re.compile(r"[0-9]{4,6}\Z")
_HASH_PATTERN = re.compile(r"pbkdf2_sha256\$600000\$([0-9a-f]{32})\$([0-9a-f]{64})\Z")


def is_valid_pin(pin):
    return isinstance(pin, str) and _PIN_PATTERN.fullmatch(pin) is not None


def hash_pin(pin):
    if not is_valid_pin(pin):
        raise ValueError("PIN must contain 4 to 6 ASCII digits")
    salt = secrets.token_bytes(PIN_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, PIN_ITERATIONS, PIN_HASH_BYTES)
    return "pbkdf2_sha256${}${}${}".format(PIN_ITERATIONS, salt.hex(), digest.hex())


def is_valid_pin_hash(encoded_hash):
    return isinstance(encoded_hash, str) and _HASH_PATTERN.fullmatch(encoded_hash) is not None


def verify_pin(pin, encoded_hash):
    if not is_valid_pin(pin) or not is_valid_pin_hash(encoded_hash):
        return False
    match = _HASH_PATTERN.fullmatch(encoded_hash)
    salt, expected = (bytes.fromhex(value) for value in match.groups())
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, PIN_ITERATIONS, PIN_HASH_BYTES)
    return hmac.compare_digest(digest, expected)


def normalize_timeout(timeout):
    # Do not accept rounded floats, signs, or arbitrary user-defined timeouts.
    value = str(timeout)
    if value not in tuple(str(option) for option in LOCK_TIMEOUT_OPTIONS):
        return 0
    return int(value)


class ScreenLockPolicy:
    def __init__(self, pin_hash="", timeout=0, clock=time.monotonic):
        self.pin_hash = str(pin_hash or "")
        self.timeout = normalize_timeout(timeout)
        self._clock = clock
        self._last_activity = self._clock()
        self.failed_attempts = 0
        self._cooldown_until = None
        # A damaged credential must not turn a configured lock off.
        self.locked = self.enabled

    @property
    def enabled(self):
        return bool(self.pin_hash)

    def remaining_cooldown(self):
        if self._cooldown_until is None:
            return 0
        remaining = self._cooldown_until - self._clock()
        if remaining <= 0:
            self._reset_failures()
            return 0
        return int(math.ceil(remaining))

    def note_activity(self):
        self._last_activity = self._clock()

    def should_autolock(self):
        return bool(self.enabled and not self.locked and self.timeout > 0
                    and self._clock() - self._last_activity >= self.timeout)

    def lock(self):
        if self.enabled:
            self.locked = True
        return self.locked

    def unlock(self):
        self.locked = False
        self._reset_failures()
        self.note_activity()

    def record_failure(self):
        if self.remaining_cooldown():
            return
        self.failed_attempts += 1
        if self.failed_attempts >= MAX_FAILED_ATTEMPTS:
            self._cooldown_until = self._clock() + COOLDOWN_SECONDS

    def reset_attempts(self):
        self._reset_failures()

    def configure(self, pin_hash, timeout):
        pin_hash = str(pin_hash or "")
        if pin_hash != self.pin_hash:
            self._reset_failures()
        self.pin_hash = pin_hash
        self.timeout = normalize_timeout(timeout)
        if not self.enabled:
            self.locked = False
            self._reset_failures()
        self.note_activity()

    def _reset_failures(self):
        self.failed_attempts = 0
        self._cooldown_until = None
