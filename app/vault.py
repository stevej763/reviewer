"""The data key and the startup password that protects it.

Everything stored is encrypted with one random data key (see crypto.py). The key itself is
never written down in the clear: keyring.json in the data folder holds it wrapped (AES-GCM)
twice, once under a key derived from your startup password and once under a recovery key
that is shown a single time when you create the password. scrypt makes each password guess
slow. So the app starts locked, and until someone types the password it can't read anything.

Changing the password only re-wraps the data key, so no data is re-encrypted.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import derive_key

KEYRING_FILE = "keyring.json"
# About half a second and 128 MB per guess. Stored with each wrapped key, so changing these
# only affects passwords set afterwards.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1
MIN_PASSWORD = 12
MAX_PASSWORD = 1024
WORDLIST = Path(__file__).parent / "wordlist.txt"

# States, as the web app sees them
SETUP = "setup"        # brand new: create a password
LOCKED = "locked"      # type the password (or recovery key)
UNLOCKED = "unlocked"


class WrongSecret(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _kdf(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=2 * 128 * r * n + 2**24)


def wrap(dek: str, secret: str, slot: str) -> dict:
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = _kdf(secret, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    sealed = AESGCM(key).encrypt(nonce, dek.encode(), slot.encode())
    return {"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": _b64(salt),
            "wrapped": _b64(nonce + sealed)}


def unwrap(entry: dict, secret: str, slot: str) -> str:
    try:
        if entry.get("kdf") != "scrypt":
            raise ValueError
        n, r, p = int(entry["n"]), int(entry["r"]), int(entry["p"])
        if not (2**10 <= n <= 2**20 and 1 <= r <= 32 and 1 <= p <= 4 and 128 * r * n <= 2**28):  # don't let a file make us burn memory
            raise ValueError
        blob = _unb64(entry["wrapped"])
        key = _kdf(secret, _unb64(entry["salt"]), n, r, p)
        return AESGCM(key).decrypt(blob[:12], blob[12:], slot.encode()).decode()
    except (InvalidTag, KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise WrongSecret from None


def fingerprint(dek: str) -> str:
    """Identifies a data key without revealing it (it's random, so a hash can't be reversed)."""
    return derive_key(dek, "keyring-check").hex()[:32]


def new_recovery_key() -> str:
    """25 characters of base32 (125 bits) in groups of five, e.g. ABCDE-FGHJK-…"""
    raw = base64.b32encode(secrets.token_bytes(16)).decode("ascii")[:25]
    return "-".join(raw[i:i + 5] for i in range(0, 25, 5))


def normalize_recovery_key(text: str) -> str:
    raw = re.sub(r"[^A-Z2-7]", "", text.upper())
    return "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))


def open_keyring(keyring: dict, secret: str) -> str:
    """The data key from a keyring (ours, or one inside a backup), given its password or recovery key."""
    slots = keyring.get("slots") if isinstance(keyring, dict) else None
    if not isinstance(slots, dict):
        raise WrongSecret
    candidates = []
    if isinstance(slots.get("password"), dict):
        candidates.append(("password", secret))
    recovery = normalize_recovery_key(secret)
    if isinstance(slots.get("recovery"), dict) and len(recovery) == 29:
        candidates.append(("recovery", recovery))
    for slot, value in candidates:
        try:
            dek = unwrap(slots[slot], value, slot)
        except WrongSecret:
            continue
        if not keyring.get("check") or hmac.compare_digest(fingerprint(dek), str(keyring["check"])):
            return dek
    raise WrongSecret


# ---- password rules -------------------------------------------------------

_COMMON = ("password", "passw0rd", "qwerty", "azerty", "letmein", "welcome", "iloveyou", "admin", "login",
           "monkey", "dragon", "master", "sunshine", "princess", "football", "baseball", "shadow", "secret",
           "braindump", "brain", "dump", "changeme", "trustno1", "abc123", "starwars", "whatever", "freedom")
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890", "abcdefghijklmnopqrstuvwxyz")
_LEET = str.maketrans("013457@$!", "oieastasi")


def _strip_patterns(text: str) -> str:
    text = re.sub(r"(19|20)\d\d", " ", text)  # years
    for row in _ROWS:
        for size in range(len(row), 3, -1):
            for i in range(len(row) - size + 1):
                run = row[i:i + size]
                text = text.replace(run, " ").replace(run[::-1], " ")
    return re.sub(r"(.)\1{2,}", " ", text)  # aaa, 111


def password_problem(password: str) -> str | None:
    """Why a password isn't good enough, or None if it's fine."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters. A few random words work well."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    if password != password.strip():
        return "The password can't start or end with a space."
    text = password.lower()
    if len(set(text)) < 6:
        return "That password repeats itself too much."
    # Take out the predictable parts and see how much is left.
    rest = _strip_patterns(text).translate(_LEET)
    for word in _COMMON:
        rest = rest.replace(word, " ")
    rest = _strip_patterns(rest)
    if len(rest.replace(" ", "")) < 8:
        return "That password is too easy to guess. Try a few random words, or press Suggest."
    return None


