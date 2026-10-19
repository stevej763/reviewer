"""Infrastructure settings from environment variables.

Everything personal (your profile, PIN, AI model choices) is set in the app's
first-time setup screen and stored in the database instead; see appconfig.py.
The AI-related variables here only pre-fill that screen.
"""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    # Treat "NAME=" (set but empty) the same as unset.
    return os.environ.get(name, "").strip() or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _load_or_create_secret(data_dir: Path) -> str:
    """Use SECRET_KEY if set, otherwise persist a random one in the data dir."""
    configured = _env("SECRET_KEY")
    if configured:
        return configured
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value)
    path.chmod(0o600)
    print(f"[reviewer] Generated an encryption key at {path}. Back it up with your database.")
    return value


@dataclass
class Settings:
    data_dir: Path
    secret_key: str
    cookie_secure: bool
    session_days: int
    max_pin_attempts: int
    lockout_minutes: int

    # Defaults for the setup screen
    default_provider: str
    anthropic_api_key: str
    anthropic_model: str
    anthropic_effort: str
    anthropic_fallbacks: bool
    ollama_url: str
    ollama_model: str

    whisper_model: str
    whisper_language: str
    whisper_compute_type: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reviewer.db"


def load_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        data_dir=data_dir,
        secret_key=_load_or_create_secret(data_dir),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        session_days=int(_env("SESSION_DAYS", "30")),
        max_pin_attempts=int(_env("MAX_PIN_ATTEMPTS", "5")),
        lockout_minutes=int(_env("LOCKOUT_MINUTES", "15")),
        default_provider=_env("LLM_PROVIDER", "ollama").lower(),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=_env("ANTHROPIC_EFFORT", "medium"),
        anthropic_fallbacks=_env_bool("ANTHROPIC_FALLBACKS", True),
        ollama_url=_env("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/"),
        ollama_model=_env("OLLAMA_MODEL", "qwen3:8b"),
        whisper_model=_env("WHISPER_MODEL", "small"),
        whisper_language=_env("WHISPER_LANGUAGE", "en"),
        whisper_compute_type=_env("WHISPER_COMPUTE_TYPE", "int8"),
    )
