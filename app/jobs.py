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
import time
from collections.abc import Callable
from pathlib import Path

from .crypto import Box
from .db import Store
from .llm import Cancelled, Provider, ProviderError
from .timing import Timings

# A recording that hasn't sent a chunk for this long is treated as interrupted.
SALVAGE_AFTER_SECONDS = 10 * 60

CANCELLED_MESSAGE = "Summary cancelled. Your text is saved; try again whenever you like."

log = logging.getLogger("braindump.jobs")


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
        self.discard_chunks(entry_id)

    # A recording in progress arrives as numbered chunks, each stored (encrypted) as it lands,
    # so whatever has been uploaded survives a closed tab, a locked phone or a dropped connection.

    def _chunk_dir(self, entry_id: int) -> Path:
        return self.root / f"{entry_id}.chunks"

    def begin(self, entry_id: int, suffix: str) -> None:
        folder = self._chunk_dir(entry_id)
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder / "format").write_text(suffix)

    def chunk_format(self, entry_id: int) -> str:
        path = self._chunk_dir(entry_id) / "format"
        return path.read_text().strip() if path.exists() else ".webm"

    def save_chunk(self, entry_id: int, seq: int, data: bytes) -> None:
        folder = self._chunk_dir(entry_id)
        folder.mkdir(mode=0o700, exist_ok=True)
        tmp = folder / f"{seq:06d}.tmp"
        tmp.write_bytes(self._box.seal_bytes(data))
        tmp.chmod(0o600)
        tmp.replace(folder / f"{seq:06d}.part")  # atomic, so a half-written chunk never counts

    def chunk_numbers(self, entry_id: int) -> list[int]:
        folder = self._chunk_dir(entry_id)
        if not folder.exists():
            return []
        return sorted(int(p.stem) for p in folder.glob("*.part"))

    def last_chunk_time(self, entry_id: int) -> float | None:
        folder = self._chunk_dir(entry_id)
        if not folder.exists():
            return None
        times = [p.stat().st_mtime for p in folder.glob("*.part")]
        return max(times) if times else folder.stat().st_mtime

    def assemble(self, entry_id: int, suffix: str | None = None) -> int:
        """Join the chunks (in order, stopping at the first gap) into one recording. Returns bytes kept."""
        folder = self._chunk_dir(entry_id)
        data = bytearray()
        for expected, seq in enumerate(self.chunk_numbers(entry_id)):
            if seq != expected:
                break  # MediaRecorder chunks only decode in order, so later ones are unusable
            data += self._box.open_bytes((folder / f"{seq:06d}.part").read_bytes())
        if data:
            self.save(entry_id, bytes(data), suffix or self.chunk_format(entry_id))
        self.discard_chunks(entry_id)
        return len(data)

    def discard_chunks(self, entry_id: int) -> None:
        folder = self._chunk_dir(entry_id)
        if folder.exists():
            for p in folder.iterdir():
                p.unlink(missing_ok=True)
            folder.rmdir()


def day_entries(store: Store, entry_date: str) -> tuple[list[dict], str]:
    """A day's finished entries (oldest first) and a fingerprint that changes when any of them does."""
    entries = [e for e in store.list_entries(entry_date, entry_date) if e["status"] == "done" and e["text"].strip()]
    entries.sort(key=lambda e: e["id"])
    return entries, json.dumps([[e["id"], e["updated_at"]] for e in entries])


