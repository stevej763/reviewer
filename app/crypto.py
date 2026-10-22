"""Encryption at rest for entry and report contents.

One data key feeds two derived keys: one encrypts stored text (Fernet), the other signs
login cookies. The data key is protected by the startup password (see vault.py); losing
both the password and the recovery key means losing the data.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_key(secret: str, purpose: str) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"reviewer-v1", info=purpose.encode())
    return hkdf.derive(secret.encode())


class Box:
    """Seals and opens stored values. Created locked when the key isn't known yet (the app
    starts before the startup password is typed); `unlock` supplies it."""

    def __init__(self, secret: str | None = None):
        self._fernet: Fernet | None = None
        if secret is not None:
            self.unlock(secret)

    def unlock(self, secret: str) -> None:
        self._fernet = Fernet(base64.urlsafe_b64encode(derive_key(secret, "data-encryption")))

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            raise RuntimeError("The app is locked.")
        return self._fernet

    def seal(self, text: str) -> bytes:
        return self.fernet.encrypt(text.encode())

    def seal_bytes(self, data: bytes) -> bytes:
        return self.fernet.encrypt(data)

    def open_bytes(self, blob: bytes) -> bytes:
        try:
            return self.fernet.decrypt(blob)
        except InvalidToken as exc:
            raise RuntimeError("Could not decrypt stored data: the encryption key doesn't match") from exc

    def open(self, blob: bytes) -> str:
        try:
            return self.fernet.decrypt(blob).decode()
        except InvalidToken as exc:
            raise RuntimeError("Could not decrypt stored data: the encryption key doesn't match") from exc
