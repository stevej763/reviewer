"""AI providers: Claude (default) and a local Ollama model, behind one interface."""

import json
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

    def summarize(self, text: str, entry_date: str) -> dict:
        raise NotImplementedError

    def write(self, system: str, prompt: str) -> str:
        raise NotImplementedError


def _summary_prompt(text: str, entry_date: str) -> str:
    weekday = date.fromisoformat(entry_date).strftime("%A")
    return f"Date: {weekday} {entry_date}\n\n<brain_dump>\n{text}\n</brain_dump>"


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

    def __init__(self, settings: Settings):
        self.model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._fallbacks = settings.anthropic_fallbacks
        self._client = anthropic.Anthropic(max_retries=3)

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
            raise ProviderError("Claude rejected the API key. Check ANTHROPIC_API_KEY.") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Claude is rate limiting requests. Try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Couldn't reach the Claude API.") from exc

    def summarize(self, text: str, entry_date: str) -> dict:
        response = self._call(
            self._client.beta.messages.create,
            model=self.model,
            max_tokens=16000,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": _summary_prompt(text, entry_date)}],
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


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, settings: Settings):
        self.model = settings.ollama_model
        self._client = httpx.Client(base_url=settings.ollama_url, timeout=httpx.Timeout(600, connect=10))

    def _chat(self, system: str, prompt: str, fmt: dict | None = None) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_ctx": 32768},
        }
        if fmt:
            body["format"] = fmt
        try:
            resp = self._client.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Couldn't reach Ollama: {exc}") from exc
        if resp.status_code == 404:
            raise ProviderError(f"Ollama doesn't have model '{self.model}'. Run: ollama pull {self.model}")
        if resp.status_code >= 400:
            raise ProviderError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        return resp.json()["message"]["content"]

    def summarize(self, text: str, entry_date: str) -> dict:
        system = SUMMARY_SYSTEM + "\n\nRespond with a single JSON object matching the schema."
        raw = self._chat(system, _summary_prompt(text, entry_date), fmt=SUMMARY_SCHEMA)
        try:
            return normalize_summary(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ProviderError("The local model returned invalid JSON. Try again or use a bigger model.") from exc

    def write(self, system: str, prompt: str) -> str:
        return self._chat(system, prompt).strip()


def build_providers(settings: Settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if "claude" in settings.providers:
        providers["claude"] = ClaudeProvider(settings)
    if "ollama" in settings.providers:
        providers["ollama"] = OllamaProvider(settings)
    return providers
