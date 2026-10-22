import json
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

    def summarize(self, text, entry_date, about_me="", system=None, cancel=None):
        self.prompts.append(about_me)
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
    def transcribe(self, audio, suffix=".webm", language=None, model_name=None, on_progress=None):
        return f"heard {len(audio)} bytes as {suffix} in {language}"


class DrainingClient(TestClient):
    """Runs queued background work after every request, so tests see finished results."""

    def request(self, *args, **kwargs):
        response = super().request(*args, **kwargs)
        self.app.state.worker.drain()
        return response


def entry(c, entry_id):
    return c.get(f"/api/entries/{entry_id}").json()


SETUP = {
    "profile": {"name": "Steve", "role": "Engineer", "company": "Acme", "goals": "Get promoted",
                "context": "PSP = payment service provider"},
    "pin": PIN,
    "ai": {"provider": "ollama", "ollama_url": "http://host.docker.internal:11434", "ollama_model": "qwen3:8b",
           "anthropic_api_key": "sk-ant-secret-1234", "anthropic_model": "claude-opus-5-5"},
    "speech": {"language": "en"},
}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(providers=None, setup=True):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SECRET_KEY", "test-secret")
        settings = load_settings()
        app = create_app(settings, providers=providers if providers is not None else {"claude": FakeProvider()},
                         transcriber=FakeTranscriber(), background=False)
        c = DrainingClient(app)
        if setup:
            assert c.post("/api/setup", json=SETUP).status_code == 200
            c.cookies.clear()
        return c, settings
    return _make


@pytest.fixture
def client(make_client):
    c, _ = make_client()
    assert c.post("/api/login", json={"pin": PIN}).status_code == 200
    return c


def test_first_run_setup(make_client):
    c, _ = make_client(setup=False)
    s = c.get("/api/session").json()
    assert s["setup_required"] and s["defaults"]["ai"]["ollama_model"] == "qwen3:8b"
    assert c.post("/api/login", json={"pin": PIN}).status_code == 409
    assert c.get("/api/entries").status_code == 409
    bad = {**SETUP, "pin": "12ab"}
    assert c.post("/api/setup", json=bad).status_code == 422
    assert c.post("/api/setup", json=SETUP).status_code == 200
    assert c.get("/api/entries").status_code == 200  # setup logs you in
    assert c.get("/api/session").json()["name"] == "Steve"
    assert c.post("/api/setup", json=SETUP).status_code == 409  # can't redo setup

    # PIN is stored hashed and the API key encrypted, never in plain text
    db = sqlite3.connect(c.app.state.store.path)
    blob = b"".join(bytes(r[0]) for r in db.execute("SELECT value FROM settings"))
    assert PIN.encode() not in blob and b"sk-ant" not in blob
    assert PIN not in c.app.state.config.get()["pin_hash"]


def test_settings_hide_secrets_and_update(client):
    cfg = client.get("/api/settings").json()
    assert "pin_hash" not in cfg and "anthropic_api_key" not in cfg["ai"]
    assert cfg["ai"]["anthropic_key_set"] and cfg["ai"]["anthropic_key_hint"] == "…1234"
    new_ai = {**SETUP["ai"], "anthropic_api_key": None, "ollama_model": "gemma3:4b"}
    cfg = client.put("/api/settings", json={"ai": new_ai, "profile": {"name": "S"}}).json()
    assert cfg["ai"]["ollama_model"] == "gemma3:4b" and cfg["ai"]["anthropic_key_set"]  # key kept
    assert cfg["profile"]["name"] == "S" and cfg["profile"]["role"] == ""


def test_change_pin_signs_out_other_devices(make_client):
    c1, _ = make_client()
    c1.post("/api/login", json={"pin": PIN})
    c2 = DrainingClient(c1.app)
    c2.post("/api/login", json={"pin": PIN})
    assert c1.post("/api/settings/pin", json={"current": "000000", "new": "112233"}).status_code == 400
    assert c1.post("/api/settings/pin", json={"current": PIN, "new": "112233"}).status_code == 200
    assert c1.get("/api/entries").status_code == 200
    assert c2.get("/api/entries").status_code == 401
    assert c2.post("/api/login", json={"pin": PIN}).status_code == 401
    assert c2.post("/api/login", json={"pin": "112233"}).status_code == 200


