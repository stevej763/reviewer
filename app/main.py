"""HTTP API and static web app."""

import json
import logging
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import reports
from .appconfig import PROVIDERS, AppConfig, hash_pin
from .auth import COOKIE_NAME, PinAuth
from .config import Settings, load_settings
from .crypto import Box
from .db import Store
from .jobs import AudioStore, Worker, day_entries
from .llm import (ModelDownloads, OllamaClient, Provider, ProviderError, about_me, build_providers,
                  normalize_summary)
from .prompts import REPORT_STYLES
from .transcribe import MODELS as SPEECH_MODELS
from .transcribe import Transcriber

log = logging.getLogger("reviewer")
STATIC_DIR = Path(__file__).parent / "static"
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_CHUNK_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 50_000


def _check_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Dates must look like YYYY-MM-DD") from exc
    return value


class LoginIn(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


def _check_pin(value: str) -> str:
    value = value.strip()
    if not value.isdigit() or not 4 <= len(value) <= 12:
        raise ValueError("PIN must be 4 to 12 digits")
    return value


class ProfileIn(BaseModel):
    name: str = Field(default="", max_length=100)
    role: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    goals: str = Field(default="", max_length=2000)
    context: str = Field(default="", max_length=6000)


class AiIn(BaseModel):
    provider: str = "ollama"
    ollama_url: str = Field(default="", max_length=300)
    ollama_model: str = Field(default="", max_length=200)
    anthropic_api_key: str | None = Field(default=None, max_length=300)  # None = keep current
    anthropic_model: str = Field(default="", max_length=100)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v):
        if v not in PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
        return v

    @field_validator("ollama_url")
    @classmethod
    def _url(cls, v):
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("Ollama address must start with http:// or https://")
        return v


class SpeechIn(BaseModel):
    language: str = Field(default="en", max_length=10)
    model: str = "small"

    @field_validator("model")
    @classmethod
    def _model(cls, v):
        if v not in SPEECH_MODELS:
            raise ValueError(f"speech model must be one of {', '.join(SPEECH_MODELS)}")
        return v


class SetupIn(BaseModel):
    profile: ProfileIn
    pin: str
    ai: AiIn
    speech: SpeechIn = SpeechIn()

    @field_validator("pin")
    @classmethod
    def _pin(cls, v):
        return _check_pin(v)


class SettingsIn(BaseModel):
    profile: ProfileIn | None = None
    ai: AiIn | None = None
    speech: SpeechIn | None = None


class PinChangeIn(BaseModel):
    current: str = Field(max_length=64)
    new: str

    @field_validator("new")
    @classmethod
    def _pin(cls, v):
        return _check_pin(v)


class OllamaIn(BaseModel):
    url: str = Field(max_length=300)
    model: str | None = Field(default=None, max_length=200)


class EntryIn(BaseModel):
    date: str
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    provider: str | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _check_date(v)


class EntryUpdate(BaseModel):
    date: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_CHARS)
    summary: dict | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _check_date(v) if v else v


class RecordingStartIn(BaseModel):
    date: str
    provider: str | None = None
    mime: str = Field(default="", max_length=100)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _check_date(v)


class RecordingFinishIn(BaseModel):
    chunks: int = Field(ge=0)
    partial: bool = False  # process what arrived even if pieces are missing


class SummarizeIn(BaseModel):
    provider: str | None = None


class ReportIn(BaseModel):
    start: str
    end: str
    style: str = "review"
    provider: str | None = None
    notes: str = Field(default="", max_length=6000)

    @field_validator("start", "end")
    @classmethod
    def _dates(cls, v):
        return _check_date(v)

    @field_validator("style")
    @classmethod
    def _style(cls, v):
        if v not in REPORT_STYLES:
            raise ValueError(f"style must be one of {', '.join(REPORT_STYLES)}")
        return v


