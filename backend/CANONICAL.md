# CANONICAL.md - Foundry Video Editor Backend
# Source of truth for critical backend and thumbnail behavior.
# Every editing session must compare server.py to this file before changing fragile paths.
# Last updated: April 1, 2026
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

---

## 15. Local project model and workflow structure

Canonical product rules:
- A project represents one source video, not one clip.
- Editable working data lives locally on the app install, not in Dropbox.
- Dropbox remains the source-of-truth location for source media and intentional final exports.
- Recent projects may be shared across users of the same local app installation.

Canonical backend requirements:
- `server.py` owns the first-phase local project store.
- Local project JSON files live under `%LOCALAPPDATA%/Foundry Video Editor/projects/`.
- Keep one JSON file per source-video project, keyed by the resolved source video path when available.
- `project_name` should be derived from the cleaned source filename via `bare_stem(...)`.
- `/projects/open_source` must create-or-resume the local project for a selected source video.
- `/projects/recent` must return recent project summaries for the UI.
- `/projects/update` must persist transcript metadata, clip candidates, edited clip markers, thumbnail draft markers, and export markers.
- Project persistence must stay local-only; do not write working project state back into Dropbox automatically.

Canonical frontend workflow requirements in `index.html`:
- Left navigation is grouped into:
  - `SHORT CLIPS`: `Clips`, `Thumbnails`
  - `CAPTIONS`: `Caption Videos`, `Edit Captions`
- The app is designed for interns and other first-time users, so low cognitive load should win over exposing extra system detail.
- Project context now lives in a compact, collapsible left-sidebar section only, not as large cards in the main clips workspace.
- The sidebar project section should keep the default view human-readable:
  - `Current Project`
  - `Recent Projects`
  - simple project labels
  - last modified timestamp
  - simple counts for clips, thumbnails, and exports
- Raw Dropbox paths should stay hidden by default.
- Recent-project clicks should help the user resume work, not just surface stored metadata.
- When the user selects a source video, the app must check the local project store and resume the existing project context automatically when the matching source is available.
- If the source video must be confirmed again, the relink/resume flow should stay lightweight and truthful:
  - explain that the saved project was found
  - explain that the source video may need to be confirmed once
  - restore saved clips, thumbnail drafts, and export history automatically after relink when available
- Resuming should not feel like starting over. Keep the main workspace focused on the current task, not project-management UI.

Regression risk: High.

---

## 16. Shared thumbnail composer and editable draft model

Canonical product rules:
- There is one shared thumbnail composer, not separate standalone-vs-clip editors.
- The standalone `Thumbnails` tab and the clip workflow are two entry points into that same composer.
- The clip workflow should preload composer context when available: source video, clip start/end, candidate frames, suggested titles, and target aspect ratio.
- Finished thumbnails are not auto-saved to Dropbox; only explicit export should create Dropbox output.

Canonical frontend requirements in `index.html`:
- The standalone `Thumbnails` tab launches the shared composer instead of maintaining a second independent thumbnail editor flow.
- The clip workflow launches the same composer dialog with clip context preloaded.
- The shared composer must keep working with the existing `/thumbnail` and `/thumbnailstatus/<jobid>` async backend contract.
- Thumbnail edit state should be modeled as editable project data, not just a final PNG blob.

Canonical editable thumbnail draft model:
- Save draft entries into the local project under `thumbnail_drafts`.
- Each draft should store at least:
  - `draft_id`
  - `entry_point`
  - `source_filename` / `source_path`
  - `clip_start` / `clip_end` when relevant
  - `style`
  - `target_format`
  - `selected_frame_index`
  - `available_frames`
  - `suggested_titles`
  - `selected_text_box_id`
  - `text_boxes`
  - `logo` placeholder metadata
  - `video_info`
- `text_boxes` is the forward-compatible editable structure even if only one text box is currently surfaced in the UI.
- Each text box should preserve text content, position, font, text color, background color, background opacity, and shadow state.

Canonical backend requirements:
- `/projects/update` must accept richer thumbnail draft saves and persist them locally without exporting.
- Keep local thumbnail drafts separate from `exports`.
- Backward compatibility with the earlier lightweight thumbnail save event is acceptable, but the canonical structure is the richer draft model above.

Regression risk: High.

---

## 17. Clip export handoff and thumbnail reuse

