"""User-level configuration chosen in the setup screen, stored encrypted in the database."""

import copy
import hashlib
import hmac
import secrets
import threading

from .config import Settings
from .db import Store

KEY = "app"
PROVIDERS = ("ollama", "claude")


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


class AppConfig:
    """Thread-safe access to the stored config, with env-based defaults."""

    def __init__(self, store: Store, settings: Settings):
        self._store = store
        self._settings = settings
        self._lock = threading.Lock()
        self.version = 0  # bumps on every save so cached providers can rebuild

    def defaults(self) -> dict:
        s = self._settings
        return {
            "profile": {"name": "", "role": "", "company": "", "goals": "", "context": ""},
            "pin_hash": None,
            "ai": {
                "provider": s.default_provider if s.default_provider in PROVIDERS else "ollama",
                "ollama_url": s.ollama_url,
                "ollama_model": s.ollama_model,
                "anthropic_api_key": s.anthropic_api_key,
                "anthropic_model": s.anthropic_model,
            },
            "speech": {"language": s.whisper_language},
        }

    def get(self) -> dict:
        stored = self._store.get_setting(KEY) or {}
        merged = self.defaults()
        for section, values in stored.items():
            if isinstance(values, dict) and isinstance(merged.get(section), dict):
                merged[section].update(values)
            else:
                merged[section] = values
        return merged

    @property
    def is_setup(self) -> bool:
        return bool(self.get().get("pin_hash"))

    def update(self, changes: dict) -> dict:
        with self._lock:
            current = self.get()
            for section, values in changes.items():
                if isinstance(values, dict) and isinstance(current.get(section), dict):
                    current[section].update(values)
                else:
                    current[section] = values
            self._store.set_setting(KEY, current)
            self.version += 1
            return current

    def check_pin(self, pin: str) -> bool:
        stored = self.get().get("pin_hash")
        return bool(stored) and verify_pin(pin, stored)

    def public(self) -> dict:
        """Config safe to send to the browser (no PIN hash, API key masked)."""
        cfg = copy.deepcopy(self.get())
        cfg.pop("pin_hash", None)
        key = cfg["ai"].pop("anthropic_api_key", "")
        cfg["ai"]["anthropic_key_set"] = bool(key)
        cfg["ai"]["anthropic_key_hint"] = f"…{key[-4:]}" if len(key) > 8 else ""
        return cfg
