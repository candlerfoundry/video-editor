# CANONICAL.md - Foundry Video Editor Backend
# Source of truth for critical backend and thumbnail behavior.
# Every editing session must compare server.py to this file before changing fragile paths.
# Last updated: June 15, 2026 (bleep accuracy + audible preview — §36)
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

> **DEPRECATED June 10, 2026 (manual thumbnail flow):** the live UI no longer
> calls `/thumbnail` — frames are picked by hand (see section 21). The route,
> worker, and `/thumbnailstatus/<jobid>` are retained for backward
> compatibility (stale cached frontends) and must keep honoring this contract
> while they exist. Sections 7 and 8 describe the retained worker. The old
> standalone Thumbnails-tab editor code (`generateThumbnail`, `redoFrames`,
> `redoTitles`, `thumb-state-*`) is vestigial and unreachable.

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
    timestamps = [start_t + i * (end_t - start_t) / 29 for i in range(30)]
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
- Composite frame scoring: multiply sharpness score by brightness_weight to penalize dark/silhouette frames.
  - Hard-reject frames with mean_brightness < 15 (black cut frames).
  - brightness_weight = clamp((mean_brightness - 20) / 60, 0.15, 1.0).
  - composite = sharpness * brightness_weight.
- Claude Vision ranking prompt must note stage/auditorium context and instruct the model to strongly avoid dark silhouette, motion-blurred, and off-center frames.

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
  - compact grouped rows that still scale cleanly with 20+ projects
- Raw Dropbox paths should stay hidden by default.
- Recent-project clicks should help the user resume work, not just surface stored metadata.
- When the user selects a source video, the app must check the local project store and resume the existing project context automatically when the matching source is available.
- When a saved project already has an exact original-video path and that file still exists, resume should try that path silently first with no picker prompt.
- If the original video must be confirmed again, the fallback flow should stay lightweight and truthful:
  - ask only after the saved exact path is broken
  - use plain English such as `Oops, we've misplaced the original video for these clips. Please click the original video so we can proceed.`
  - show the expected original filename when possible
  - restore saved clips, thumbnail drafts, and export history automatically after the user picks the original video
- Resuming should not feel like starting over. Keep the main workspace focused on the current task, not project-management UI.

Canonical backend/frontend resume requirements:
- `server.py` should persist the exact original-video path under `project['source_video']['path']`.
- The backend may expose a saved-project reopen route and a local stream route so the frontend can reopen that original video without a browser picker when the path still works.
- The user should never see `relink`, `source file`, or similar technical wording in the default resume flow.

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
- The `Done Editing - Next` handoff opens a single unified save modal (`#dialog-export-flow`).
- The save modal handles folder confirmation AND thumbnail selection in one place — there is no separate post-save thumbnail dialog in the primary flow.
- The save sequence is:
  - infer the correct parent folder inside `Social Media Clips` from source-video context
  - auto-confirm folder when found (no user click required); show green ✓ notice with folder name
  - a "Change destination" toggle reveals a dropdown override; dropdown includes "Other — browse all folders…" which calls `/project_export_folder/list_all`
  - once folder is confirmed, immediately start background thumbnail frame generation and show existing drafts visually
  - user chooses: "Save without Thumbnail" or selects a frame/draft and clicks "Save with Thumbnail"
  - thumbnail is attached in the same `doExport()` call, not a second dialog
- Existing thumbnail reuse must come from project-aware local thumbnail drafts, not ad hoc Dropbox image picking.

Canonical frontend requirements in `index.html`:
- The primary clip handoff button reads `Done Editing - Next`.
- `#dialog-export-flow` is draggable by its `.dialog-header` using CSS `transform: translate()`.
  - Transform resets to `''` at the start of each `openExportFormDialog()` call so it reopens centered.
  - Uses `dataset.dragTx` / `dataset.dragTy` to accumulate translation across moves.
- Folder confirmed state: `renderExportFolderLookup()` auto-calls `confirmExportFolderSelection()` when `data.folder_exists` is true.
  - No "Use This Folder" button in the primary flow.
  - "Check Again" button is removed.
  - Folder change is opt-in via toggle.
- Thumbnail section (`#export-flow-thumb-section`) appears after folder confirmation:
  - Shows existing project thumbnail drafts as rendered canvas images with "Use This" and "Edit" buttons.
  - Simultaneously starts background thumbnail generation via `/thumbnail` → `pollThumbnailJob`.
  - Generated frames appear in `#export-flow-thumb-frames` frame-grid; double-click any frame to enlarge in a full-screen overlay.
  - "Pick frame from video" button opens inline scrub panel (`#ef-video-picker`) bounded to clip in/out range; "Use This Frame" captures and injects to front of frame list.
  - "Edit in detail…" button opens the full `#dialog-thumb` composer with selected frame preloaded.
  - "Regenerate" button restarts thumbnail job.
- Footer buttons:
  - `#btn-save-clip-folder` — "Save without Thumbnail" (ghost), always enabled once folder confirmed, calls `doExport(false)`.
  - `#btn-save-with-thumb` — "Save with Thumbnail" (primary), hidden until a frame or draft is selected, calls `doExport(true)`.
- `doExport(withThumbnail)` — if `withThumbnail=true`, attaches selected draft or builds a new lower-third draft from the selected frame and calls `attachThumbnailBlobToSavedClip()` in the same call, then no further thumbnail dialog is shown.
- `openPostSaveThumbnailDialog()` still exists but is no longer called by the primary save flow.

Canonical backend/project requirements:
- `thumbnail_drafts` remain the source of reusable thumbnail state for export handoff.
- `server.py` must expose `/project_export_folder/check`, `/project_export_folder/create`, and `/project_export_folder/list_all`.
- `/project_export_folder/list_all` must return `{ "folders": [...] }` — a flat list of all available relative folder paths inside `Social Media Clips`.
  - Regressed before the June 10, 2026 session: this route was documented here and called by `index.html` (`loadAllExportFolders`) but missing from `server.py`, so "Other — browse all folders…" 404'd. Fixed by: restoring the route (wraps `list_social_media_existing_project_folders()`, returns relative paths).
- Clip export must save into `Social Media Clips/<parent-folder>/<project-folder>/`.
- Thumbnail attachment after clip save must support patching the Airtable record with the thumbnail Dropbox URL.
- `exports` records `thumbnail_mode` and `thumbnail_draft_id` when a thumbnail is attached.
- Dropbox writes remain intentional — no file is written until the user clicks a save button.

Canonical state variables in `index.html`:
- `_efSelectedThumbFrame` — index into `_efThumbFrames`; -1 = none selected.
- `_efThumbFrames` — base64 frames generated for the export-flow thumbnail section.
- `_efSelectedDraftId` — draft id if user chose an existing draft.
- `_efThumbJobPending` — prevents duplicate concurrent thumbnail jobs from the save modal.

Regression risk: High.

---

## 18. Live thumbnail editor behavior

Canonical product rules:
- The shared thumbnail composer behaves as a live editor, not a pick-a-style wizard.
- The editor keeps AI frame suggestions and AI title suggestions, but final composition is manual and reusable.
- Preview and edit are merged: users edit directly on top of the thumbnail image.
- The Foundry logo, graphic shapes, and emoji elements may all be placed on the canvas as part of the editable draft.
- Starter layout cards (Lower Third, Centered Headline, etc.) have been removed. Layout style is still stored in the draft for backward-compat but is not surfaced as a chooser UI.

Canonical frontend requirements in `index.html`:
- After `/thumbnail` finishes, the shared composer opens straight into the live editor state.
- The editor canvas shows the selected source frame plus active treatment immediately.
- A brightness slider (30–200%) adjusts the source frame before drawing; value stored as `draft.brightness`.
- Text overlays are visible and draggable on top of the image while editing.
- The editor supports multiple text boxes in the saved draft model with add/select/remove controls.
- Supported editable text-box controls:
  - text content, position (x/y), font family, font size, text color, background color, background opacity, shadow toggle
