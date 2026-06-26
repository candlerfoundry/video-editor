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

## 48. Recent Projects: delete (June 16, 2026)

Backend + frontend; handshake bumped to 2026-06-16-projdelete (new route).
- server.py: POST /projects/delete {project_id} removes <id>.json from
  project_store_dir under project_store_lock. project_id is validated against
  ^[A-Za-z0-9_-]+$ (no path traversal). Original video + exported clips are
  untouched.
- index.html: each Recent Projects item shows a x delete control
  (.recent-project-delete) -> deleteRecentProject(id) -> confirm -> POST ->
  drop from recentProjects -> re-render. stopPropagation so it does not also
  open the project.
- Names already come from getProjectLabel (project_name / bare_stem /
  filename).

Deploy: overwrite server.py next to the exe + restart launcher (the handshake
banner prompts it), then hard-refresh.

Regression risk: Low.

## 49. Editor fixes round (June 16, 2026)

Frontend-only.
- NO AUTOPLAY: the two clip-edit loaders no longer call vid.play() in their
  loadedmetadata handler (preview buttons + Play Selection still play on
  demand).
- Pick-frame scrub video (#dlg-pick-video) max-height 200 -> 520px (was tiny,
  letterboxed in black).
- BLEEP PREVIEW audible on ANY playback: the persistent #edit-video timeupdate
  now mutes speech + plays the 1 kHz tone inside bleep ranges (previously only
  Play Selection did); pause unmutes + stops the tone.
- RECENT PROJECTS: name no longer collapses (.recent-project-name max-width
  0 -> 100%, flex 1 1 100%; .sidebar-project-item-head flex-wrap) so the
  project/video name shows like the current project. Right-click a recent
  project to delete (oncontextmenu -> deleteRecentProject; the x button stays).
  Sidebar widened 240 -> 300px.

STILL OPEN: thumbnail text-box resize (corner handles only appear once the box
is selected - likely a discoverability issue); a true expandable/resizable
sidebar.

Regression risk: Low-Medium.

## 50. Thumbnail text-box + photo handling (June 16, 2026)

Frontend-only.
- RESIZE: corner handles now scale font_size ONLY (no longer box.width/x), so
  resizing no longer leaves a tiny glyph inside a giant box.
- BOX HUGS TEXT: the overlay text div is width:max-content with max-width =
  box.width (wrap cap), matching the canvas (which already fit the text). The
  selected box gets z-index 20 so it comes to the front and stays draggable
  even when boxes overlap.
- DELETE: each Text Boxes chip has an x (dlgRemoveTextBoxById) to delete that
  specific box reliably (Remove button + Delete key still work; keeps >=1).
- PHOTO: pan is no longer gated to zoom>1 - drag an empty area of the photo to
  reposition the crop at any zoom (no "Move photo" needed for empty areas; the
  button still helps when text covers the spot, and now works with text in
  frame).

STILL OPEN (bigger): full Canva-style photo - click the image to select, corner
handles to scale, no mode button.

Regression risk: Medium (drag/render).

## Free-form text boxes + Saved Thumbnails gallery (June 16, 2026)
- Thumbnail text boxes are free-form rectangles: each box has `width` AND `height`
  (centred on `x,y`). Text wraps to the width and the font auto-sizes to fill the
  rectangle via `fitFontSizeToBox`. All 8 resize handles are active on a selected
  box; dragging any handle reshapes the rectangle and anchors the opposite edge.
  The font-size slider scales the whole box (width+height) about its centre.
- `normalize_text_box` now persists `height`, `letter_spacing`, `line_spacing`,
  `bg_pill`, and `runs` (previously dropped on save).
- Backend `GET /thumbnails/list` returns every saved thumbnail draft across all
  projects, each slimmed to its selected frame. The Thumbnails tab renders these
  as a "Saved Thumbnails" gallery (`loadThumbnailGallery`) with per-card Open
  (re-open in composer) and Download (render PNG) actions. Loaded on tab open.

## Thumbnail editor Phase 1 (June 17, 2026)
- Fonts: removed Poppins, Boogaloo, Just Another Hand, Patrick Hand SC. Added
  Covered By Your Grace, Permanent Marker, Rock Salt, Shadows Into Light Two,
  Waiting for the Sunrise (Google Fonts). Default text font is now Handmade Sans.
- Create Thumbnail window is freely resizable via a bottom-right corner grip
  (startThumbResize); adds class `user-resized`; Expand/Collapse clears it.
