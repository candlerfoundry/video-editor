# CANONICAL.md — Foundry Video Editor Backend
# Source of truth for all critical implementations.
# Every Claude Code session must read this file and verify server.py matches before editing.
# Last updated: March 30, 2026
#
# HOW TO USE THIS FILE:
# 1. At session start: read this file in full
# 2. For each section: search server.py for the function and quote the current version
# 3. If it matches canonical: proceed
# 4. If it differs: restore canonical version before making any other changes
# 5. After all edits: re-verify each section is still present and correct
# 6. NEVER commit if any canonical feature is missing
# 7. UPDATE THIS FILE whenever a canonical function is intentionally changed,
#    a new fragile function is added, or a previously-regressed bug is fixed.
#    Always include backend/CANONICAL.md in every git add that touches server.py or index.html.

---

## 1. FLASK APP STARTUP — app.run MUST have threaded=True

if __name__ == '__main__':
    print('[startup] Starting Foundry Video Editor backend...', flush=True)
    print(f'[startup] Python: {sys.executable}', flush=True)
    print(f'[startup] ffmpeg: {FFMPEG_EXE}', flush=True)
    app.run(host='0.0.0.0', port=5000, threaded=True)

WHY: Without threaded=True Flask cannot handle concurrent requests.
The health poll (every 3s) will timeout during thumbnail generation without this.
REGRESSION RISK: High. Often dropped when server.py is restructured.

---

## 2. FFMPEG PATH RESOLUTION — find_ffmpeg()

CREATE_NO_WINDOW = 0x08000000

def find_ffmpeg():
    root = os.path.join(os.path.expanduser('~'), 'Dropbox')
    candidates = [
        os.path.join(root, 'Scripts', 'FFMPEG', 'ffmpeg.exe'),  # canonical first
        os.path.join(root, 'Scripts', 'FFMPEG', 'bin', 'ffmpeg.exe'),
        os.path.join(root, 'FFMPEG', 'ffmpeg.exe'),
        os.path.join(root, 'ffmpeg', 'bin', 'ffmpeg.exe'),
        'ffmpeg',  # system PATH fallback
    ]
    for p in candidates:
        try:
            r = subprocess.run([p, '-version'], capture_output=True, timeout=5,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                print(f'[ffmpeg] Found at: {p}', flush=True)
                return p
        except Exception:
            continue
    print('[ffmpeg] ERROR: not found in any location', flush=True)
    return None

FFMPEG_EXE = find_ffmpeg()  # called once at module startup

WHY: ffmpeg was previously hardcoded and broke when path changed.
Canonical path is Dropbox/Scripts/FFMPEG/ffmpeg.exe.
REGRESSION RISK: High. Hardcoded paths reappear when routes are rewritten.

---

## 3. VIDEO PATH CACHE — prevents os.walk blocking health poll

# At module level:
video_path_cache = {}  # {filename: full_absolute_path_to_video_file}

def find_video_in_dropbox(filename):
    dropbox_root = os.path.join(os.path.expanduser('~'), 'Dropbox')
    for root, dirs, files in os.walk(dropbox_root):
        if filename in files:
            path = os.path.join(root, filename)
            print(f'[find_video] Found: {path}', flush=True)
            return path
    print(f'[find_video] Not found: {filename}', flush=True)
    return None

WHY: os.walk across Dropbox takes 5-10 seconds and blocks Flask if called
synchronously before starting a background thread. Cache the result from
/find_json and reuse it in /thumbnail, /clips, /export_clip.
REGRESSION RISK: Medium. Cache lookup often removed when routes are rewritten.

---

## 4. /find_json ROUTE

@app.route('/find_json', methods=['POST'])
def find_json():
    try:
        filename = request.json.get('filename', '')
        print(f'[find_json] Searching for: {filename}', flush=True)

        cached = video_path_cache.get(filename)
        if cached and os.path.exists(cached):
            video_folder = os.path.dirname(cached)
        else:
            video_path = find_video_in_dropbox(filename)
            if not video_path:
                return jsonify({'json_found': False, 'error': 'Video not found in Dropbox'})
            video_path_cache[filename] = video_path
            video_folder = os.path.dirname(video_path)

        print(f'[find_json] Video folder: {video_folder}', flush=True)

        matches = glob.glob(os.path.join(video_folder, '*Transcript (Words).json'))
        if not matches:
            matches = glob.glob(os.path.join(video_folder, '*Words*.json'))
        if not matches:
            parent = os.path.dirname(video_folder)
            matches = glob.glob(os.path.join(parent, '*Transcript (Words).json'))

        print(f'[find_json] JSON matches: {matches}', flush=True)

        if not matches:
            return jsonify({'json_found': False, 'error': 'No transcript found near this video'})

        json_path = matches[0]
        with open(json_path, 'r', encoding='utf-8') as f:
            content = json.load(f)

        return jsonify({'json_found': True, 'json_path': json_path, 'json_content': content})

    except Exception as e:
        print(f'[find_json] ERROR: {e}', flush=True)
        return jsonify({'json_found': False, 'error': str(e)})

CRITICAL: Return key is json_found (NOT found). Frontend checks data.json_found.
CRITICAL: Always returns — never hangs. Entire body in try/except.
REGRESSION RISK: High. Key name 'found' vs 'json_found' has broken this repeatedly.

---

## 5. THUMBNAIL ROUTE — ASYNC JOB PATTERN

# At module level:
thumbnail_jobs = {}  # {job_id: {status, result, error, created_at}}

@app.route('/thumbnail', methods=['POST'])
def thumbnail():
    data = request.json
    filename = data.get('filename')
    clip_start = data.get('clip_start', 0)
    clip_end = data.get('clip_end', None)
    job_id = 'thumb_' + uuid.uuid4().hex[:8]
    thumbnail_jobs[job_id] = {'status': 'processing', 'result': None,
                              'error': None, 'created_at': time.time()}
    t = threading.Thread(target=run_thumbnail_job,
                         args=(job_id, filename, clip_start, clip_end))
    t.daemon = True
    t.start()
    return jsonify({'job_id': job_id, 'status': 'processing'})

@app.route('/thumbnail_status/<job_id>', methods=['GET'])
def thumbnail_status(job_id):
    job = thumbnail_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'error': 'job not found'})
    if job['status'] == 'processing':
        return jsonify({'status': 'processing'})
    if job['status'] == 'error':
        return jsonify({'status': 'error', 'error': job['error']})
    return jsonify({'status': 'complete', **job['result']})

