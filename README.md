# Reviewer

A private, self-hosted work journal. At the end of the day, talk (or type) for a few minutes about
what you did. Reviewer transcribes it locally, turns it into easy-to-read highlights with an AI
model, and stores it encrypted. Later, pick a date range and get a performance review, brag doc,
status update or blog post, exportable as Word, Markdown, a web page or a CSV spreadsheet.

- **Speech to text runs on your server** (faster-whisper). Audio is transcribed and thrown away; it is never stored or sent anywhere.
- **Summaries and reports** use Claude by default, or a local Ollama model. You can switch per entry or per report and re-run old entries with the other model to compare.
- **PIN login** with an escalating lockout, signed session cookies, and everything you say encrypted at rest.
- **Phone-first web app.** Add it to your home screen for an app-like experience.

## Quick start

```sh
cp .env.example .env
# Edit .env: set APP_PIN, SECRET_KEY (openssl rand -base64 48) and ANTHROPIC_API_KEY
docker compose up -d --build
```

Open `http://<homelab-ip>:8000`. The first recording downloads the Whisper model (~500 MB for
`small`) into the data volume, so it takes a minute; after that a few minutes of speech
transcribes in seconds.

### Using it from your phone (HTTPS)

Browsers only allow microphone access on HTTPS (or `localhost`), so put Reviewer behind
something that gives it a certificate:

- **Tailscale** (simplest if you already use it): `tailscale serve --bg 8000` on the homelab gives you `https://<machine>.<tailnet>.ts.net`, reachable only from your devices.
- **Your existing reverse proxy** (Caddy, Traefik, Nginx Proxy Manager...): proxy to port 8000. Allow uploads of at least 50 MB and a long response timeout (~5 min) for report generation.

Then set `COOKIE_SECURE=true` in `.env` and `docker compose up -d`. On iPhone: Share → Add to
Home Screen. On Android: menu → Install app.

I'd recommend not exposing it to the open internet. If you do, use a long PIN.

### Using Ollama

Either point `OLLAMA_URL` at an Ollama you already run, or start the bundled one:

```sh
docker compose --profile ollama up -d
docker compose exec ollama ollama pull llama3.1:8b
# in .env: OLLAMA_URL=http://ollama:11434
docker compose up -d
```

When both are configured, a model picker appears next to the save and report buttons, and each
entry has "Redo with Claude / Ollama" buttons so you can compare. `LLM_PROVIDER` picks the
default. Bigger local models (e.g. `qwen2.5:14b`, `llama3.3:70b`) give noticeably better
summaries if your hardware can run them.

## Configuration

Everything is set in `.env`; see [`.env.example`](.env.example) for every option. The important ones:

| Variable | Default | What it does |
|---|---|---|
| `APP_PIN` | (required) | PIN to unlock the app |
| `SECRET_KEY` | auto-generated | Encrypts stored data and signs logins. **Back it up**: without it the database can't be read |
| `ANTHROPIC_API_KEY` | | Enables Claude |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model |
| `OLLAMA_URL` / `OLLAMA_MODEL` | / `llama3.1:8b` | Enables a local model |
| `LLM_PROVIDER` | `claude` | Default model: `claude` or `ollama` |
| `WHISPER_MODEL` | `small` | `base` is faster, `medium`/`large-v3` more accurate |
| `COOKIE_SECURE` | `false` | Set `true` once served over HTTPS |

## Data and backups

Everything lives in the `reviewer-data` Docker volume: `reviewer.db` (SQLite), plus the
auto-generated `.secret_key` if you didn't set `SECRET_KEY`, and the cached Whisper model.
Entry text, summaries and reports are encrypted in the database. Dates are not, so entries can
be filtered by range.

- Back up the volume (or just `reviewer.db`) along with your `SECRET_KEY`.
- **History → Export CSV** gives a spreadsheet of every highlight.
- `/api/export/backup.json` (while logged in) downloads everything decrypted as JSON.

## What gets sent where

- Audio: nowhere. It's transcribed in the container and discarded.
- With Claude: the text of each entry when it's summarised, and the summaries in your chosen date range when you make a report, go to Anthropic's API.
- With Ollama: nothing leaves your network.

## Development

```sh
./scripts/test.sh          # runs the test suite inside the Docker image
```

The app is FastAPI (`app/main.py`) with a dependency-free frontend in `app/static/`. Prompts and
the summary schema live in `app/prompts.py` if you want to tweak what gets extracted.
