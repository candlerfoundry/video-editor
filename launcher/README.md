# Foundry Video Editor — Launcher Setup

## First time only

1. Download this folder from Dropbox: `Scripts/Video Editor Downloads/foundry-video-editor-backend/`
2. Make sure `server.py` is in the **same folder** as `Foundry Video Editor.exe`
3. Double-click `Foundry Video Editor.exe`

The launcher will install dependencies automatically and open the editor in your browser.

## Every time after that

Just double-click `Foundry Video Editor.exe` — that's it.

## Troubleshooting

| Message | Fix |
|---|---|
| `server.py not found` | Make sure the .exe and server.py are in the same folder |
| `Python not found` | Install Python from [python.org](https://python.org) — check "Add to PATH" during install |
| Backend won't start | Make sure Dropbox is running and fully synced |
| App opens but backend won't connect | Restart the launcher; check that nothing else is using port 5000 |

## For IT / advanced users

`start_server.bat` in the same folder starts the backend manually with a visible terminal window. Useful for seeing error messages or running the backend without the launcher app.