WHY: Without async pattern, Flask blocks for 30-60s during thumbnail generation,
causing health poll timeouts and yellow 'connecting' dot.
REGRESSION RISK: Very high. Sync implementation reappears when route is rewritten.

NOTE: Current implementation uses form data (not JSON), jobid (not job_id), and
/thumbnailstatus/<jobid> (no underscore) to match index.html's polling calls.
Both the route name and field name must match what index.html sends/expects.

---

## 6. FRAME TIMESTAMP SPREAD — THE MOST-REGRESSED FIX

# Inside _thumbnail_worker(), after getting video duration:
start_t = max(17, float(clipstart or 0))
end_t = float(clipend) if clipend else total_duration
if end_t <= start_t:
    end_t = total_duration
timestamps = [start_t + i * (end_t - start_t) / 19 for i in range(20)]
print(f'[thumbnail] timestamps from {start_t:.1f}s to {end_t:.1f}s ({len(timestamps)} frames)', flush=True)

WHY: Without even distribution, all 20 frames cluster at the start of the video.
The 8 returned frames then all show the same moment.
This line has been written and lost in sessions 14b and 14c.
REGRESSION RISK: CRITICAL. Must be present. Verify every session.

NOTE: Variable names match the current _thumbnail_worker function signature:
  clipstart, clipend (function params), total_duration (local var from ffprobe).

---

## 7. FRAME EXTRACTION — PIL + numpy only, NO OpenCV

