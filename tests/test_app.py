import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import PinAuth
from app.config import load_settings
from app.llm import Provider, ProviderError, normalize_summary
from app.main import create_app

PIN = "246810"


class FakeProvider(Provider):
    def __init__(self, name="claude", fail=False):
        self.name = name
        self.model = f"fake-{name}"
        self.fail = fail
        self.prompts = []

    def summarize(self, text, entry_date):
        if self.fail:
            raise ProviderError("boom")
        return normalize_summary({
            "headline": f"Did things on {entry_date}",
            "highlights": [{"text": text[:40], "category": "delivery", "impact": "Unblocked the team"}],
            "wins": ["Shipped"], "projects": ["Apollo"], "people": ["Sam"],
            "challenges": [], "follow_ups": ["Write docs"], "tags": ["Release"],
        })

    def write(self, system, prompt):
        self.prompts.append(prompt)
        return "# My Review\n\n## Delivery\n\n- **Shipped** Apollo <script>alert(1)</script>\n"


class FakeTranscriber:
    def transcribe(self, audio, suffix=".webm"):
        return f"heard {len(audio)} bytes as {suffix}"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(providers=None):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("APP_PIN", PIN)
        monkeypatch.setenv("SECRET_KEY", "test-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        settings = load_settings()
        app = create_app(settings, providers=providers if providers is not None else {"claude": FakeProvider()},
                         transcriber=FakeTranscriber())
        return TestClient(app), settings
    return _make


@pytest.fixture
def client(make_client):
    c, _ = make_client()
    assert c.post("/api/login", json={"pin": PIN}).status_code == 200
    return c


def test_requires_login(make_client):
    c, _ = make_client()
    assert c.get("/api/entries").status_code == 401
    assert c.post("/api/login", json={"pin": "000000"}).status_code == 401
    assert c.post("/api/login", json={"pin": PIN}).status_code == 200
    assert c.get("/api/entries").status_code == 200
    c.post("/api/logout")
    assert c.get("/api/entries").status_code == 401


def test_lockout_escalates_and_blocks_correct_pin():
    now = [0.0]
    auth = PinAuth("1234", "s", 30, max_attempts=3, lockout_minutes=1, clock=lambda: now[0])
    for _ in range(2):
        with pytest.raises(Exception) as exc:
            auth.check_pin("0000")
        assert exc.value.status_code == 401
    with pytest.raises(Exception) as exc:
        auth.check_pin("0000")
    assert exc.value.status_code == 429
    with pytest.raises(Exception) as exc:  # locked even with the right PIN
        auth.check_pin("1234")
    assert exc.value.status_code == 429
    now[0] += 61
    for _ in range(3):
        with pytest.raises(Exception):
            auth.check_pin("0000")
    assert auth.locked_for() == 120  # doubled
    now[0] += 121
    assert auth.is_valid(auth.check_pin("1234"))


def test_entry_is_summarised_and_encrypted_at_rest(client, make_client):
    r = client.post("/api/entries", json={"date": "2026-10-19", "text": "Fixed the flaky deploy pipeline secretword"})
    assert r.status_code == 200
    entry = r.json()
    assert entry["summary"]["headline"] == "Did things on 2026-10-19"
    assert entry["summary"]["tags"] == ["release"]
    assert entry["provider"] == "claude"

    db = sqlite3.connect(client.app.state.store.path)
    raw = b"".join(bytes(row[0]) + bytes(row[1]) for row in db.execute("SELECT raw_text, summary FROM entries"))
    assert b"secretword" not in raw and b"Apollo" not in raw


def test_failed_summary_still_saves_text(make_client):
    c, _ = make_client({"claude": FakeProvider(fail=True)})
    c.post("/api/login", json={"pin": PIN})
    entry = c.post("/api/entries", json={"date": "2026-10-19", "text": "Important stuff"}).json()
    assert entry["summary"] is None
    assert entry["summary_error"] == "boom"
    assert c.get("/api/entries").json()[0]["text"] == "Important stuff"


def test_switch_provider_and_resummarise(make_client):
    c, _ = make_client({"claude": FakeProvider("claude"), "ollama": FakeProvider("ollama")})
    c.post("/api/login", json={"pin": PIN})
    entry = c.post("/api/entries", json={"date": "2026-10-19", "text": "x", "provider": "ollama"}).json()
    assert entry["model"] == "fake-ollama"
    redo = c.post(f"/api/entries/{entry['id']}/summarize", json={"provider": "claude"}).json()
    assert redo["model"] == "fake-claude"
    assert c.post(f"/api/entries/{entry['id']}/summarize", json={"provider": "nope"}).status_code == 400


def test_edit_filter_delete(client):
    a = client.post("/api/entries", json={"date": "2026-10-01", "text": "a"}).json()
    client.post("/api/entries", json={"date": "2026-10-15", "text": "b"})
    assert client.put(f"/api/entries/{a['id']}", json={"text": "a2", "date": "2026-10-02"}).json()["text"] == "a2"
    in_range = client.get("/api/entries", params={"start": "2026-10-01", "end": "2026-10-10"}).json()
    assert [e["text"] for e in in_range] == ["a2"]
    assert client.delete(f"/api/entries/{a['id']}").status_code == 200
    assert len(client.get("/api/entries").json()) == 1
    assert client.post("/api/entries", json={"date": "19/10/2026", "text": "x"}).status_code == 422


def test_report_and_exports(client):
    client.post("/api/entries", json={"date": "2026-10-01", "text": "Shipped Apollo"})
    client.post("/api/entries", json={"date": "2026-10-05", "text": "Mentored Sam"})
    assert client.post("/api/reports", json={"start": "2025-01-01", "end": "2025-02-01"}).status_code == 400

    report = client.post("/api/reports", json={"start": "2026-10-01", "end": "2026-10-31", "style": "brag",
                                               "notes": "promotion"}).json()
    assert report["title"] == "My Review"
    full = client.get(f"/api/reports/{report['id']}").json()
    assert "<strong>Shipped</strong>" in full["html"]
    assert "<script>" not in full["html"]

    md = client.get(f"/api/reports/{report['id']}/export", params={"format": "md"})
    assert md.text.startswith("# My Review")
    docx = client.get(f"/api/reports/{report['id']}/export", params={"format": "docx"})
    assert docx.content[:2] == b"PK"
    html = client.get(f"/api/reports/{report['id']}/export", params={"format": "html"})
    assert "<title>My Review</title>" in html.text

    csv = client.get("/api/export/highlights.csv").text.splitlines()
    assert csv[0].startswith("date,category,highlight")
    assert len(csv) == 3
    assert client.get("/api/reports").json()[0]["id"] == report["id"]


def test_report_prompt_contains_entries(make_client):
    fake = FakeProvider()
    c, _ = make_client({"claude": fake})
    c.post("/api/login", json={"pin": PIN})
    c.post("/api/entries", json={"date": "2026-10-01", "text": "Shipped Apollo"})
    c.post("/api/reports", json={"start": "2026-10-01", "end": "2026-10-31", "notes": "focus on leadership"})
    prompt = fake.prompts[0]
    assert "2026-10-01" in prompt and "Unblocked the team" in prompt and "focus on leadership" in prompt


def test_transcribe(client):
    r = client.post("/api/transcribe", files={"audio": ("r.mp4", b"abc", "audio/mp4")})
    assert r.json() == {"text": "heard 3 bytes as .mp4"}


def test_security_headers_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "Reviewer" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200


def test_normalize_summary_handles_sloppy_output():
    s = normalize_summary({"headline": "h", "highlights": ["did x", {"text": "y", "category": "WEIRD"}, {}],
                           "tags": "Solo"})
    assert [h["category"] for h in s["highlights"]] == ["other", "other"]
    assert s["tags"] == ["solo"] and s["wins"] == []