def test_profile_reaches_prompts(make_client):
    fake = FakeProvider()
    c, _ = make_client({"claude": fake})
    c.post("/api/login", json={"pin": PIN})
    c.post("/api/entries", json={"date": "2026-10-01", "text": "x"})
    assert "Role: Engineer" in fake.prompts[0] and "Get promoted" in fake.prompts[0]
    assert "PSP = payment service provider" in fake.prompts[0]


def test_build_providers_from_settings():
    from app.llm import build_providers
    s = load_settings()
    assert set(build_providers({"ollama_url": "http://x", "ollama_model": "m"}, s)) == {"ollama"}
    both = build_providers({"ollama_url": "http://x", "ollama_model": "m", "anthropic_api_key": "k",
                            "anthropic_model": "claude-opus-5-5"}, s)
    assert set(both) == {"ollama", "claude"}


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
    auth = PinAuth(lambda p: p == "1234", lambda: "epoch", "s", 30, max_attempts=3, lockout_minutes=1,
                   clock=lambda: now[0])
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
    assert r.status_code == 202 and r.json()["status"] == "summarising"
    entry = client.get(f"/api/entries/{r.json()['id']}").json()
    assert entry["status"] == "done"
    assert entry["summary"]["headline"] == "Did things on 2026-10-19"
    assert entry["summary"]["tags"] == ["release"]
    assert entry["provider"] == "claude"

    db = sqlite3.connect(client.app.state.store.path)
    raw = b"".join(bytes(row[0]) + bytes(row[1]) for row in db.execute("SELECT raw_text, summary FROM entries"))
    assert b"secretword" not in raw and b"Apollo" not in raw


def test_failed_summary_still_saves_text(make_client):
    c, _ = make_client({"claude": FakeProvider(fail=True)})
    c.post("/api/login", json={"pin": PIN})
    e = entry(c, c.post("/api/entries", json={"date": "2026-10-19", "text": "Important stuff"}).json()["id"])
    assert e["summary"] is None and e["status"] == "failed" and e["error"] == "boom"
    assert c.get("/api/entries").json()[0]["text"] == "Important stuff"


def test_switch_provider_and_resummarise(make_client):
    c, _ = make_client({"claude": FakeProvider("claude"), "ollama": FakeProvider("ollama")})
    c.post("/api/login", json={"pin": PIN})
    eid = c.post("/api/entries", json={"date": "2026-10-19", "text": "x", "provider": "ollama"}).json()["id"]
    assert entry(c, eid)["model"] == "fake-ollama"
    c.post(f"/api/entries/{eid}/summarize", json={"provider": "claude"})
    assert entry(c, eid)["model"] == "fake-claude"
    c.post(f"/api/entries/{eid}/summarize", json={"provider": "nope"})
    bad = entry(c, eid)
    assert bad["status"] == "failed" and "isn't set up" in bad["error"]


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
    prompt = fake.prompts[-1]
    assert "2026-10-01" in prompt and "Unblocked the team" in prompt and "focus on leadership" in prompt


def test_recording_is_saved_then_processed(make_client, tmp_path):
    c, _ = make_client()
    c.post("/api/login", json={"pin": PIN})
    worker = c.app.state.worker
    # Upload without draining: the server answers before any processing happens.
    r = TestClient.request(c, "POST", "/api/recordings", data={"date": "2026-10-19"},
                           files={"audio": ("r.mp4", b"secret-audio", "audio/mp4")})
    assert r.status_code == 202 and r.json()["status"] == "transcribing"
    eid = r.json()["id"]
    audio_files = list((tmp_path / "pending-audio").iterdir())
    assert len(audio_files) == 1 and b"secret-audio" not in audio_files[0].read_bytes()  # encrypted

    worker.drain()
    e = entry(c, eid)
    assert e["status"] == "done" and e["text"] == "heard 12 bytes as .mp4 in en"
    assert e["summary"]["headline"]
    assert not list((tmp_path / "pending-audio").iterdir())  # audio deleted once transcribed