def suggest_passphrase(words: int = 5) -> str:
    choices = [w for w in WORDLIST.read_text().split() if w.isalpha()]
    return "-".join(secrets.choice(choices) for _ in range(words))


# ---- the vault ---------------------------------------------------------------

class Vault:
    """Holds the data key once unlocked (in memory only) and manages keyring.json."""

    def __init__(self, data_dir: Path):
        self._dir = data_dir
        self._lock = threading.Lock()
        self._dek: str | None = None
        self._listeners: list = []
        self._read()  # a damaged keyring should stop the app starting, not surface later

    @property
    def keyring_path(self) -> Path:
        return self._dir / KEYRING_FILE

    def _read(self) -> dict | None:
        if not self.keyring_path.exists():
            return None
        data = json.loads(self.keyring_path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("slots"), dict):
            raise RuntimeError(f"{self.keyring_path} is damaged.")
        return data

    def _write(self, keyring: dict) -> None:
        tmp = self.keyring_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(keyring, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.keyring_path)

    @property
    def state(self) -> str:
        if self._dek is not None:
            return UNLOCKED
        if self.keyring_path.exists():
            return LOCKED
        return SETUP

    @property
    def unlocked(self) -> bool:
        return self._dek is not None

    @property
    def has_password(self) -> bool:
        return self.keyring_path.exists()

    @property
    def dek(self) -> str:
        if self._dek is None:
            raise RuntimeError("The app is locked.")
        return self._dek

    def on_unlock(self, callback) -> None:
        """Run `callback(dek)` when the vault unlocks (straight away if it already is)."""
        self._listeners.append(callback)
        if self._dek is not None:
            callback(self._dek)

    def _opened(self, dek: str) -> None:
        first = self._dek is None
        self._dek = dek
        if first:
            for callback in self._listeners:
                callback(dek)

    def create_password(self, password: str) -> str:
        """Make a new random data key, wrap it, and return the recovery key."""
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        with self._lock:
            if self.has_password:
                raise RuntimeError("A startup password is already set.")
            dek = secrets.token_urlsafe(48)
            recovery = new_recovery_key()
            self._write({"format": "brain-dump-keyring", "version": 1, "check": fingerprint(dek),
                         "slots": {"password": wrap(dek, password, "password"),
                                   "recovery": wrap(dek, recovery, "recovery")}})
            # Only once the keyring is safely on disk and opens with the new password.
            if open_keyring(self._read(), password) != dek:
                raise RuntimeError("Couldn't save the startup password.")
        self._opened(dek)
        return recovery

    def unlock(self, password: str) -> None:
        """Unlock with the password or the recovery key. Raises WrongSecret."""
        keyring = self._read()
        if keyring is None:
            raise RuntimeError("No startup password has been set yet.")
        self._opened(open_keyring(keyring, password))

    def recover(self, recovery_key: str, new_password: str) -> None:
        """Forgot the password: unlock with the recovery key and set a new password."""
        problem = password_problem(new_password)
        if problem:
            raise ValueError(problem)
        keyring = self._read()
        if keyring is None:
            raise RuntimeError("No startup password has been set yet.")
        only_recovery = {**keyring, "slots": {"recovery": keyring["slots"].get("recovery")}}
        dek = open_keyring(only_recovery, recovery_key)
        self._opened(dek)
        self.change_password(new_password)

    def unlock_with_key(self, dek: str) -> None:
        """Unlock with the data key itself (the Mac app keeps it in the Keychain behind Touch ID)."""
        keyring = self._read()
        if keyring is None or not hmac.compare_digest(fingerprint(dek), str(keyring.get("check", ""))):
            raise WrongSecret
        self._opened(dek)

    def check_password(self, password: str) -> bool:
        keyring = self._read()
        if keyring is None:
            return False
        try:
            return open_keyring({**keyring, "slots": {"password": keyring["slots"]["password"]}}, password) == self.dek
        except (WrongSecret, KeyError):
            return False

    def change_password(self, new: str) -> None:
        """Re-wrap the data key under a new password (the caller has checked the old one)."""
        problem = password_problem(new)
        if problem:
            raise ValueError(problem)
        with self._lock:
            keyring = self._read()
            keyring["slots"]["password"] = wrap(self.dek, new, "password")
            self._write(keyring)

    def export_keyring(self) -> dict | None:
        """The wrapped keys, for a backup: useless without the password or recovery key."""
        keyring = self._read()
        if keyring is None:
            return None
        return {"check": keyring.get("check"), "slots": keyring["slots"]}
