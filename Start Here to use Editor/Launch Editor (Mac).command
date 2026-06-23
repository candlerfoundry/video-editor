#!/bin/bash
# Foundry Video Editor - Mac launcher. Double-click to start the editor.
# Run "Setup (Mac).command" once first (see START HERE (Mac).html).

cd "$(dirname "$0")"

# Make sure Homebrew tools (ffmpeg) are on PATH even when double-clicked.
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /usr/local/bin/brew ]   && eval "$(/usr/local/bin/brew shellenv)"

echo "===================================================="
echo "  Foundry Video Editor - starting up"
echo "===================================================="

# Find server.py: next to this file, the parent folder, or the Foundry
# Dropbox folder (handles Mac's "Dropbox (Team)" naming).
SERVER=""
for p in "./server.py" "../server.py" "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/server.py; do
  if [ -f "$p" ]; then SERVER="$p"; break; fi
done
if [ -z "$SERVER" ]; then
  echo "ERROR: Could not find server.py. Make sure Dropbox has finished syncing."
  read -p "Press Return to close..."; exit 1
fi
BACKDIR="$(cd "$(dirname "$SERVER")" && pwd)"
echo "Backend: $SERVER"

# Prefer the private environment that Setup created; fall back to system python.
VENV="$HOME/Library/Application Support/Foundry Video Editor/venv"
if [ -x "$VENV/bin/python" ]; then
  PY="$VENV/bin/python"
else
  PY="$(command -v python3 || command -v python)"
  if [ -z "$PY" ]; then
    echo "Setup hasn't been run yet. Open 'START HERE (Mac).html' and run Setup first."
    read -p "Press Return to close..."; exit 1
  fi
  echo "Installing dependencies (first run can take a few minutes)..."
  "$PY" -m pip install -r "$BACKDIR/requirements.txt" -q --disable-pip-version-check 2>/dev/null
fi

# Make sure ffmpeg can actually burn captions (needs the libass "subtitles"
# filter). Older setups installed the slim "ffmpeg" without it, so self-heal
# on launch by installing/activating ffmpeg-full. The check is cheap; once it
# passes this block does nothing on future launches.
ffmpeg_has_captions() {
  command -v ffmpeg >/dev/null 2>&1 || return 1
  ffmpeg -hide_banner -filters 2>/dev/null | awk '{print $2}' | grep -qx subtitles
}
if ! ffmpeg_has_captions; then
  echo "Updating ffmpeg so captions can burn (one-time, please wait)..."
  unset HOMEBREW_ASK 2>/dev/null || true
  export HOMEBREW_NO_ENV_HINTS=1
  brew install ffmpeg-full >/dev/null 2>&1
  brew link --overwrite --force ffmpeg-full >/dev/null 2>&1 || true
  hash -r 2>/dev/null || true
  if ffmpeg_has_captions; then
    echo "ffmpeg is ready for captions."
  else
    echo "WARNING: couldn't enable captions automatically. Re-run 'Setup (Mac).command'."
  fi
fi

echo "Starting backend..."
"$PY" "$SERVER" &
SVPID=$!

echo "Waiting for the backend to be ready..."
for i in $(seq 1 90); do
  if curl -s http://localhost:5000/health >/dev/null 2>&1; then
    echo "Backend running. Opening the editor in your browser..."
    open "https://foundry-video-editor.netlify.app"
    echo ""
    echo "The editor is open in your browser."
    echo "KEEP THIS WINDOW OPEN while you work. Close it to stop the editor."
    wait $SVPID
    exit 0
  fi
  if ! kill -0 "$SVPID" 2>/dev/null; then
    echo "ERROR: Backend stopped unexpectedly."
    read -p "Press Return to close..."; exit 1
  fi
  sleep 1
done
echo "Backend did not respond within 90 seconds. Close this window and try again."
kill "$SVPID" 2>/dev/null
read -p "Press Return to close..."
