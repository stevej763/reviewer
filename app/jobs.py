"""Background processing: recordings are transcribed, then entries are summarised.

The browser gets an answer as soon as the audio or text is safely stored, so you
can close the page. Work happens one item at a time on a single worker thread
(Whisper and a local model both want the machine to themselves). Status lives in
the database, so anything unfinished is picked up again after a restart.

Entry statuses: transcribing -> summarising -> done, or failed (with an error).

End-of-day summaries (combining a day's entries) are built when you ask for one.
A running summary can be cancelled.
"""

import json
import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from .crypto import Box
from .db import Store
from .llm import Cancelled, Provider, ProviderError

CANCELLED_MESSAGE = "Summary cancelled. Your text is saved; try again whenever you like."

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


def day_entries(store: Store, entry_date: str) -> tuple[list[dict], str]:
    """A day's finished entries (oldest first) and a fingerprint that changes when any of them does."""
    entries = [e for e in store.list_entries(entry_date, entry_date) if e["status"] == "done" and e["text"].strip()]
    entries.sort(key=lambda e: e["id"])
    return entries, json.dumps([[e["id"], e["updated_at"]] for e in entries])


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
        # Cancelling: queued items are skipped; the running one is told to stop via the event.
        self._cancelled: set[tuple[str, int | str]] = set()
        self._current: tuple[str, int | str] | None = None
        self._cancel_event = threading.Event()
        self._progress: dict[int, float] = {}  # entry id -> transcription progress (in memory only)

    def start(self) -> None:
        for entry_id in self._store.pending_entry_ids():
            self.enqueue(entry_id)
        for day in self._store.pending_days():
            self.enqueue_day(day)
        self._thread = threading.Thread(target=self._loop, name="reviewer-worker", daemon=True)
        self._thread.start()

    def enqueue(self, entry_id: int) -> None:
        with self._lock:
            self._cancelled.discard(("entry", entry_id))
        self._queue.put(("entry", entry_id))

    def enqueue_day(self, entry_date: str) -> None:
        """Queue a build of a day's end-of-day summary (no-op if one is already queued)."""
        with self._lock:
            self._cancelled.discard(("day", entry_date))
            if entry_date in self._queued_days:
                return
            self._queued_days.add(entry_date)
        self._queue.put(("day", entry_date))

    def progress(self, entry_id: int) -> float | None:
        """How far through transcription a recording is, or None if it isn't being transcribed now."""
        return self._progress.get(entry_id)

    def cancel(self, kind: str, key) -> None:
        with self._lock:
            self._cancelled.add((kind, key))
            if self._current == (kind, key):
                self._cancel_event.set()

    def _is_cancelled(self, kind: str, key) -> bool:
        with self._lock:
            return (kind, key) in self._cancelled

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
        with self._lock:
            if kind == "day":
                self._queued_days.discard(key)
            self._current = (kind, key)
            self._cancel_event.clear()
        try:
            if kind == "entry":
                self.process(key)
            else:
                self.process_day(key)
        finally:
            with self._lock:
                self._current = None
                self._cancelled.discard((kind, key))

    def check_day(self, entry_date: str) -> None:
        """After entries change: a day left with fewer than two entries has no end-of-day summary."""
        entries, _ = day_entries(self._store, entry_date)
        if len(entries) < 2 and self._store.get_day(entry_date):
            self._store.delete_day(entry_date)

    def _day_cancelled(self, entry_date: str, previous: dict | None) -> None:
        if previous and previous.get("summary"):
            self._store.save_day(entry_date, status="done", error=None)  # keep the last good summary
        else:
            self._store.delete_day(entry_date)

    def process_day(self, entry_date: str) -> None:
        previous = self._store.get_day(entry_date)
        if self._is_cancelled("day", entry_date):
            self._day_cancelled(entry_date, previous)
            return
        entries, fingerprint = day_entries(self._store, entry_date)
        if len(entries) < 2:
            self._store.delete_day(entry_date)  # a single entry is its own summary
            return
        self._store.save_day(entry_date, status="summarising", entry_count=len(entries))
        try:
            provider = self._pick_provider(None)
            summary = provider.summarize_day(entries, entry_date, self._about_me(), cancel=self._cancel_event)
        except Cancelled:
            self._day_cancelled(entry_date, previous)
            return
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
            log.warning("end-of-day summary for %s failed: %s", entry_date, detail)
            self._store.save_day(entry_date, status="failed", error=detail)
            return
        self._store.save_day(entry_date, status="done", error=None, summary=summary, fingerprint=fingerprint,
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
            self._progress[entry_id] = 0.0
            try:
                text = self._transcribe(*audio, on_progress=lambda f: self._progress.__setitem__(entry_id, f))
            except Exception as exc:
                log.exception("transcription failed")
                self._store.set_status(entry_id, "failed", f"Transcription failed: {exc}")
                return
            finally:
                self._progress.pop(entry_id, None)
            if not text.strip():
                self._store.set_status(entry_id, "failed", "No speech was picked up in that recording.")
                return
            combined = "\n\n".join(t for t in (entry["text"].strip(), text.strip()) if t)
            self._store.update_entry(entry_id, text=combined)
            self._store.set_status(entry_id, "summarising")
            self._audio.delete(entry_id)  # audio is never kept once transcribed
            entry = self._store.get_entry(entry_id)

        if entry["status"] == "summarising":
            if self._is_cancelled("entry", entry_id):
                self._store.set_status(entry_id, "failed", CANCELLED_MESSAGE)
                return
            try:
                provider = self._pick_provider(entry["wanted_provider"])
                summary = provider.summarize(entry["text"], entry["date"], self._about_me(),
                                             cancel=self._cancel_event)
            except Cancelled:
                self._store.set_status(entry_id, "failed", CANCELLED_MESSAGE)
                return
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