def test_unfinished_work_resumes_after_restart(make_client, tmp_path):
    c, settings = make_client()
    c.post("/api/login", json={"pin": PIN})
    r = TestClient.request(c, "POST", "/api/recordings", data={"date": "2026-10-19"},
                           files={"audio": ("r.webm", b"abc", "audio/webm")})
    eid = r.json()["id"]
    # "Restart": a new app on the same data picks the job back up.
    app2 = create_app(settings, providers={"claude": FakeProvider()}, transcriber=FakeTranscriber(), background=False)
    for pending in app2.state.store.pending_entry_ids():
        app2.state.worker.enqueue(pending)
    app2.state.worker.drain()
    assert app2.state.store.get_entry(eid)["status"] == "done"


def test_failed_transcription_can_be_retried(make_client):
    class Flaky(FakeTranscriber):
        calls = 0

        def transcribe(self, audio, suffix=".webm", language=None, model_name=None, on_progress=None):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("decoder crashed")
            return "worked second time"

    c, settings = make_client()
    app = create_app(settings, providers={"claude": FakeProvider()}, transcriber=Flaky(), background=False)
    c = DrainingClient(app)
    c.post("/api/login", json={"pin": PIN})
    eid = c.post("/api/recordings", data={"date": "2026-10-19"},
                 files={"audio": ("r.webm", b"abc", "audio/webm")}).json()["id"]
    e = entry(c, eid)
    assert e["status"] == "failed" and "decoder crashed" in e["error"]
    c.post(f"/api/entries/{eid}/retry")
    assert entry(c, eid)["text"] == "worked second time"
    assert c.post(f"/api/entries/{eid}/retry").status_code == 409  # only failed entries


def test_security_headers_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "Brain Dump" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200


def test_normalize_summary_handles_sloppy_output():
    s = normalize_summary({"headline": "h", "highlights": ["did x", {"text": "y", "category": "WEIRD"}, {}],
                           "tags": "Solo"})
    assert [h["category"] for h in s["highlights"]] == ["other", "other"]
    assert s["tags"] == ["solo"] and s["wins"] == []


def test_ollama_context_size():
    from app.llm import _context_size
    assert _context_size("a" * 100, output_tokens=2048) == 8192
    assert _context_size("a" * 60000, output_tokens=4096) == 32768
    assert _context_size("a" * 10_000_000, output_tokens=4096) == 65536


def test_end_of_day_summary_is_on_demand(make_client):
    fake = FakeProvider()
    c, _ = make_client({"claude": fake})
    c.post("/api/login", json={"pin": PIN})
    first = c.post("/api/entries", json={"date": "2026-10-19", "text": "Morning: started the deploy fix"}).json()
    assert c.post("/api/days/2026-10-19/generate").status_code == 400  # needs two entries

    c.post("/api/entries", json={"date": "2026-10-19", "text": "Afternoon: deploy fix shipped"})
    assert c.get("/api/days/2026-10-19").status_code == 404  # not built automatically
    calls = len(fake.prompts)
    day = c.post("/api/days/2026-10-19/generate").json()
    assert len(fake.prompts) == calls + 1
    day = c.get("/api/days/2026-10-19").json()
    assert day["status"] == "done" and day["entry_count"] == 2 and day["summary"]["headline"]
    assert "fingerprint" not in day and day["stale"] is False

    # A third entry makes it out of date, but doesn't rebuild it
    c.post("/api/entries", json={"date": "2026-10-19", "text": "Evening: release notes"})
    assert c.get("/api/days/2026-10-19").json()["stale"] is True
    assert len(fake.prompts) == calls + 2  # just the new entry's own summary

    # Moving entries away so only one is left removes the day summary
    c.put(f"/api/entries/{first['id']}", json={"date": "2026-10-18"})
    assert c.get("/api/days/2026-10-19").status_code == 200
    third = [e for e in c.get("/api/entries").json() if e["text"].startswith("Evening")][0]
    c.delete(f"/api/entries/{third['id']}")
    assert c.get("/api/days/2026-10-19").status_code == 404


