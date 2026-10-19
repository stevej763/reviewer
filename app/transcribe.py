"""Local speech-to-text with faster-whisper. Audio never leaves the server and isn't stored."""

import tempfile
import threading

from .config import Settings


class Transcriber:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self._settings.whisper_model,
                    device="cpu",
                    compute_type=self._settings.whisper_compute_type,
                    download_root=str(self._settings.data_dir / "whisper-models"),
                )
        return self._model

    def warm_up(self):
        self._load()

    def transcribe(self, audio: bytes, suffix: str = ".webm") -> str:
        model = self._load()
        # PyAV (bundled with faster-whisper) decodes webm/opus from Chrome and mp4/aac from Safari.
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(audio)
            tmp.flush()
            segments, _info = model.transcribe(
                tmp.name,
                language=self._settings.whisper_language or None,
                vad_filter=True,
                beam_size=5,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
