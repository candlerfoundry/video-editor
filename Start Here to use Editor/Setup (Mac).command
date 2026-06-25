#!/bin/bash
# Foundry Video Editor - one-time Mac setup.
# Run this ONCE: open Terminal, type "bash " (with a space), drag this file
# into the window, and press Return. It installs everything the editor needs
# and makes the launcher double-clickable.

echo "===================================================="
echo "  Foundry Video Editor - one-time setup"
echo "===================================================="
echo "This installs the tools the editor needs. It may ask for your Mac"
echo "password and take several minutes. You only do this once."
echo

HERE="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# 1. Homebrew (the tool installer)
# ---------------------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/brew ] && [ ! -x /usr/local/bin/brew ]; then
  echo "[1/4] Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
  echo "[1/4] Homebrew already installed."
fi
# put brew on PATH for the rest of this script (Apple Silicon or Intel)
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /usr/local/bin/brew ]   && eval "$(/usr/local/bin/brew shellenv)"
# make brew/ffmpeg available in future Terminal sessions too
BREW_BIN="$(command -v brew)"
if [ -n "$BREW_BIN" ] && ! grep -q "brew shellenv" "$HOME/.zprofile" 2>/dev/null; then
  echo "eval \"\$($BREW_BIN shellenv)\"" >> "$HOME/.zprofile"
fi

# ---------------------------------------------------------------------------
# 2. python + ffmpeg (the caption-capable build)
# ---------------------------------------------------------------------------
# The editor burns captions with ffmpeg's "subtitles" filter, which only
# exists in an ffmpeg built with libass. Homebrew's plain "ffmpeg" formula no
# longer ships libass, so we install "ffmpeg-full" instead. ffmpeg-full is
# keg-only (Homebrew keeps it off the PATH), so we force-link it so that plain
# "ffmpeg" is the caption-capable build the editor calls.
echo "[2/4] Installing python and ffmpeg (this is the long part)..."
unset HOMEBREW_ASK 2>/dev/null || true
brew install python ffmpeg-full
brew link --overwrite --force ffmpeg-full >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 3. the editor's Python environment (kept on this Mac, not in Dropbox)
# ---------------------------------------------------------------------------
echo "[3/4] Setting up the editor's dependencies..."
APPSUP="$HOME/Library/Application Support/Foundry Video Editor"
VENV="$APPSUP/venv"
mkdir -p "$APPSUP"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip -q

REQ=""
for p in "$HERE/../requirements.txt" "$HERE/requirements.txt" "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/requirements.txt; do
  if [ -f "$p" ]; then REQ="$p"; break; fi
done
if [ -n "$REQ" ]; then
  "$VENV/bin/python" -m pip install -r "$REQ" -q
else
  echo "    WARNING: requirements.txt not found - is Dropbox synced? Re-run setup once it is."
fi

# ---------------------------------------------------------------------------
# 4. install a PERMANENT launcher outside Dropbox
# ---------------------------------------------------------------------------
# Dropbox does not carry the Unix "executable" bit from Windows to Mac, so a
# launcher kept *inside* Dropbox loses its double-click ability every time the
# file re-syncs (e.g. after the team edits the code). To make this permanent we
# install a tiny launcher in ~/Applications (which Dropbox never touches) that
# runs the Dropbox script via `bash`. `bash <file>` does not require the file to
# be executable, so the in-Dropbox launcher's stripped bit no longer matters,
# and this local launcher's own exec bit is set once and never re-synced away.
echo "[4/5] Installing the launcher..."
LOCAL_LAUNCHER="$HOME/Applications/Foundry Editor.command"
mkdir -p "$HOME/Applications"
cat > "$LOCAL_LAUNCHER" <<'LAUNCH'
#!/bin/bash
# Permanent Foundry Video Editor launcher. Lives outside Dropbox so its
# executable bit is never stripped by a Dropbox re-sync. Runs the real launcher
# from Dropbox via `bash`, which does not need that file to be executable.
bash "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/"Start Here to use Editor"/"Launch Editor (Mac).command"
LAUNCH
chmod +x "$LOCAL_LAUNCHER"
xattr -d com.apple.quarantine "$LOCAL_LAUNCHER" 2>/dev/null || true
echo "    Permanent launcher installed: $LOCAL_LAUNCHER"