def test_cancel_summaries(make_client):
    c, _ = make_client()
    c.post("/api/login", json={"pin": PIN})
    worker = c.app.state.worker
    # Queued but not started: cancelling skips it.
    eid = TestClient.request(c, "POST", "/api/entries", json={"date": "2026-10-19", "text": "x"}).json()["id"]
    assert TestClient.request(c, "POST", f"/api/entries/{eid}/cancel").status_code == 200
    worker.drain()
    e = entry(c, eid)
    assert e["status"] == "failed" and "cancelled" in e["error"].lower() and e["summary"] is None
    assert c.post(f"/api/entries/{eid}/cancel").status_code == 409  # nothing running now
    c.post(f"/api/entries/{eid}/retry")
    assert entry(c, eid)["status"] == "done"

    # Day summary cancelled before it ever finished: it goes away
    c.post("/api/entries", json={"date": "2026-10-19", "text": "y"})
    TestClient.request(c, "POST", "/api/days/2026-10-19/generate")
    TestClient.request(c, "POST", "/api/days/2026-10-19/cancel")
    worker.drain()
    assert c.get("/api/days/2026-10-19").status_code == 404
    # Cancelling a rebuild keeps the previous summary
    c.post("/api/days/2026-10-19/generate")
    TestClient.request(c, "POST", "/api/days/2026-10-19/generate")
    TestClient.request(c, "POST", "/api/days/2026-10-19/cancel")
    worker.drain()
    day = c.get("/api/days/2026-10-19").json()
    assert day["status"] == "done" and day["summary"]


def test_running_ollama_summary_stops_on_cancel():
    import threading
    from app.llm import Cancelled, OllamaClient

    cancel = threading.Event()
    client = OllamaClient("http://ollama.invalid")

    class FakeResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def iter_lines(self):
            yield '{"message": {"content": "{"}}'
            cancel.set()  # user presses Cancel mid-stream
            yield '{"message": {"content": "\\"headline\\""}}'

    client._http.stream = lambda *a, **k: FakeResp()
    with pytest.raises(Cancelled):
        client.chat("m", "s", "p", fmt=None, output_tokens=10, cancel=cancel)


def test_day_summary_input_includes_every_entry():
    from app.llm import Provider
    seen = {}

    class Capture(Provider):
        name, model = "x", "y"

        def summarize(self, text, entry_date, about_me="", system=None, cancel=None):
            seen.update(text=text, system=system)
            return {}

    Capture().summarize_day([{"text": "one", "created_at": "2026-10-19T09:05:00+00:00"},
                             {"text": "two", "created_at": "2026-10-19T15:30:00+00:00"}], "2026-10-19")
    assert 'recorded_at="09:05 UTC"' in seen["text"] and "two" in seen["text"]
    assert "end-of-day" in seen["system"]


def test_daily_and_custom_reports(make_client):
    fake = FakeProvider()
    c, _ = make_client({"claude": fake})
    c.post("/api/login", json={"pin": PIN})
    c.post("/api/entries", json={"date": "2026-10-19", "text": "Spoke to the PSP about refunds"})
    r = c.post("/api/reports", json={"start": "2026-10-19", "end": "2026-10-19", "style": "daily"})
    assert r.status_code == 200
    assert "Daily notes" in fake.prompts[-1] and "What I said:\nSpoke to the PSP" in fake.prompts[-1]

    assert c.post("/api/reports", json={"start": "2026-10-19", "end": "2026-10-19", "style": "custom"}).status_code == 400
    c.post("/api/reports", json={"start": "2026-10-19", "end": "2026-10-19", "style": "custom",
                                 "notes": "Write it as a haiku"})
    assert "My instructions:\nWrite it as a haiku" in fake.prompts[-1]


def test_long_ranges_use_summaries_only(make_client):
    fake = FakeProvider()
    c, _ = make_client({"claude": fake})
    c.post("/api/login", json={"pin": PIN})
    c.post("/api/entries", json={"date": "2026-10-01", "text": "raw words here"})
    c.post("/api/reports", json={"start": "2026-09-01", "end": "2026-10-31"})
    assert "What I said" not in fake.prompts[-1]



def test_speech_model_setting(client):
    assert "large-v3-turbo" in client.get("/api/session").json()["speech_models"]
    assert client.get("/api/settings").json()["speech"]["model"] == "small"
    r = client.put("/api/settings", json={"speech": {"language": "en", "model": "large-v3-turbo"}})
    assert r.json()["speech"]["model"] == "large-v3-turbo"
    assert client.put("/api/settings", json={"speech": {"language": "en", "model": "nope"}}).status_code == 422



