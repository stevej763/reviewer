"""Encryption at rest for entry and report contents.

One SECRET_KEY feeds two derived keys: one encrypts stored text (Fernet),
the other signs login cookies. Losing SECRET_KEY means losing the data.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_key(secret: str, purpose: str) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"reviewer-v1", info=purpose.encode())
    return hkdf.derive(secret.encode())


class Box:
    def __init__(self, secret: str):
        self._fernet = Fernet(base64.urlsafe_b64encode(derive_key(secret, "data-encryption")))

    def seal(self, text: str) -> bytes:
        return self._fernet.encrypt(text.encode())

    def open(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken as exc:
            raise RuntimeError("Could not decrypt stored data: SECRET_KEY has changed?") from exc
