# CLAUDE.md — Foundry Video Editor
# Read this file before doing anything else in this repo.

## IDENTITY
- Repo: candlerfoundry/video-editor (org is candlerfoundry, NOT esavant)
- Clone pattern: git clone https://{PAT}@github.com/candlerfoundry/video-editor.git /tmp/video-editor
- Git identity: git config user.email "esavant@emory.edu" && git config user.name "Emily Avant"
- Netlify auto-deploys from main branch (frontend only)

## SECRET / SESSION CONTEXT — PULL FROM AIRTABLE EVERY SESSION
- GitHub WRITE PAT: Airtable base appiL0Z2RilcAT2Cw, table tblJbNxqgUbK7YT02, record recaOvVBu0kq9MCcU,
  field "Claude Context Doc" — the whole field value is the token (strip markdown escapes, `\_` → `_`).
  NEVER ask Emily to paste it. (The older record recSOFCTAc9NRo7wr holds a READ-ONLY token.)
- Session log / outstanding tasks: record reca91hvXmEY2mKbI
- This project's full context doc: record recxrTpbEIoSqacQm (may be stale — backend/CANONICAL.md wins)

## PRODUCT VISION
This app replaces Opus Clip for The Candler Foundry. It is faster than Opus Clip because
it scans the Words JSON transcript — not the video — to identify viral moments. Claude
ranks and surfaces the best 8-10 clip candidates (30-90 sec each). The UX mirrors Opus
Clip: load video → auto-detect JSON → Find Viral Clips → ranked cards → preview →
edit → export.

## DROPBOX ARCHITECTURE
* All users share one Dropbox account. Dropbox must be installed before the app works.
* Portable paths — never hardcode a username. server.py finds the Dropbox root with
  `resolve_dropbox_root()` and then uses:
  - Claude API key:      <Dropbox>/Scripts/api_key.txt
  - Airtable API key:    <Dropbox>/Scripts/airtable_api_key.txt
  - Dropbox API creds:   <Dropbox>/Scripts/dropbox_credentials.json
  - ffmpeg:              <Dropbox>/Scripts/FFMPEG/ffmpeg.exe (see `find_ffmpeg()` for fallbacks)
  - Launcher + the server.py it runs: <Dropbox>/Scripts/Foundry Video Editor/ (App Launcher.exe)
* Videos and their Words JSON live in the same Dropbox folder. `/find_json` picks the JSON whose
  name starts with the video's bare stem (suffixes like "- Horizontal - Uncaptioned" removed):
  '3MB-1 - What is a Parable - Arnold - Horizontal - Uncaptioned.mp4'
  → same folder → '3MB-1 - What is a Parable - Arnold - Transcript (Words).json'
* If no JSON is found: Generate Transcript (POST /generate_transcript_upload) runs Whisper and
  SAVES the Words JSON next to the video under that same naming, so it is found next time.

## CRITICAL ARCHITECTURE RULES
- ALL frontend code lives in index.html — no separate .js or .css files
- Netlify publishes ONLY index.html + brand/, fonts/, TCF_Logo-Orange.png, Graphic_1-3.png
  (netlify.toml build step copies them into site/). A new page asset must be added to that list.
- All CSS in a <style> tag, all JS in a <script> tag. Dialog markup sits AFTER the script, so
  top-level code must not `getElementById(...)` a dialog to attach listeners — use document-level
  listeners for those.
- CDN libraries loaded via <script src> in <head> are fine
- Backend: backend/server.py — Flask on 127.0.0.1:5000 ONLY (never 0.0.0.0), CORS limited to the
  Netlify site + localhost. PIL + numpy + ffmpeg subprocess only (no OpenCV).
- The page's EXPECTED_BACKEND_BUILD must equal server.py's BACKEND_BUILD — bump BOTH on any
  backend contract change (the page shows an amber banner on a mismatch).
- api.github.com is BLOCKED — git clone/push only, never curl to api.github.com
- Do NOT create new files unless explicitly instructed

## WORKFLOW RULES
- Read backend/CANONICAL.md (source of truth; newest sections are at the END), then the parts of
  index.html / server.py you will touch. Both files are large — Grep, then Read with offset/limit.
- Surgical edits only; never rewrite a working function from scratch.
- Update backend/CANONICAL.md in the SAME commit as any code change.
- Show Emily what changed (diff summary) before pushing unless she has said to push directly.
- Verify before commit: `py -m py_compile backend/server.py`; `node --check` on the extracted
  <script>; test page changes in a browser against the local backend (intercept /projects/update
  so tests never write her real projects).
