"""PIN login with signed session cookies and an escalating lockout.

This is a single-user app, so failed attempts are counted globally rather than
per IP (IPs are easy to rotate, and behind a proxy they're all the same anyway).
After MAX_PIN_ATTEMPTS wrong PINs the app locks for LOCKOUT_MINUTES, and each
further lockout doubles that, capped at 24 hours. That makes guessing even a
short PIN impractical; a correct PIN resets everything. The startup password
(vault.py) gets its own counter of the same kind.
"""

import secrets
import time
from collections.abc import Callable

from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .crypto import derive_key

COOKIE_NAME = "reviewer_session"
MAX_LOCKOUT_SECONDS = 24 * 3600


class Lockout:
    """Counts wrong guesses and locks for a while after too many, doubling each time."""

    def __init__(self, max_attempts: int, lockout_minutes: float, what: str, clock=time.monotonic):
        self._max_attempts = max_attempts
        self._base_lockout = lockout_minutes * 60
        self._what = what
        self._clock = clock
        self._failures = 0
        self._lockouts = 0
        self._locked_until = 0.0

    def locked_for(self) -> int:
        return max(0, int(self._locked_until - self._clock()))

    def check(self) -> None:
        """Raise 429 while locked."""
        wait = self.locked_for()
        if wait:
            raise HTTPException(429, f"Too many wrong {self._what}s. Try again in {_human(wait)}.")

    def succeeded(self) -> None:
        self._failures = 0
        self._lockouts = 0

    def failed(self, status: int = 401) -> HTTPException:
        """Count a wrong guess and return the error to raise: `status` (wrong) or 429 (now locked)."""
        self._failures += 1
        if self._failures >= self._max_attempts:
            duration = min(self._base_lockout * (2 ** self._lockouts), MAX_LOCKOUT_SECONDS)
            self._locked_until = self._clock() + duration
            self._lockouts += 1
            self._failures = 0
            return HTTPException(429, f"Too many wrong {self._what}s. Locked for {_human(int(duration))}.")
        left = self._max_attempts - self._failures
        return HTTPException(status, f"Wrong {self._what}. {left} attempt{'s' if left != 1 else ''} left.")


class PinAuth:
    def __init__(self, verify: Callable[[str], bool], epoch: Callable[[], str], secret: str | None,
                 session_days: int, max_attempts: int, lockout_minutes: int, clock=time.monotonic):
        self._verify = verify
        # Sessions are tied to the current PIN, so changing the PIN signs out every device.
        self._epoch = epoch
        self._serializer: URLSafeTimedSerializer | None = None
        if secret is not None:
            self.set_secret(secret)
        self.max_age = session_days * 86400
        self.lockout = Lockout(max_attempts, lockout_minutes, "PIN", clock)
        self._clock = clock
        self._codes: dict[str, float] = {}  # one-time Mac app login codes -> expiry

    def set_secret(self, secret: str) -> None:
        """Cookies are signed with a key derived from the data key, so they survive restarts
        but can't be checked (or forged) until the app is unlocked."""
        self._serializer = URLSafeTimedSerializer(derive_key(secret, "session-cookie").hex(), salt="session")

    def locked_for(self) -> int:
        return self.lockout.locked_for()

    def check_pin(self, pin: str) -> str:
        """Return a session token, or raise 401 (wrong) / 429 (locked)."""
        self.lockout.check()
        if self._verify(pin):
            self.lockout.succeeded()
            return self.issue_token()
        raise self.lockout.failed()

    def issue_code(self, seconds: int = 60) -> str:
        """A single-use code the Mac app swaps for a session after Touch ID."""
        now = self._clock()
        self._codes = {c: t for c, t in self._codes.items() if t > now}
        code = secrets.token_urlsafe(32)
        self._codes[code] = now + seconds
        return code

    def redeem_code(self, code: str) -> str:
        expires = self._codes.pop(code, None) if code else None
        if expires is None or expires < self._clock():
            raise HTTPException(401, "That sign-in code has expired. Try again.")
        return self.issue_token()

    def issue_token(self) -> str:
        if self._serializer is None:
            raise HTTPException(423, "Locked")
        return self._serializer.dumps({"epoch": self._epoch()})

    def is_valid(self, token: str | None, idle_seconds: int = 0) -> bool:
        """`idle_seconds` > 0 also rejects a token that hasn't been refreshed (see issue_token)
        within that long, which is how auto-lock is enforced on the server."""
        if not token or self._serializer is None:
            return False
        max_age = min(self.max_age, idle_seconds) if idle_seconds > 0 else self.max_age
        try:
            data = self._serializer.loads(token, max_age=max_age)
        except (BadSignature, SignatureExpired):
            return False
        return isinstance(data, dict) and bool(data.get("epoch")) and data["epoch"] == self._epoch()


def _human(seconds: int) -> str:
    if seconds >= 3600:
        hours = round(seconds / 3600, 1)
        return f"{hours:g} hour{'s' if hours != 1 else ''}"
    minutes = max(1, -(-seconds // 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