def create_app(settings: Settings | None = None, providers: dict[str, Provider] | None = None,
               transcriber: Transcriber | None = None, background: bool = True) -> FastAPI:
    """`providers` overrides the configured AI providers; `background=False` leaves queued
    work for the caller to run with app.state.worker.drain() (both used by tests)."""
    settings = settings or load_settings()
    box = Box(settings.secret_key)
    store = Store(settings.db_path, box)
    config = AppConfig(store, settings)
    auth = PinAuth(config.check_pin, lambda: (config.get().get("pin_hash") or "")[-16:], settings.secret_key,
                   settings.session_days, settings.max_pin_attempts, settings.lockout_minutes)
    transcriber = transcriber or Transcriber(settings)
    downloads = ModelDownloads()
    provider_cache: dict = {"version": None, "providers": {}}

    def current_providers() -> dict[str, Provider]:
        if providers is not None:
            return providers
        if provider_cache["version"] != config.version:
            provider_cache["providers"] = build_providers(config.get()["ai"], settings)
            provider_cache["version"] = config.version
        return provider_cache["providers"]

    def default_provider() -> str | None:
        available = current_providers()
        preferred = config.get()["ai"]["provider"]
        if preferred in available:
            return preferred
        return next(iter(available), None)

    app = FastAPI(title="Reviewer", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            # Revalidate (cheap, via ETag) so updates show up after a rebuild.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(_request: Request, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    def require_login(request: Request):
        if not config.is_setup:
            raise HTTPException(409, "Finish setting up the app first.")
        if not auth.is_valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(401, "Not logged in")

    def pick_provider(name: str | None) -> Provider:
        available = current_providers()
        if not available:
            raise HTTPException(503, "No AI model is set up yet. Add one in Settings.")
        name = (name or default_provider() or "").lower()
        if name not in available:
            raise HTTPException(400, f"The '{name}' model isn't set up. Check Settings.")
        return available[name]

    def me() -> str:
        return about_me(config.get()["profile"])

    def set_session_cookie(response: Response, token: str):
        response.set_cookie(COOKIE_NAME, token, max_age=auth.max_age, httponly=True,
                            secure=settings.cookie_secure, samesite="strict", path="/")

    # ---- auth ------------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request):
        setup_done = config.is_setup
        authenticated = setup_done and auth.is_valid(request.cookies.get(COOKIE_NAME))
        info = {
            "setup_required": not setup_done,
            "authenticated": authenticated,
            "locked_for": auth.locked_for(),
            "report_styles": {k: v["label"] for k, v in REPORT_STYLES.items()},
            "speech_models": SPEECH_MODELS,
        }
        if not setup_done:
            info["defaults"] = config.public()  # pre-fills the setup screen
        if authenticated:
            info["providers"] = [{"name": p.name, "model": p.model} for p in current_providers().values()]
            info["default_provider"] = default_provider()
            info["name"] = config.get()["profile"]["name"]
        return info

    # ---- first-time setup and settings ------------------------------------

    @app.post("/api/setup")
    def setup(body: SetupIn, response: Response):
        if config.is_setup:
            raise HTTPException(409, "Already set up. Log in instead.")
        ai = body.ai.model_dump()
        if ai["anthropic_api_key"] is None:
            ai.pop("anthropic_api_key")
        config.update({"profile": body.profile.model_dump(), "ai": ai, "speech": body.speech.model_dump(),
                       "pin_hash": hash_pin(body.pin)})
        set_session_cookie(response, auth.issue_token())
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(require_login)])
    def get_settings():
        return config.public()

    @app.put("/api/settings", dependencies=[Depends(require_login)])
    def put_settings(body: SettingsIn):
        changes = {}
        if body.profile:
            changes["profile"] = body.profile.model_dump()
        if body.ai:
            ai = body.ai.model_dump()
            if ai["anthropic_api_key"] is None:
                ai.pop("anthropic_api_key")  # leave the saved key alone
            changes["ai"] = ai
        if body.speech:
            changes["speech"] = body.speech.model_dump()
        config.update(changes)
        return config.public()

    @app.post("/api/settings/pin", dependencies=[Depends(require_login)])
    def change_pin(body: PinChangeIn, response: Response):
        if not config.check_pin(body.current.strip()):
            raise HTTPException(400, "Current PIN is wrong.")
        config.update({"pin_hash": hash_pin(body.new)})
        # Other devices are signed out; keep this one signed in.
        set_session_cookie(response, auth.issue_token())
        return {"ok": True}

    def require_setup_or_login(request: Request):
        # Ollama helpers are used by the setup screen (before there's a PIN) and by Settings.
        if config.is_setup:
            require_login(request)

    @app.post("/api/ollama/models", dependencies=[Depends(require_setup_or_login)])
    def ollama_models(body: OllamaIn):
        return {"models": OllamaClient(body.url).models()}

    @app.post("/api/ollama/pull", dependencies=[Depends(require_setup_or_login)])
    def ollama_pull(body: OllamaIn):
        if not body.model:
            raise HTTPException(400, "Choose a model to download.")
        return downloads.start(body.url, body.model)

    @app.get("/api/ollama/pull", dependencies=[Depends(require_setup_or_login)])
    def ollama_pull_status():
        return downloads.state

    # ---- auth ------------------------------------------------------------

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        if not config.is_setup:
            raise HTTPException(409, "Finish setting up the app first.")
        set_session_cookie(response, auth.check_pin(body.pin.strip()))
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    # ---- background processing ------------------------------------------

    audio_store = AudioStore(settings.data_dir / "pending-audio", box)
    worker = Worker(
        store, audio_store,
        transcribe=lambda data, suffix, on_progress=None: transcriber.transcribe(
            data, suffix=suffix, language=config.get()["speech"]["language"],
            model_name=config.get()["speech"]["model"], on_progress=on_progress),
        pick_provider=pick_provider,
        about_me=me,
        speech_model=lambda: config.get()["speech"]["model"],
    )
    if background:
        worker.start()

    def queue_summary(entry_id: int, provider: str | None) -> dict:
        store.set_status(entry_id, "summarising", wanted_provider=provider or "")
        worker.enqueue(entry_id)
        return store.get_entry(entry_id)

    # ---- speech ----------------------------------------------------------

    def audio_suffix(content_type: str) -> str:
        content_type = (content_type or "").lower()
        if any(t in content_type for t in ("mp4", "m4a", "aac")):
            return ".mp4"
        if "ogg" in content_type:
            return ".ogg"
        if "wav" in content_type:
            return ".wav"
        return ".webm"

    # Chunked recordings: the app starts one, uploads a piece every few seconds while you talk,
    # then finishes it. Each piece is stored as it arrives.

    @app.post("/api/recordings/start", dependencies=[Depends(require_login)])
    def start_recording(body: RecordingStartIn):
        entry = store.add_entry(body.date, "", None, None, None, status="recording",
                                wanted_provider=body.provider or "")
        audio_store.begin(entry["id"], audio_suffix(body.mime))
        return entry

    def recording_entry(entry_id: int) -> dict:
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Recording not found")
        if entry["status"] != "recording":
            raise HTTPException(409, "This recording has already been finished.")
        return entry

    @app.put("/api/recordings/{entry_id}/chunks/{seq}", dependencies=[Depends(require_login)])
    async def upload_chunk(entry_id: int, seq: int, request: Request):
        recording_entry(entry_id)
        if not 0 <= seq < 100_000:
            raise HTTPException(400, "Bad chunk number")
        data = await request.body()
        if len(data) > MAX_CHUNK_BYTES:
            raise HTTPException(413, "Chunk is too large.")
        if data:
            audio_store.save_chunk(entry_id, seq, data)  # re-sending the same chunk just overwrites it
        return {"ok": True, "seq": seq}

    @app.post("/api/recordings/{entry_id}/finish", status_code=202, dependencies=[Depends(require_login)])
    def finish_recording(entry_id: int, body: RecordingFinishIn):
        recording_entry(entry_id)
        have = set(audio_store.chunk_numbers(entry_id))
        missing = [n for n in range(body.chunks) if n not in have]
        if missing and not body.partial:
            raise HTTPException(409, {"message": "Some pieces haven't arrived yet.", "missing": missing})
        return worker.finish_recording(entry_id)

    @app.post("/api/recordings", status_code=202, dependencies=[Depends(require_login)])
    def upload_recording(audio: UploadFile = File(...), date: str = Form(...), provider: str | None = Form(None)):
        """Store a recording and return straight away; it's transcribed and summarised in the background."""
        try:
            _check_date(date)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        data = audio.file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Recording is too large.")
        if not data:
            raise HTTPException(400, "Empty recording.")
        suffix = audio_suffix(audio.content_type)
        entry = store.add_entry(date, "", None, None, None, status="transcribing", wanted_provider=provider or "")
        audio_store.save(entry["id"], data, suffix)
        worker.enqueue(entry["id"])
        return entry

    # ---- entries ---------------------------------------------------------

    def with_progress(entry: dict) -> dict:
        if entry["status"] == "transcribing":
            entry["progress"] = worker.progress(entry["id"])  # None = waiting its turn / loading model
        if entry["status"] in ("transcribing", "summarising"):
            entry["eta_seconds"] = worker.eta("entry", entry["id"])  # None = not started yet
        return entry

    @app.get("/api/entries", dependencies=[Depends(require_login)])
    def list_entries(start: str | None = None, end: str | None = None, limit: int | None = None):
        return [with_progress(e) for e in store.list_entries(start, end, limit)]

    @app.post("/api/entries", status_code=202, dependencies=[Depends(require_login)])
    def create_entry(body: EntryIn):
        # Saved first, summarised in the background, so the brain dump is never lost.
        entry = store.add_entry(body.date, body.text.strip(), None, None, None)
        return queue_summary(entry["id"], body.provider)

    @app.get("/api/entries/{entry_id}", dependencies=[Depends(require_login)])
    def get_entry(entry_id: int):
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        return with_progress(entry)

    @app.put("/api/entries/{entry_id}", dependencies=[Depends(require_login)])
    def update_entry(entry_id: int, body: EntryUpdate):
        summary = None
        if body.summary is not None:
            summary = normalize_summary(body.summary)
        before = store.get_entry(entry_id)
        entry = store.update_entry(entry_id, entry_date=body.date, text=body.text, summary=summary)
        if not entry:
            raise HTTPException(404, "Entry not found")
        for day in {before["date"], entry["date"]}:
            worker.check_day(day)
        return entry

    @app.post("/api/entries/{entry_id}/summarize", dependencies=[Depends(require_login)])
    def resummarize(entry_id: int, body: SummarizeIn):
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        if entry["status"] in ("transcribing", "summarising"):
            raise HTTPException(409, "This entry is still being processed.")
        if not entry["text"].strip():
            raise HTTPException(400, "This entry has no text to summarise.")
        return queue_summary(entry_id, body.provider)

    @app.post("/api/entries/{entry_id}/cancel", dependencies=[Depends(require_login)])
    def cancel_entry(entry_id: int):
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        if entry["status"] != "summarising":
            raise HTTPException(409, "Only a summary that's being written can be cancelled.")
        worker.cancel("entry", entry_id)
        return {"ok": True}

    @app.post("/api/entries/{entry_id}/retry", dependencies=[Depends(require_login)])
    def retry_entry(entry_id: int):
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        if entry["status"] != "failed":
            raise HTTPException(409, "Only failed entries can be retried.")
        if audio_store.exists(entry_id):
            store.set_status(entry_id, "transcribing")
            worker.enqueue(entry_id)
            return store.get_entry(entry_id)
        return queue_summary(entry_id, entry["wanted_provider"])

    @app.delete("/api/entries/{entry_id}", dependencies=[Depends(require_login)])
    def delete_entry(entry_id: int):
        entry = store.get_entry(entry_id)
        if not entry or not store.delete_entry(entry_id):
            raise HTTPException(404, "Entry not found")
        audio_store.delete(entry_id)
        worker.check_day(entry["date"])
        return {"ok": True}

    # ---- end-of-day summaries --------------------------------------------

    def _day_out(day: dict) -> dict:
        out = {k: v for k, v in day.items() if k != "fingerprint"}
        if day["status"] == "summarising":
            out["eta_seconds"] = worker.eta("day", day["date"])
        # Stale = entries were added, edited or removed since it was written.
        out["stale"] = day["status"] == "done" and day_entries(store, day["date"])[1] != day["fingerprint"]
        return out

    @app.get("/api/days", dependencies=[Depends(require_login)])
    def list_days(start: str | None = None, end: str | None = None):
        return [_day_out(d) for d in store.list_days(start, end)]

    @app.get("/api/days/{entry_date}", dependencies=[Depends(require_login)])
    def get_day(entry_date: str):
        day = store.get_day(entry_date)
        if not day:
            raise HTTPException(404, "No end-of-day summary for that date")
        return _day_out(day)

    @app.post("/api/days/{entry_date}/generate", dependencies=[Depends(require_login)])
    def generate_day(entry_date: str):
        """Build (or rebuild) the end-of-day summary. Only runs when you ask for it."""
        try:
            _check_date(entry_date)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        entries, _ = day_entries(store, entry_date)
        if len(entries) < 2:
            raise HTTPException(400, "An end-of-day summary needs at least two finished entries.")
        store.save_day(entry_date, status="summarising", error=None, entry_count=len(entries))
        worker.enqueue_day(entry_date)
        return _day_out(store.get_day(entry_date))

    @app.post("/api/days/{entry_date}/cancel", dependencies=[Depends(require_login)])
    def cancel_day(entry_date: str):
        day = store.get_day(entry_date)
        if not day or day["status"] != "summarising":
            raise HTTPException(409, "That end-of-day summary isn't running.")
        worker.cancel("day", entry_date)
        return {"ok": True}

    # ---- reports ---------------------------------------------------------

    @app.post("/api/reports", dependencies=[Depends(require_login)])
    def create_report(body: ReportIn):
        if body.start > body.end:
            raise HTTPException(400, "Start date is after end date.")
        if body.style == "custom" and not body.notes.strip():
            raise HTTPException(400, "Write your prompt for the custom report.")
        # Skip recordings that haven't been transcribed yet.
        entries = [e for e in store.list_entries(body.start, body.end) if e["text"].strip()]
        if not entries:
            raise HTTPException(400, "No log entries in that date range.")
        provider = pick_provider(body.provider)
        system, prompt = reports.report_prompt(entries, body.start, body.end, body.style, body.notes, me())
        content = provider.write(system, prompt)
        fallback_title = f"{REPORT_STYLES[body.style]['label']}: {body.start} to {body.end}"
        title = reports.title_from_markdown(content, fallback_title)
        return store.add_report(body.start, body.end, body.style, title, content, provider.name, provider.model)

    @app.get("/api/reports", dependencies=[Depends(require_login)])
    def list_reports():
        return store.list_reports()

    @app.get("/api/reports/{report_id}", dependencies=[Depends(require_login)])
    def get_report(report_id: int):
        report = store.get_report(report_id)
        if not report:
            raise HTTPException(404, "Report not found")
        return {**report, "html": reports.render_markdown(report["content"])}

    @app.delete("/api/reports/{report_id}", dependencies=[Depends(require_login)])
    def delete_report(report_id: int):
        if not store.delete_report(report_id):
            raise HTTPException(404, "Report not found")
        return {"ok": True}

    @app.get("/api/reports/{report_id}/export", dependencies=[Depends(require_login)])
    def export_report(report_id: int, format: str = "md"):
        report = store.get_report(report_id)
        if not report:
            raise HTTPException(404, "Report not found")
        stem = f"report-{report['start']}-to-{report['end']}-{report['style']}"
        if format == "md":
            return _download(report["content"].encode(), f"{stem}.md", "text/markdown; charset=utf-8")
        if format == "html":
            page = reports.to_html(report["title"], report["content"])
            return _download(page.encode(), f"{stem}.html", "text/html; charset=utf-8")
        if format == "docx":
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            return _download(reports.to_docx(report["content"]), f"{stem}.docx", mime)
        raise HTTPException(400, "format must be md, html or docx")

    @app.get("/api/export/highlights.csv", dependencies=[Depends(require_login)])
    def export_csv(start: str | None = None, end: str | None = None):
        data = reports.highlights_csv(store.list_entries(start, end))
        name = f"highlights-{start or 'all'}-to-{end or 'now'}.csv"
        return _download(data.encode(), name, "text/csv; charset=utf-8")

    @app.get("/api/export/backup.json", dependencies=[Depends(require_login)])
    def export_backup():
        payload = {"entries": store.list_entries(), "reports": [store.get_report(r["id"]) for r in store.list_reports()]}
        return _download(json.dumps(payload, indent=2).encode(), "reviewer-backup.json", "application/json")

    # ---- static web app --------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.state.store = store
    app.state.config = config
    app.state.worker = worker
    app.state.transcriber = transcriber
    return app


def _download(data: bytes, filename: str, media_type: str) -> Response:
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