- Canva-style color popover (openColorPopover) replaces the swatch grids for
  text color, background color, and element color. Includes a rainbow custom-hex
  control + hex field, the refined FOUNDRY_PALETTE (dropped CF Orange #E8541A,
  CF Amber #F5A623, CF Green #2D6A4F), and a Recent/in-this-design row backed by
  localStorage `fve_recent_colors` + colorsUsedInDesign().

## Thumbnail editor Phase 2 — Notes/Shapes/Squiggles pack (June 17, 2026)
- Curated 64 elements from the "Notes by Basia Stryjecka" pack into
  brand/elements/<slug>/ across speech-bubbles, large-shapes, highlights,
  arrows, squiggles, lines. Each has a working asset + small _t.png preview;
  brand/elements/manifest.json lists them.
- Single-tone elements stored as grayscale-luminance PNGs (mode LA) so they can
  be recolored to any brand color at runtime (shade = 0.45 + 0.7*gray). Two-tone
  elements kept as RGBA with detected dominant hues; recolor segments pixels by
  nearest hue and tints each region independently (color + color2).
- Picker: dialog-elements (openElementsPicker) with category tabs + preview grid;
  insertPackElement adds a draggable/resizable image element.
- Recolor engine: getPackCanvas + ensurePackUrl (cached); overlay shows tinted
  dataURL, export bakes the tinted canvas. Element list shows 1 colour trigger
  (single) or 2 (two-tone), wired to the Canva color popover.
- Fix: color popover now re-parents into the open modal <dialog> (top layer) so
  it isn't hidden behind the modal.

## Thumbnail editor Phase 3 — layout redesign (June 17, 2026)
- Editor is now a 3-column flex layout (order: left controls 300px · canvas · right
  sidebar 340px). The canvas lives in its own .thumb-editor-canvas column that
  grows to fill space and is sticky while the side controls scroll.
- fitDlgCanvas() sizes #dlg-canvas-wrap responsively from the available column
  width and the visible dialog-body height (aspect-preserving, capped ~1.1x source
  resolution), so the canvas scales up when the window is expanded / on big
  monitors instead of leaving dead space. Called on open, format change, expand,
  drag-resize, and window resize. Below 1180px the layout wraps (canvas on top).

## Thumbnail editor Phase 4 + fixes (June 17, 2026)
- Element recolor was muddy: shade band widened the texture into near-black.
  Tightened to sh = 0.80 + 0.26*gray (single) and 0.80 + 0.24*L (two-tone) so
  elements read as solid on-brand color with subtle hand-drawn texture.
- Text-box interaction fixes: dlgSelectTextBox is now lightweight (no full
  dlgOpenEditor rebuild) and pushes control state to the sidebar + refreshes
  chips; selecting only re-runs on a different box. Double-click / begin-edit now
  set the edited box as selected so the sidebar always targets the right box. A
  4px drag threshold stops accidental nudges when clicking to select/edit.
- Phase 4: design elements now support rotation (-180..180) and opacity (5-100)
  via per-selected-element sliders in the element list (setDesignElemRotation /
  setDesignElemOpacity). Rotation applies in the overlay (CSS transform) and is
  baked into export (ctx rotate about the element centre) for shapes and images.
  (Resizing a rotated element is off-axis for now — rotate after sizing.)

## Editor batch (June 17, 2026 pm)
- Clip editor: live "Length" badge (updateClipDuration) = (out-in) minus edited-out
  cut ranges within the selection; updates on time-field edits, handle drags, cuts.
- Thumbnail: clicking empty canvas deselects text/elements/logo and hides selection
  chrome (text-box dashed border now only on hover/selected/editing) for a clean
  preview. Pack image elements resize freely (aspect unlocked; logos stay locked).
- Design elements: flip H/V (setDesignElemFlip; overlay scale + export scale about
  centre) and layer order back/forward (dlgMoveElemLayer) added to the element
  controls, alongside opacity/rotation.
- Basic filled shapes circle_fill + square_fill added to SHAPE_LIBRARY with Circle/
  Oval/Square buttons (resizable; oval = stretched circle, rectangle = stretched square).
- Composer can start from a saved draft: dlgOpenSavedThumbs / dlgUseSavedThumb
  (dialog-saved-thumbs) lists this project's saved thumbnails to re-open & edit.

## Captions editor tweaks (June 17, 2026)
- Removed the Split caption / Join with next controls (confusing) — functions
  remain defined but unwired.
- Caption font size: editor slider now 70-300%; backend size_scale clamp widened
  to 0.5-3.0 (was 1.6 max), so captions can be much larger.
- Letter outline: on/off + thickness slider (0-10 level). Burns via ASS Outline
  (outline_px = font_size * level * 0.02) and previews via -webkit-text-stroke +
  paint-order. spec field outline_width.
- Letter spacing: slider 0-20 (% of font). Burns via ASS Spacing, previews via
  CSS letter-spacing. spec field letter_spacing.
- BACKEND_BUILD -> 2026-06-17-captions (server.py must be updated for these).
- NOTE: line spacing for burned captions is not supported by libass (no
  line-spacing style field), so it was not added.

## Phase 5 — launcher facelift (June 17, 2026)
- Launcher renamed "App Launcher" (window title, label, exe/spec/build names).
- New tech-logo rocket icon (launcher/rocket.ico); foundry.ico removed.
- Launcher window draws a rocket (tk Canvas) and uses rocket-themed copy:
  subtitle "Mission control — prepping for launch", status "Fueling up…" /
  "Cleared for launch", button "Launch 🚀".
- spec/build.bat bundle rocket.ico (icon + --add-data) and output "App Launcher.exe".
  NOTE: the .exe must be rebuilt via launcher/build.bat to pick this up.

## Editor fixes + crisp icon (June 17, 2026 pm2)
- Edit-out precision: cutBoundsForWords() clamps cut/bleep ranges to neighbour
  word midpoints (using _wordsData by dataset.idx) so cutting no longer grabs the
  previous/next word from Whisper's early word-start times. Applied to single
  click and drag-select.
- Clip editor "Back to results" now saves edits (clipEditorBack persists the full
  clip_selected payload + local edited_clips upsert) with a "Saving… / ✓ Edits
  saved" indicator, then returns. Edited clips show an "✎ Edited" badge in the
  results list (seedEditedKeys + _editedClipKeys; real-edit detection).
- Launcher rocket.ico regenerated with crisp native per-size frames (manual ICO
  packer, sizes 16-256) instead of one downscaled image.

## Clip editor: nudge, persistence, edited-to-top (June 17, 2026 pm3)
- Manual cut fine-tuning: #clip-cuts-list lists each edit-out cut with ±0.05s
  nudge buttons for start/end, a "preview join" play, and remove. (renderCutsList,
  nudgeCut, playCut, removeCutByIndex). Rendered from updateAllEditorUI.
- Persistence fix: selecting a clip used to write a clip_selected with NO edits,
  which the backend upsert replaced over the saved edits (wiping them). Both
  select paths now send the full payload (_buildClipEditPayload), the split path
  restores saved edits first, and a debounced autosaveClipEdits() persists after
  every edit — so edits survive relaunch.
- renderCandidates sorts edited clips to the top (then by hook_score).

## Thumbnail editor: Pick Frame freeze + resize handle + delete UX fixes (June 22, 2026)
- Pick Frame freeze: dlg-frame-picker (<dialog>) was nested inside dialog-thumb
  (also a <dialog> opened via showModal()). Calling showModal() on a nested modal
  dialog causes the browser to freeze (inner dialog's top-layer promotion inerts
  its ancestor). Fix: moved dlg-frame-picker to document root (just before
  </body>) so showModal() has no parent modal to conflict with. No JS changes
  required — dlgOpenFramePicker/dlgCloseFramePicker still find it by ID.
- Resize handles clipped: renderDialogOverlayLayer() set div.style.overflow =
  'hidden' inline, overriding the CSS class .dlg-overlay-text { overflow:visible }.
  Resize handles use transform:translate(-50%,-50%) so they extend outside the
  element boundary; overflow:hidden was clipping them in half. Fix: removed the
  inline assignment (replaced with comment). fitFontSizeToBox ensures text never
  overflows, so removing the clip is safe.
- Delete UX for single text box: Remove button silently did nothing (dlgRemoveActiveTextBox
  guards length <= 1) and the chip showed no × — no user feedback. Fix: both
  dlgOpenEditor and _refreshTextBoxChips now always render the × on every chip,
  grayed out (opacity:0.3, pointer-events:none) when boxes.length === 1. The
  Remove button (now id="dlg-textbox-remove-btn") is disabled with an explanatory
  title when it cannot act.

---

## Fix 4: subtitles filter filename= option (June 22, 2026)

**Problem:** Export failed with ffmpeg error "No option name near 'captions.ass'" on stitched (multi-cut) exports with captions. The filter chain `subtitles=captions.ass[vout]` caused ffmpeg to parse `captions.ass[vout]` as the filename instead of treating `[vout]` as the output pad label.

**Fix:** Changed both occurrences of `subtitles=captions.ass` to `subtitles=filename=captions.ass` in `backend/server.py` (lines 663 and 2671). Using the explicit named-option form tells ffmpeg where the filename value ends.

- `server.py:663` — `/caption` endpoint single-filter `-vf` path
- `server.py:2671` — `caption_filter` variable used by stitched and single-segment export paths

---

## Clip speed (1 / 1.5 / 2x) + outline-toggle preview fix + IG caption auto-render (June 22, 2026)

Build bumped to `2026-06-22-speed` (BACKEND_BUILD + EXPECTED_BACKEND_BUILD together, §32) because `/export_clip` gains an optional `speed` form field.

### 1. Clip speed — preview + burned export
- **Contract:** `/export_clip` accepts optional `speed` in {1.0, 1.5, 2.0}; anything else (or absent) -> 1.0 (original render path untouched, fully backward-compatible).
- **Frontend (index.html):** speed stored in `editorCaptionStyle.speed` (persists/restores with the clip edit like every other caption-style field). `setClipSpeed(v)` sets it, updates the `#clip-speed-row` 1x/1.5x/2x buttons, calls `applyPreviewSpeed()` (sets `edit-video.playbackRate`), and autosaves. `applyPreviewSpeed()` is re-called in the editor `loadedmetadata` handler because `playbackRate` resets on every `load()`. `syncSpeedButtons()` runs after `restoreSavedClipEdits` and in `editorUndo`. Caption timing in preview is unaffected (driven by `currentTime`, which playbackRate does not remap).
- **Backend (server.py /export_clip):** speed applied at the END of the video chain — `...{caption_filter}{speed_v_suffix}` where `speed_v_suffix = ",setpts=PTS/{S}"`. Placing `setpts` AFTER the `subtitles` filter compresses burned captions in lock-step with the footage, so the ASS timestamps need no rescaling. Audio gets a matching `atempo={S}` (valid 0.5-2.0, so 1.5 and 2.0 are single-stage). Applies to BOTH render paths:
  - Stitched (multi-cut): `[vcat]...{speed_v_suffix}[vout]` plus a separate `[acat]atempo={S}[aout]` node, mapping `[aout]`.
  - Single-segment non-bleep: appended to `-vf` plus `-af atempo={S}`.
  - Single-segment bleep: `setpts` on `[vout]`; the censor-tone `amix` (which MUST keep `normalize=0`, §35/§36) feeds an extra `[amx]atempo={S}[aout]` stage. The amix normalize=0 invariant is preserved — atempo is added AFTER the mix, never inside it.
- `speed == 1.0` short-circuits every branch to the exact original command (no setpts/atempo added).

### 2. Outline-toggle preview fix
- **Bug:** `#caption-overlay` had a hardcoded `text-shadow` (4-way black border + soft drop) in CSS, so the "Outline" checkbox (which only flips `-webkit-text-stroke`) appeared to do nothing in the preview — though the burn already respected it (`Outline=0` when off, §26/§30).
- **Fix:** removed the static `text-shadow` from CSS; it is now set in JS in the `_olOn` branch of `updateCaptionOverlay` — applied when outline is on, `'none'` when off. Preview now visibly matches the toggle and the burned ASS border.

### 3. IG caption auto-render on clip open
- **Bug:** `ensureIgCaption()` (auto-writes the Instagram caption, June 18) only fired from `dlgOpenEditor()` (the thumbnail composer). Opening a viral clip in the editor left the caption empty until the user clicked Regenerate.
- **Fix:** both clip-editor entry paths (viral + split) call `ensureIgCaption()` via `setTimeout(...,0)` after bounds/transcript are set. It no-ops if a saved caption was restored or one is already in flight — one backend call per clip, same cost as Regenerate.

---

## Mac launcher files now tracked in the repo + permanent launcher (June 22, 2026)

The Mac launch bundle lives in the repo at `Start Here to use Editor/` (previously it existed only in the Dropbox deploy folder, untracked — which is how a truncated/edited copy could drift unnoticed). Tracked files:
- `Start Here to use Editor/Launch Editor (Mac).command` — finds `server.py` (globs `$HOME/Dropbox*/...` to handle "Dropbox (Team)" naming), prefers the Setup venv, self-heals ffmpeg-full for the libass `subtitles` filter, starts the backend, waits ≤90s on `/health`, then opens the Netlify URL. Must stay running (it `wait`s on the backend).
- `Start Here to use Editor/Setup (Mac).command` — one-time install (Homebrew, python, ffmpeg-full force-linked, venv in `~/Library/Application Support/Foundry Video Editor`).
- `Start Here to use Editor/START HERE (Mac).html` — setup/troubleshooting page.

These are deployed by copying them into the Dropbox `Start Here to use Editor` folder. They are NOT served by the app (the frontend is the Netlify site); they only launch the local backend.

### Dropbox exec-bit gotcha + permanent launcher
macOS requires a `.command` file to have the Unix executable bit to be double-clickable, and **Dropbox does not carry that bit from Windows → Mac**. So a launcher kept *inside* Dropbox loses its double-click ability whenever the file re-syncs (e.g. after the team edits the code), producing: *"could not be executed because you do not have appropriate access privileges."* macOS offers no Finder way to restore the bit.

Fix (in `Setup (Mac).command` step 4): install a permanent launcher at `~/Applications/Foundry Editor.command` (outside Dropbox) that runs the Dropbox script via `bash` — `bash <file>` does not need the file to be executable, so the in-Dropbox bit no longer matters and the local launcher's own bit is set once and never re-synced away. `START HERE (Mac).html` points users at the `~/Applications` launcher and documents the access-privileges error.

One-time recovery if a user is already stuck (their `Setup`/launcher bit was stripped): run, in Terminal,
`mkdir -p ~/Applications && printf '#!/bin/bash\nbash "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/"Start Here to use Editor"/"Launch Editor (Mac).command"\n' > "$HOME/Applications/Foundry Editor.command" && chmod +x "$HOME/Applications/Foundry Editor.command"` — then launch from `~/Applications/Foundry Editor.command`. (Re-running Setup via the `bash <drag file>` method does the same thing.)

### ⚠️ Editing-session note: cloud-mount truncation
The Dropbox and session "outputs" folders are cloud-synced. Reading or `cp`-ing files through the **shell** on those mounts returns truncated/torn views of larger files (observed: `server.py`, `index.html`, and the launcher files all read short via bash even though the real files are intact). The Read/Edit tools are reliable; the shell is not. Do git work in `/tmp`, author files there, and verify with the Read tool — never trust a bash `cp`/`cat` of a cloud-mount file. This is the most likely original cause of the truncated deployed `server.py`.

---

## Backend-offline banner + Mac UX fixes (June 22, 2026)

No build bump: none of these change the frontend/backend contract (the `/health` handshake, request/response shapes are unchanged). They do require redeploying `server.py` + the Netlify frontend to take effect.

### 1. Backend-offline banner (index.html)
Returning users (those with the `fve_setup_complete` localStorage flag) stay in the app view when the backend isn't running — previously they only saw a subtle "Connecting…" header label while most actions silently no-op'd. Added `showOfflineBanner()` (a fixed top banner, sibling of `showStaleBackendBanner()`): shown after `_healthFailCount >= 2` consecutive failed health polls (debounced so it doesn't flash on momentary blips or first paint), auto-hidden the instant the backend connects. New users are unaffected (they get the setup wizard). The offline and stale banners are mutually exclusive (offline hides stale when shown).

### 2. Mac project persistence (server.py)
`project_store_dir` previously sat under `LOCALAPPDATA or tempfile.gettempdir()`. `LOCALAPPDATA` is Windows-only, so on macOS projects landed in a temp dir that macOS periodically purges → the recent-projects list vanished. Added `_stable_app_data_root()`: Windows → `%LOCALAPPDATA%`; macOS → `~/Library/Application Support`; Linux → `$XDG_DATA_HOME` or `~/.local/share`; temp only as last resort. A one-time best-effort migration copies any `*.json` projects from the old temp location into the stable store (no-op on Windows, where old == new). Source videos/exported clips were never affected (they live in Dropbox); only local edit-state was at risk.

### 3. /clips and /split read the API key the Mac-aware way (server.py)
Both endpoints hardcoded `~/Dropbox/Scripts/api_key.txt` and read it without `utf-8-sig`, bypassing `read_api_key()`/`resolve_dropbox_root()`. On a Mac whose Foundry folder syncs to `~/Dropbox (Team Name)`, the key wasn't found and clip-finding/splitting failed. Both now call `read_api_key()` (resolves the real Dropbox root, handles a BOM). This is the same helper every other endpoint already uses.

### 4. Windows-only wording neutralized
The "Backend is not connected. Make sure start_server.bat is running." alert (meaningless on Mac) now reads "Start the Foundry Video Editor launcher, then try again." Remaining Windows-centric copy in the setup wizard (Step text referencing `start_server.bat`) is a known follow-up — the wizard isn't yet platform-aware.

### Architecture note (multi-user)
The Netlify site is a static UI only; there is no shared app server. Each user runs their own local backend (`localhost:5000`) and the page talks only to theirs. Projects live in the per-machine app-data store above, so "Recent Projects" is per-user/per-machine and never shared. Shared state is only what lives in the Foundry Dropbox account (source videos, exported clips) and Airtable (clip records); concurrent exports are independent API calls with no cross-user locking.

## Caption preview WYSIWYG calibration (June 24, 2026)

Frontend-only (index.html); no backend contract change, handshake NOT bumped.
The burn path (spec_to_ass / srt_to_ass in server.py) is UNCHANGED.

**Symptom (reported on Unstuck selfie clips, not TheoEd):** the in-frame caption
preview looked much bigger than the burned captions in the saved file.

**Root cause (isolated, measured):** the preview overlay (browser/CSS) and the
burn (ffmpeg/libass) set the SAME nominal font size (height x 0.034 x size_scale),
but the two engines render that nominal size at different cap-heights. Measured on
a real 200% export: size_scale 2.0 -> ASS Fontsize 130 -> libass cap-height ~66px
on a 1080x1920 frame (cap-ratio ~0.51); Chrome renders the same font at cap-ratio
~0.72 (canvas measureText). So at identical settings the on-screen caption is
~1.4x bigger than the burn. This gap exists for EVERY clip, but is only visually
obvious on 9:16 selfie sources because the editor displays them tall (measured
element 637x1133), making the oversized preview caption physically large; on 16:9
TheoEd sources the editor video is short (~640x360) so the same proportional gap
is a small, unremarkable caption. That is why it shows on Unstuck and not TheoEd.
(libass's lower cap-ratio is its normal font-sizing model -- it sizes by the
font's vertical metrics, not the em; sandbox libass measured ~0.63 for DejaVu/
Liberation, and real Arial Black on Windows ~0.51. The browser sizes by the em.)

**Fix (updateCaptionOverlay):** multiply the overlay font by
`CAPTION_BURN_CALIBRATION = 0.70` (= libass ~0.51 / Chrome ~0.72, measured from a
real 200% export) so the preview renders the TRUE burned size for every clip and
every size_scale. The constant is empirical -- if a future ffmpeg build / font
changes the libass cap-ratio it can be re-measured (burn cap-height in px on a
1080x1920 export / (1920 * 0.034 * size_scale * 0.72)).

**Note on absolute size:** with the calibrated preview, the saved-file size is now
shown accurately. The default size_scale is left at 2.0 (caption size is fully
controlled by the now-trustworthy size slider); raising the default or the base
0.034 coefficient is a separate decision because it also affects TheoEd clips.

No build bump: captions_spec contract, routes, request/response shapes unchanged.
Deploy = Netlify push + hard-refresh; no launcher restart needed.

Regression risk: Low (single preview-only coefficient).

## Frame-picker freeze diagnostic — TEMPORARY (June 24, 2026)

Frontend-only (index.html); no backend change, handshake NOT bumped. **REMOVE
this instrumentation once the freeze cause is identified.**

Context: the thumbnail frame picker intermittently freezes the browser/app (NOT
the whole computer) when it opens with the source video loaded. Ruled out so far:
modal/dialog mechanics (tested in isolation), JS infinite loops, a stale frontend
(deployed copy verified current), and the JS open-flow itself (it ran to
completion when no video was loaded). So the freeze needs the loaded video and
lives in a video-load/seek callback that only fires on a real machine. Because it
is intermittent and may be a silent main-thread block, a passive always-on
capture was added instead of trying to catch it live.

Mechanism (all in localStorage so it survives the freeze + reload):
- Watchdog: writes `fve_wd`=Date.now() every 250ms; stops the instant the main
  thread blocks.
- Breadcrumbs: `window._fveBC(msg)` appends to `fve_freezelog` (capped 80) at the
  picker steps: `gen:before dlgOpenEditor`, `gen:before dlgOpenFramePicker`,
  `picker:open`, `picker:modal-shown`, `applyBounds:start`,
  `applyBounds:before-seek`, `applyBounds:after-seek`.
- `fve_clean_exit` (set on beforeunload) distinguishes a freeze (no clean exit)
  from a normal close.
- On the NEXT load after a non-clean exit with breadcrumbs, the console prints a
  `[FVE FREEZE DIAGNOSTIC]` block: a verdict (main-thread blocked vs
  GPU/compositor, from how long the watchdog kept ticking after the last
  breadcrumb), the last action before the freeze, and the breadcrumb table.

### WHAT TO DO WHEN A FREEZE HAPPENS
1. You don't need to do anything during the freeze itself.
2. Reload the app (hard-refresh, Ctrl+Shift+R).
3. Open the browser console (F12 → Console). An orange **[FVE FREEZE DIAGNOSTIC]**
   block from the frozen session will be printed.
4. Type `fveFreezeReport()` and press Enter — it copies the full report to your
   clipboard. Paste that to Claude.
5. The "verdict" + "last action before freeze" pinpoint the cause so the real fix
   is targeted (not a guess).

Interpretation: verdict "MAIN-THREAD BLOCKED" + the last breadcrumb = the exact
operation that hung (a JS-level hang, fixable in code). Verdict "JS kept
running … GPU/compositor" means the hang is below JS (different fix: avoid the
concurrent second `<video>` decode in the picker / reduce GPU load).

## Clip speed slider — continuous 1×–2× (June 24, 2026)

Build bumped to `2026-06-24-speedslider` (BACKEND_BUILD + EXPECTED_BACKEND_BUILD
together, §32) because `/export_clip`'s `speed` field widens from a 3-value
whitelist to a continuous range.

- **UI (index.html):** the clip-editor Speed control changed from three buttons
  (1× / 1.5× / 2×) to a range slider `#clip-speed-slider` (min 1, max 2, step
  0.05) plus a live readout `#clip-speed-readout`. `oninput="setClipSpeed(...)"`
  updates live; undo/autosave are safe because `pushEditorUndo('speed')` coalesces
  within 600ms and `autosaveClipEdits()` debounces 700ms.
- **Frontend logic:** `CLIP_SPEEDS=[1,1.5,2]` whitelist replaced by
  `clampClipSpeed(v)` (clamps to 1.0–2.0, rounds to 2 dp). `getClipSpeed()` and
  `setClipSpeed()` use it; `syncSpeedButtons()` now sets the slider value +
  readout (name kept; still called from the clip-load handlers and editorUndo).
  `applyPreviewSpeed()` (sets `edit-video.playbackRate`) is unchanged. The old
  `.speed-btn` CSS rule is now unused (left in place, harmless).
- **Backend (server.py /export_clip):** `speed = speed if speed in (1.0,1.5,2.0)`
  replaced by `speed = max(1.0, min(2.0, round(speed,2)))`. atempo is valid for
  0.5–2.0 in a single stage, so any value in 1.0–2.0 works without a 2-stage
  chain; >2.0 is intentionally not allowed. The `speed == 1.0` short-circuit (no
  setpts/atempo) and the §35/§36 `amix normalize=0` invariant are unchanged —
  atempo is still applied AFTER the censor-tone mix.

Max is 2× by request (no slow-motion / no >2×). Deploy: push (Netlify frontend)
+ redeploy server.py next to the exe + restart launcher (the handshake banner
will prompt it) + hard-refresh.

Regression risk: Low-Medium (touches the export speed path + clip-editor UI).

## Mac: double-clickable "Foundry Editor.app" built by Setup (June 25, 2026)

No backend/frontend contract change — this is launcher/setup packaging only, so
the build handshake is NOT bumped. server.py and index.html are untouched.

Non-technical interns wanted a normal app icon, not a `.command` script. Setup
now builds a real macOS app so the day-to-day launch is a single double-click.

- **What Setup builds (step 5 of `Start Here to use Editor/Setup (Mac).command`):**
  a tiny AppleScript app compiled with `osacompile` to `~/Desktop/Foundry Editor.app`.
  Its only action is to open Terminal and run the existing launcher via
  `bash "$HOME"/Dropbox*/Scripts/"Foundry Video Editor"/"Start Here to use Editor"/"Launch Editor (Mac).command"`.
  It deliberately **reuses the maintained launcher** rather than duplicating its
  logic — the app is just a friendly front door. The Terminal window shows live
  status and must stay open while editing (the launcher `wait`s on the backend).
- **Why the Desktop / why an app:** like the permanent `~/Applications` launcher,
  the app lives **outside Dropbox**, so its icon and Unix exec bit are never
  stripped by a Dropbox re-sync (the §"Dropbox exec-bit gotcha" problem). Running
  the in-Dropbox `.command` via `bash` means that file needs no exec bit either.
  The `~/Applications/Foundry Editor.command` permanent launcher is still
  installed (step 4) as a backup.
- **Icon:** `Start Here to use Editor/Foundry Editor.icns` (repo-tracked, built
  from `brand/foundry-f-orange.png` on a cream squircle). Setup copies it over
  `Contents/Resources/applet.icns`, deletes the `osacompile`-generated
  `Assets.car`, and sets `CFBundleIconFile=applet` (the asset catalog would
  otherwise override the `.icns`).
- **Signing / Gatekeeper (verified on macOS 26.5.1, June 25 2026):** editing the
  bundle invalidates `osacompile`'s signature, so Setup re-signs **ad-hoc**
  (`codesign --force --deep -s -`) — an unsigned-but-modified bundle can be
  refused as "damaged". The app is otherwise unsigned (no Developer ID).
  KEY FACT: Gatekeeper only gates files carrying the `com.apple.quarantine`
  attribute. **The Setup-built app is created locally by `osacompile`, so it has
  NO quarantine → it double-clicks with NO Gatekeeper prompt, even with
  Gatekeeper enabled.** This is the primary, intern-friendly path — and it's why
  Setup building the app (vs. shipping only the zip) matters.
  The **downloaded zip** is the opposite: the browser stamps quarantine on it, so
  it IS gated. On **macOS 15 (Sequoia) and 26 (Tahoe)** Apple REMOVED the old
  Control-click→Open bypass for unsigned/ad-hoc apps — the dialog reads "Apple
  could not verify … is free of malware" and only offers **Move to Trash / Done**
  (no Open button). The user must go to **System Settings → Privacy & Security →
  "Open Anyway"**. So do NOT document "right-click → Open" for modern macOS; tell
  zip recipients to either run Setup (which builds a clean local copy) or use
  "Open Anyway". (Older macOS still shows the right-click→Open path; the Setup
  app sidesteps the whole issue regardless.)
  NOTE: on a Mac with Gatekeeper assessment disabled (`spctl --global-disable`)
  no prompt appears at all — don't assume its absence means the app is signed.
  If a fully promptless downloaded copy is ever required, sign + notarize with an
  Apple Developer ID ($99/yr); not needed for the Setup-built path.
- **Distribution zip:** `ditto -c -k --keepParent "Foundry Editor.app" "Foundry Editor.zip"`.
  Use `ditto` (NOT `zip`) so the bundle's exec bits and resource forks survive the
  round-trip. The zip only launches the editor; it does **not** install
  ffmpeg/venv, so Setup must still have been run once on that Mac anyway — and
  since Setup also builds a clean (un-quarantined, promptless) Desktop copy, the
  zip is a secondary convenience. A downloaded zip hits the Gatekeeper block
  above, so prefer the Setup-built app; if using the zip on macOS 15+/26, open it
  via System Settings → Privacy & Security → "Open Anyway".
- **Editing-session note:** build/verify the `.app` in a local non-Dropbox path
  (e.g. `/tmp`, the Desktop) — never inside Dropbox, where the cloud mount strips
  exec bits and truncates reads (§"cloud-mount truncation").

Deploy: copy the updated `Start Here to use Editor/` files (including
`Foundry Editor.icns`) into the Dropbox `Start Here to use Editor` folder. Interns
re-run Setup once to get the Desktop app.

Regression risk: Low (no code path changed; Windows launcher untouched; new Setup
step is guarded with `|| true` fallbacks to `~/Applications/Foundry Editor.command`).

## Frame-picker freeze fix: release concurrent video decoders (June 26, 2026)

Frontend-only (`index.html`); no backend contract change, handshake NOT bumped.

Root cause (identified via the §"Frame-picker freeze diagnostic" instrumentation):
the thumbnail frame picker decodes its source in a SECOND `<video>`
(`dlg-pick-video`) while the editor's `edit-video` (and sometimes the hidden
`clips-video`) still hold a decoder for the SAME full-res source. Two concurrent
full-res hardware decodes can exhaust the GPU/decoder; when the picker then seeks
(e.g. far into a long clip) the tab freezes. Evidence: the captured breadcrumbs
end exactly at `applyBounds:after-seek`, and the picker video has NO post-seek JS
handler (`seeked`/`canplay`/`timeupdate`) — so a JS hang is ruled out, leaving the
decode/compositor path §1875 predicted. (The watchdog *verdict* was not captured
this round — the freeze is intermittent and not reproducible on demand.)

