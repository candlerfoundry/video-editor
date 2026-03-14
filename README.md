# Foundry Video Editor

A professional web-based video editing tool for the Candler Foundry team.

## Architecture

- **Frontend** (`index.html`) — deployed to Netlify, runs in any browser
- **Backend** (`backend/server.py`) — local Flask server on `localhost:5000`, handles ffmpeg, Whisper, and Claude API calls

The frontend auto-detects whether the backend is running. If not, it shows a Setup Wizard with instructions.

## Setup (First Time)

### 1. Install Python 3.14
Download from [python.org](https://python.org) — check "Add Python to PATH" during install.

### 2. Install dependencies
```
pip install flask flask-cors openai-whisper anthropic Pillow
```

### 3. Install ffmpeg
Download ffmpeg and place it in `C:\Users\esavant\Dropbox\FFMPEG\` or add it to your system PATH.

### 4. Start the backend
Double-click `backend/start_server.bat` — a terminal window will open and stay running.

### 5. Open the app
Visit the Netlify URL (or open `index.html` locally). The app will connect automatically.

## Tabs

| Tab | Description | Status |
|-----|-------------|--------|
| Caption Videos | Select MP4s, generate SRT via Whisper, burn captions via ffmpeg | Session 2 |
| Edit Captions | Load SRT, edit entries, save back | Session 5 |
| Thumbnails | Extract frames, Whisper tiny, Claude AI correction, 3 styles | Session 3 |
| Clips | Whisper medium + Claude viral clip detection, preview, export | Session 4 |

## Output File Naming

| Type | Naming |
|------|--------|
| Captioned video | `{base} (Captioned).mp4` |
| Timestamped SRT | `{base} (Time-Stamped).srt` |
| Clean transcript | `{base} (Clean).txt` |
| Word-level JSON | `{base} (Words).json` |

## API Key

The backend reads your Claude API key from:
```
C:\Users\esavant\Dropbox\3MB\api_key.txt
```

## Brand Colors

| Name | Hex |
|------|-----|
| Orange | `#E8541A` |
| Amber | `#F5A623` |
| Background | `#111111` |
| Surface | `#1C1C1C` |
| Green | `#2D6A4F` |
