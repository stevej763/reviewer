"""AI providers: a local Ollama model (default) and Claude, behind one interface."""

import json
import threading
import time
from datetime import date

import anthropic
import httpx

from .config import Settings
from .prompts import CATEGORIES, SUMMARY_SCHEMA, SUMMARY_SYSTEM


class ProviderError(RuntimeError):
    """A provider failed in a way worth showing to the user."""


class Provider:
    name: str
    model: str

    def summarize(self, text: str, entry_date: str, about_me: str = "") -> dict:
        raise NotImplementedError

    def write(self, system: str, prompt: str) -> str:
        raise NotImplementedError


def about_me(profile: dict) -> str:
    """A short description of the user from their profile, for prompts."""
    parts = []
    if profile.get("name"):
        parts.append(f"Name: {profile['name']}")
    if profile.get("role"):
        parts.append(f"Role: {profile['role']}")
    if profile.get("company"):
        parts.append(f"Team / company: {profile['company']}")
    if profile.get("goals"):
        parts.append(f"Current goals and what matters in my role: {profile['goals']}")
    return "\n".join(parts)


def _summary_prompt(text: str, entry_date: str, me: str) -> str:
    weekday = date.fromisoformat(entry_date).strftime("%A")
    prefix = f"<about_me>\n{me}\n</about_me>\n\n" if me else ""
    return f"{prefix}Date: {weekday} {entry_date}\n\n<brain_dump>\n{text}\n</brain_dump>"


def normalize_summary(data: dict) -> dict:
    """Coerce model output into the expected shape (local models can be sloppy)."""
    out = {"headline": str(data.get("headline") or "").strip()}
    highlights = []
    for item in data.get("highlights") or []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        category = str(item.get("category") or "other").lower()
        highlights.append({
            "text": str(item["text"]).strip(),
            "category": category if category in CATEGORIES else "other",
            "impact": str(item.get("impact") or "").strip(),
        })
    out["highlights"] = highlights
    for key in ("wins", "projects", "people", "challenges", "follow_ups", "tags"):
        values = data.get(key) or []
        if isinstance(values, str):
            values = [values]
        out[key] = [str(v).strip() for v in values if str(v).strip()]
    out["tags"] = [t.lower() for t in out["tags"]]
    return out


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, api_key: str, model: str, effort: str = "medium", fallbacks: bool = True):
        self.model = model
        self._effort = effort
        self._fallbacks = fallbacks
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=3)

    def _extra(self) -> dict:
        # Server-side fallback: if the model declines (e.g. a safety false positive),
        # the API re-runs the request on a recommended fallback model in the same call.
        if not self._fallbacks:
            return {}
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    @staticmethod
    def _text(response) -> str:
        if response.stop_reason == "refusal":
            raise ProviderError("Claude declined to process this text.")
        if response.stop_reason == "max_tokens":
            raise ProviderError("Claude's response was cut off (max_tokens).")
        return "".join(block.text for block in response.content if block.type == "text")

    def _call(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Claude rejected the API key. Check it in Settings.") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Claude is rate limiting requests. Try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Couldn't reach the Claude API.") from exc

    def summarize(self, text: str, entry_date: str, about_me: str = "") -> dict:
        response = self._call(
            self._client.beta.messages.create,
            model=self.model,
            max_tokens=16000,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": _summary_prompt(text, entry_date, about_me)}],
            output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
            **self._extra(),
        )
        return normalize_summary(json.loads(self._text(response)))

    def write(self, system: str, prompt: str) -> str:
        # Reports can be long, so stream to avoid HTTP timeouts.
        def run(**kwargs):
            with self._client.beta.messages.stream(**kwargs) as stream:
                return stream.get_final_message()

        response = self._call(
            run,
            model=self.model,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "high"},
            **self._extra(),
        )
        return self._text(response).strip()


def _context_size(*texts: str, output_tokens: int) -> int:
    """Pick a context window big enough for the prompt plus the answer.

    Ollama defaults to a small window and silently truncates longer prompts, but a
    huge fixed window wastes memory on a 16 GB laptop, so size it per request.
    """
    needed = sum(len(t) for t in texts) // 3 + output_tokens + 512
    size = 8192
    while size < needed and size < 65536:
        size *= 2
    return size