Canonical product rules:
- After clip editing, users stay in the clip workflow and move directly into export choices.
- The `Done Editing - Next` handoff should start a guided save flow, not a single all-in-one export form.
- The save sequence is now:
  - check `Social Media Clips` for the source-video project folder
  - confirm the found folder, or create/choose a folder if none exists
  - save the clip into that folder
  - ask about thumbnail handling as a separate follow-up step
- Existing thumbnail reuse must still come from project-aware local thumbnail drafts for the same source-video project, not from ad hoc Dropbox image picking.
- Existing drafts may still be opened for edit or duplicate-and-edit from the post-save thumbnail step.

Canonical frontend requirements in `index.html`:
- The primary clip handoff button should read `Done Editing - Next`.
- The initial save dialog should explain that the app is checking the source-video folder inside `Social Media Clips`.
- If a matching folder exists, the UI should confirm it with the user before saving.
- If no matching folder exists, the UI should prompt the user to create the suggested folder or choose another existing folder before saving.
- Thumbnail choices should only appear after the clip save succeeds.
- `Create New Thumbnail` must launch the shared composer with current clip context preloaded:
  - source video
  - clip start/end
  - candidate frames when already available
  - suggested titles when already available
- `Use Existing Thumbnail` must list saved project thumbnail drafts for the current source-video project context.
- Reusing a saved draft should render a fresh export PNG from the saved editable draft data, not depend on a previously flattened Dropbox image.
- The post-save thumbnail step should always offer:
  - `Skip For Now`
  - `Use Existing Thumbnail` when project drafts exist
  - `Create New Thumbnail`

Canonical backend/project requirements:
- `thumbnail_drafts` remain the source of reusable thumbnail state for export handoff.
- `server.py` must expose a lightweight folder-check/create flow for `Social Media Clips` project folders.
- Clip export must save into `Social Media Clips/<project-folder>/` rather than dropping every clip into the root folder.
- Thumbnail attachment after clip save must support saving into the same project folder and, when available, patching the existing Airtable record with the thumbnail Dropbox URL.
- `exports` may record `thumbnail_mode` and `thumbnail_draft_id` when a thumbnail is attached during clip export.
- Clip export must still keep Dropbox writes intentional: no final thumbnail or clip file should be written until the user confirms export.

Regression risk: High.

---

## 18. Live thumbnail editor behavior

Canonical product rules:
- The shared thumbnail composer now behaves like a live editor, not a pick-a-style wizard.
- The editor keeps AI frame suggestions and AI title suggestions, but final composition is manual and reusable.
- Preview and edit are effectively merged: users edit directly on top of the thumbnail image.
- The Foundry logo is optional. If `TCF_Logo-Orange.png` exists in the repo/frontend root, it may be placed and resized as part of the editable draft.

Canonical frontend requirements in `index.html`:
- After `/thumbnail` finishes, the shared composer should open straight into the live editor state.
- The editor canvas must show the selected source frame plus the active layout treatment immediately.
- Text overlays must be visible on top of the image while editing and draggable in the live preview.
- The editor must support multiple text boxes in the saved draft model and surface basic add/select/remove controls in the UI.
- Supported editable text-box controls in this phase are:
  - text content
  - position
  - font family
  - font size
  - text color
  - background color
  - background opacity
  - shadow toggle
- Supported layout starters in this phase are:
  - `lower_third`
  - `centered_headline`
  - `top_banner`
  - `bottom_banner`
  - `minimal_text`
- Layout starters are editable starting points, not locked templates.
- The editor must keep format switching (`instagram`, `youtube_shorts`) compatible with the live draft and preserve positions proportionally where possible.

Canonical editable thumbnail draft model updates:
- `style` now represents the starter layout id above. Older values (`warm_bar`, `bold_corner`, `kinetic_slash`) should continue mapping safely to the newer layout ids.
- Each `text_box` should now preserve:
  - `id`
  - `text`
  - `x` / `y`
  - `width`
  - `align`
  - `font_family`
  - `font_size`
  - `color`
  - `background_color`
  - `background_opacity`
  - `shadow`
- `logo` should now preserve:
  - `enabled`
  - `placement`
  - `asset`
  - `x` / `y`
  - `width`

Known limitations for this phase:
- Text boxes are draggable but not free-resized by drag handles yet.
- Logo placement is draggable and size-adjustable, but there is no full transform/rotation tool yet.
- The live preview uses HTML overlay layers on top of the canvas for editing, then re-renders the final PNG from the saved draft data on export/save.

Regression risk: High.
