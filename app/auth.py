"""PIN login with signed session cookies and an escalating lockout.

This is a single-user app, so failed attempts are counted globally rather than
per IP (IPs are easy to rotate, and behind a proxy they're all the same anyway).
After MAX_PIN_ATTEMPTS wrong PINs the app locks for LOCKOUT_MINUTES, and each
further lockout doubles that, capped at 24 hours. That makes guessing even a
short PIN impractical; a correct PIN resets everything.
"""

import hmac
import time

from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .crypto import derive_key

COOKIE_NAME = "reviewer_session"
MAX_LOCKOUT_SECONDS = 24 * 3600


class PinAuth:
    def __init__(self, pin: str, secret: str, session_days: int, max_attempts: int, lockout_minutes: int,
                 clock=time.monotonic):
        self._pin = pin
        self._serializer = URLSafeTimedSerializer(derive_key(secret, "session-cookie").hex(), salt="session")
        self.max_age = session_days * 86400
        self._max_attempts = max_attempts
        self._base_lockout = lockout_minutes * 60
        self._clock = clock
        self._failures = 0
        self._lockouts = 0
        self._locked_until = 0.0

    def locked_for(self) -> int:
        return max(0, int(self._locked_until - self._clock()))

    def check_pin(self, pin: str) -> str:
        """Return a session token, or raise 401 (wrong) / 429 (locked)."""
        wait = self.locked_for()
        if wait:
            raise HTTPException(429, f"Too many wrong PINs. Try again in {_human(wait)}.")
        if hmac.compare_digest(pin.encode(), self._pin.encode()):
            self._failures = 0
            self._lockouts = 0
            return self._serializer.dumps({"ok": True})
        self._failures += 1
        if self._failures >= self._max_attempts:
            duration = min(self._base_lockout * (2 ** self._lockouts), MAX_LOCKOUT_SECONDS)
            self._locked_until = self._clock() + duration
            self._lockouts += 1
            self._failures = 0
            raise HTTPException(429, f"Too many wrong PINs. Locked for {_human(duration)}.")
        left = self._max_attempts - self._failures
        raise HTTPException(401, f"Wrong PIN. {left} attempt{'s' if left != 1 else ''} left.")

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            self._serializer.loads(token, max_age=self.max_age)
            return True
        except (BadSignature, SignatureExpired):
            return False


def _human(seconds: int) -> str:
    if seconds >= 3600:
        hours = round(seconds / 3600, 1)
        return f"{hours:g} hour{'s' if hours != 1 else ''}"
    minutes = max(1, -(-seconds // 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