# Best-effort: also make the in-Dropbox launcher double-clickable right now, for
# anyone who runs it directly. This bit may be stripped again on a future
# Dropbox re-sync — which is exactly why the permanent launcher above exists.
for p in "$HERE/Launch Editor (Mac).command" "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/"Start Here to use Editor"/"Launch Editor (Mac).command"; do
  if [ -f "$p" ]; then
    chmod +x "$p" 2>/dev/null || true
    xattr -d com.apple.quarantine "$p" 2>/dev/null || true
  fi
done

# ---------------------------------------------------------------------------
# 5. build a friendly, double-clickable "Foundry Editor" app on the Desktop
# ---------------------------------------------------------------------------
# Non-technical users want a normal app icon, not a .command script. We compile
# a tiny AppleScript app that opens a Terminal window running the real launcher
# via `bash` (so it needs no exec bit, same reasoning as the permanent launcher
# above). The app lives on the Desktop — OUTSIDE Dropbox — so its icon and
# permissions are never stripped by a Dropbox re-sync. The Terminal window it
# opens shows startup status and must stay open while the editor is in use.
echo "[5/5] Creating the 'Foundry Editor' app on your Desktop..."
APP="$HOME/Desktop/Foundry Editor.app"
TMP_AS="$(mktemp -t foundryeditor).applescript"
cat > "$TMP_AS" <<'AS'
tell application "Terminal"
	activate
	do script "clear; bash \"$HOME\"/Dropbox*/Scripts/\"Foundry Video Editor\"/\"Start Here to use Editor\"/\"Launch Editor (Mac).command\""
end tell
AS
rm -rf "$APP"
if osacompile -o "$APP" "$TMP_AS" >/dev/null 2>&1; then
  # Brand icon: find the bundled .icns (next to this script, or in Dropbox).
  ICNS=""
  for p in "$HERE/Foundry Editor.icns" "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/"Start Here to use Editor"/"Foundry Editor.icns"; do
    if [ -f "$p" ]; then ICNS="$p"; break; fi
  done
  if [ -n "$ICNS" ]; then
    cp "$ICNS" "$APP/Contents/Resources/applet.icns"
    # osacompile may also write an asset catalog that overrides applet.icns;
    # remove it and point the plist back at the .icns.
    rm -f "$APP/Contents/Resources/Assets.car"
    /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile applet" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
  fi
  /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string org.candlerfoundry.videoeditor.launcher" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
  # Editing the bundle invalidates osacompile's signature; re-sign ad-hoc so
  # macOS doesn't refuse to open it as "damaged".
  codesign --remove-signature "$APP" >/dev/null 2>&1 || true
  codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
  xattr -dr com.apple.quarantine "$APP" >/dev/null 2>&1 || true
  touch "$APP"
  rm -f "$TMP_AS"
  echo "    Created: $APP"
  APP_OK=1
else
  rm -f "$TMP_AS"
  echo "    (Could not build the Desktop app automatically — you can still launch"
  echo "     from ~/Applications/Foundry Editor.command.)"
  APP_OK=0
fi

echo
echo "Setup complete!"
if [ "${APP_OK:-0}" = "1" ]; then
  echo "Launch the editor by double-clicking 'Foundry Editor' on your Desktop."
  echo "(It was built right here on your Mac, so it opens with no security warning.)"
else
  echo "Launch the editor from:   ~/Applications/Foundry Editor.command"
fi
echo "Keep the Terminal window it opens open while you work."
read -p "Press Return to close..."
