# Brain Dump

A private work journal that runs on your Mac. At the end of the day, talk (or type) for a few
minutes about what you did. Brain Dump transcribes it locally, turns it into easy-to-read
highlights with an AI model, and stores it encrypted. Later, pick a date range and get a
performance review, brag doc, status update or blog post, exportable as Word, Markdown, a web
page or a CSV spreadsheet.

- **Everything stays on your Mac.** Speech is transcribed in the container (faster-whisper) and the audio is thrown away. Summaries and reports come from a local model in Ollama, running natively on your Mac.
- **First-time setup in the app**: your name, role and goals (used to tailor summaries and reports), a PIN, and which AI model to use. Change any of it later in Settings.
- **Startup password**: your data is encrypted with a random key that is itself locked by a password you type when Brain Dump starts. Until then the app can't read anything, and the key is never stored in the clear. A one-time **recovery key** is the backup way in.
- **PIN login** for everyday use, stored as a salted scrypt hash, with an escalating lockout after wrong guesses. Entries, reports and settings are encrypted at rest.
- **Auto-lock**: asks for the PIN again after 5 minutes of inactivity (change it in Settings → PIN & data). The Mac app also locks when you close its window.
- **Claude is optional.** Add an Anthropic API key in Settings if you have one. (A Claude.ai or Claude Code subscription doesn't include an API key.)

## Setup on a Mac

**1. Install and open Ollama** from [ollama.com/download](https://ollama.com/download). It runs
natively so it can use the Mac's GPU (Docker on a Mac can't), and listens on port 11434.

**2. Start Brain Dump:**

```sh
docker compose up -d --build
```

**3. Open [http://localhost:8000](http://localhost:8000)**, create a startup password (save the
recovery key it shows you) and fill in the setup screen. The
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

**Touch ID.** After you first unlock with your startup password, the app offers to use Touch ID.
It then keeps the data key in your Keychain, and one touch at launch unlocks your data and signs
you in; the PIN screen offers Touch ID too. If your fingerprints change, Touch ID turns itself
off until you type the password again. Turn it on or off in the Brain Dump menu. The key is
handed to the bundled server over a private pipe, never through the environment or a file.

The strongest form (a Keychain item that only Touch ID can open) needs the app signed with an
Apple Development certificate and Team ID. The default ad-hoc build can't use that, so it falls
back to a Touch ID check in front of an ordinary Keychain item (Console.app shows which one is in
use: search for "Brain Dump"). With the ad-hoc build, macOS may ask for your login password to let a rebuilt app read the
Keychain item.

## Configuration

Nothing is required in `.env`. See [`.env.example`](.env.example) for the optional settings.
The encryption key isn't one of them: it's always protected by the startup password, so the app
needs the password (or Touch ID in the Mac app) after every restart.

### How the startup password works

`keyring.json` in the data folder holds the data key wrapped (AES-GCM) twice: once under a key
derived from your password with scrypt (about half a second per guess), once under the recovery
key. Wrong passwords are rate-limited (5 tries, then a wait that doubles). Changing the password
only re-wraps the key, so nothing is re-encrypted. After a restart, every API call except
unlocking returns `423 Locked`, and unfinished transcriptions and summaries carry on once you
unlock. Login cookies are signed with a key derived from the data key, so you stay signed in
across restarts (subject to auto-lock).

If the data folder has data but no `keyring.json`, the app won't let you create a new password
(the new key couldn't open the old data): put `keyring.json` back, or start with an empty data
folder and load a backup.

Forget the password? **Forgot it? Use your recovery key** on the unlock screen sets a new one.
Lose both and the data can't be recovered.

The app only listens on `localhost`. Browsers allow the microphone on `localhost`, so recording
works in any browser on this Mac. Using it from your phone would need HTTPS (e.g. `tailscale
serve`) and changing the port binding in `docker-compose.yml`.

## Data and backups

Everything lives in the `reviewer-data` Docker volume: `reviewer.db` (SQLite), `keyring.json`
(the wrapped key) and the speech model cache.

- **Settings → PIN & data → Download backup** saves everything, still encrypted, together with
  the wrapped key. It opens with this app's startup password or recovery key (as they were when
  the backup was made). Because the wrapped key travels with the backup, a stolen backup is only
  as safe as your password, so pick a good one.
- **Load a backup** on the same screen adds the entries, end-of-day summaries and reports from a
  backup, given the startup password, recovery key or encryption key of the app that made it
  (leave it empty for this app's own backups). Backups made before startup passwords existed
  need the encryption key.
  That's how you move from the server to the Mac app or back. Nothing already there is
  replaced, loading the same backup twice adds nothing, and your PIN and AI settings stay as they are.
- **History → Export CSV** gives a spreadsheet of every highlight.

If you forget your PIN, stop the app and delete the volume to start over
(`docker compose down -v`), which also deletes your entries, so take a backup first.

## Development

```sh
./scripts/test.sh          # runs the test suite inside the Docker image
```

The app is FastAPI (`app/main.py`) with a dependency-free frontend in `app/static/`. Prompts and
the summary schema live in `app/prompts.py` if you want to tweak what gets extracted.
