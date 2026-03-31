# CANONICAL.md - Foundry Video Editor Backend
# Source of truth for critical backend and thumbnail behavior.
# Every editing session must compare server.py to this file before changing fragile paths.
# Last updated: March 31, 2026
#
# HOW TO USE THIS FILE:
# 1. Read it before editing server.py or launcher/launcher.py.
# 2. Verify the current implementation still matches the canonical sections below.
# 3. If a canonical behavior changes intentionally, update this file in the same commit.
# 4. Never commit server.py without also staging backend/CANONICAL.md.

---

## 1. Flask startup and logging safety

Canonical startup requirements:

```python
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)
sys.stdout = sys.stderr

LOG_LEVEL_NAME = os.environ.get('FVE_LOG_LEVEL', 'INFO').upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)
THUMBNAIL_DEBUG = os.environ.get('FVE_THUMBNAIL_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stderr,
)

logger = logging.getLogger('foundry_video_editor')
thumb_logger = logging.getLogger('foundry_video_editor.thumbnail')
if THUMBNAIL_DEBUG:
    thumb_logger.setLevel(logging.DEBUG)
else:
    thumb_logger.setLevel(max(LOG_LEVEL, logging.INFO))

if __name__ == '__main__':
    logger.info('[startup] Starting Foundry Video Editor backend...')
    logger.info('[startup] Python: %s', sys.executable)
    logger.info('[startup] ffmpeg: %s', FFMPEG_EXE)
    app.run(host='0.0.0.0', port=5000, threaded=True)
```

Why:
- `threaded=True` is required so `/health` keeps answering while long jobs run.
- `sys.stdout = sys.stderr` protects older launcher builds that only drain stderr.
- Thumbnail paths must use `logging`, not `print()`, so log volume is controlled and routable.
- The same rule also applies to `/find_json`, `/clips`, `/split`, caption helpers, and export integrations.
- Normal job lifecycle logs belong at `INFO`.
- Per-frame diagnostics belong at `DEBUG` and must stay behind `FVE_THUMBNAIL_DEBUG`, off by default.

Regression risk: High.

---

## 2. Launcher backend process I/O

Canonical launcher pattern in `launcher/launcher.py`:

```python
self._backend_log_path = self._get_backend_log_path()
self._backend_log_handle = open(self._backend_log_path, 'a', encoding='utf-8', buffering=1)
self.proc = subprocess.Popen(
    [python_exe, server_path],
    stdout=self._backend_log_handle,
    stderr=subprocess.STDOUT,
    creationflags=0x08000000,
)
```

Safe launcher patterns:
- Redirect backend `stdout` and `stderr` to a log file.
- Inherit console output directly.
- Use pipes only if every piped stream is drained continuously for the full process lifetime.

Unsafe launcher patterns:
- `stdout=PIPE` with no reader.
- `stdout=PIPE` while only draining `stderr`.
- Verbose `print()` calls inside thumbnail loops.

Why this mattered:
- The recurring disconnect during `Generate Thumbnail` was a process I/O deadlock.
- Thumbnail generation produced enough output to fill an unread pipe.
- Once the pipe filled, the Python backend blocked on write and `/health` timed out.

Regression risk: Critical.

---

## 3. ffmpeg path resolution

`find_ffmpeg()` must probe Dropbox-installed ffmpeg first and log the chosen path.

Canonical requirements:
- Prefer `Dropbox/Scripts/FFMPEG/ffmpeg.exe`
- Fall back to `Dropbox/Scripts/FFMPEG/bin/ffmpeg.exe`
- Fall back to Dropbox alternates
- Fall back to system `ffmpeg`
- Log success/failure with `logger`, not `print()`

Regression risk: High.

---

## 4. Video path cache

Canonical requirements:
- Keep `video_path_cache = {}` at module scope.
- `/find_json` populates it.
- `/thumbnail`, `/clips`, and `/export_clip` reuse it before walking Dropbox.
- `find_video_in_dropbox()` may still walk Dropbox as a fallback.

Why:
- Full Dropbox walks are expensive and should not happen on the request path unless necessary.

Regression risk: Medium.

---

## 5. /find_json contract

Canonical requirements:
- Route must always return `json_found`, not `found`.
- Entire body stays wrapped in `try/except`.
- It must return quickly and never hang forever.
- Cache successful video paths into `video_path_cache`.

Frontend contract:
- `index.html` checks `data.json_found`.

Logging rules:
- `INFO`: lookup start, cache store, final chosen transcript JSON
- `DEBUG`: folder listings and glob result details
- `WARNING`: missing video or missing transcript JSON
- `EXCEPTION`: unexpected route failure
- do not add `print()` calls back to this route or Dropbox-walk helpers

Regression risk: High.

---

## 6. Thumbnail route and async job contract

Canonical requirements:
- Keep `thumbnail_jobs` at module scope.
- `/thumbnail` must return immediately after starting a daemon thread.
- `/thumbnailstatus/<jobid>` must report `processing`, `complete`, or `error`.
- Route naming and field naming must continue matching `index.html`:
  - form data, not JSON
  - `jobid`, not `job_id`
  - `/thumbnailstatus/<jobid>`, not `/thumbnail_status/<job_id>`