class OllamaClient:
    """Talks to an Ollama server: chat, list installed models, download models."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._http = httpx.Client(base_url=self.url, timeout=httpx.Timeout(900, connect=5))

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            return self._http.post(path, json=body)
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"Couldn't reach Ollama at {self.url}. Is the Ollama app running on your Mac?") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc

    def models(self) -> list[dict]:
        try:
            resp = self._http.get("/api/tags", timeout=5)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Couldn't reach Ollama at {self.url}: {exc}") from exc
        return [{"name": m["name"], "size": m.get("size", 0)} for m in resp.json().get("models", [])]

    def chat(self, model: str, system: str, prompt: str, fmt: dict | None, output_tokens: int) -> str:
        body = {
            "model": model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_ctx": _context_size(system, prompt, output_tokens=output_tokens)},
            # Reasoning models (qwen3, deepseek-r1...) are much slower with thinking on
            # and don't need it for summarising.
            "think": False,
            "keep_alive": "15m",
        }
        if fmt:
            body["format"] = fmt
        resp = self._post("/api/chat", body)
        if resp.status_code == 400 and "think" in resp.text.lower():
            body.pop("think")  # model doesn't support the think switch
            resp = self._post("/api/chat", body)
        if resp.status_code == 404:
            raise ProviderError(f"Ollama doesn't have the model '{model}' yet. Download it in Settings.")
        if resp.status_code >= 400:
            raise ProviderError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        return resp.json()["message"]["content"]


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, url: str, model: str):
        self.model = model
        self.client = OllamaClient(url)

    def summarize(self, text: str, entry_date: str, about_me: str = "") -> dict:
        system = SUMMARY_SYSTEM + "\n\nRespond with a single JSON object matching the schema."
        raw = self.client.chat(self.model, system, _summary_prompt(text, entry_date, about_me),
                               fmt=SUMMARY_SCHEMA, output_tokens=2048)
        try:
            return normalize_summary(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ProviderError("The local model returned invalid JSON. Try again or use a bigger model.") from exc

    def write(self, system: str, prompt: str) -> str:
        return self.client.chat(self.model, system, prompt, fmt=None, output_tokens=4096).strip()


def build_providers(ai: dict, settings: Settings) -> dict[str, Provider]:
    """Build the providers that are configured in the app's AI settings."""
    providers: dict[str, Provider] = {}
    if ai.get("ollama_url") and ai.get("ollama_model"):
        providers["ollama"] = OllamaProvider(ai["ollama_url"], ai["ollama_model"])
    if ai.get("anthropic_api_key"):
        providers["claude"] = ClaudeProvider(ai["anthropic_api_key"], ai.get("anthropic_model") or settings.anthropic_model,
                                             settings.anthropic_effort, settings.anthropic_fallbacks)
    return providers


class ModelDownloads:
    """Background `ollama pull` with progress, so the UI can show a download bar."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state: dict = {"model": None, "status": "idle", "completed": 0, "total": 0, "error": None}

    def start(self, url: str, model: str) -> dict:
        with self._lock:
            if self.state["status"] == "downloading":
                return dict(self.state)
            self.state = {"model": model, "status": "downloading", "completed": 0, "total": 0, "error": None}
        threading.Thread(target=self._run, args=(url, model), daemon=True).start()
        return dict(self.state)

    def _run(self, url: str, model: str):
        layers: dict[str, tuple[int, int]] = {}
        attempts = 0
        while True:
            try:
                self._pull_once(url, model, layers)
                with self._lock:
                    self.state["status"] = "done"
                return
            except httpx.TransportError as exc:
                # Big downloads sometimes drop mid-stream; Ollama resumes where it left off.
                attempts += 1
                if attempts <= 5:
                    with self._lock:
                        self.state["detail"] = "connection dropped, resuming"
                    time.sleep(3)
                    continue
                error = f"Download kept failing: {exc}"
            except Exception as exc:  # bad model names, Ollama errors
                error = str(exc) or exc.__class__.__name__
            with self._lock:
                self.state["status"] = "error"
                self.state["error"] = error
            return

    def _pull_once(self, url: str, model: str, layers: dict):
        with httpx.stream("POST", f"{url.rstrip('/')}/api/pull", json={"model": model, "stream": True},
                          timeout=httpx.Timeout(None, connect=5)) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise ProviderError(f"Ollama error {resp.status_code}: {resp.text[:200]}")
            for line in resp.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if "error" in event:
                    raise ProviderError(event["error"])
                with self._lock:
                    # Progress arrives per layer; add the layers up for one overall bar.
                    if event.get("digest") and event.get("total"):
                        layers[event["digest"]] = (event.get("completed", 0), event["total"])
                        self.state["completed"] = sum(c for c, _ in layers.values())
                        self.state["total"] = sum(t for _, t in layers.values())
                    self.state["detail"] = event.get("status", "")
