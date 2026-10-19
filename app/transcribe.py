"""Local speech-to-text with faster-whisper. Audio never leaves the server and isn't stored."""

import tempfile
import threading

from .config import Settings


# Speech models offered in Settings, smallest first. Download sizes are approximate.
MODELS = {
    "base": "Fastest, least accurate (~150 MB)",
    "small": "Good balance; the default (~500 MB)",
    "medium": "More accurate, about 2-3x slower than small (~1.5 GB)",
    "large-v3-turbo": "Most accurate for its speed; recommended for accuracy (~1.6 GB)",
    "large-v3": "Most accurate, slowest; needs plenty of memory (~3 GB)",
}


class Transcriber:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._model = None
        self._model_name = None
        self._lock = threading.Lock()

    def _load(self, name: str | None = None):
        name = name or self._settings.whisper_model
        with self._lock:
            if self._model is None or self._model_name != name:
                from faster_whisper import WhisperModel

                self._model = None  # free the old model before loading a bigger one
                self._model = WhisperModel(
                    name,
                    device="cpu",
                    compute_type=self._settings.whisper_compute_type,
                    download_root=str(self._settings.data_dir / "whisper-models"),
                )
                self._model_name = name
        return self._model

    def warm_up(self, name: str | None = None):
        self._load(name)

    def transcribe(self, audio: bytes, suffix: str = ".webm", language: str | None = None,
                   model_name: str | None = None, on_progress=None) -> str:
        """`on_progress(fraction)` is called as each segment is transcribed (0.0 to 1.0)."""
        model = self._load(model_name)
        # PyAV (bundled with faster-whisper) decodes webm/opus from Chrome and mp4/aac from Safari.
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(audio)
            tmp.flush()
            segments, info = model.transcribe(
                tmp.name,
                language=(language if language is not None else self._settings.whisper_language) or None,
                vad_filter=True,
                beam_size=5,
            )
            # Segments are produced lazily as decoding goes, each with an end time in the
            # recording, so how far through the recording we are is progress.
            texts = []
            for seg in segments:
                texts.append(seg.text.strip())
                if on_progress and info.duration:
                    on_progress(min(seg.end / info.duration, 1.0))
            return " ".join(texts).strip()
