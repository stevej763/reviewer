"""Learns how fast this machine transcribes and summarises, to estimate time left.

Each finished job records its speed: seconds per second of audio for transcription,
seconds per character of text for summaries, kept per model (the last 20 runs). An
estimate is the median of those speeds times the size of the new job. Until a model
has history, rough defaults are used.
"""

import statistics
import threading

from .db import Store

KEY = "timings"
KEEP = 20
# Text overhead in characters, so short entries don't predict near-zero time
# (prompt processing and model warm-up cost something regardless of length).
SUMMARY_OVERHEAD_CHARS = 1500

# Seconds of work per second of audio on a typical CPU-only server.
DEFAULT_TRANSCRIBE = {"base": 0.25, "small": 0.75, "medium": 2.0, "large-v3-turbo": 1.6, "large-v3": 4.0}
# Seconds per character of text (plus overhead) for a summary.
DEFAULT_SUMMARY = {"ollama": 0.006, "claude": 0.004}


class Timings:
    def __init__(self, store: Store):
        self._store = store
        self._lock = threading.Lock()

    def _history(self) -> dict:
        return self._store.get_setting(KEY) or {}

    @staticmethod
    def _key(kind: str, model: str) -> str:
        return f"{kind}:{model}"

    def record(self, kind: str, model: str, size: float, seconds: float) -> None:
        if size <= 0 or seconds <= 0:
            return
        with self._lock:
            history = self._history()
            rates = history.get(self._key(kind, model), [])
            rates = (rates + [seconds / size])[-KEEP:]
            history[self._key(kind, model)] = rates
            self._store.set_setting(KEY, history)

    def estimate(self, kind: str, model: str, size: float) -> float:
        rates = self._history().get(self._key(kind, model))
        if rates:
            rate = statistics.median(rates)
        elif kind == "transcribe":
            rate = DEFAULT_TRANSCRIBE.get(model, 1.0)
        else:
            rate = DEFAULT_SUMMARY.get(model.split(":", 1)[0], 0.006)
        return rate * size

    @staticmethod
    def summary_size(text: str) -> int:
        return len(text) + SUMMARY_OVERHEAD_CHARS