frames = []
with tempfile.TemporaryDirectory() as tmpdir:
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(tmpdir, f'frame_{i:03d}.jpg')
        subprocess.run(
            [FFMPEG_EXE, '-ss', str(ts), '-i', video_path,
             '-frames:v', '1', '-q:v', '2', out_path],
            capture_output=True, creationflags=CREATE_NO_WINDOW
        )
        if not os.path.exists(out_path):
            continue
        img = Image.open(out_path).convert('RGB')
        gray = np.array(img.convert('L')).astype(float)
        sharpness = float(np.var(np.gradient(gray)))
        with open(out_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
        frames.append({'b64': b64, 'sharpness': sharpness, 'ts': ts})
        print(f'[thumbnail] frame {i} ts={ts:.1f}s sharpness={sharpness:.1f}', flush=True)

    frames.sort(key=lambda x: x['sharpness'], reverse=True)
    top8 = frames[:8]

CRITICAL: NEVER import cv2 or opencv. Use PIL (pillow) + numpy only.
Required in requirements.txt: pillow, numpy
REGRESSION RISK: High. OpenCV gets reintroduced when thumbnail route is rewritten.

---

## 8. /clips ROUTE — KEY REQUIREMENTS

Must:
- Use Words JSON path from frontend (found by /find_json — do not re-search)
- Send to claude-sonnet-4-6 (NEVER opus)
- Include Foundry mission context in prompt
- Require 30-90 second clips in prompt
- Filter clips <25s or >95s; retry once if <3 valid remain
- Space clips 45s apart minimum
- Strip code fences: start=raw.find('['); end=raw.rfind(']'); raw=raw[start:end+1]
- Return {candidates: [{start_time, end_time, hook_score, hook_line, why_it_works}]}

WHY: Clips were previously always 10 seconds (hardcoded duration) and always
the same (stale cached JSON path). Both are fixed by passing fresh values from frontend.
REGRESSION RISK: Medium.

---

## 9. HEALTH POLL — index.html

async function pollHealth() {
    try {
        const resp = await fetch('http://localhost:5000/health', {
            signal: AbortSignal.timeout(2500)
        });
        const data = await resp.json();
        setConnectionStatus(data.status === 'ok' ? 'connected' : 'disconnected');
    } catch (e) {
        setConnectionStatus('connecting');
    }
}
pollHealth();
setInterval(pollHealth, 3000);

function setConnectionStatus(state) {
    // NO early-return guard — if (backendOnline) return caused a regression
    const dot = document.getElementById('status-dot');
    const label = document.getElementById('status-label');
    if (state === 'connected') {
        dot.style.background = '#2D6A4F';
        dot.style.animation = 'none';
        label.textContent = 'Backend connected';
    } else {
        dot.style.background = '#F5A623';
        dot.style.animation = 'blink 1s infinite';
        label.textContent = 'Connecting...';
    }
}

WHY: AbortSignal.timeout() is correct (not manual AbortController).
Early-return guard caused stale UI — removed in session 13c-2, must not return.
REGRESSION RISK: Medium.

---

## 10. SIMULTANEOUS VIDEO PLAYBACK — index.html

// At top of <script>:
let currentlyPlaying = null;

function playVideo(videoEl) {
    if (currentlyPlaying && currentlyPlaying !== videoEl) {
        currentlyPlaying.pause();
        currentlyPlaying.currentTime = 0;
    }
    currentlyPlaying = videoEl;
    videoEl.play();
}

// On back-to-results navigation:
if (currentlyPlaying) {
    currentlyPlaying.pause();
    currentlyPlaying = null;
}

WHY: Multiple videos playing simultaneously has regressed in sessions 8.5 and 13a.
REGRESSION RISK: High. currentlyPlaying gets dropped when JS is reorganized.

---

## 11. /find_json RETURN KEY CONTRACT

server.py MUST return:  { "json_found": true/false, ... }
index.html MUST check:  if (data.json_found) { ... }

NEVER use 'found' — always 'json_found'.
This mismatch has broken auto-load multiple times.
REGRESSION RISK: High.

---

## 12. ZIP BUILD COMMAND — flat format

cd /tmp/video-editor
zip -j foundry-video-editor-backend.zip backend/server.py backend/start_server.bat backend/requirements.txt

The -j flag strips folder paths. Files extract directly with no subfolder nesting.
NEVER use: zip -r foundry-video-editor-backend.zip foundry-video-editor-backend/
That creates nested folders that break the intern install workflow.
REGRESSION RISK: Medium.