- A "Design Elements" sidebar panel replaces the Starter Layouts section. It contains:
  - **Logo** button — toggles the Foundry logo (`TCF_Logo-Orange.png`) onto the canvas; draggable.
  - **Shapes** — 6 hand-drawn SVG elements: circle, arrow, wavy underline, star, brackets, checkmark.
  - **Emojis** — 12 preset emoji buttons.
  - All added elements appear in an "Added elements" list with per-element Remove buttons.
- All graphic elements (shapes and emoji) are stored in `draft.graphic_elements` array and are:
  - draggable on the canvas overlay layer (`.dlg-design-elem-overlay`)
  - rendered to the final exported PNG via canvas drawing in `renderThumbnailDraftToCanvas`
- The editor keeps format switching (`instagram`, `youtube_shorts`) compatible with the live draft.

Canonical editable thumbnail draft model:
- `style` — stored for backward-compat but no longer surfaced as a UI chooser.
- `brightness` — integer 30–200, default 100. Applied as `ctx.filter = brightness(N%)` before drawing frame.
- `text_boxes` — array; each entry preserves: `id`, `text`, `x`, `y`, `width`, `align`, `font_family`, `font_size`, `color`, `background_color`, `background_opacity`, `shadow`.
- `logo` — preserves: `enabled`, `placement`, `asset`, `x`, `y`, `width`.
- `graphic_elements` — array of draggable design elements. Each entry preserves:
  - `id` (unique string)
  - `type` — `'shape'` or `'emoji'`
  - For shapes: `shape_type` (`circle`|`arrow`|`underline`|`star`|`brackets`|`check`), `color`, `x`, `y`, `width`, `height`
  - For emoji: `emoji` (unicode string), `x`, `y`, `size`
- `available_frames` — base64 JPEG array from backend.
- `selected_frame_index` — integer index into `available_frames`.
- `suggested_titles`, `video_info`, `target_format`, `entry_point`, `source_filename`, `clip_start`, `clip_end`, `draft_id`.

Clip preview behavior:
- `togglePreview()` in Stage 2 uses a `timeupdate` listener to stop playback at `c.end_time`.
  - Do NOT revert this to `setTimeout` — it was unreliable for slow-loading videos.
  - This matches the existing `toggleSplitPreview()` pattern.
- Previewing a candidate does NOT commit `editorInTime` / `editorOutTime`. Only `selectAndEdit()` commits those values.

