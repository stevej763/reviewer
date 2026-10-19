"""Background processing: recordings are transcribed, then entries are summarised.

The browser gets an answer as soon as the audio or text is safely stored, so you
can close the page. Work happens one item at a time on a single worker thread
(Whisper and a local model both want the machine to themselves). Status lives in
the database, so anything unfinished is picked up again after a restart.

Entry statuses: transcribing -> summarising -> done, or failed (with an error).

When a day has two or more finished entries, an end-of-day summary combining them is
(re)built after any of that day's entries changes.
"""

import json
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
        self._queue: queue.Queue[tuple[str, int | str]] = queue.Queue()
        self._queued_days: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for entry_id in self._store.pending_entry_ids():
            self.enqueue(entry_id)
        for day in self._store.pending_days() + self._store.days_needing_summary():
            self.enqueue_day(day)
        self._thread = threading.Thread(target=self._loop, name="reviewer-worker", daemon=True)
        self._thread.start()

    def enqueue(self, entry_id: int) -> None:
        self._queue.put(("entry", entry_id))

    def enqueue_day(self, entry_date: str, force: bool = False) -> None:
        """Queue a rebuild of a day's end-of-day summary (no-op if one is already queued)."""
        with self._lock:
            if entry_date in self._queued_days:
                return
            self._queued_days.add(entry_date)
        self._queue.put(("day-force" if force else "day", entry_date))

    def drain(self) -> None:
        """Process everything queued right now, on the calling thread (used by tests)."""
        while not self._queue.empty():
            self._run(*self._queue.get())

    def _loop(self) -> None:
        while True:
            kind, key = self._queue.get()
            try:
                self._run(kind, key)
            except Exception:  # never let one bad item kill the worker
                log.exception("processing %s %s failed", kind, key)

    def _run(self, kind: str, key) -> None:
        if kind == "entry":
            self.process(key)
            entry = self._store.get_entry(key)
            if entry and entry["status"] in ("done", "failed"):
                self.enqueue_day(entry["date"])
        else:
            with self._lock:
                self._queued_days.discard(key)
            self.process_day(key, force=kind == "day-force")

    def process_day(self, entry_date: str, force: bool = False) -> None:
        entries = [e for e in self._store.list_entries(entry_date, entry_date)
                   if e["status"] == "done" and e["text"].strip()]
        if len(entries) < 2:
            self._store.delete_day(entry_date)  # a single entry is its own summary
            return
        entries.sort(key=lambda e: e["id"])
        fingerprint = json.dumps([[e["id"], e["updated_at"]] for e in entries])
        current = self._store.get_day(entry_date)
        if not force and current and current["status"] == "done" and current["fingerprint"] == fingerprint:
            return  # nothing changed since the last build
        self._store.save_day(entry_date, status="summarising", entry_count=len(entries))
        try:
            provider = self._pick_provider(None)
            summary = provider.summarize_day(entries, entry_date, self._about_me())
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
            log.warning("end-of-day summary for %s failed: %s", entry_date, detail)
            self._store.save_day(entry_date, status="failed", error=detail)
            return
        self._store.save_day(entry_date, status="done", summary=summary, fingerprint=fingerprint,
                             entry_count=len(entries), provider=provider.name, model=provider.model)

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
