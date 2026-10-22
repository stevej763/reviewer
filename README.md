# Brain Dump

A private work journal that runs on your Mac. At the end of the day, talk (or type) for a few
minutes about what you did. Brain Dump transcribes it locally, turns it into easy-to-read
highlights with an AI model, and stores it encrypted. Later, pick a date range and get a
performance review, brag doc, status update or blog post, exportable as Word, Markdown, a web
page or a CSV spreadsheet.

- **Everything stays on your Mac.** Speech is transcribed in the container (faster-whisper) and the audio is thrown away. Summaries and reports come from a local model in Ollama, running natively on your Mac.
- **First-time setup in the app**: your name, role and goals (used to tailor summaries and reports), a PIN, and which AI model to use. Change any of it later in Settings.
- **PIN login** stored as a salted scrypt hash, with an escalating lockout after wrong guesses. Entries, reports and settings are encrypted at rest.
- **Claude is optional.** Add an Anthropic API key in Settings if you have one. (A Claude.ai or Claude Code subscription doesn't include an API key.)

## Setup on a Mac

**1. Install and open Ollama** from [ollama.com/download](https://ollama.com/download). It runs
natively so it can use the Mac's GPU (Docker on a Mac can't), and listens on port 11434.

**2. Start Brain Dump:**

```sh
docker compose up -d --build
```

**3. Open [http://localhost:8000](http://localhost:8000)** and fill in the setup screen. The
Ollama address is `http://host.docker.internal:11434`, which is how the container reaches the
Ollama app on your Mac. Press **Check**, pick a model and press **Download**. `qwen3:8b`
(5.2 GB) is a good fit for a 16 GB MacBook Air.

The first recording also downloads the speech model (~500 MB), so it takes a minute; after that,
a few minutes of speech transcribes in seconds.

### Choosing a model

| Model | Download | Notes |
|---|---|---|
| `qwen3:8b` | 5.2 GB | Default. Good summaries, quick on an M-series Mac |
| `gemma3:12b` | 8.1 GB | Nicer writing for reports; slower and heavier on 16 GB |
| `gemma3:4b` | 3.3 GB | Fastest and lightest; simpler summaries |
| `llama3.1:8b` | 4.9 GB | Solid all-rounder |

You can switch models in Settings at any time, and each entry has a "Redo summary" button to
compare. Ollama unloads the model after 15 minutes idle, so the first summary after a break
takes a few extra seconds.

## Mac app

There is also a self-contained Mac app, **Brain Dump.app**, which runs the whole thing without
Docker. It carries its own Python and starts the server when you open it, then stops it
when you quit. You still need the Ollama app for summaries.

```sh
./macos/build.sh    # Apple Silicon, needs the Xcode command line tools
```

This builds `macos/dist/Brain Dump.app` and `macos/dist/Brain Dump.dmg`. Open the disk image
and drag Brain Dump to Applications. In the setup screen the Ollama address is
`http://localhost:11434`. The app keeps its data in `~/Library/Application Support/Brain Dump`
(View → Show Data Folder), separate from the Docker volume. Its server log is in
`~/Library/Logs/Brain Dump`.

The app is signed only for the Mac that built it. Copied to another Mac, it needs
right-click → Open the first time.

## Configuration

Nothing is required in `.env`. See [`.env.example`](.env.example) for the optional settings.
The one worth knowing about is `SECRET_KEY`: it encrypts your data. If it isn't set, one is
generated and kept in the data volume. **If you set it, back it up**, because without it a
backed-up database can't be read.

The app only listens on `localhost`. Browsers allow the microphone on `localhost`, so recording
works in any browser on this Mac. Using it from your phone would need HTTPS (e.g. `tailscale
serve`) and changing the port binding in `docker-compose.yml`.

## Data and backups

Everything lives in the `reviewer-data` Docker volume: `reviewer.db` (SQLite), the generated
key if you didn't set `SECRET_KEY`, and the speech model cache.

- **Settings → Download backup** gives you everything decrypted as JSON.
- **History → Export CSV** gives a spreadsheet of every highlight.

If you forget your PIN, stop the app and delete the volume to start over
(`docker compose down -v`), which also deletes your entries, so take a backup first.

## Development

```sh
./scripts/test.sh          # runs the test suite inside the Docker image
```

The app is FastAPI (`app/main.py`) with a dependency-free frontend in `app/static/`. Prompts and
the summary schema live in `app/prompts.py` if you want to tweak what gets extracted.
