# Foundry Video Editor

A web-based video tool for the Candler Foundry team: find the best short clips in a long talk,
edit and caption them, make thumbnails, and save everything to Dropbox and Airtable.

## Architecture

- **Frontend** (`index.html`) — one self-contained page deployed to Netlify
  (https://foundry-video-editor.netlify.app). Netlify publishes only the page and its assets
  (see `netlify.toml`).
- **Backend** (`backend/server.py`) — a local Flask server on `127.0.0.1:5000` that each person
  runs on their own computer. It does the heavy work: ffmpeg, Whisper transcription, Claude
  calls, Dropbox links and Airtable records. It only accepts connections from the same computer.

The page checks for the backend every few seconds. If it isn't running, the page shows setup
steps; if its version doesn't match the page, it shows an amber "different versions" banner
(restart the launcher, then hard-refresh).

## Setup (first time)

Everything is already in the shared Foundry Dropbox. Open
**Dropbox → Scripts → Foundry Video Editor → Start Here to use Editor** and follow
**START HERE (PC)** or **START HERE (Mac)**. In short: install Python and Dropbox, then start the
launcher (**App Launcher.exe** on Windows, **Launch Editor** on a Mac). The launcher installs the
Python packages from `backend/requirements.txt`, starts the backend and opens the app.

Keys and tools the backend reads from `Dropbox/Scripts/`: `api_key.txt` (Claude),
`airtable_api_key.txt`, `dropbox_credentials.json`, and `FFMPEG/ffmpeg.exe`.

## Tabs

| Tab | What it does |
|-----|--------------|
| Clips | Load a video; its Words JSON transcript is found automatically (or generated with Whisper and saved next to the video). Claude suggests viral clips or splits the talk into parts. Edit trims, cut words, bleeps, speed, reframing, split-screen and burned-in captions, then save to Dropbox + Airtable with an Instagram cover thumbnail. |
| Thumbnails | Make a 9:16 thumbnail from any video frame or an uploaded image: text boxes (optional), logos, graphics. Save & Download in one step; every saved thumbnail is in the Saved Thumbnails gallery. |
| Caption Videos | Transcribe a whole video with Whisper and burn captions onto it (size is automatic). |
| Edit Captions | Open an .srt file, fix the text, save it back. |

## Output file naming

| Type | Naming |
|------|--------|
| Saved clip | the name in the Save Clip window; if taken, `name (2).mp4`, `(3)`… (never overwritten) |
| Captioned video | `{base} (Captioned).mp4` |
| Generated transcript | `{video stem} - Transcript (Words).json`, next to the video |

## For developers

Read `CLAUDE.md` first, then `backend/CANONICAL.md` — the source of truth for how the app
behaves (newest sections are at the end). Update CANONICAL.md in the same commit as any change,
and bump `BACKEND_BUILD` (server.py) and `EXPECTED_BACKEND_BUILD` (index.html) together when the
backend contract changes.