June 11, 2026 amendments (Emily's feedback session):
- NO baked style overlay in `renderThumbnailDraftToCanvas` — the dark scrim and
  orange accent line were removed. Text legibility comes from each text box's
  own background/shadow. Do not re-add `applyThumbStyleOverlay` to the draft
  render (the helper remains only for vestigial legacy preview paths).
- `.dlg-design-elem-overlay` MUST keep `pointer-events: auto` — the parent
  `#dlg-overlay-layer` is `pointer-events: none`, and without the override
  shapes cannot be selected or dragged (this was a live bug).
- `setDlgTitleText` must NOT call `dlgBuildHeroState()` — rebuilding the frame
  grid per keystroke made the source frame flicker/disappear. It updates the
  selected text-box chip label in place instead.
- The "Foundry Logo" placement card was removed; the logo is an ordinary
  drag/drop Design Element (panel button), resizable via handles.
- Each row in "Added elements" has a per-shape color input
  (`setDesignElemColor`).
- Choosing a text background color auto-raises background opacity to 70 when
  it was 0 (an invisible color change looked like a broken control).
- Frames captured in the save modal mirror into `_dlgFrames` (and vice versa)
  so the user is never asked to pick the same frame twice.
- The editor canvas column is the dominant grid column
  (`minmax(480px, 1.8fr)`).
- Emoji presets no longer exist (only Logo + Shapes); ignore older references.

Known limitations:
- The live preview uses HTML overlay layers for editing; the final PNG is re-rendered from draft data on export.
- No rotation tool yet.

Regression risk: High.

---

## 19. Transcript clip trimming and word cut-outs (added June 10, 2026)

Canonical product rules:
- The Stage 3 transcript panel is an editing surface, not just a readout.
- The orange `[` and `]` boundary markers are draggable: dragging them moves
  `editorInTime` / `editorOutTime` to the word under the pointer and stays in
  sync with the timeline handles and the Start/End time fields.
- Clicking a pre-clip / post-clip word still extends/trims (pre-existing
  behavior — keep it).
- Dragging across in-clip words strikes them out (a "cut"): the words render
  with `tx-cut` (line-through), preview playback skips them, and export
  stitches the kept segments together. Clicking any struck word restores its
  whole cut range.

Canonical frontend requirements in `index.html`:
- `editorCutRanges` — module-scope array of `{start, end}` absolute-second
  ranges, kept sorted and non-overlapping (`addCutRange` merges overlaps;
  `clampCutRangesToClip()` runs inside `updateAllEditorUI()` so cuts never
  escape the in/out range when boundaries move).
- Cuts are cleared when a new candidate/split part is selected and by
  `resetToAISuggestion()`.
- Boundary markers carry `data-boundary="in"|"out"`. The drag logic uses
  document-level pointer listeners (same pattern as the timeline handle drag)
  because `renderTranscriptAlways()` rebuilds the panel mid-drag.
- During a cut-select drag, highlight via the `tx-cut-select` class only — do
  NOT rebuild the transcript DOM on pointermove.
- `_txSuppressClick` swallows the click event that follows a completed drag so
  it does not also scrub/extend.
- `renderTimelineBar()` renders one striped `.clip-tl-cut` overlay per cut
  range inside the selected region.
- `playSelection()` uses a `timeupdate` listener (NOT `setTimeout`) so playback
  stops at the out-point and seeks over cut ranges. Do not revert to the timer.
- `doExport()` appends a `cut_ranges` form field (JSON `[[start, end], ...]`,
  absolute seconds, clamped to in/out) when cuts exist, and excludes cut words
  from `clip_transcript` via `isWordCut()`.

Canonical backend requirements in `server.py`:
- `/export_clip` accepts optional form field `cut_ranges`.
- `compute_kept_segments(starttime, endtime, cut_ranges_raw)` subtracts cuts
  from the clip range: clamps, merges overlaps, drops slivers (< 0.05s), caps
  at 20 segments, and falls back to the full `[starttime, endtime]` range on
  any invalid input (forgiving — never fail an export because of a bad cut
  payload).
- With > 1 kept segment, export runs ONE ffmpeg pass:
  `-filter_complex "[0:v]trim=..,setpts=PTS-STARTPTS[vN];[0:a]atrim=..,asetpts=PTS-STARTPTS[aN];..concat=n=N:v=1:a=1[vcat][acat];[vcat]crop=ih*9/16:ih,scale=1080:1920[vout]"`
  with `-map [vout] -map [acat]`, same codec settings as the legacy path.
- With 1 kept segment (no cuts), export runs ONE frame-accurate input-seek
  pass from the original input (UPDATED June 15 — see §36; the old two-pass
  stream-copy pre-extract snapped to keyframes and shifted bleeps/captions
  early). The >1-segment (cuts) path stays separate and is unchanged.

Word bleeping (added June 11, 2026):
- `editorBleepRanges` parallels `editorCutRanges`: ranges whose AUDIO is muted
  while video keeps playing (for words social platforms might flag).
- A Cut/Bleep mode toggle above the transcript decides what a drag across
  in-clip words does (`_txEditMode`). Bleeped words render `tx-bleep` (amber);
  clicking one restores it. Amber striped `.clip-tl-bleep` overlays show on
  the timeline. `playSelection()` mutes the video element inside bleep ranges.
- `doExport()` sends `bleep_ranges` (JSON, absolute seconds). Backend
  `parse_bleep_ranges()` clamps/merges (forgiving — invalid input means no
  bleeps, never a failed export); `build_bleep_audio_chain()` emits chained
  `volume=0:enable='between(t,a,b)'` filters. Legacy path applies it via
  `-af` with times offset by `starttime`; stitched path prefixes each
  `[0:a]...atrim` chain with absolute times. Verified: muted ranges measure
  -91 dB while surrounding audio is untouched.

Known limitations:
- Cut/bleep ranges are not yet persisted to the local project store
  (`/projects/update`); they live only in the editor session until export.
- Stitches are hard cuts (standard jump-cut style); no audio crossfade.
- Bleeps were silence originally; an audible 1 kHz censor tone was added
  (§35) and made frame-accurate on the no-cuts path (§36).

Regression risk: High.

---

## 20. Clip suggestion ordering and transcript previews (added June 10, 2026)

Canonical requirements:
- `/clips` sorts valid candidates by `hook_score` descending before returning
  (10s render first). `index.html` also re-sorts `_candidates` after fetch and
  after restoring saved candidates, so order survives either path.
- Each Stage 2 candidate card (and split part card) renders a `cand-tx`
  transcript column to the right of the card body, built client-side by
  `clipTranscriptText(start, end)` from `_wordsData`. If the transcript is not
  loaded (e.g. project restore without words), the column is simply omitted —
  never block card rendering on transcript availability.
- The back buttons (`.stage3-back`) are styled as prominent tinted buttons,
  not bare text links.

Regression risk: Low.

---

## 21. Manual thumbnail frame picking and /thumbnail_titles (added June 10, 2026)

Canonical product rules:
- Thumbnail frames are picked BY HAND from the video. There is no automatic
  frame extraction, scoring, or Claude Vision ranking in the live flow.
  ("Not a script that finds a bunch of useless images" — Emily.)
- AI title suggestions remain, via a lightweight title-only backend call.

Canonical backend requirements in `server.py`:
- `/thumbnail_titles` (POST JSON `{"clip_transcript": "..."}`) returns
  `{"titles": [...]}` synchronously using the same Foundry title prompt and
  `claude-sonnet-4-6`. Forgiving contract: empty/missing transcript or any
  failure returns `{"titles": []}` with HTTP 200 — never block the composer
  on title generation. No Whisper fallback (too slow for a sync call).

Canonical frontend requirements in `index.html`:
- `dlgGenerateThumbnail()` opens the composer editor immediately and
  auto-opens the frame picker (`dlgOpenFramePicker()`); it fires
  `fetchThumbTitles(clip_start, clip_end)` in the background and repopulates
  the titles grid when they arrive.
- `fetchThumbTitles(startT, endT)` builds the clip transcript from
  `_wordsData` (capped 3000 chars) and POSTs `/thumbnail_titles`; returns []
  on any failure.
- `dlgCapturePickedFrame()` / `efCaptureVideoFrame()` capture the paused
  video frame to JPEG base64 (max edge 1600, q 0.92) and inject it at the
  front of the frame list (cap 12). The composer capture also synthesizes
  `video_info` from the video element when none exists and mirrors frames
  into `_sharedThumbnailDraft.available_frames`.
- `dlgRedoFrames()` is an alias for `dlgOpenFramePicker()`; the "Redo Frames"
  button was removed. `dlgRedoTitles()` uses `/thumbnail_titles`.
- `startExportFlowThumb()` shows saved drafts, then opens the export-flow
  video picker (`efOpenVideoPicker()`) instead of starting a job; the
  "Regenerate" button was removed. Existing `_dlgFrames` are still reused.
- Frame pickers stay bounded to the clip in/out range where one exists.
- Captured-frame rendering must keep obeying section 11 (cover-fit,
  aspect-preserving) and section 18 (live editor draft model).

Regression risk: High — do NOT reintroduce the automatic 30-frame job into
the live flow without explicit instruction.

---

## 22. Airtable export integration (fixed June 10, 2026 — was silently broken)

History: the Airtable record creation in `/export_clip` had NEVER worked. It
authenticated with `read_api_key()` (the ANTHROPIC key from `api_key.txt`),
Airtable returned 401, and the failure was logged as a quiet warning while the
export still reported success. The frontend also never sent `item_code`, so
source-video linking could never fire, and Type detection used keywords
("theoed"), missing code-prefixed filenames like `THEO-170 - ...`.

Canonical requirements in `server.py`:
- `read_airtable_api_key()` reads `Scripts\airtable_api_key.txt`. ALL Airtable
  HTTP calls (source lookup, record creation, thumbnail PATCH) use this key.
  NEVER use `read_api_key()` (Anthropic) for Airtable.
- Source linking is by item code against the `Code` field:
  - `POD-*` codes → table `tbloVdhcMFMaMw5KC` (POD & YouTube) → link field
    `Full-Length Podcast`.
  - All other codes → table `tblS1Bk29cXyGGUdo` (3MB, UNST, TheoEd, OND) →
    link field `Full-Length Video`.
  - Linking the right field populates the dependent lookups (Featured
    Participant, transcripts, YT links) automatically — do not write to
    lookup/rollup/formula/AI/button fields directly.
- Record creation includes only writable fields: Status ("Draft"), Type (only
  when non-empty), Content Title, Clip - Dropbox URL, Thumbnail - Dropbox URL,
  Clip Transcript, and the source link field.
- Forgiving retry: if creation fails and Type was included, retry ONCE without
  Type (an invalid single-select rejects the whole record). Never fail the
  clip export because of Airtable.

Canonical requirements in `index.html`:
- `extractItemCode(filename)` returns the leading filename token matching
  `/^\s*([A-Z0-9]{2,5}-\d+(?:\.\d+)?)/` uppercased (THEO-170, POD-3.1,
  3MB-62, UNST-223). `doExport()` sends it as `item_code`.
- `detectClipType(filename)` maps by code prefix first (POD→Podcast Clip,
  3MB→3MB Clip, THEO→TheoEd Clip, UNST→Unstuck), then keyword fallbacks,
  defaulting to Podcast Clip. It only pre-fills the dropdown — the user's
  selection wins.
- Type dropdown options must stay aligned with the Airtable Type single-select
  choices (as of June 10, 2026: Podcast Clip, 3MB Clip, TheoEd Clip, Unstuck,
  Blog Promo, Course Promo). "No type" (empty value) is allowed; backend omits
  Type when empty.

Verified June 10, 2026 against the live base: a record with Type "Unstuck",
both link fields, URLs, and transcript was accepted and all lookups populated.

Regression risk: High.

---

## 23. Instagram cover-frame burn and project switching (added June 11, 2026)

Cover-frame burn:
- Emily's Zapier "Publish Video" action has no cover-URL field, and Instagram
  uses frame zero as the reel cover. When the user saves WITH a thumbnail and
  the "Use thumbnail as Instagram cover" checkbox (`#export-cover-frame`,
  checked by default) is on, `doExport()` sends `cover_frame=1`.
- Backend Step B2 in `/export_clip` prepends the saved thumbnail as ONE frame
  (1/30 s, `-t 0.04`) via a concat `filter_complex`: cover scaled/cropped to
  1080×1920 + `setsar=1`, main video branch also `setsar=1` (REQUIRED — SAR
  mismatch breaks concat), silent `anullsrc` audio for the cover, both audio
  branches `aformat`-normalized to 48 kHz stereo.
- Forgiving: if the burn fails, the un-burned clip is kept and a warning is
  logged — never fail the export.
- The standalone thumbnail PNG still saves to Dropbox/Airtable as before
  (YouTube etc. take a real thumbnail upload).

Project switching (Stage 2):
- The summary-bar link is "Save & switch project" (`switchProject()`), which
  resets the editor and shows a success banner explaining the project is
  resumable from Recent Projects. Clip candidates are already persisted via
  `clip_candidates_updated`, so switching loses nothing; multiple video
  projects can be worked in parallel.

Regression risk: Medium.

---

## 24. Composer Phase B: shape library, brand assets, zoom/pan, undo (added June 11, 2026)

Shape library:
- `SHAPE_LIBRARY` in `index.html` is the SINGLE source of truth for design
  shapes: SVG path data in a 100x100 viewBox. The overlay renders it via
  `shapeOverlaySvg()`; the export canvas renders the SAME data via
  `drawShapeToCanvas()` using `Path2D` + `DOMMatrix` (transform baked into
  path coordinates so stroke widths stay uniform under non-uniform resize).
  NEVER reintroduce separate hand-coded bezier drawing for export — overlay
  and export must come from the library or they will drift apart visually.
- Shape types: circle, scribble_circle, arrow, arrow_straight, underline,
  underline_double, highlight, star, sparkle (filled), brackets, check, and
  quote (a Georgia-serif U+201C glyph, rendered as text in both surfaces).
- All shapes are draggable, resizable (handles), and recolorable (per-row
  color input + brand swatch row in the Design Elements card).

Brand image elements:
- Brand logos live in the repo under `brand/` (served same-origin by
  Netlify): foundry-f-orange/black, foundry-name-orange/black, theoed-mic,
  theoed-name, 3mb-logo. Sourced from Dropbox
  `Operations/Logos and Branding/Logos and Graphics/` + the Brand Assets
  Airtable table (`tbl7u6D5cTuI842hH`).
- They are ordinary `graphic_elements` entries: `{type:'image', asset,
  label, x, y, width, height}` — draggable/resizable like shapes; export
  draws them object-fit:contain via `loadBrandImage()` (cached).
- The legacy single `draft.logo` model remains for old drafts but no UI
  creates it any more.
- Brand color swatches (cream #FAFAF2, navy #1E2530, orange #C84826,
  yellow #F6A85D, baby blue #D6ECF9 + CF palette) appear on the text color
  row, background color, and shape color rows.

Photo zoom/pan:
- Draft fields: `frame_zoom` (1-3), `frame_offset_x/y` (-1..1). Rendered by
  `drawImageCoverZoomed()` (cover-fit base, zoom window, offsets within the
  available margin). Zoom slider + Reset in the editor; when zoom > 1,
  dragging the empty canvas area pans the photo.
- `buildDefaultThumbnailDraft` and both composer reuse paths preserve
  `graphic_elements`, `brightness`, and the zoom fields. Backend
  `normalize_thumbnail_draft` persists them too — before June 11 it silently
  DROPPED graphic_elements and brightness from every saved draft.

Undo:
- `pushDlgUndo(label)` snapshots the draft (JSON, max 50, 600ms coalescing
  per label) before every mutating action: drags/resizes, element add/
  remove/recolor, text typing, colors, brightness, zoom, pan.
- Ctrl/Cmd+Z pops the stack while the composer dialog is open (native undo
  is left alone inside text inputs).

Airtable visibility (amends section 22):
- `/export_clip` returns `airtable_error` (HTTP body excerpt) when record
  creation fails, and the save panel shows "Added to Airtable" with a record
  link or a visible warning with the reason. Never silent again.
- Both key readers use `encoding="utf-8-sig"` (Notepad BOM tolerance).

Regression risk: High.

---

## 25. Feedback round 2 (June 11, 2026): edit modes, metadata, single thumbnail flow

Word edit modes (amends section 19):
- The Cut/Bleep mode toggle lives in a card in the editor LEFT zone (under the
  time fields), NOT at the top of the transcript — it must stay reachable
  while scrolled deep into the transcript. Labels: "Edit out" (removes words
  and their frames, red strikethrough) vs "Bleep (mute)" (keeps video,
  silences audio, amber).

Export metadata (amends section 22):
- `extractItemCode()` + code-prefix `detectClipType()` ACTUALLY shipped this
  commit — the a3c0073 patch script failed before writing and the old
  keyword-based function (defaulting to 'Podcast Clip') silently survived.
  Detection now also falls back to the active project's source filename and
  returns '' (No type) when nothing matches.
- `extractSpeakerName()` takes the last " - " segment of the cleaned source
  filename. `doExport()` sends `content_title` = "Speaker — first 5 words…";
  backend prefers it over `hook_line` for Content Title.
- `/export_clip` returns `dropbox_error` and `source_link_error` alongside
  `airtable_error`; the save panel shows three status lines (Dropbox link /
  Airtable record / full-length link). NO integration failure is silent.

Single thumbnail flow (amends sections 17/21):
- ALL frame picking and editing happens in the Create Thumbnail composer.
  The save modal's thumbnail section shows: existing drafts ("Use This" /
  "Edit"), a "Create thumbnail…" button (opens the composer), and a preview
  row when a composer-made thumbnail is ready. The save modal has NO frame
  grid and NO video picker of its own (the `ef-video-picker` functions are
  vestigial).
- `doExport(true)` attaches in priority order: composer-made `thumbnailBlob`
  → selected draft → legacy frame quick-draft.
- The Instagram cover burn ALSO lives in `/export_thumbnail` (cover_frame
  form flag): thumbnails attach after the clip is saved, so the burn prepends
  the cover onto the already-saved clip file (Dropbox links are path-based
  and survive the overwrite). The /export_clip Step B2 burn remains for the
  direct-thumbnail path.

Composer (amends sections 18/24):
- Text boxes default to background_opacity 70 — the block behind the title is
  essential because it covers burned-in captions. Do not default it to 0.
- AI title suggestions build the transcript from the EDITED clip (cut words
  excluded) via fetchThumbTitles.
- The live editor canvas renders with `omitElements: true` — elements exist
  ONLY as HTML overlays while editing. Baking them into the canvas too
  created ghost duplicates after moves/resizes. Export still draws them.
- Overlay shape paths use `vector-effect="non-scaling-stroke"`; the quote
  glyph uses uniform scaling (xMidYMid meet / min(w,h) on canvas); quote,
  sparkle, star, and brand images are aspect-locked during resize.
- Logos are added from a dropdown with image previews (BRAND_LOGOS +
  toggleLogoDropdown), not a thumbnail grid.
- TheoEd brand blues are #103EDF and #4166E6 (the earlier #D6ECF9 baby blue
  was wrong and was removed from the swatch rows).

