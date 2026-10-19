"""Background processing: recordings are transcribed, then entries are summarised.

The browser gets an answer as soon as the audio or text is safely stored, so you
can close the page. Work happens one item at a time on a single worker thread
(Whisper and a local model both want the machine to themselves). Status lives in
the database, so anything unfinished is picked up again after a restart.

Entry statuses: transcribing -> summarising -> done, or failed (with an error).
"""

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from .crypto import Box
from .db import Store
from .llm import Provider, ProviderError

log = logging.getLogger("reviewer.jobs")


class AudioStore:
    """Encrypted audio waiting to be transcribed. Deleted once transcribed."""

    def __init__(self, root: Path, box: Box):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._box = box

    def _path(self, entry_id: int) -> Path:
        return self.root / f"{entry_id}.audio"

    def save(self, entry_id: int, data: bytes, suffix: str) -> None:
        # Keep the container format (.webm/.mp4...) so the decoder can recognise it.
        blob = self._box.seal_bytes(suffix.encode().ljust(8, b" ") + data)
        path = self._path(entry_id)
        path.write_bytes(blob)
        path.chmod(0o600)

    def load(self, entry_id: int) -> tuple[bytes, str] | None:
        path = self._path(entry_id)
        if not path.exists():
            return None
        raw = self._box.open_bytes(path.read_bytes())
        return raw[8:], raw[:8].decode().strip()

    def exists(self, entry_id: int) -> bool:
        return self._path(entry_id).exists()

    def delete(self, entry_id: int) -> None:
        self._path(entry_id).unlink(missing_ok=True)


class Worker:
    def __init__(self, store: Store, audio: AudioStore, transcribe: Callable[[bytes, str], str],
                 pick_provider: Callable[[str | None], Provider], about_me: Callable[[], str]):
        self._store = store
        self._audio = audio
        self._transcribe = transcribe
        self._pick_provider = pick_provider
        self._about_me = about_me
        self._queue: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for entry_id in self._store.pending_entry_ids():
            self._queue.put(entry_id)
        self._thread = threading.Thread(target=self._loop, name="reviewer-worker", daemon=True)
        self._thread.start()

    def enqueue(self, entry_id: int) -> None:
        self._queue.put(entry_id)

    def drain(self) -> None:
        """Process everything queued right now, on the calling thread (used by tests)."""
        while not self._queue.empty():
            self.process(self._queue.get())

    def _loop(self) -> None:
        while True:
            entry_id = self._queue.get()
            try:
                self.process(entry_id)
            except Exception:  # never let one bad entry kill the worker
                log.exception("processing entry %s failed", entry_id)

    def process(self, entry_id: int) -> None:
        entry = self._store.get_entry(entry_id)
        if not entry:
            self._audio.delete(entry_id)
            return

        if entry["status"] == "transcribing":
            audio = self._audio.load(entry_id)
            if audio is None:
                self._store.set_status(entry_id, "failed", "The recording is missing, so it can't be transcribed.")
                return
            try:
                text = self._transcribe(*audio)
            except Exception as exc:
                log.exception("transcription failed")
                self._store.set_status(entry_id, "failed", f"Transcription failed: {exc}")
                return
            if not text.strip():
                self._store.set_status(entry_id, "failed", "No speech was picked up in that recording.")
                return
            combined = "\n\n".join(t for t in (entry["text"].strip(), text.strip()) if t)
            self._store.update_entry(entry_id, text=combined)
            self._store.set_status(entry_id, "summarising")
            self._audio.delete(entry_id)  # audio is never kept once transcribed
            entry = self._store.get_entry(entry_id)

        if entry["status"] == "summarising":
            try:
                provider = self._pick_provider(entry["wanted_provider"])
                summary = provider.summarize(entry["text"], entry["date"], self._about_me())
            except ProviderError as exc:
                self._store.set_status(entry_id, "failed", str(exc))
                return
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
                log.exception("summary failed")
                self._store.set_status(entry_id, "failed", f"Summary failed: {detail}")
                return
            self._store.update_entry(entry_id, summary=summary, provider=provider.name, model=provider.model)
            self._store.set_status(entry_id, "done")
