"""HTTP API and static web app."""

import json
import logging
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import reports
from .auth import COOKIE_NAME, PinAuth
from .config import Settings, load_settings
from .crypto import Box
from .db import Store
from .llm import Provider, ProviderError, build_providers, normalize_summary
from .prompts import REPORT_STYLES
from .transcribe import Transcriber

log = logging.getLogger("reviewer")
STATIC_DIR = Path(__file__).parent / "static"
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 50_000


def _check_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Dates must look like YYYY-MM-DD") from exc
    return value


class LoginIn(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


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


class SummarizeIn(BaseModel):
    provider: str | None = None


class ReportIn(BaseModel):
    start: str
    end: str
    style: str = "review"
    provider: str | None = None
    notes: str = Field(default="", max_length=2000)

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
               transcriber: Transcriber | None = None) -> FastAPI:
    settings = settings or load_settings()
    box = Box(settings.secret_key)
    store = Store(settings.db_path, box)
    auth = PinAuth(settings.pin, settings.secret_key, settings.session_days,
                   settings.max_pin_attempts, settings.lockout_minutes)
    providers = providers if providers is not None else build_providers(settings)
    transcriber = transcriber or Transcriber(settings)

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
        if not auth.is_valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(401, "Not logged in")

    def pick_provider(name: str | None) -> Provider:
        if not providers:
            raise HTTPException(503, "No AI provider configured. Set ANTHROPIC_API_KEY and/or OLLAMA_URL.")
        name = (name or settings.default_provider).lower()
        if name not in providers:
            raise HTTPException(400, f"Provider '{name}' isn't configured.")
        return providers[name]

    # ---- auth ------------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request):
        return {
            "authenticated": auth.is_valid(request.cookies.get(COOKIE_NAME)),
            "locked_for": auth.locked_for(),
            "providers": [{"name": p.name, "model": p.model} for p in providers.values()],
            "default_provider": settings.default_provider if settings.default_provider in providers else None,
            "report_styles": {k: v["label"] for k, v in REPORT_STYLES.items()},
        }

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        token = auth.check_pin(body.pin.strip())
        response.set_cookie(COOKIE_NAME, token, max_age=auth.max_age, httponly=True,
                            secure=settings.cookie_secure, samesite="strict", path="/")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    # ---- speech ----------------------------------------------------------

    @app.post("/api/transcribe", dependencies=[Depends(require_login)])
    def transcribe(audio: UploadFile = File(...)):
        data = audio.file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Recording is too large.")
        if not data:
            raise HTTPException(400, "Empty recording.")
        content_type = (audio.content_type or "").lower()
        suffix = ".mp4" if "mp4" in content_type or "m4a" in content_type or "aac" in content_type else ".webm"
        if "ogg" in content_type:
            suffix = ".ogg"
        if "wav" in content_type:
            suffix = ".wav"
        try:
            text = transcriber.transcribe(data, suffix=suffix)
        except Exception as exc:  # decoding / model errors
            log.exception("transcription failed")
            raise HTTPException(500, f"Transcription failed: {exc}") from exc
        return {"text": text}

    # ---- entries ---------------------------------------------------------

    def _summarize(entry_id: int, text: str, entry_date: str, provider_name: str | None) -> dict:
        provider = pick_provider(provider_name)
        summary = provider.summarize(text, entry_date)
        return store.update_entry(entry_id, summary=summary, provider=provider.name, model=provider.model)

    @app.get("/api/entries", dependencies=[Depends(require_login)])
    def list_entries(start: str | None = None, end: str | None = None, limit: int | None = None):
        return store.list_entries(start, end, limit)

    @app.post("/api/entries", dependencies=[Depends(require_login)])
    def create_entry(body: EntryIn):
        # Save the raw text first so a failed summary never loses the brain dump.
        entry = store.add_entry(body.date, body.text.strip(), None, None, None)
        try:
            return _summarize(entry["id"], entry["text"], entry["date"], body.provider)
        except (ProviderError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return {**entry, "summary_error": detail}

    @app.get("/api/entries/{entry_id}", dependencies=[Depends(require_login)])
    def get_entry(entry_id: int):
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        return entry

    @app.put("/api/entries/{entry_id}", dependencies=[Depends(require_login)])
    def update_entry(entry_id: int, body: EntryUpdate):
        summary = None
        if body.summary is not None:
            summary = normalize_summary(body.summary)
        entry = store.update_entry(entry_id, entry_date=body.date, text=body.text, summary=summary)
        if not entry:
            raise HTTPException(404, "Entry not found")
        return entry

    @app.post("/api/entries/{entry_id}/summarize", dependencies=[Depends(require_login)])
    def resummarize(entry_id: int, body: SummarizeIn):
        entry = store.get_entry(entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        return _summarize(entry_id, entry["text"], entry["date"], body.provider)

    @app.delete("/api/entries/{entry_id}", dependencies=[Depends(require_login)])
    def delete_entry(entry_id: int):
        if not store.delete_entry(entry_id):
            raise HTTPException(404, "Entry not found")
        return {"ok": True}

    # ---- reports ---------------------------------------------------------

    @app.post("/api/reports", dependencies=[Depends(require_login)])
    def create_report(body: ReportIn):
        if body.start > body.end:
            raise HTTPException(400, "Start date is after end date.")
        entries = store.list_entries(body.start, body.end)
        if not entries:
            raise HTTPException(400, "No log entries in that date range.")
        provider = pick_provider(body.provider)
        system, prompt = reports.report_prompt(entries, body.start, body.end, body.style, body.notes)
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
    app.state.transcriber = transcriber
    return app


def _download(data: bytes, filename: str, media_type: str) -> Response:
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