Fix (in the manual-frame-picker block of `index.html`):
- `_dlgReleaseOtherVideoDecoders()` — on picker open, for each of `edit-video` /
  `clips-video` that has a `src`: save `{src, currentTime}`, `pause()`,
  `removeAttribute('src')`, `load()` to free the decoder. Gated on
  `_dlgReleasedVideos.length` so a re-open while already open can't clobber state.
- `_dlgRestoreOtherVideoDecoders()` — on close, restore each saved `src` and seek
  back to its prior `currentTime` (once metadata is ready).
- `_dlgTeardownFramePicker()` — single teardown wired to the dialog's native
  `close` event (registered once in `dlgOpenFramePicker`). This is the load-bearing
  part: the picker `<dialog>` can be dismissed with **Escape/backdrop**, which does
  NOT call `dlgCloseFramePicker()` — only the `close` event fires. Teardown also
  releases the picker's own decoder (previously leaked after each open) and
  re-enables `dlg-btn-pick-frame` (previously left disabled on an Escape dismiss).
- `dlgCloseFramePicker()` now just calls `panel.close()` (→ `close` event →
  teardown); the capture path still reads the picker video BEFORE close, so
  clearing its `src` on close is safe.

Respects §13 (videos are `pause()`d before mutation; the `currentlyPlaying`
reference is untouched) and §21/§18 (the picker's seek + paused-frame capture and
the `_sharedThumbnailDraft` write are unchanged).

DIAGNOSTIC DELIBERATELY KEPT: contrary to §"Frame-picker freeze diagnostic"'s
"remove once identified", the breadcrumb/watchdog instrumentation STAYS for now.
The freeze is intermittent, un-reproducible on demand, and we never captured the
watchdog verdict — so the instrumentation remains as a safety net to catch any
recurrence (and confirm this fix held). Remove it only after a sustained stretch
of real use with no freeze. When reading a future capture, reload the SAME frozen
tab (do not open a second tab — a second tab's watchdog overwrites `fve_wd` and
destroys the main-thread-vs-GPU verdict).

Regression risk: Medium — touches the `edit-video` / `clips-video` lifecycle
(§13 High-risk area). Mitigated by: acting only on videos that currently have a
`src`, gating the release against double-open, and restoring on EVERY close path
(Cancel / Use This Frame / Escape / backdrop). Verify in real use: open the frame
picker, scrub/seek, capture a frame, and Escape-dismiss — the editor video must
return to its prior position each time.