Regression risk: High.

---

## 26. True-pixel captions (srt_to_ass) and export-time caption burning (June 11, 2026, round 3)

THE GIANT-CAPTION BUG (root cause of Emily's long-standing complaint):
- ffmpeg's `subtitles=file.srt:force_style=...` styles SRT against libass's
  default 384x288 canvas and scales to the video. On 1080x1920 vertical video
  every size is multiplied ~6.7x — Fontsize=36 rendered ~240px tall and
  MarginV pushed captions off the frame. Any caption burning MUST go through
  `srt_to_ass(srt_text, width, height)`, which emits a full ASS script with
  `PlayResX/PlayResY` set to the real video size so sizes are TRUE pixels.
- Sizing inside srt_to_ass: vertical (<0.75) 3.4% of height; horizontal
  (>1.4) 5.5%; square 4.5%. White, Arial, outline 3, bottom-center
  (alignment 2), MarginV 12%/6%/8% of height.
- `/caption` uses it (top position + yellow variants are token-replacements
  on the Style line — keep the tokens `,1,3,0,2,` and
  `&H00FFFFFF,&H00FFFFFF` stable or update both sides).
- Captions baked into a source video are pixels and CANNOT be resized or
  repositioned by the app. Cropping a captioned 16:9 master to 9:16 cuts
  captions off. The workflow answer is uncaptioned sources + burning at
  export (below). No library reburn needed — Words JSONs already exist.

Export-time caption burning:
- Save modal checkbox "Burn captions onto the clip" (unchecked by default;
  for UNCAPTIONED sources — double captions otherwise).
- Frontend `buildClipCaptionsSrt()`: words within [in,out], cut ranges
  REMOVED with times remapped onto the output timeline, bleeped words masked
  as `****`, max 10 words/entry (house style). Sent as `captions_srt`.
- Backend writes `captions.ass` via `srt_to_ass(..., 1080, 1920)` and appends
  `,subtitles=captions.ass` to the video filter in BOTH export paths; both
  ffmpeg passes now run with `cwd=tmp_dir` (relative filter filename).
- Verified by rendering: 65px text, two wrapped lines, bottom margin 230px on
  a 1080x1920 frame.

Also in round 3:
- The Cut/Bleep control is a sticky `.tx-mode-rail` to the RIGHT of the
  transcript (stays visible while scrolling). The round-2 "card under the
  time fields" was lost to the patch-abort bug (#13) and never shipped.
- `doExport(true)` thumbnailBlob priority (also lost to bug #13) is now
  verified in-file: composer-made thumbnails attach and trigger the cover
  burn via /export_thumbnail.
- Saving WITHOUT a thumbnail when one was composed asks for confirmation.

PROCESS RULE (after bug #13 struck twice): after every patch script, verify
the change exists in the FILE with grep — never trust the script's own "ok"
output, because an assert later in the same script aborts before the write.

Regression risk: High.

---

## 27. Dropbox link sync race + night fixes (June 11, 2026, late)

THE NULL-URL MYSTERY SOLVED: credentials were always fine (thumbnail links
worked). Share links are requested seconds after ffmpeg writes the clip, but
a ~50 MB file takes minutes for the Dropbox desktop client to upload — the
API returns not_found and the clip URL stayed null. Small PNGs sync in
seconds, which is why thumbnails got links.
- `_background_link_and_patch(dbx_path, airtable_record_id, field)`: daemon
  thread polls `files_get_metadata` (15 s interval, 30 min cap), creates the
  link when the file lands, and PATCHes the Airtable record's
  "Clip - Dropbox URL". Wired into /export_clip whenever the clip link fails
  with no hard error or with not_found. The user-facing dropbox_error
  explains the link will appear in Airtable automatically.

Also fixed:
- `dlgApplySuggestedTitle` no longer calls `dlgBuildHeroState()` (same
  source-frame-wipe family as bug #12); it updates title chips, the text-box
  chip, and the textarea in place.
- Photo pan: Shift+drag pans from ANYWHERE on the canvas (even over text and
  elements); plain drag still pans on empty areas. Hint text updated.
- TheoEd light blue is **#41B6E6** (Emily corrected her earlier 4166e6).
- Favicon: `<link rel="icon" ... brand/foundry-f-orange.png>` — the Chrome
  tab now shows the Foundry F.
- Cover burn VERIFIED working in production (frame-0 extraction of Emily's
  Sharpe clip shows the thumbnail; it is 1/30 s by design — imperceptible in
  playback, used by Instagram as the cover).

Regression risk: Medium.

---

## 28. In-frame caption editor + morning items (June 12, 2026)

In-frame caption editor (Stage 3):
- `#caption-overlay` sits on the editor video (inside `.edit-video-wrap`,
  position relative) and mirrors the burn: bottom 12%, width 92%, font size
  3.4% of the rendered video height, white bold with 4-way text-shadow
  outline. Synced via timeupdate/seeked; hidden during cut ranges and when
  the Captions toggle is off.
- `buildClipCaptionGroups()` is the SINGLE source of truth: groups of <=10
  kept words carrying BOTH source-time windows (overlay sync) and remapped
  output-time windows (burn). Cut words removed; bleeped words masked ****.
- Interactions: click a word = toggle emphasis (color from the Emphasis
  swatches; stored as word indices in `editorCaptionEmphasis[group]`);
  double-click = edit the group's text (stored in
  `editorCaptionOverrides[group]`; editing clears that group's emphasis
  since indices shift). Esc cancels, Enter commits.
- Style state `editorCaptionStyle = {font, emphasis_color, show}`. Fonts are
  system-safe for libass on Windows: Arial, Arial Black, Impact, Verdana,
  Tahoma, Trebuchet MS, Georgia (whitelisted backend-side in
  ASS_CAPTION_FONTS; unknown -> Arial).
- Caption edits reset on clip selection; overlay refreshes inside
  `updateAllEditorUI()` so trims/cuts re-group live.
- Burn: `doExport` sends `captions_spec` JSON {font, emphasis_color,
  groups:[{start,end,words,emphasis}]} (output timeline).
  Backend `spec_to_ass()` renders it with true-pixel sizing and
  `{\1c&HBBGGRR&}word{\1c&H00FFFFFF&}` inline emphasis runs
  (`hex_to_ass_color` converts RGB->BGR). Legacy `captions_srt` remains as
  fallback. The save modal's "Burn captions" checkbox pre-checks to match
  the editor's Captions toggle.
- VERIFIED by rendered frame: amber emphasized word inside white Arial Black
  caption at 1080x1920.

Morning items also shipped:
- Clips picker says use the UNCAPTIONED master; choosing a "(Captioned)"
  file warns (alert) before proceeding.
- Composer "Upload image…" (`dlgUploadImageAsFrame`): uploaded PNG/JPEG/WebP
  becomes a frame (downscaled to 1600, injected first, mirrored to the save
  modal). Flows through draft, PNG export, Airtable, cover burn.
- "Move photo" hand tool (`_dlgPanTool`, `.pan-mode` on the canvas wrap):
  when on, ALL canvas drags pan the photo and element overlays ignore
  pointer events; auto-off on zoom Reset. Shift+drag still works without it.
- Cut/bleep persistence: `openDoneEditingModal` upserts the edited clip with
  `cut_ranges`/`bleep_ranges`; backend `clip_selected` sanitizes (cap 40)
  and keys the upsert by hook_line+mode (keying by times created duplicates
  whenever trims changed). `restoreSavedClipEdits()` re-applies saved trims
  and word edits when the same clip (by hook line) is reopened.
- Launcher: `launcher/foundry.ico` (multi-size, from brand/foundry-f-orange),
  build.bat passes `--icon foundry.ico`, launcher.py sets the Tk window icon.
  EMILY MUST RUN `launcher/build.bat` ON HER MACHINE and replace the exe next
  to server.py (PyInstaller is Windows-side).

Regression risk: High.

---

## 29. Launcher health-check patience (June 12, 2026)

The backend imports Whisper/torch at module load — 20-60s on a cold start.
The launcher's health deadline was 15s, so it declared "Backend failed to
start" and dumped the log while the server was still loading (the log showed
a perfectly healthy startup — the werkzeug "development server" WARNING is
normal Flask boilerplate, not an error). Deadline is now 90s. Do not lower it.

Regression risk: Medium.

---

## 30. Caption wave 1: OpusClip-class captions (June 12, 2026)

Style model (`editorCaptionStyle`): preset, font, emphasis_color, mode
('group' | 'karaoke' | 'word'), all_caps, emphasis_caps, group_size (words
per screen 1-8), pop, pos_bottom (draggable 0.02-0.75), show.

Presets (CAPTION_PRESETS — keep these the source of defaults):
- bold_pop (DEFAULT): Arial Black, karaoke, ALL CAPS, 4 words, pop, amber
- karaoke: Arial, karaoke, sentence case, 6 words
- one_word: Arial Black, word-at-a-time, ALL CAPS, pop
- clean: Arial, whole-group, 8 words (the original June-12-morning look)

Group refactor: `buildClipCaptionGroups()` words are now OBJECTS
{text, srcStart, srcEnd, outStart, outEnd} grouped by group_size.
`groupDisplayWords()` returns objects; overridden text distributes timings
evenly across the group window. ALL consumers must use `.text`.

Overlay v2: per-word active highlight (karaoke) and single-word mode driven
by word srcStart/srcEnd; rAF loop while playing (timeupdate is ~4Hz, too
coarse); render-key = group:activeWord:mode. ALL CAPS + emphasis-caps applied
via captionDisplayText(). cap-pop CSS animation. Font 3.4% of video height
(6% in word mode). DRAG the overlay vertically to set pos_bottom (5px
threshold separates drag from word-click; _capSuppressClick guards).

AI pre-emphasis: `/caption_emphasis` (POST {clip_transcript} ->
{words: [...]}) — Claude picks 5-15 verbatim feature words; forgiving [] on
failure. Frontend `runAiEmphasis()` auto-runs on clip select (once per clip
range) + manual "AI emphasize" button; matches words case/punct-insensitively
(_normWord) and is re-applied on regroup (reapplyAiEmphasis). Manual click
adjustments still work on top.

Burn (`spec_to_ass` v2): spec carries mode/all_caps/emphasis_caps/pop/
pos_bottom and per-word output times. 'karaoke' emits one Dialogue per word
window with the active word color-run; 'word' emits one Dialogue per word
(+ \t pop scale); 'group' as before. pos_bottom -> MarginV. Word-mode font
6%/8.5%/7% by orientation. Legacy plain-string words and captions_srt still
accepted. VERIFIED by rendered frames (karaoke caps + amber active word at
the correct timestamp; giant single word).

Persistence: caption_style/caption_overrides/caption_emphasis ride with the
edited clip (clip_selected payload; backend stores bounded copies) and
restore in restoreSavedClipEdits().

Regression risk: High.

---

## 31. Caption wave 2 (June 12, 2026): spacing, emphasis styles, line breaks, split/merge

Spacing: overlay uses letter-spacing 0.02em + word-spacing 0.18em; the burn
sets ASS Style Spacing = 2% of font size and joins words with TWO spaces
(ASS has no word-spacing property). Keep overlay and burn in step.

Emphasis style options (any combination; editorCaptionStyle flags):
- emphasis_color_on (default on) — accent color
- emphasis_caps (default on) — ALL CAPS
- emphasis_larger — 118% inline scale ({\fscx118\fscy118})
- emphasis_pulse — one-shot grow-in (\t scale tags) each time shown
When color is OFF, the karaoke active word falls back to a 112% scale cue.
Backend `styled_run()` is the single place inline tags are built.

Font size: caption-size-slider 70-160% (size_scale) multiplies the base
orientation percentage in both overlay and spec_to_ass.

Editing: Enter inserts a LINE BREAK (plaintext newline); Ctrl/Cmd+Enter or
click-away commits; Esc cancels. Overrides keep newlines; groupDisplayWords
tokenizes lines into word objects with `br` flags; overlay renders <br>,
burn renders \N. The ✎ Edit text button opens editing for the on-screen
caption (synthesizes dblclick).

Split/merge groups: editorCaptionBreaks (array of global word start indices;
null = automatic by group_size). First split/merge materializes the current
boundaries, then "÷ Split here" adds a break at the highlighted word and
"+ Merge next" removes the next boundary. Changing words/screen resets to
automatic. Breaks persist with the edited clip (caption_breaks) and restore.

AI emphasis UX: it AUTO-RUNS when a clip opens (this confused Emily — the
button seemed dead because the work was already done). The button is now
"Re-run AI emphasis" and #ai-emphasis-status shows "✓ N words emphasized".

Verified by render: two-line break, double word gaps, letter spacing,
caps+larger emphasis with color off, 120% size.

Wave 3 backlog (agreed with Emily): per-group retiming (drag caption edges),
per-group position overrides, active-word background pill, caption background
box opacity, karaoke fill-style (words stay colored after spoken),
auto-emojis (experimental).

Regression risk: High.

---

## 32. Frontend/backend version handshake (June 12, 2026)

Netlify auto-deploys the frontend on every push, but the backend only
updates when Emily restarts the launcher. Version skew once burned Python
dict reprs into captions (new frontend sent word OBJECTS; the old backend's
spec_to_ass v1 str()-ed them into the text).

- `BACKEND_BUILD` in server.py and `EXPECTED_BACKEND_BUILD` in index.html
  MUST be bumped TOGETHER (same commit) whenever the frontend/backend
  contract changes (new routes, new form fields, changed spec shapes).
- `/health` returns `build`; `pollHealth()` compares and shows a fixed amber
  top banner telling the user to restart the launcher. Missing `build`
  (pre-handshake backends) counts as stale.
- The health poll contract from section 12 is unchanged (no early-return
  guard; 2.5s timeout; 3s interval).
- Current build id: 2026-06-15-bleepfix.

Regression risk: Medium — forgetting to bump BOTH constants makes every
user see the stale banner (or worse, silences a real mismatch).

---

## 33. Caption polish round (June 12, 2026, afternoon)

- EDITING GUARD IS ABSOLUTE: while `_capEditingGroup` is set, updateCaptionOverlay
  NEVER redraws (even force=true). The pause/seeked force-redraws were stomping
  the contentEditable buffer — that's why text edits "didn't save".
- Active-word tracking: last word that has STARTED plus an 80ms perceptual
  lead. The old fallback snapped back to word 0 during inter-word gaps, which
  made captions feel laggy/jerky vs OpusClip. Do not regress this.
- Within an unchanged karaoke group, only span classes/styles are updated
  (DOM-preserving) — no innerHTML rebuild per word, no text jitter.
- Pop animation: 220ms cubic-bezier(0.34,1.56,0.64,1) overshoot.
- Spacing settled: overlay 0.01em letters / 0.10em words; burn = single-space
  join + ASS Spacing 1% of font size. (Two spaces + 2% read too wide.)
- The AI emphasis button was REMOVED — it runs silently per clip (auto on
  open) with only the "✓ N words emphasized" note. Cost: ~3k chars in /
  ~300 tokens out per opened clip (fractions of a cent); intentionally NOT
  bundled into /clips (would compute emphasis for all candidates against
  pre-trim text).
- Handshake bumped: 2026-06-12-wave2b (both constants).

Regression risk: Medium.

---

## 34. Export speed + metadata round (June 12, 2026, afternoon)

- All export encodes use `-preset veryfast` (~35% faster than `fast`, no
  visible quality loss at crf 18 for social video).
- Cover frame is 0.10s (3 frames) — findable when scrubbing to the start,
  still effectively invisible at playback speed; frame 0 is what Instagram
  uses. `/export_thumbnail` returns `cover_burned` and the UI confirms
  "✓ Cover frame burned — scrub to the very start to see it".
- Thumbnails save into a `Thumbnails/` SUBFOLDER inside the project clip
  folder (PNG clutter complaint); Dropbox links point there. The PNG is
  still needed for the Airtable Thumbnail URL / YouTube.
- Default filename: "[CODE] - [first three clip words] - [YYYY-MM-DD HHMM].mp4"
  (clip-range timestamps were useless for telling clips apart).
- Content Title is now built SERVER-side when the source record was found:
  "[Full speaker name] — [first 4 words]…" using Instructor/Speaker (video
  table) or Featured Participant (POD table) from the lookup; the frontend's
  last-name version remains the fallback.
- KNOWN INEFFICIENCY (next): saving with a thumbnail still encodes twice
  (export + cover-burn re-encode in /export_thumbnail). Planned fix: send
  the composer thumbnail WITH /export_clip and fold the cover into the main
  filter graph — one encode total.
- Handshake: 2026-06-12-wave2c.

June 12 late-afternoon amendments:
- Filename format: `CODE - Source Title (“First three words” - Month D, YYYY).mp4`
  with TYPOGRAPHIC quotes (straight quotes are illegal Windows filename
  characters). Source title = second ' - ' part of the cleaned source name.
- Standalone thumbnail PNGs are OFF (Emily): frontend SAVE_THUMBNAIL_PNG=false
  sends save_png=0; /export_thumbnail writes the image to a TEMP dir, burns
  the cover, deletes it — no Thumbnails/ file, no shared link, no Airtable
  thumbnail patch. Flip SAVE_THUMBNAIL_PNG to restore everything.

Regression risk: Medium.

---

## 35. Editor usability round (June 12, 2026, evening)

Three click modes (`_txEditMode`: scrub DEFAULT | cut | bleep), sticky rail:
- Scrub: clicking an in-clip word seeks (the old behavior, now explicit).
- Edit out / Bleep: clicking a word applies the mode to THAT WORD — no drag
  required (Emily clicked Bleep then a word and nothing happened). Dragging
  still applies to ranges; drag-select only arms in cut/bleep modes.

Editor undo (separate from the composer's): `pushEditorUndo()` snapshots
trims, cuts, bleeps, caption style/overrides/emphasis/breaks before every
destructive op (mode clicks, drags, restores, split/join, group size,
caption text commits, emphasis toggles). ↶ Undo button in the rail +
Ctrl+Z in Stage 3 (composer closed, not in text fields). This also answers
the split/merge data-loss complaint (regrouping clears overrides — now
undoable; split/join buttons renamed 'Split caption' / 'Join with next'
with an explainer line).

AUDIBLE bleep (replaces silence):
- Export: speech muted via volume=0 windows AND a 1 kHz sine (gated by
  `build_bleep_tone_expr`, 0.25 amplitude) mixed in via
  `amix=...:normalize=0` — normalize=0 is REQUIRED (default normalization
  ducks the tone to near-silence; found by testing). Legacy path switches to
  filter_complex with a lavfi sine input when bleeping; stitched path mixes
  the tone on the full timeline then asplit→atrim per kept segment.
- Preview: WebAudio 1 kHz oscillator (startBleepTone/stopBleepTone) during
  bleep windows in Play Selection; stops on pause/end.

Transcript follow-along: `.tx-current` highlight + centered auto-scroll
follows playback (monotonic index cache; recompute on seeks); "Follow"
toggle in the transcript header, on by default.

Transport: large play/pause toggle (#btn-play-toggle, icon swaps with
play/pause events) + live playhead readout (#playhead-time, M:SS.cc).

Handshake: 2026-06-12-wave2d.

Regression risk: High.

## 36. Bleep accuracy + audible preview (June 15, 2026)

Two fixes after Emily reported the bleep "not working" in saved files.

Export accuracy (server.py, /export_clip no-cuts branch):
- ROOT CAUSE: the single-segment path pre-extracted the clip with a
  stream-copy seek (`-ss START -to END -i input -c copy`), which can only cut
  on a keyframe. The temp clip therefore started a fraction of a second before
  `starttime`, so every bleep window (and burned caption) landed early — enough
  to slide the censor tone off a short word. Demonstrated: an 8.000s request
  produced an 8.167s stream-copy clip.
- FIX: the no-cuts branch now does ONE pass straight from the original input
  with an input-seek re-encode (`-ss starttime -t (end-start) -i input`), which
  is sample-accurate and resets PTS to 0. Bleep/tone keep offset=starttime; no
  `temp_clip` pre-extract. Verified on a synthetic source: tone energy falls
  exactly inside the intended window and output duration is exact. Also fixes
  slight caption drift on the same path. The >1-segment (cuts) path was already
  accurate and is unchanged.
- Intentionally supersedes the §19 rule "no-cuts two-pass runs UNCHANGED — do
  not merge." Stitched (cuts) path stays separate.

Audible bleep preview (index.html):
- ROOT CAUSE: the WebAudio preview oscillator (`startBleepTone`) never resumed
  the AudioContext, which browsers create SUSPENDED until a user gesture — so
  the Play Selection tone was usually silent, making users think bleeping was
  broken.
- FIX: `startBleepTone()` now calls `_bleepCtx.resume()`. New
  `previewBleepBlip()` plays a ~400ms 1 kHz tone (short attack/release) the
  instant a bleep is applied — on single-word apply (line ~4093) and drag-apply
  (line ~5048) — so the user hears what the export will sound like. Applying a
  bleep is the user gesture that unlocks audio for the rest of the session.

Handshake bumped to 2026-06-15-bleepfix (BACKEND_BUILD + EXPECTED_BACKEND_BUILD).

Regression risk: Medium-High (export filter graph + audio).

## 37. macOS support (June 15, 2026)

The launcher `.exe` is Windows-only (PyInstaller); macOS refuses to run it.
The editor itself is portable — the UI is the Netlify site and `server.py` is
plain Python. Mac runs the backend directly (no exe) via a `.command` double-
click launcher in `Start Here to use Editor`; deps install with pip, ffmpeg
comes from Homebrew (find_ffmpeg already falls back to bare `ffmpeg`/`ffprobe`
on PATH, and CREATE_NO_WINDOW is 0 off Windows).

- `resolve_dropbox_root()` replaces the hardcoded `~/Dropbox`. Windows and a
  single personal Dropbox still resolve `~/Dropbox` (the marker
  `Scripts/Foundry Video Editor` lives there). On a Mac where a Business/Team
  account syncs to `~/Dropbox (Team Name)` — or a second personal account
  occupies `~/Dropbox` — it picks whichever `~/Dropbox*` folder actually holds
  the Foundry marker. Everything derived from `dropbox_root` (api keys,
  video search, clip output) follows automatically. Do NOT revert to a
  hardcoded `~/Dropbox`.
- No frontend/backend contract change, so the build handshake is NOT bumped.

Regression risk: Low (Windows path is byte-identical to before).

## 38. Thumbnail editor - Canva refresh, Stage 1 (June 16, 2026)

Frontend-only (index.html); no backend contract change, handshake NOT bumped.
- FONTS: the thumbnail font pickers (#thumb-font-select and #dlg-font-select)
  now offer only Poppins / Boogaloo / Just Another Hand / Patrick Hand SC
  (open-license, loaded from Google Fonts). Default thumbnail font is Poppins
  (was Montserrat). The old families stay in the <link> (UI/legacy paths may
  use them). Caption fonts are SYSTEM fonts (Arial/Impact/...) and were
  untouched - the thumbnail font swap does not affect captions.
- SHAPES REMOVED: the Design Elements "Shapes" grid and "Shape color" row were
  deleted from the dlg composer (Emily: not useful; a curated hand-drawn
  graphics pack will replace them later). The logo dropdown and dlg-elem-list
  stay. setSelectedElemBrandColor()/dlgAddDesignElement() remain DEFINED (used
  by logos / inline element color) - do not delete. The shape render code in
  CANONICAL section 24 persists for saved drafts that still carry shape
  graphic_elements; only the picker UI was removed.
- COLOR: the text-background custom color is now a hex circle inline with the
  brand swatches (matches the text-color picker).
- Legacy thumb-* standalone editor (style cards Caption Bar/Bold Corner/
  Kinetic Slash) was LEFT INTACT (still wired via openThumbEditor); only its
  font list was swapped. Retiring it is deferred.

NEXT (Stage 2): inline on-canvas text editing (remove the side "Selected Text"
textarea), layout reorg to use the empty space, snapping/guides, drag-resize
handles, layer order/duplicate/nudge, text align + rounded text-bg.

Regression risk: Medium (font swap + DOM removal in a fragile composer).

## 39. Thumbnail editor Stage 2a - typography (June 16, 2026)

Frontend-only (index.html); no backend contract change, handshake NOT bumped.
Applies to the dlg composer (the live editor). Text box model gained
letter_spacing (px, default 0), line_spacing (multiplier, default 1.25),
bg_pill (bool). Wired through BOTH render paths that must stay in sync:
- Editor preview = DOM .dlg-overlay-text divs (renderDialogOverlayLayer):
  letterSpacing and lineHeight scaled by 1/max(scaleX,scaleY); pill -> 999px
  border-radius.
- Export/canvas = drawThumbTextBlock: ctx.letterSpacing set BEFORE wrapText
  (so wrapping/measuring honor it) and reset to '0px' at function end so it
  does not leak to later draws; lineH = fontSize * line_spacing; pill ->
  roundRect radius min(blockH/2, blockW/2).
- Max font size raised 120 -> 400 (dlg-size-slider + setDlgFontSize clamp).
- New controls in the Text Boxes card: Letter Spacing + Line Spacing sliders,
  Align L/C/R (tx-mode-btn .selected), Rounded-box toggle. State mirrors:
  _dlgLetterSpacing/_dlgLineSpacing/_dlgAlign/_dlgBgPill, persisted via
  syncDraftFromDialogState and restored in applyDraftToDialogState +
  dlgOpenEditor.
- ctx.letterSpacing requires a modern Chromium (the target runtime) - fine.

NEXT (Stage 2b): inline on-canvas text editing (remove the side textarea),
layout reorg, snapping/guides, layer order/duplicate/nudge.

Regression risk: Medium (touches both text render paths).

## 40. Thumbnail editor Stage 2b part 1 - editing UX (June 16, 2026)

Frontend-only (index.html); no backend contract change; handshake NOT bumped.
- Inline on-canvas text editing ALREADY existed (double-click a
  .dlg-overlay-text div -> contentEditable; _dlgEditingTextId guards
  renderDialogOverlayLayer from rebuilding mid-edit). The redundant side
  "Selected Text" textarea (#dlg-title-input) was REMOVED; all its references
  are null-guarded so nothing breaks, and a hint line replaces it.
  setDlgTitleText remains defined (harmless).
- Keyboard (only when #dialog-thumb is open, not editing text, focus not in a
  field): arrows nudge the active text box (Shift = 10px); Delete/Backspace
  removes it (guarded to keep >=1 box; Ctrl+Z restores).
- dlgDuplicateActiveTextBox() clones the active box (all props via
  makeThumbnailTextBox spread) offset +30,+30. dlgMoveTextBoxLayer('forward'|
  'backward') swaps array order = z-order (later index draws on top in both the
  overlay DOM and the canvas). Buttons added to the Text Boxes header.

NEXT (Stage 2b part 2): drag snapping + alignment guides; conservative canvas
enlargement to use the empty space (full layout reorg only if wanted after).

Regression risk: Low-Medium (additive; textarea removal is null-safe).

## 41. Thumbnail editor Stage 2b part 2 - snapping + larger preview (June 16, 2026)

Frontend-only (index.html); no backend contract change; handshake NOT bumped.
- SNAPPING: dragging a text box or logo snaps its center to the canvas centre
  X / centre Y within ~16 screen px; _dlgSetGuides() shows thin orange centre
  guide lines (.dlg-guide-v / .dlg-guide-h appended to #dlg-canvas-wrap, hidden
  on mouseup). Threshold is canvas-space (16 * scaleX / scaleY).
- LARGER PREVIEW: THUMB_TARGET_FORMATS display sizes raised to reduce the dead
  space beside the canvas - instagram 360x450 -> 440x550, youtube_shorts
  320x568 -> 372x661. Render dims (1080x1350 / 1080x1920) unchanged; overlay
  positions stay correct because getThumbCanvasScale derives scale from the
  live wrap size. Tune these display values if the modal scrolls too much.
- Note: the "letters too close" default came from .dlg-overlay-text CSS
  letter-spacing:-0.02em; Stage 2a inline letterSpacing now overrides it to 0
  by default (adjustable via the Letter Spacing slider).

Regression risk: Low-Medium (drag math + preview sizing).

## 42. Thumbnail inline-edit reliability fix (June 16, 2026)

Removing the side textarea (section 40) exposed a latent bug: double-click
editing was unreliable because selecting a text box rebuilds the overlay divs,
so the browser dblclick often did not register on a stable element. Fix: the
edit logic is extracted into _dlgBeginTextEdit(bid), which re-queries the
current .dlg-overlay-text[data-box-id] element; it is entered via (a) the
dblclick handler and (b) MANUAL double-click detection in the text mousedown
branch (_dlgLastTextClick, two clicks <350ms on the same box id) so editing
survives the click->rebuild. Frontend only.

## 43. Self-hosted custom font: Handmade Sans (June 16, 2026)

Frontend-only. First purchased/self-hosted font wired into the thumbnail
editor - proves the pattern for future Creative Market fonts.
- Files committed at /fonts/HandmadeSans.woff2 (133KB) + .otf fallback (332KB).
  Netlify serves repo root (publish="."); the /* SPA rewrite is non-forced so
  real files win over it.
- @font-face family 'Handmade Sans', font-weight 100 900 (single master; the
  range avoids synthetic faux-bold when 700 is requested), font-display swap.
- Added to BOTH thumbnail font pickers (#thumb-font-select, #dlg-font-select).
- Preloaded at init (document.fonts.load) and the export path awaits
  document.fonts.ready before rendering the PNG so the burned thumbnail uses
  the real face.
- LICENSING: self-hosting serves the font publicly (webfont embedding). Confirm
  the Creative Market Webfont license covers this; a desktop-only license would
  require server-side text rendering instead.

Pattern for the next font: drop the file in /fonts/, add an @font-face block and
a picker <option>, done.

Regression risk: Low (additive; existing fonts untouched).

## 44. Thumbnail editor refinements (June 16, 2026)

Frontend-only (index.html); no backend contract change; handshake NOT bumped.
- Align buttons (L/C/R) already worked but had no selected-state CSS, so they
  looked inert. Added .tx-mode-btn.selected styling. Alignment was never broken
  - just missing visual feedback.
- Removed the "Rounded box" toggle (looked weird). bg_pill model field +
  setDlgBgPill remain but unused; render falls to the default rounded-12 radius.
- Logo transparency: logo.opacity (0-100, default 100) in
  normalizeThumbnailLogo; applied in drawThumbnailLogo (canvas globalAlpha for
  export) AND the .dlg-logo-overlay img (editor preview). New "Logo Opacity"
  slider + setDlgLogoOpacity, synced in dlgOpenEditor.
- Text box scaling: corner resize handles now scale font_size AND width by the
  same ratio (Canva-style uniform scale) instead of width only; the size
  slider/readout update live. resize-text dragging captures origFont.
- Alignment guides: _dlgSetGuides(vx, hy) positions guide lines at a canvas x/y
  (not a fixed centre); dragging a text box or logo snaps to the canvas centre
  AND any other box/logo centre, drawing a guide at the matched position - makes
  box-to-box alignment obvious.

DEFERRED (next): per-character / per-word text colour (highlight a selection and
recolour). Needs a rich-text run model + per-run canvas drawing - a dedicated
change.

Regression risk: Medium (drag math + render).

## 45. Per-character / word text colour (June 16, 2026)

Frontend-only. Text boxes gained optional box.runs = [{text, color}]; box.text
stays the plain concatenation. Single-colour text keeps box.runs=null and the
original render path (zero change).
- Editing: contentEditable switched plaintext-only -> true so selections can be
  recoloured. While a box is being edited, clicking a text-colour swatch
  recolours ONLY the selection (execCommand foreColor, styleWithCSS on); a
  delegated mousedown preventDefault on "#dlg-state-editor .color-swatch" keeps
  the selection from blurring. With no selection / not editing, a swatch sets
  the whole-box colour and clears runs.
- On blur, _dlgParseRunsFromEl walks text nodes, reads each node's effective
  colour (nearest ancestor style.color / color attr via _toHex rgb->hex),
  rebuilds runs + box.text; collapses to runs=null when a single colour.
- Canvas: drawThumbTextBlock branches when box.runs present -> _drawColoredLine
  draws each wrapped line as same-colour segments (char colours mapped from
  runs; line start advances by line.length+1, assuming single spaces). Block
  size still derived from the plain wrap.
- Overlay preview shows coloured <span> runs (escHtml) when runs present.
- KNOWN LIMITS: hard Enter line breaks are not preserved (rich CE) and the
  char->line mapping assumes single spaces; auto-wrap covers the common case.

Regression risk: Medium-High (rich contentEditable + canvas run rendering) -
needs in-browser testing.

## 46. Frame picker - bigger grid + enlarge popout (June 16, 2026)

Frontend-only. The Source Frames grid was 4-up at 320px (~74px cells, too small
to judge). Now #dlg-frame-grid is 2-up at full width (~2x bigger). Each cell
shows a magnifier button on hover (.frame-enlarge-btn) that opens
dlgOpenFramePopout(i): a fixed full-screen overlay with the frame large
(max 92vw / 78vh), "Use this frame" (selects + closes) and Close (also closes on
backdrop click or Esc). Clicking the cell still selects directly. Legacy
thumb-frame-grid unchanged.

Regression risk: Low (additive).

## 47. Logo opacity (element-based) + frame popout upscale (June 16, 2026)

Frontend-only.
- LOGO OPACITY BUG: logos added via "Add a logo" are graphic_elements
  (type:'image'), NOT _sharedThumbnailDraft.logo, so the first slider did
  nothing. setDlgLogoOpacity now sets opacity on the selected image element (or
  all image elements if none selected, plus draft.logo). Applied in BOTH the
  editor overlay (.dlg-design-elem-overlay wrap opacity) and the export canvas
  (globalAlpha around the image drawImage).
- FRAME POPOUT: the enlarge popout used max-width/height, which never upscales a
  small (low-res) frame, so it looked tiny in a big black overlay. Now
  width:92vw / height:80vh + object-fit:contain so the frame scales UP to fill.
  The magnifier button is always visible (opacity .85) for discoverability.

Regression risk: Low.