def test_transcription_progress_is_reported(make_client):
    seen = []

    class Slow(FakeTranscriber):
        def transcribe(self, audio, suffix=".webm", language=None, model_name=None, on_progress=None):
            for f in (0.25, 0.5, 1.0):
                on_progress(f)
                seen.append(c.get(f"/api/entries/{eid}").json()["progress"])
            return "done talking"

    c, settings = make_client()
    app = create_app(settings, providers={"claude": FakeProvider()}, transcriber=Slow(), background=False)
    c = TestClient(app)
    c.post("/api/login", json={"pin": PIN})
    eid = c.post("/api/recordings", data={"date": "2026-10-19"},
                 files={"audio": ("r.webm", b"abc", "audio/webm")}).json()["id"]
    assert c.get(f"/api/entries/{eid}").json()["progress"] is None  # queued
    app.state.worker.drain()
    assert seen == [0.25, 0.5, 1.0]
    assert "progress" not in c.get(f"/api/entries/{eid}").json()



def test_chunked_recording(make_client, tmp_path):
    captured = {}

    class Capture(FakeTranscriber):
        def transcribe(self, audio, suffix=".webm", language=None, model_name=None, on_progress=None):
            captured.update(audio=audio, suffix=suffix)
            return "joined up"

    c, settings = make_client()
    app = create_app(settings, providers={"claude": FakeProvider()}, transcriber=Capture(), background=False)
    c = DrainingClient(app)
    c.post("/api/login", json={"pin": PIN})
    rec = c.post("/api/recordings/start", json={"date": "2026-10-21", "mime": "audio/mp4"}).json()
    assert rec["status"] == "recording"
    rid = rec["id"]
    for seq, piece in ((0, b"aaa"), (2, b"ccc"), (1, b"bbb"), (1, b"bbb")):  # out of order + a resend
        assert c.put(f"/api/recordings/{rid}/chunks/{seq}", content=piece).status_code == 200
    stored = b"".join(p.read_bytes() for p in (tmp_path / "pending-audio" / f"{rid}.chunks").glob("*.part"))
    assert b"aaa" not in stored  # encrypted on disk

    r = c.post(f"/api/recordings/{rid}/finish", json={"chunks": 4})
    assert r.status_code == 409 and r.json()["detail"]["missing"] == [3]
    c.post(f"/api/recordings/{rid}/finish", json={"chunks": 3})
    assert captured == {"audio": b"aaabbbccc", "suffix": ".mp4"}
    e = entry(c, rid)
    assert e["status"] == "done" and e["text"] == "joined up"
    assert not list((tmp_path / "pending-audio").iterdir())
    assert c.put(f"/api/recordings/{rid}/chunks/3", content=b"x").status_code == 409


def test_interrupted_recording_is_salvaged(make_client):
    c, _ = make_client()
    c.post("/api/login", json={"pin": PIN})
    worker = c.app.state.worker
    # The tab dies after two chunks, and a gap means chunk 3 can't be used.
    rid = c.post("/api/recordings/start", json={"date": "2026-10-21", "mime": "audio/webm"}).json()["id"]
    for seq in (0, 1, 3):
        c.put(f"/api/recordings/{rid}/chunks/{seq}", content=b"x" * 10)
    assert worker.salvage_interrupted(idle_seconds=3600) == []  # still recent: leave it alone
    assert worker.salvage_interrupted(idle_seconds=0) == [rid]
    worker.drain()
    e = entry(c, rid)
    assert e["status"] == "done" and e["text"] == "heard 20 bytes as .webm in en"

    # Nothing arrived at all: it's marked failed rather than left hanging
    empty = c.post("/api/recordings/start", json={"date": "2026-10-21"}).json()["id"]
    worker.salvage_interrupted(idle_seconds=0)
    assert entry(c, empty)["status"] == "failed"



def test_timings_learn_from_history(make_client):
    c, _ = make_client()
    t = c.app.state.worker.timings
    assert t.estimate("transcribe", "small", 100) == 75  # default 0.75 s per audio second
    for secs in (30, 40, 50):  # this machine is faster than the default
        t.record("transcribe", "small", 100, secs)
    assert t.estimate("transcribe", "small", 200) == 80  # median 0.4 s/s
    assert t.estimate("transcribe", "medium", 100) == 200  # other models keep their own default


