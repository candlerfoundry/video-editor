# CLAUDE.md — Foundry Video Editor
# Read this file before doing anything else in this repo.

## IDENTITY
- Repo: candlerfoundry/video-editor (org is candlerfoundry, NOT esavant)
- Clone pattern: git clone https://{PAT}@github.com/candlerfoundry/video-editor.git /tmp/video-editor
- Git identity: git config user.email "esavant@emory.edu" && git config user.name "Emily Savant"
- Netlify auto-deploys from main branch (frontend only)

## SECRET / SESSION CONTEXT — PULL FROM AIRTABLE EVERY SESSION
- GitHub PAT: Airtable base appiL0Z2RilcAT2Cw, table tblJbNxqgUbK7YT02, record recSOFCTAc9NRo7wr, field: Claude Context Doc — extract token after "Token:". NEVER ask Emily to paste it.
- Session log / outstanding tasks: record reca91hvXmEY2mKbI
- This project's full context doc: record recxrTpbEIoSqacQm

## CRITICAL ARCHITECTURE RULES
- ALL frontend code lives in index.html — no separate .js or .css files
- Netlify serves only index.html as the entire frontend
- All CSS in a <style> tag, all JS in a <script> tag
- CDN libraries loaded via <script src> in <head> are fine
- Backend: backend/server.py — runs locally via Flask on localhost:5000
- api.github.com is BLOCKED — git clone/push only, never curl to api.github.com
- Do NOT create new files unless explicitly instructed

## WORKFLOW RULES
- Read index.html AND backend/server.py in full before making any changes
- Run git diff before every commit — show Emily the full diff and wait for her explicit approval
- Never commit without Emily's approval
- When server.py changes, rebuild the zip:
  cd /tmp && rm -rf foundry-video-editor-backend && mkdir foundry-video-editor-backend
  cp /tmp/video-editor/backend/start_server.bat foundry-video-editor-backend/
  cp /tmp/video-editor/backend/server.py foundry-video-editor-backend/
  cp /tmp/video-editor/backend/requirements.txt foundry-video-editor-backend/
  zip -r /tmp/video-editor/foundry-video-editor-backend.zip foundry-video-editor-backend/

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
- Python: C:\Users\esavant\AppData\Local\Programs\Python\Python314\python.exe
- ffmpeg: C:\Users\esavant\Dropbox\FFMPEG\
- API key file: C:\Users\esavant\Dropbox\3MB\api_key.txt
- Claude model: claude-sonnet-4-6 (never opus)
- Words JSON format: [{"word": "hello", "start": 0.0, "end": 0.4}, ...]
- All Windows subprocess calls: CREATE_NO_WINDOW = 0x08000000
- CORS: flask-cors required, CORS(app, origins=['*'])
- Frontend polls localhost:5000/health every 3s
- Captioned output naming: {base} (Captioned).mp4
- Transcript files: (Time-Stamped).srt | (Clean).txt | (Words).json
- Special characters in filenames: copy to clean temp path before passing to ffmpeg or Whisper
