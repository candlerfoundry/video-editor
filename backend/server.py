# =============================================================================
# CANONICAL.md RULE — READ BEFORE EDITING
# This file is governed by backend/CANONICAL.md.
# Before editing ANY function: read CANONICAL.md and compare to current code.
# After editing ANY function: update CANONICAL.md to match the new version.
# NEVER commit server.py without also committing an updated CANONICAL.md.
# =============================================================================

"""
Foundry Video Editor — Local Flask Backend
Runs on localhost:5000
"""

import datetime
import glob
import io
import os
import json
import logging
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import anthropic
import whisper
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

# The desktop launcher drains stderr but older builds may not drain stdout.
# Route app logging to stderr so verbose thumbnail jobs cannot block the backend
# by filling an unread stdout pipe.
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

app = Flask(__name__)
CORS(app)

CREATE_NO_WINDOW = 0x08000000 if platform.system() == 'Windows' else 0

STYLE_STR = "Fontname=Arial,Outline=1,Shadow=0,BorderStyle=1,Spacing=1"

# ── Dropbox portable paths ──
dropbox_root = os.path.join(os.path.expanduser('~'), 'Dropbox')
API_KEY_PATH = os.path.join(dropbox_root, 'Scripts', 'api_key.txt')


def find_ffmpeg():
    candidates = [
        os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'FFMPEG', 'ffmpeg.exe'),
        os.path.join(os.path.expanduser('~'), 'Dropbox', 'FFMPEG', 'ffmpeg.exe'),
        os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'FFMPEG', 'bin', 'ffmpeg.exe'),
        'ffmpeg',  # system PATH fallback
    ]
    for path in candidates:
        try:
            result = subprocess.run(
                [path, '-version'], capture_output=True, timeout=5,
                creationflags=CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                logger.info('[ffmpeg] Found at: %s', path)
                return path
        except Exception:
            continue
    logger.error('[ffmpeg] ERROR: ffmpeg not found in any expected location')
    return None

FFMPEG_EXE = find_ffmpeg()

logger.info('[startup] Python: %s', sys.executable)
logger.info('[startup] Working dir: %s', os.getcwd())
logger.info('[startup] ffmpeg: %s', FFMPEG_EXE)

# ── Thumbnail async job store ──
thumbnail_jobs = {}  # {job_id: {status, result, error, created_at}}

# ── Video path cache (populated by /find_json, reused by /thumbnail) ──
video_path_cache = {}  # {filename: full_absolute_path}


def find_video_in_dropbox(filename):
    """Walk Dropbox to find a video file by exact filename. Returns full path or None."""
    for root, dirs, files in os.walk(dropbox_root):
        if filename in files:
            found = os.path.join(root, filename)
            logger.info('[find_video] Found "%s" at %s', filename, found)
            return found
    logger.warning('[find_video] "%s" not found in Dropbox', filename)
    return None

# ── Whisper models (loaded once on first use) ──
_WHISPER_MODEL      = None
_WHISPER_TINY_MODEL = None

def get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        _WHISPER_MODEL = whisper.load_model("medium")
    return _WHISPER_MODEL

def get_whisper_tiny():
    global _WHISPER_TINY_MODEL
    if _WHISPER_TINY_MODEL is None:
        _WHISPER_TINY_MODEL = whisper.load_model("tiny")
    return _WHISPER_TINY_MODEL


# ── SRT helpers ──
def format_srt_time(seconds):
    ms = int(round((seconds % 1) * 1000))
    s  = int(seconds)
    m  = (s // 60) % 60
    h  = s // 3600
    s  = s % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = format_srt_time(seg["start"])
            end   = format_srt_time(seg["end"])
            text  = seg["text"].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


# ── API key ──
def read_api_key():
    try:
        with open(API_KEY_PATH, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


# ── Adaptive caption sizing ──
def get_video_dimensions(video_path, ffmpeg_exe):
    """Returns (width, height) using ffprobe. Returns (1920, 1080) as safe default on failure."""
    def _probe(ffprobe_exe):
        result = subprocess.run(
            [ffprobe_exe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'json', video_path],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        data = json.loads(result.stdout)
        w = data['streams'][0]['width']
        h = data['streams'][0]['height']
        print(f'[dimensions] {video_path}: {w}x{h}')
        return w, h

    # Derive ffprobe path from ffmpeg path; fall back to bare 'ffprobe'
    ffprobe = ffmpeg_exe.replace('ffmpeg.exe', 'ffprobe.exe') if ffmpeg_exe != 'ffmpeg' else 'ffprobe'
    try:
        return _probe(ffprobe)
    except Exception as e:
        if ffprobe != 'ffprobe':
            try:
                return _probe('ffprobe')
            except Exception:
                pass
        print(f'[dimensions] ffprobe failed, using default 1920x1080: {e}')
        return 1920, 1080


def get_video_stream_info(video_path, ffmpeg_exe):
    """
    Returns raw stream dimensions plus display dimensions that account for rotation metadata.
    """
    def _probe(ffprobe_exe):
        result = subprocess.run(
            [ffprobe_exe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,side_data_list:stream_tags=rotate',
             '-of', 'json', video_path],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        data = json.loads(result.stdout or '{}')
        stream = (data.get('streams') or [{}])[0]
        width = int(stream.get('width') or 1920)
        height = int(stream.get('height') or 1080)

        rotation = 0
        tags = stream.get('tags') or {}
        if tags.get('rotate') is not None:
            try:
                rotation = int(float(tags['rotate']))
            except Exception:
                rotation = 0

        if not rotation:
            for side_data in stream.get('side_data_list') or []:
                if side_data.get('rotation') is None:
                    continue
                try:
                    rotation = int(float(side_data['rotation']))
                    break
                except Exception:
                    continue

        rotation = rotation % 360
        if rotation in (90, 270):
            display_width, display_height = height, width
        else:
            display_width, display_height = width, height

        info = {
            'width': width,
            'height': height,
            'rotation': rotation,
            'display_width': display_width,
            'display_height': display_height,
            'orientation': 'portrait' if display_height > display_width else 'landscape',
        }
        thumb_logger.info(
            'Source stream %sx%s rotation=%s display=%sx%s orientation=%s',
            width, height, rotation, display_width, display_height, info['orientation'],
        )
        return info

    ffprobe = ffmpeg_exe.replace('ffmpeg.exe', 'ffprobe.exe') if ffmpeg_exe != 'ffmpeg' else 'ffprobe'
    try:
        return _probe(ffprobe)
    except Exception as e:
        if ffprobe != 'ffprobe':
            try:
                return _probe('ffprobe')
            except Exception:
                pass
        thumb_logger.warning('ffprobe stream probe failed, using default metadata: %s', e)
        return {
            'width': 1920,
            'height': 1080,
            'rotation': 0,
            'display_width': 1920,
            'display_height': 1080,
            'orientation': 'landscape',
        }


def get_caption_style(width, height):
    """
    Returns a dict of ffmpeg subtitle style params based on video dimensions.
    Vertical (portrait): larger font, higher vertical position, narrower margins.
    Horizontal (landscape): standard font and positioning.
    Square: intermediate values.
    """
    aspect = width / height if height > 0 else 1.78

    if aspect < 0.75:      # vertical / portrait (e.g. 9:16 iPhone, 1080x1920)
        return {
            'fontsize': 36,
            'margin_v': int(height * 0.12),
            'margin_h': int(width * 0.05),
            'bold': 1,
            'label': 'vertical',
        }
    elif aspect > 1.4:     # horizontal / landscape (e.g. 16:9)
        return {
            'fontsize': 22,
            'margin_v': int(height * 0.06),
            'margin_h': int(width * 0.04),
            'bold': 1,
            'label': 'horizontal',
        }
    else:                  # square or near-square
        return {
            'fontsize': 28,
            'margin_v': int(height * 0.08),
            'margin_h': int(width * 0.04),
            'bold': 1,
            'label': 'square',
        }


# ── Health ──
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})


# ── Caption Videos ──
@app.route("/caption", methods=["POST"])
def caption():
    """
    Accepts multipart/form-data:
      file       — the source MP4
      font_size  — integer, caption font size in points (default 18)
    Returns the captioned MP4 as a download.
    """
    if not FFMPEG_EXE:
        return jsonify({'error': 'ffmpeg not found. Expected at Dropbox\\Scripts\\FFMPEG\\ffmpeg.exe'}), 500

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    position   = request.form.get("position",   "bottom")
    text_color = request.form.get("text_color", "white")

    # Alignment: bottom-center=2, top-center=8 (ASS numpad layout)
    alignment = 8 if position == "top" else 2
    # PrimaryColour in ASS format (&HAABBGGRR)
    color_map = {"white": "&H00FFFFFF", "yellow": "&H0000FFFF"}
    primary_colour = color_map.get(text_color, "&H00FFFFFF")

    original_name = file.filename or "video.mp4"
    base_name     = os.path.splitext(os.path.basename(original_name))[0]
    output_name   = base_name + " (Captioned).mp4"

    tmp_dir = tempfile.mkdtemp()
    try:
        src_ext     = os.path.splitext(file.filename or 'video.mp4')[1].lower() or '.mp4'
        input_path  = os.path.join(tmp_dir, 'source' + src_ext)
        srt_path    = os.path.join(tmp_dir, "captions.srt")
        output_path = os.path.join(tmp_dir, output_name)

        file.save(input_path)

        # Detect video dimensions and choose adaptive caption style
        width, height = get_video_dimensions(input_path, FFMPEG_EXE)
        cap_style = get_caption_style(width, height)
        print(f'[captions] Orientation: {cap_style["label"]} — fontsize {cap_style["fontsize"]}')

        style = (
            f"Fontsize={cap_style['fontsize']},{STYLE_STR},"
            f"Alignment={alignment},PrimaryColour={primary_colour},"
            f"MarginV={cap_style['margin_v']},MarginH={cap_style['margin_h']}"
        )

        # Transcribe with Whisper medium
        model  = get_whisper_model()
        result = model.transcribe(input_path)
        write_srt(result["segments"], srt_path)

        # Burn subtitles with ffmpeg
        # Use cwd=tmp_dir and relative filename to avoid Windows path escaping issues
        cmd = [
            FFMPEG_EXE, "-y",
            "-i", input_path,
            "-vf", f"subtitles=captions.srt:force_style='{style}'",
            "-c:a", "copy",
            output_path,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            cwd=tmp_dir,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[-800:]
            return jsonify({"error": "ffmpeg failed: " + err}), 500

        with open(output_path, "rb") as f:
            video_bytes = f.read()

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return send_file(
        io.BytesIO(video_bytes),
        as_attachment=True,
        download_name=output_name,
        mimetype="video/mp4",
    )


# ── Transcribe ──
@app.route("/transcribe", methods=["POST"])
def transcribe():
    """
    Accepts: { "file_path": "...", "model": "tiny"|"medium" }
    Returns: { "transcript": "...", "srt_path": "...", "words_path": "..." }
    Session 2/3: wire Whisper here.
    """
    data = request.get_json(force=True)
    file_path = data.get("file_path", "")
    base = os.path.splitext(file_path)[0]

    return jsonify({
        "status": "stub",
        "message": "Whisper transcription not yet implemented",
        "input": file_path,
        "srt_path": base + " (Time-Stamped).srt",
        "clean_path": base + " (Clean).txt",
        "words_path": base + " (Words).json",
    })


# ── Find JSON ──
SUFFIX_PATTERNS = [
    r'\s*-\s*Horizontal\s*-\s*Uncaptioned',
    r'\s*-\s*Vertical\s*-\s*Uncaptioned',
    r'\s*\(Uncaptioned\)',
    r'\s*-\s*Uncaptioned',
    r'\s*\(Captioned\)',
    r'\s*-\s*Horizontal',
    r'\s*-\s*Vertical',
]

def bare_stem(name):
    stem = os.path.splitext(name)[0]
    for pat in SUFFIX_PATTERNS:
        stem = re.sub(pat, '', stem, flags=re.IGNORECASE).strip()
    return stem


@app.route('/find_json', methods=['POST'])
def find_json():
    try:
        data = request.json
        filename = data.get('filename', '')
        print(f'[find_json] Looking for video: {filename}')

        # Check cache first; fall back to os.walk
        video_path = video_path_cache.get(filename)
        if video_path and not os.path.exists(video_path):
            print(f'[find_json] Cached path stale, re-walking: {video_path}')
            video_path = None

        if not video_path:
            video_path = find_video_in_dropbox(filename)

        if not video_path:
            print(f'[find_json] ERROR: video file not found in Dropbox')
            return jsonify({'json_found': False, 'error': 'Video file not found in Dropbox'})

        # Cache for later use by /thumbnail and other routes
        video_path_cache[filename] = video_path
        print(f'[cache] Stored path for {filename}', flush=True)

        video_folder = os.path.dirname(video_path)
        print(f'[find_json] Found video at: {video_path}')
        print(f'[find_json] Searching folder: {video_folder}')
        print(f'[find_json] Files in folder: {os.listdir(video_folder)}')

        # Search same folder for Words JSON
        matches = glob.glob(os.path.join(video_folder, '*Transcript (Words).json'))
        print(f'[find_json] Glob matches in same folder: {matches}')

        if not matches:
            # Try parent folder one level up
            parent_folder = os.path.dirname(video_folder)
            matches = glob.glob(os.path.join(parent_folder, '*Transcript (Words).json'))
            print(f'[find_json] Glob matches in parent folder: {matches}')

        if not matches:
            print(f'[find_json] FAILED: no Words JSON found near {video_folder}')
            return jsonify({'json_found': False, 'error': 'No transcript found near this video'})

        json_path = matches[0]
        print(f'[find_json] SUCCESS: {json_path}')

        with open(json_path, 'r', encoding='utf-8') as f:
            content = json.load(f)

        return jsonify({
            'json_found': True,
            'json_path': json_path,
            'json_filename': os.path.basename(json_path),
            'json_content': content
        })

    except Exception as e:
        print(f'[find_json] ERROR: {e}', flush=True)
        return jsonify({'json_found': False, 'error': str(e)})


# ── Generate Transcript ──
@app.route("/generate_transcript", methods=["POST"])
def generate_transcript():
    """
    Accepts: { "file_path": "C:/path/to/Video.mp4" }
    Runs Whisper medium, saves (Words).json alongside source.
    Returns: { "json_path": "..." }
    """
    data = request.get_json(force=True)
    file_path = data.get("file_path", "").strip()
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "file_path not found"}), 400

    directory = os.path.dirname(file_path)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    out_path = os.path.join(directory, stem + ' - Transcript (Words).json')

    # Copy to clean temp path to handle special characters
    tmp_dir = tempfile.mkdtemp()
    try:
        src_ext     = os.path.splitext(file_path)[1].lower() or '.mp4'
        clean_input = os.path.join(tmp_dir, 'source' + src_ext)
        shutil.copy2(file_path, clean_input)

        model = get_whisper_model()
        result = model.transcribe(clean_input, word_timestamps=True)

        words = []
        for seg in result.get('segments', []):
            for w in seg.get('words', []):
                words.append({
                    'word':  w.get('word', ''),
                    'start': round(w.get('start', 0.0), 3),
                    'end':   round(w.get('end',   0.0), 3),
                })

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(words, f, ensure_ascii=False, indent=2)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({"json_path": out_path})


# ── Generate Transcript (upload) ──
@app.route("/generate_transcript_upload", methods=["POST"])
def generate_transcript_upload():
    """
    Accepts multipart/form-data: file (MP4).
    Runs Whisper medium, returns { "words": [...] }.
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    tmp_dir = tempfile.mkdtemp()
    try:
        src_ext    = os.path.splitext(file.filename or 'video.mp4')[1].lower() or '.mp4'
        input_path = os.path.join(tmp_dir, 'source' + src_ext)
        file.save(input_path)

        model = get_whisper_model()
        result = model.transcribe(input_path, word_timestamps=True)

        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []):
                words.append({
                    "word":  w.get("word", ""),
                    "start": round(w.get("start", 0.0), 3),
                    "end":   round(w.get("end",   0.0), 3),
                })
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({"words": words})


# ── Thumbnails — async job worker ──

def _thumbnail_worker(job_id, filename, clipstart, clipend, clip_transcript):
    import base64
    from PIL import Image as PILImage
    import numpy as np

    try:
        # Clean up jobs older than 10 minutes
        now = datetime.datetime.utcnow()
        for jid in list(thumbnail_jobs.keys()):
            age = (now - thumbnail_jobs[jid].get('created_at', now)).total_seconds()
            if age > 600:
                thumbnail_jobs.pop(jid, None)

        # Check cache first (populated by /find_json); os.walk only as fallback
        video_path = video_path_cache.get(filename)
        if video_path and not os.path.exists(video_path):
            thumb_logger.info('Job %s cached path stale, re-walking: %s', job_id, video_path)
            video_path = None

        if not video_path:
            thumb_logger.info('Job %s cache miss for "%s"; searching Dropbox', job_id, filename)
            video_path = find_video_in_dropbox(filename)
            if video_path:
                video_path_cache[filename] = video_path

        if not video_path:
            thumb_logger.warning('Job %s failed: video "%s" not found in Dropbox', job_id, filename)
            thumbnail_jobs[job_id] = {
                'status': 'error',
                'error': f'Video "{filename}" not found in Dropbox',
            }
            return

        thumb_logger.info('Job %s started for "%s"', job_id, video_path)

        tmp_dir = tempfile.mkdtemp()
        try:
            stream_info = get_video_stream_info(video_path, FFMPEG_EXE)

            # ── Get total duration ──
            ffprobe_exe = FFMPEG_EXE.replace('ffmpeg.exe', 'ffprobe.exe') if (
                FFMPEG_EXE and FFMPEG_EXE != 'ffmpeg'
            ) else 'ffprobe'
            if not os.path.isfile(ffprobe_exe):
                ffprobe_exe = 'ffprobe'
            probe = subprocess.run(
                [ffprobe_exe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'json', video_path],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
            )
            try:
                total_duration = float(json.loads(probe.stdout)['format']['duration'])
            except Exception:
                total_duration = 90.0

            # Always sample full video for thumbnail frame selection
            # (user wants to pick the best moment from anywhere in the video)
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

            # ── Extract frames ──
            scored = []
            frame_errors = 0
            for i, ts in enumerate(timestamps):
                fp = os.path.join(tmp_dir, f'frame_{i:03d}.jpg')
                subprocess.run(
                    [FFMPEG_EXE, '-y', '-ss', str(ts), '-i', video_path,
                     '-frames:v', '1', '-q:v', '2', fp],
                    capture_output=True, creationflags=CREATE_NO_WINDOW
                )
                if not os.path.isfile(fp):
                    continue
                try:
                    img = PILImage.open(fp)
                    arr = np.array(img.convert('L'), dtype=float)
                    sharpness = float(np.var(np.gradient(arr)))
                    thumb_logger.debug(
                        'Job %s frame %s ts=%.1fs sharpness=%.1f',
                        job_id, i, ts, sharpness,
                    )
                    scored.append((sharpness, fp))
                except Exception as fe:
                    frame_errors += 1
                    thumb_logger.debug('Job %s frame %s failed to score: %s', job_id, i, fe)

            if not scored:
                thumb_logger.warning('Job %s failed: could not extract usable frames', job_id)
                thumbnail_jobs[job_id] = {
                    'status': 'error',
                    'error': 'Could not extract usable frames from video',
                }
                return

            scored.sort(reverse=True)
            scored = scored[:8]
            thumb_logger.info(
                'Job %s extracted %s usable frames (%s frame errors), keeping top %s',
                job_id, len(scored), frame_errors, len(scored),
            )

            frame_b64s = []
            encoded_sizes = []
            for _, fp in scored:
                try:
                    pil = PILImage.open(fp).convert('RGB')
                    original_size = pil.size
                    pil.thumbnail((1600, 1600), PILImage.LANCZOS)
                    buf = io.BytesIO()
                    pil.save(buf, 'JPEG', quality=92, optimize=True)
                    encoded_sizes.append((original_size, pil.size))
                    thumb_logger.debug(
                        'Job %s encoded frame %s: %sx%s -> %sx%s',
                        job_id, os.path.basename(fp),
                        original_size[0], original_size[1], pil.size[0], pil.size[1],
                    )
                    frame_b64s.append(base64.b64encode(buf.getvalue()).decode())
                except Exception as ee:
                    thumb_logger.debug('Job %s frame encode failed for %s: %s', job_id, fp, ee)

            if not frame_b64s:
                thumb_logger.warning('Job %s failed: frame encoding produced no output', job_id)
                thumbnail_jobs[job_id] = {'status': 'error', 'error': 'Frame encoding failed'}
                return
            if encoded_sizes:
                largest = max(size[1][0] * size[1][1] for size in encoded_sizes)
                thumb_logger.info('Job %s encoded %s frames for ranking (largest encoded frame area=%s)', job_id, len(frame_b64s), largest)

            # ── Claude Vision ranking ──
            api_key_val = read_api_key()
            client      = anthropic.Anthropic(api_key=api_key_val)
            try:
                content = []
                for i, b64 in enumerate(frame_b64s):
                    content.append({"type": "text", "text": f"Frame {i}:"})
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                    })
                content.append({
                    "type": "text",
                    "text": (
                        "Rank these frames best to worst for a YouTube thumbnail. "
                        "Prefer: eyes open and engaged, mouth not wide open mid-word, "
                        "warm/confident expression, good posture, sharp focus. "
                        f"There are {len(frame_b64s)} frames (0-indexed). "
                        "Return ONLY a JSON array of ALL indices, best first. Example: [2,0,3,1]"
                    )
                })
                rank_msg = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=120,
                    messages=[{"role": "user", "content": content}]
                )
                rank_text = rank_msg.content[0].text.strip()
                s = rank_text.find('['); e = rank_text.rfind(']')
                if s != -1 and e != -1:
                    ranks   = json.loads(rank_text[s:e+1])
                    seen    = set()
                    ordered = []
                    for idx in ranks:
                        if isinstance(idx, int) and 0 <= idx < len(frame_b64s) and idx not in seen:
                            ordered.append(frame_b64s[idx]); seen.add(idx)
                    for idx in range(len(frame_b64s)):
                        if idx not in seen: ordered.append(frame_b64s[idx])
                    frame_b64s = ordered
            except Exception as ve:
                thumb_logger.warning('Job %s vision ranking failed; keeping sharpness order: %s', job_id, ve)

            # FIX 5: titles from clip_transcript (clip-specific range, sent by frontend)
            thumb_logger.info('Job %s clip transcript length=%s chars', job_id, len(clip_transcript))
            if not clip_transcript:
                # Fall back: Whisper tiny on the full video
                tiny       = get_whisper_tiny()
                w_result   = tiny.transcribe(video_path)
                clip_transcript = (w_result.get("text") or "")[:3000]
                thumb_logger.info('Job %s used Whisper fallback transcript length=%s chars', job_id, len(clip_transcript))

            titles = []
            try:
                title_msg = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=800,
                    messages=[{"role": "user", "content": (
                        "The Candler Foundry produces faith-based video content for clergy, scholars, "
                        "and the spiritually curious public. The best thumbnail titles are short, surprising, "
                        "and emotionally resonant — a question or statement that makes someone stop scrolling. "
                        "Avoid jargon, church-speak, or academic language. Aim for human, honest, direct.\n\n"
                        "Generate exactly 8 title options. Rules:\n"
                        "- MAX 60 characters each — strip any over 60 chars\n"
                        "- Short, punchy, emotionally direct: a provocative question or bold statement\n"
                        "- No filler phrases, no colons that only pad length\n"
                        "Return ONLY a JSON array of 8 strings, no markdown.\n\n"
                        f"Clip transcript ({len(clip_transcript)} chars):\n{clip_transcript}"
                    )}]
                )
                raw = title_msg.content[0].text.strip()
                s = raw.find('['); e = raw.rfind(']')
                if s != -1 and e != -1:
                    parsed = json.loads(raw[s:e+1])
                    titles = [str(t) for t in parsed if len(str(t)) <= 60][:8]
                    if len(titles) < 5:
                        titles = [str(t)[:60] for t in parsed[:8]]
            except Exception as te:
                thumb_logger.warning('Job %s title generation failed: %s', job_id, te)
                titles = ["Add Your Title Here"] * 8

            thumb_logger.info(
                'Job %s complete: %s ranked frames, %s titles, orientation=%s',
                job_id, len(frame_b64s), len(titles), stream_info.get('orientation'),
            )

            thumbnail_jobs[job_id] = {
                'status':     'complete',
                'result':     {'frames': frame_b64s, 'titles': titles,
                               'clip_transcript': clip_transcript, 'video_info': stream_info},
                'created_at': now,
            }

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as exc:
        thumb_logger.exception('Job %s worker exception', job_id)
        thumbnail_jobs[job_id] = {'status': 'error', 'error': str(exc)}


@app.route('/thumbnail', methods=['POST'])
def thumbnail():
    """
    Starts async thumbnail job.
    Accepts form fields: filename, clipstart, clipend, clip_transcript
    Returns immediately: {"jobid": "...", "status": "processing"}
    """
    if not FFMPEG_EXE:
        return jsonify({'error': 'ffmpeg not found. Expected at Dropbox\\Scripts\\FFMPEG\\ffmpeg.exe'}), 500

    filename        = request.form.get('filename', '').strip()
    clipstart       = request.form.get('clipstart', '').strip()
    clipend         = request.form.get('clipend',   '').strip()
    clip_transcript = request.form.get('clip_transcript', '')

    if not filename:
        return jsonify({'error': 'filename is required'}), 400

    job_id = 'thumb' + uuid.uuid4().hex[:8]
    thumb_logger.info(
        'Queueing thumbnail job %s for filename="%s" clipstart="%s" clipend="%s" transcript_chars=%s',
        job_id, filename, clipstart, clipend, len(clip_transcript),
    )
    thumbnail_jobs[job_id] = {
        'status':     'processing',
        'created_at': datetime.datetime.utcnow(),
    }
    t = threading.Thread(
        target=_thumbnail_worker,
        args=(job_id, filename, clipstart, clipend, clip_transcript),
        daemon=True
    )
    t.start()
    return jsonify({'jobid': job_id, 'status': 'processing'})


@app.route('/thumbnailstatus/<jobid>', methods=['GET'])
def thumbnailstatus(jobid):
    """Poll thumbnail job status."""
    job = thumbnail_jobs.get(jobid)
    if not job:
        thumb_logger.warning('Status poll for missing thumbnail job %s', jobid)
        return jsonify({'status': 'error', 'error': 'Job not found'}), 404
    if job['status'] == 'processing':
        return jsonify({'status': 'processing'})
    if job['status'] == 'complete':
        return jsonify({'status': 'complete', **job['result']})
    return jsonify({'status': 'error', 'error': job.get('error', 'Unknown error')}), 500


# ── Clips ──
def _parse_claude_json(raw):
    """Strip markdown fences and extract the first JSON array from a Claude response."""
    raw = re.sub(r'^```json\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'^```\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()
    start = raw.find('[')
    end   = raw.rfind(']')
    if start != -1 and end != -1:
        raw = raw[start:end+1]
    return json.loads(raw)


def _call_clips_claude(transcript_text, client):
    """Call Claude for viral clip detection. Returns raw list of candidate dicts."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{
            "role": "user",
            "content": f"""You are an expert short-form video editor for The Candler Foundry at Emory University — a theological continuing education program that equips church leaders, seminary alumni, and ministry practitioners with faith-grounded, academically rigorous content. Content comes from TheoEd talks, podcast interviews, and teaching sessions featuring pastors, professors, and faith leaders.

Your job is to find viral-worthy clip moments from these theological talks and personal testimonials for Instagram Reels, YouTube Shorts, and TikTok.

The transcript below uses the format: [timestamp_in_seconds] word [timestamp] word ...
Use these timestamps directly as start_time and end_time values — do not calculate or estimate them.

HARD CONSTRAINT — CLIP DURATION (non-negotiable):
Every clip MUST be between 30 and 90 seconds long (duration = end_time - start_time).
- Clips shorter than 30 seconds: DO NOT include — too brief for meaningful theological content
- Clips longer than 90 seconds: DO NOT include — too long for short-form platforms
- Ideal sweet spot: 45–75 seconds

WHAT PERFORMS WELL ON THESE PLATFORMS for Candler Foundry content:
- Personal turning points: a moment where someone's faith, perspective, or life changed
- Surprising or counterintuitive statements about God, Scripture, or spiritual life
- Vulnerable admissions or honest struggles ("I used to think...", "I never expected...")
- A single, quotable sentence that stands alone without context
- Emotional peaks — grief, joy, conviction, humor
- Provocative questions that make a viewer stop scrolling
- Strong opening hooks that don't require setup (viewer should be hooked within 3 seconds)

AVOID:
- Clips that begin mid-sentence or require prior context to understand
- Academic or overly technical theological language with no emotional anchor
- Transitions ("and so," "as I was saying," "moving on to")
- Clips that trail off or end without a clear conclusion

DIVERSITY RULE — this is critical:
- Spread clips across the full duration of the video — do not cluster them
- Each clip must start at least 45 seconds after the previous clip's start_time
- Find moments from the beginning, middle, AND end of the video
- Vary the emotional tone: mix conviction, humor, vulnerability, insight

Return ONLY a valid JSON array. No markdown, no code fences, no explanation. Start with [ and end with ].

Each object must have exactly these fields:
- start_time: float (copy the timestamp number directly from the transcript)
- end_time: float (timestamp of the last word in the segment)
- duration: float (end_time - start_time, rounded to 1 decimal — MUST be 30.0–90.0)
- hook_score: integer 1-10 (10 = most likely to stop a scroll)
- hook_line: string (the single most compelling sentence in this segment, quoted verbatim)
- why_it_works: string (1 sentence — name the specific technique: vulnerability, surprise, strong hook, emotional peak, etc.)

Find 8 clips, all between 30 and 90 seconds. Rank by hook_score descending.

Transcript:
{transcript_text}"""
        }]
    )
    raw = message.content[0].text.strip()
    print(f"[clips] Claude raw (first 300 chars): {raw[:300]}")
    return _parse_claude_json(raw)