def test_eta_while_running(make_client):
    c, _ = make_client()
    c.post("/api/login", json={"pin": PIN})
    w = c.app.state.worker
    w._begin("entry", 7, 60)
    assert 55 <= w.eta("entry", 7) <= 60
    assert w.eta("entry", 8) is None and w.eta("day", "2026-10-21") is None
    w._running["started"] -= 20  # 20s in, transcription 80% through: real progress wins
    w._progress[7] = 0.8
    assert w.eta("entry", 7) == 5
    w._end("transcribe", "small", 100)
    assert w.eta("entry", 7) is None
    assert round(w.timings.estimate("transcribe", "small", 100)) == 20


def test_backup_stays_encrypted_and_moves_to_another_app(make_client, tmp_path, monkeypatch):
    c, _ = make_client()
    c.post("/api/login", json={"pin": PIN})
    c.post("/api/entries", json={"date": "2026-10-19", "text": "Shipped Apollo"})
    c.post("/api/entries", json={"date": "2026-10-19", "text": "Mentored Sam"})
    c.post("/api/days/2026-10-19/generate")
    c.post("/api/reports", json={"start": "2026-10-01", "end": "2026-10-31"})

    raw = c.get("/api/export/backup.json").content
    assert b"Shipped Apollo" not in raw and b"Mentored" not in raw and b"sk-ant" not in raw
    assert c.post("/api/settings/key", json={"pin": "999999"}).status_code == 400
    key = c.post("/api/settings/key", json={"pin": PIN}).json()["key"]
    assert key == "test-secret"

    # Loading into this same app with no key adds nothing twice
    r = c.post("/api/import/backup", files={"file": ("b.json", raw)}).json()
    assert (r["entries"], r["days"], r["reports"]) == (0, 0, 0)

    # A second app with its own key and an empty profile
    other = tmp_path / "other"
    monkeypatch.setenv("DATA_DIR", str(other))
    monkeypatch.setenv("SECRET_KEY", "other-secret")
    app2 = create_app(load_settings(), providers={"claude": FakeProvider()}, transcriber=FakeTranscriber(),
                      background=False)
    c2 = DrainingClient(app2)
    c2.post("/api/setup", json={**SETUP, "profile": {}})
    c2.post("/api/entries", json={"date": "2026-10-20", "text": "Already here"})

    files = {"file": ("b.json", raw)}
    assert c2.post("/api/import/backup", files=files).status_code == 400  # this app's key doesn't fit
    assert c2.post("/api/import/backup", files=files, data={"key": "wrong"}).status_code == 400
    bad = c2.post("/api/import/backup", files={"file": ("b.json", b"{}")}, data={"key": key})
    assert bad.status_code == 400 and "isn't a Brain Dump backup" in bad.json()["detail"]

    r = c2.post("/api/import/backup", files=files, data={"key": key}).json()
    assert (r["entries"], r["days"], r["reports"], r["profile"]) == (2, 1, 1, True)
    assert {e["text"] for e in c2.get("/api/entries").json()} == {"Shipped Apollo", "Mentored Sam", "Already here"}
    imported = next(e for e in c2.get("/api/entries").json() if e["text"] == "Shipped Apollo")
    assert imported["summary"]["highlights"][0]["impact"] == "Unblocked the team"
    day = c2.get("/api/days/2026-10-19").json()
    assert day["summary"]["highlights"] and day["stale"] is False
    assert c2.get("/api/reports").json()[0]["title"] == "My Review"
    assert c2.get("/api/settings").json()["profile"]["name"] == "Steve"
    assert app2.state.config.check_pin(PIN)  # PIN unchanged

    # Stored under the new app's key, and importing again changes nothing
    assert app2.state.store.raw_rows("entries")[-1]["raw_text"] not in raw
    r = c2.post("/api/import/backup", files=files, data={"key": key}).json()
    assert (r["entries"], r["days"], r["reports"]) == (0, 0, 0)


def test_double_encoded_summaries_still_read(client):
    # The first version of backup import stored summaries as a JSON string inside JSON.
    e = client.post("/api/entries", json={"date": "2026-10-19", "text": "Shipped Apollo"}).json()
    store = client.app.state.store
    with store._conn() as conn:
        conn.execute("UPDATE entries SET summary=? WHERE id=?", (store.box.seal(json.dumps(json.dumps(e["summary"]))),
                                                                 e["id"]))
    assert entry(client, e["id"])["summary"] == e["summary"]