class Worker:
    def __init__(self, store: Store, audio: AudioStore, transcribe: Callable[[bytes, str], str],
                 pick_provider: Callable[[str | None], Provider], about_me: Callable[[], str],
                 speech_model: Callable[[], str] = lambda: "small"):
        self._store = store
        self._speech_model = speech_model
        self.timings = Timings(store)
        # The job being worked on now, for time-left estimates: key, start time, estimated seconds.
        self._running: dict | None = None
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
        self._finish_lock = threading.Lock()
        self._progress: dict[int, float] = {}  # entry id -> transcription progress (in memory only)

    def resume(self) -> None:
        """Queue whatever was unfinished when the app last stopped (once it's unlocked)."""
        for entry_id in self._store.pending_entry_ids():
            self.enqueue(entry_id)
        for day in self._store.pending_days():
            self.enqueue_day(day)

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="braindump-worker", daemon=True)
        self._thread.start()
        threading.Thread(target=self._salvage_loop, name="braindump-salvage", daemon=True).start()

    def _salvage_loop(self) -> None:
        while True:
            try:
                self.salvage_interrupted()
            except Exception:
                log.exception("salvaging interrupted recordings failed")
            time.sleep(60)

    def finish_recording(self, entry_id: int) -> dict:
        """Join a chunked recording's uploaded pieces and queue it for transcription."""
        with self._finish_lock:  # the app finishing and the salvage sweep can race
            entry = self._store.get_entry(entry_id)
            if not entry or entry["status"] != "recording":
                return entry
            kept = self._audio.assemble(entry_id)
            if kept:
                self._store.set_status(entry_id, "transcribing")
            else:
                self._store.set_status(entry_id, "failed",
                                       "The recording stopped before any audio reached the server.")
        if kept:
            self.enqueue(entry_id)
        return self._store.get_entry(entry_id)

    def salvage_interrupted(self, idle_seconds: int = SALVAGE_AFTER_SECONDS) -> list[int]:
        """Recordings whose upload stopped (tab closed, phone locked, connection lost) and never
        finished: transcribe whatever arrived, so nothing that reached the server is lost."""
        salvaged = []
        for entry_id in self._store.entry_ids_with_status("recording"):
            last = self._audio.last_chunk_time(entry_id)
            if last is None or time.time() - last >= idle_seconds:
                self.finish_recording(entry_id)
                salvaged.append(entry_id)
        return salvaged

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

    def _begin(self, kind: str, key, estimate: float | None) -> None:
        self._running = {"key": (kind, key), "started": time.monotonic(), "estimate": estimate}

    def _end(self, timing_kind: str, model: str, size: float) -> None:
        running, self._running = self._running, None
        if running:
            self.timings.record(timing_kind, model, size, time.monotonic() - running["started"])

    def eta(self, kind: str, key) -> int | None:
        """Estimated seconds left for the job running now, or None if it isn't running.
        0 means it's taking longer than estimated."""
        running = self._running
        if not running or running["key"] != (kind, key) or running["estimate"] is None:
            return None
        elapsed = time.monotonic() - running["started"]
        fraction = self._progress.get(key) if kind == "entry" else None
        if fraction and fraction > 0.1 and elapsed > 5:
            remaining = elapsed / fraction - elapsed  # transcription: extrapolate from real progress
        else:
            remaining = running["estimate"] - elapsed
        return max(0, round(remaining))  # 0 = running over the estimate

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
        size = self.timings.summary_size("".join(e["text"] for e in entries))
        try:
            provider = self._pick_provider(None)
            model = f"{provider.name}:{provider.model}"
            self._begin("day", entry_date, self.timings.estimate("summary", model, size))
            summary = provider.summarize_day(entries, entry_date, self._about_me(), cancel=self._cancel_event)
            self._end("summary", model, size)
        except Cancelled:
            self._running = None
            self._day_cancelled(entry_date, previous)
            return
        except Exception as exc:
            self._running = None
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
            speech_model = self._speech_model()
            duration = {}

            def on_progress(fraction: float, seconds: float | None = None):
                # The first call comes once the model is loaded and the audio length is known,
                # so model download/loading time doesn't skew the timing history.
                if seconds and "audio" not in duration:
                    duration["audio"] = seconds
                    self._begin("entry", entry_id, self.timings.estimate("transcribe", speech_model, seconds))
                self._progress[entry_id] = fraction

            try:
                text = self._transcribe(*audio, on_progress=on_progress)
                if duration:
                    self._end("transcribe", speech_model, duration["audio"])
            except Exception as exc:
                log.exception("transcription failed")
                self._store.set_status(entry_id, "failed", f"Transcription failed: {exc}")
                return
            finally:
                if self._running and self._running["key"] == ("entry", entry_id):
                    self._running = None
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
            size = self.timings.summary_size(entry["text"])
            try:
                provider = self._pick_provider(entry["wanted_provider"])
                model = f"{provider.name}:{provider.model}"
                self._begin("entry", entry_id, self.timings.estimate("summary", model, size))
                summary = provider.summarize(entry["text"], entry["date"], self._about_me(),
                                             cancel=self._cancel_event)
                self._end("summary", model, size)
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
            finally:
                if self._running and self._running["key"] == ("entry", entry_id):
                    self._running = None
            self._store.update_entry(entry_id, summary=summary, provider=provider.name, model=provider.model)
            self._store.set_status(entry_id, "done")