- On Windows, edit files with the Edit tool or Python (utf-8) — PowerShell Get-Content/Set-Content
  re-encodes and garbles characters like — and ──.
- When server.py changes, rebuild the FLAT zip (files at the root, never a nested folder):
  `zip -j foundry-video-editor-backend.zip backend/server.py backend/start_server.bat backend/requirements.txt`
  and copy server.py to <Dropbox>/Scripts/Foundry Video Editor/server.py (the launcher runs that
  copy). Emily then restarts the launcher and hard-refreshes (Ctrl+Shift+R).

## DESIGN SYSTEM
Aesthetic: light mode, Notion (minimal) + CapCut Web (bold). Clean, airy, modern SaaS. NOT dark theme. NOT the old Python desktop app aesthetic.

### Colors
- Page background: #FFFFFF
- Sidebar/panel background: #F7F7F5
- Card surface: #FFFFFF | Card border: 1px solid #E8E8E4 | Card radius: 10px
- Text primary: #1A1A1A | Text secondary: #6B6B6B
- CF Orange (primary action): #E8541A | Hover: #C94516 | Tint bg: #FEF0EB
- Success green: #2D6A4F | Error red: #CC2200
- Focus ring: 2px solid #E8541A, offset 2px

### Typography
- Font: Inter — load from https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap
- Headings: Inter 600-700, #1A1A1A
- Body: Inter 400, 15-16px, 1.6 line-height
- Secondary labels: Inter 500, 12-13px, #6B6B6B, letter-spacing 0.04em
- Transcript text: Inter 400, 17px, 1.8 line-height
- Filenames/code: monospace, background #F0F0ED, padding 2px 6px, radius 4px

### Layout
- Fixed left sidebar: 220px wide, #F7F7F5, full viewport height
- Main content: white, 32px padding, scrollable, max-width 1100px
- Cards: white bg, 1px #E8E8E4 border, 10px radius, 16px padding, shadow: 0 2px 8px rgba(0,0,0,0.06)
- Card hover: border shifts to rgba(232,84,26,0.4), shadow slightly deeper, 150ms ease

### Buttons
- Primary: #E8541A fill, white text, Inter 500 14px, padding 9px 20px, radius 6px
  Hover: #C94516 | Disabled: opacity 0.45, cursor not-allowed
- Secondary: white fill, #1A1A1A text, 1px #E8E8E4 border, same sizing, hover: #F7F7F5
- All buttons: transition 150ms ease

### Sidebar Nav Items
- Icon (inline SVG) + label, Inter 500 14px
- Default: #6B6B6B | Hover: #1A1A1A, #F0F0ED bg | Active: #E8541A text, 3px left border #E8541A, #FEF0EB bg
- Header: "Candler Foundry" Inter 700 #E8541A / "Video Editor" Inter 400 #6B6B6B

### Interactions
- Hover transitions: 150ms ease on all elements
- Card animate-in: translateY(12px)->0, opacity 0->1, 300ms ease, stagger 60ms between cards
- Drag-drop zones: dashed 2px #E8E8E4 border; hover: #E8541A border, #FEF0EB bg
- After file loaded: filename in monospace + green checkmark, #F0FBF5 bg, solid #2D6A4F border
- Loading states: skeleton shimmer only (NOT spinners)
- Empty states: inline SVG + helper text + action button

## KEY TECHNICAL NOTES
- Paths: see DROPBOX ARCHITECTURE above (everything lives under <Dropbox>/Scripts).
- Local project store (per computer): %LOCALAPPDATA%\Foundry Video Editor\projects (Windows),
  ~/Library/Application Support/Foundry Video Editor/projects (Mac). Saves are atomic (tmp + replace).
- Claude model: claude-sonnet-4-6, hard-coded in several server.py calls (grep `model=`)
- Words JSON format: [{"word": "hello", "start": 0.0, "end": 0.4}, ...]
- All Windows subprocess calls: CREATE_NO_WINDOW = 0x08000000
- CORS: flask-cors, restricted origins (see server.py top) — do NOT widen to '*'
- Frontend polls localhost:5000/health every 3s (also returns the build id for the handshake)
- Projects: the Clips tab and the Thumbnails tab each track their own project
  (`_clipProjectId` / `_thumbProjectId` in index.html); clip saves always go to the Clips project.
- Exports never overwrite: /export_clip returns the name it actually saved (`output_filename`).
- Captioned output naming: {base} (Captioned).mp4
- Transcript files: (Time-Stamped).srt | (Clean).txt | (Words).json
- Special characters in filenames: copy to clean temp path before passing to ffmpeg or Whisper