Canonical logging rules:
- `/thumbnail` logs one `INFO` queue event per job.
- `_thumbnail_worker()` logs one `INFO` start event and concise `INFO` summaries.
- `/thumbnailstatus` should only log unexpected cases such as missing jobs.
- Do not add `print()` statements to thumbnail execution paths.

Why:
- This route must remain truly asynchronous so health polling does not stall.
- Noisy output here can reintroduce launcher deadlocks if someone later weakens process I/O handling.

Regression risk: Very high.

---

## 7. Thumbnail frame sampling

Canonical requirements inside `_thumbnail_worker()`:

```python
start_t = 17.0 if total_duration > 20.0 else 0.0
end_t = max(start_t, total_duration - 0.25)
if end_t <= start_t + 0.01:
    timestamps = [round(start_t, 3)]
else:
    timestamps = [start_t + i * (end_t - start_t) / 19 for i in range(20)]
thumb_logger.info(
    'Job %s sampling %s timestamps from %.1fs to %.1fs',
    job_id, len(timestamps), start_t, end_t,
)
```

Why:
- Thumbnails sample the full video, not just the selected clip range.
- Logging here should be one summary line, not a per-frame print loop.

Regression risk: High.

---

## 8. Thumbnail extraction and logging

Canonical requirements:
- Use `get_video_stream_info()` to capture raw size, display size, rotation, and orientation.
- Use PIL plus numpy only. Do not reintroduce OpenCV.
- Preserve aspect ratio during extraction; never force frames into a fixed 16:9 canvas before the frontend renders them.
- Downscale only if needed, using high-quality resampling.
- Include `video_info` in the completed thumbnail job result.

Canonical logging rules:
- `INFO`: cache miss, job start, timestamp summary, usable-frame summary, encode summary, transcript fallback, job completion.
- `DEBUG`: per-frame sharpness and per-frame encode details.
- `WARNING`: ranking/title-generation fallback and recoverable failures.
- `EXCEPTION`: worker-level failure.

Regression risk: High.

---

## 9. /clips and /split logging discipline

Canonical requirements:
- Both routes must use `logger`, not `print()`.
- `INFO`: transcript length, attempt summaries, parsed result counts.
- `DEBUG`: Claude raw preview snippets and transcript previews.
- `WARNING`: retryable Claude/API failures.
- `EXCEPTION`: route-level failures.

Why:
- These routes can produce large prompt/response debug output.
- If they regress back to noisy `print()` loops, they can recreate the same launcher/backend I/O blockage pattern.

Instruction for future coding sessions:
- Before adding diagnostics to `/clips` or `/split`, prefer one summary line.
- Only log raw model output snippets at `DEBUG`.
- Never log full transcripts or full model responses at `INFO`.

Regression risk: High.

---

## 10. Export and integration logging

Canonical requirements:
- Dropbox-link generation and Airtable integration must use `logger.warning(...)` for recoverable failures.
- These integration failures should not crash the export route when the clip itself succeeded.
- Do not add `print()` calls in export-side Dropbox/Airtable branches.

Why:
- These integrations can fail intermittently and are often edited during operational debugging.
- Reintroducing `print()` in retry-prone integration paths recreates backend I/O risk.

Regression risk: Medium.

---

## 11. Thumbnail preview rendering

All thumbnail surfaces in `index.html` must use one aspect-ratio-preserving cover-fit helper:
- main thumbnail editor canvas
- export/dialog canvas
- style preview mini canvases

Required behavior:
- compute crop from the image's natural dimensions
- use `ctx.drawImage(img, sx, sy, sw, sh, 0, 0, W, H)`
- set `imageSmoothingEnabled = true`
- set `imageSmoothingQuality = 'high'`
- size mini-canvas backing stores to displayed CSS size with `devicePixelRatio`
- do not hardcode thumbnail previews or editor canvases to `16:9`
- the UI must expose target thumbnail formats, currently `Instagram 4:5` and `YouTube Shorts 9:16`
- style cards, editor canvases, and saved thumbnail previews must all resize to the selected target ratio
- the full selected thumbnail crop must remain visible in previews; never show only a narrow strip because the viewport stayed landscape

Why:
- Backend extraction quality and frontend preview quality can regress independently.
- A fixed landscape preview box can make a correctly extracted portrait crop look broken even when the image data is fine.

Regression risk: High.

---

## 12. Health polling

Canonical `index.html` behavior:
- poll `http://localhost:5000/health`
- use `AbortSignal.timeout(2500)`
- run every 3 seconds
- do not add an early-return guard that freezes the UI state

Why:
- This is the first signal that exposes a blocked or unreachable backend.

Regression risk: Medium.

---

## 13. Simultaneous video playback

Canonical `index.html` behavior:
- maintain one `currentlyPlaying` reference
- pause/reset the previous video before playing a new one
- clear it when navigating back to results

Regression risk: High.

---

## 14. Zip build command

Use a flat zip layout for backend delivery:

```bash
zip -j foundry-video-editor-backend.zip backend/server.py backend/start_server.bat backend/requirements.txt
```

Do not create nested folders inside the deliverable zip.

Regression risk: Medium.