@app.route('/clips', methods=['POST'])
def find_clips():
    try:
        data = request.json
        transcript = data.get('transcript', '')

        if not transcript or len(transcript) < 10:
            return jsonify({'error': 'Transcript too short', 'candidates': []})

        # transcript is a pre-formatted timestamped string: "[0.0] word [0.4] another ..."
        transcript_text = transcript if isinstance(transcript, str) else ' '.join(str(w) for w in transcript)
        print(f"[clips] Transcript length: {len(transcript_text)} chars")
        print(f"[clips] Preview: {transcript_text[:200]}")

        api_key_path = os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'api_key.txt')
        with open(api_key_path, 'r') as f:
            api_key = f.read().strip()

        client = anthropic.Anthropic(api_key=api_key)

        # Retry loop: require ≥ 3 clips with valid duration (25–95s) before returning
        valid = []
        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
            try:
                candidates = _call_clips_claude(transcript_text, client)
            except Exception as ce:
                print(f"[clips] Attempt {attempt+1} Claude call failed: {ce}")
                candidates = []

            valid = [
                c for c in candidates
                if 25 <= (c.get('end_time', 0) - c.get('start_time', 0)) <= 95
            ]
            print(f"[clips] Attempt {attempt+1}: {len(candidates)} total, {len(valid)} valid (25-95s)")

            if len(valid) >= 3:
                break
            if attempt < MAX_RETRIES - 1:
                print(f"[clips] Too few valid clips — retrying...")

        return jsonify({'candidates': valid})

    except Exception as e:
        print(f"ERROR in /clips: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'candidates': []})


# ── Sequential Split ──
@app.route('/split', methods=['POST'])
def split_video():
    """
    Divides a transcript into sequential 30-90s parts ending at natural pauses.

    Request JSON:
      transcript  — pre-formatted timestamped string "[0.0] word [0.4] word ..."
      n_parts     — optional int (2-6); Claude chooses if omitted

    Returns:
      { parts: [{part, start, end, duration, label, hook}] }
    """
    try:
        data = request.json
        transcript = data.get('transcript', '')
        n_parts    = data.get('n_parts')  # may be None, int, or string

        if not transcript or len(transcript) < 10:
            return jsonify({'error': 'Transcript too short', 'parts': []})

        transcript_text = transcript if isinstance(transcript, str) else ' '.join(str(w) for w in transcript)
        print(f"[split] Transcript length: {len(transcript_text)} chars")

        # Validate n_parts
        if n_parts is not None:
            try:
                n_parts = int(n_parts)
                n_parts = max(2, min(6, n_parts))
            except (ValueError, TypeError):
                n_parts = None

        api_key_path = os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'api_key.txt')
        with open(api_key_path, 'r') as f:
            api_key = f.read().strip()

        client = anthropic.Anthropic(api_key=api_key)

        n_instruction = (
            f"Divide into exactly {n_parts} sequential parts."
            if n_parts else
            "Choose the ideal number of sequential parts (between 2 and 6, whichever gives the most natural 45-60s segments)."
        )

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": f"""You are an expert video editor for The Candler Foundry at Emory University — a theological continuing education program producing content for church leaders and ministry practitioners.

