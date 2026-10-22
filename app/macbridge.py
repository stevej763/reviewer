"""The Mac app's private channel to this server: JSON lines over the server's stdin/stdout.

Only the Mac app that started the server holds the other end of these pipes, so this is how
it hands over the data key after Touch ID (never through the environment or a file) and gets
single-use sign-in codes for the PIN screen. Nothing here is reachable over HTTP.

Mac app -> server:  {"type": "unlock", "key": ...}   unlock with the key from the Keychain
                    {"type": "get_key"}              the key, to keep in the Keychain behind Touch ID
                    {"type": "login_code"}           a code the page swaps for a session (/api/mac/login)
Server -> Mac app:  {"type": "state", "state": ..., "password": bool}  on start
                    {"type": "unlocked", "password": bool}
                    {"type": "key", "key": ...}, {"type": "login_code", "code": ...}
                    {"type": "error", "for": ..., "message": ...}
"""

import json
import logging
import threading

from .vault import WrongSecret

log = logging.getLogger("braindump.mac")


class MacBridge:
    def __init__(self, app, out):
        self._vault = app.state.vault
        self._auth = app.state.auth
        self._config = app.state.config
        self._out = out
        self._lock = threading.Lock()

    def send(self, message: dict) -> None:
        with self._lock:
            self._out.write(json.dumps(message) + "\n")
            self._out.flush()

    def start(self) -> None:
        self.send({"type": "state", "state": self._vault.state, "password": self._vault.has_password})
        self._vault.on_unlock(lambda _dek: self.send({"type": "unlocked", "password": self._vault.has_password}))

    def serve(self, lines) -> None:
        for line in lines:
            try:
                self.handle(line)
            except Exception:
                log.exception("Mac app message failed")

    def handle(self, line: str) -> None:
        try:
            message = json.loads(line)
            kind = message["type"]
        except (ValueError, KeyError, TypeError):
            return
        if kind == "unlock":
            try:
                self._vault.unlock_with_key(str(message.get("key", "")))
            except (WrongSecret, RuntimeError):
                # A key from before a reset, say. The Mac app forgets it and asks for the password.
                self.send({"type": "error", "for": "unlock", "message": "That key doesn't fit."})
        elif kind == "get_key":
            if self._vault.unlocked and self._vault.has_password:
                self.send({"type": "key", "key": self._vault.dek})
            else:
                self.send({"type": "error", "for": "get_key", "message": "Not unlocked with a password."})
        elif kind == "login_code":
            if self._vault.unlocked and self._config.is_setup:
                self.send({"type": "login_code", "code": self._auth.issue_code()})
            else:
                self.send({"type": "error", "for": "login_code", "message": "Not ready to sign in."})