Your task is to divide this theological talk into sequential parts for a multi-part social media series.

{n_instruction}

RULES:
- Parts are sequential — Part 1 covers the beginning, Part 2 the next section, etc. No overlap.
- Each part must be 30-90 seconds long (ideal: 45-60 seconds). duration = end - start.
- End each part at a natural pause, sentence end, or topic transition — never mid-sentence.
- Give each part a short descriptive label (3-6 words) that captures the idea of that segment.
- Provide a hook sentence — the most compelling line from that segment, quoted verbatim from the transcript.
- Together the parts should cover the best portion of the talk (you don't need to use the whole video).

The transcript uses the format: [timestamp_in_seconds] word [timestamp] word ...
Copy timestamps directly — do not calculate or estimate.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation. Start with [ and end with ].

Each object must have exactly these fields:
- part: integer (1, 2, 3, ...)
- start: float (timestamp in seconds, copied directly from transcript)
- end: float (timestamp in seconds, copied directly from transcript)
- duration: float (end - start, rounded to 1 decimal — MUST be 30.0–90.0)
- label: string (3-6 word title for this segment)
- hook: string (most compelling verbatim sentence from this segment)

Transcript:
{transcript_text}"""
            }]
        )

        raw = message.content[0].text.strip()
        print(f"[split] Claude raw (first 300 chars): {raw[:300]}")
        parts = _parse_claude_json(raw)
        print(f"[split] Parsed {len(parts)} parts")

        # Sort by part number to guarantee order
        parts.sort(key=lambda p: p.get('part', 0))

        return jsonify({'parts': parts})

    except Exception as e:
        print(f"ERROR in /split: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'parts': []})


# ── Export Clip ──
@app.route("/export_clip", methods=["POST"])
def export_clip():
    """
    Accepts multipart/form-data:
      file            — source MP4 or MOV
      starttime       — float, in-point seconds
      endtime         — float, out-point seconds
      suggested_name  — desired output filename
      clip_type       — "Podcast Clip" | "3MB Clip" | "TheoEd Clip" | etc.
      hook_line       — clip title / hook sentence
      clip_transcript — plain text transcript of the clip
      item_code       — source video item code (for Airtable linking)
      thumbnail       — PNG file (optional)

    Steps: 9:16 reframe → Dropbox save → shared links → Airtable record
    Returns: { success, clip_dropbox_url, thumbnail_dropbox_url,
               airtable_record_id, airtable_url }
    """
    import urllib.request
    import urllib.parse
    import dropbox as dbx_module

    if not FFMPEG_EXE:
        return jsonify({'error': 'ffmpeg not found. Expected at Dropbox\\Scripts\\FFMPEG\\ffmpeg.exe'}), 500

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    try:
        starttime       = float(request.form.get("starttime", 0))
        endtime         = float(request.form.get("endtime",   0))
        suggested_name  = request.form.get("suggested_name",  "clip.mp4")
        clip_type       = request.form.get("clip_type",       "Podcast Clip")
        hook_line       = request.form.get("hook_line",       "")
        clip_transcript = request.form.get("clip_transcript", "")
        item_code       = request.form.get("item_code",       "")
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid parameters: {e}"}), 400

    if endtime <= starttime:
        return jsonify({"error": "endtime must be greater than starttime"}), 400

    thumbnail_file = request.files.get("thumbnail")

    # Normalise output filename to .mp4
    output_name = suggested_name if suggested_name.lower().endswith(".mp4") else suggested_name + ".mp4"
    base_name   = os.path.splitext(output_name)[0]

    # Destination: ~/Dropbox/Social Media Clips/
    clips_folder = os.path.join(dropbox_root, "Social Media Clips")
    os.makedirs(clips_folder, exist_ok=True)
    output_path  = os.path.join(clips_folder, output_name)

    tmp_dir = tempfile.mkdtemp()
    try:
        src_ext    = os.path.splitext(file.filename or 'video.mp4')[1].lower() or '.mp4'
        input_path = os.path.join(tmp_dir, 'source' + src_ext)
        temp_clip  = os.path.join(tmp_dir, 'temp_clip.mp4')
        file.save(input_path)

        # ── Step A: 9:16 vertical reframe ──
        # Pass 1: extract the in/out segment (stream copy, fast)
        r1 = subprocess.run(
            [FFMPEG_EXE, "-y",
             "-ss", str(starttime), "-to", str(endtime),
             "-i", input_path, "-c", "copy", temp_clip],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
        if r1.returncode != 0:
            err = r1.stderr.decode("utf-8", errors="replace")[-800:]
            return jsonify({"error": "ffmpeg extract failed: " + err}), 500

        # Pass 2: crop centre 9:16 and scale to 1080×1920
        r2 = subprocess.run(
            [FFMPEG_EXE, "-y",
             "-i", temp_clip,
             "-vf", "crop=ih*9/16:ih,scale=1080:1920",
             "-c:v", "libx264", "-crf", "18", "-preset", "fast",
             "-c:a", "aac",
             output_path],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
        if r2.returncode != 0:
            err = r2.stderr.decode("utf-8", errors="replace")[-800:]
            return jsonify({"error": "ffmpeg reframe failed: " + err}), 500

        # ── Step B: Save thumbnail (if provided) ──
        thumb_local_path  = None
        thumb_dbx_path    = None
        if thumbnail_file and thumbnail_file.filename:
            thumb_name       = base_name + " - Thumbnail.png"
            thumb_local_path = os.path.join(clips_folder, thumb_name)
            thumbnail_file.save(thumb_local_path)
            thumb_dbx_path   = "/Social Media Clips/" + thumb_name

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Step C: Dropbox shared links ──
    clip_url  = None
    thumb_url = None
    try:
        creds_path = os.path.join(dropbox_root, "Scripts", "dropbox_credentials.json")
        with open(creds_path, "r") as f:
            creds = json.load(f)

        dbx = dbx_module.Dropbox(
            oauth2_refresh_token=creds["refresh_token"],
            app_key=creds.get("app_key") or creds.get("appkey"),
            app_secret=creds.get("app_secret") or creds.get("appsecret"),
        )

        def get_shared_link(dbx_path):
            try:
                result = dbx.sharing_create_shared_link_with_settings(dbx_path)
                return result.url
            except dbx_module.exceptions.ApiError:
                # Link already exists — retrieve it
                links = dbx.sharing_list_shared_links(path=dbx_path)
                if links.links:
                    return links.links[0].url
                return None

        clip_url  = get_shared_link("/Social Media Clips/" + output_name)
        if thumb_dbx_path:
            thumb_url = get_shared_link(thumb_dbx_path)

    except FileNotFoundError:
        print("dropbox_credentials.json not found — skipping Dropbox link generation")
    except Exception as de:
        print(f"Dropbox error: {de}")

    # ── Step D: Search source video record in Airtable ──
    api_key          = read_api_key()
    source_record_id = None

    if api_key and item_code and clip_type != "Podcast Clip":
        formula  = '{{Code}}="{}"'.format(item_code)
        params   = urllib.parse.urlencode({"filterByFormula": formula})
        at_url   = "https://api.airtable.com/v0/appiL0Z2RilcAT2Cw/tblS1Bk29cXyGGUdo?" + params
        at_req   = urllib.request.Request(
            at_url, headers={"Authorization": "Bearer " + api_key}
        )
        try:
            with urllib.request.urlopen(at_req, timeout=10) as resp:
                records = json.loads(resp.read()).get("records", [])
                if records:
                    source_record_id = records[0]["id"]
        except Exception as ae:
            print(f"Airtable source lookup failed: {ae}")

    # ── Step E: Create Airtable record (Video Shorts & Social) ──
    airtable_record_id = None
    airtable_url       = None

    if api_key:
        # Only include writable fields — never lookup/rollup/formula/AI/button
        fields = {"Status": "Draft", "Type": clip_type}
        if hook_line:
            fields["Content Title"] = hook_line
        if clip_url:
            fields["Clip - Dropbox URL"] = clip_url
        if thumb_url:
            fields["Thumbnail - Dropbox URL"] = thumb_url
        if clip_transcript:
            fields["Clip Transcript"] = clip_transcript
        if source_record_id:
            fields["Full-Length Video"] = [source_record_id]

        payload = json.dumps({"fields": fields}).encode("utf-8")
        at_req  = urllib.request.Request(
            "https://api.airtable.com/v0/appiL0Z2RilcAT2Cw/tbll0KDqmrAlwQuAx",
            data=payload,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(at_req, timeout=15) as resp:
                at_resp = json.loads(resp.read())
                airtable_record_id = at_resp.get("id")
                if airtable_record_id:
                    airtable_url = (
                        "https://airtable.com/appiL0Z2RilcAT2Cw"
                        "/tbll0KDqmrAlwQuAx/" + airtable_record_id
                    )
        except Exception as ae:
            print(f"Airtable record creation failed: {ae}")

    return jsonify({
        "success":               True,
        "output_filename":       output_name,
        "clip_dropbox_url":      clip_url,
        "thumbnail_dropbox_url": thumb_url,
        "airtable_record_id":    airtable_record_id,
        "airtable_url":          airtable_url,
    })


if __name__ == "__main__":
    logger.info("Foundry Video Editor backend starting on http://localhost:5000")
    api_key = read_api_key()
    if api_key:
        logger.info("API key loaded.")
    else:
        logger.warning("API key not found at %s", API_KEY_PATH)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
