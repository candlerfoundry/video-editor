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
import hashlib
import io
import mimetypes
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
import unicodedata
import uuid
import anthropic
import whisper
from flask import Flask, abort, jsonify, request, send_file
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

# Bump this whenever the frontend/backend contract changes (the frontend
# carries a matching EXPECTED_BACKEND_BUILD and warns when they differ).
BACKEND_BUILD = "2026-06-29-speedcfr"

CREATE_NO_WINDOW = 0x08000000 if platform.system() == 'Windows' else 0

STYLE_STR = "Fontname=Arial,Outline=1,Shadow=0,BorderStyle=1,Spacing=1"

# ── Dropbox portable paths ──
def resolve_dropbox_root():
    """Locate the Foundry Dropbox root.

    Standard case (Windows / a personal Dropbox): ~/Dropbox. On macOS a
    Business/Team account can sync to "~/Dropbox (Team Name)" instead - and a
    second personal account may occupy ~/Dropbox - so prefer whichever
    ~/Dropbox* folder actually contains the Foundry files
    (Scripts/Foundry Video Editor). Falls back to ~/Dropbox. Windows is
    unaffected: ~/Dropbox already holds the marker, so it is returned first.
    """
    import glob
    home = os.path.expanduser('~')
    marker = os.path.join('Scripts', 'Foundry Video Editor')
    seen = set()
    candidates = [os.path.join(home, 'Dropbox')] + sorted(glob.glob(os.path.join(home, 'Dropbox*')))
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if os.path.isdir(os.path.join(cand, marker)):
            return cand
    return os.path.join(home, 'Dropbox')

dropbox_root = resolve_dropbox_root()
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
project_store_lock = threading.Lock()
def _stable_app_data_root():
    """Per-OS app-data dir that survives reboots, so saved projects persist.
    Windows: %LOCALAPPDATA%; macOS: ~/Library/Application Support; Linux:
    $XDG_DATA_HOME or ~/.local/share. Temp dir only as a last resort.
    (Pre-fix builds used LOCALAPPDATA-or-tempdir, so on Mac projects landed in
    a temp folder macOS periodically purges — losing the recent-projects list.)"""
    sysname = platform.system()
    if sysname == 'Windows':
        base = os.environ.get('LOCALAPPDATA')
    elif sysname == 'Darwin':
        base = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    if not base:
        base = tempfile.gettempdir()
    return os.path.join(base, 'Foundry Video Editor')

local_app_root = _stable_app_data_root()
project_store_dir = os.path.join(local_app_root, 'projects')
os.makedirs(project_store_dir, exist_ok=True)

# One-time best-effort migration: if a previous build saved projects in the old
# temp location, copy them into the stable store so users don't lose them.
try:
    _old_projects = os.path.join(
        os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'Foundry Video Editor', 'projects')
    if os.path.isdir(_old_projects) and os.path.abspath(_old_projects) != os.path.abspath(project_store_dir):
        for _name in os.listdir(_old_projects):
            if not _name.endswith('.json'):
                continue
            _dst = os.path.join(project_store_dir, _name)
            if not os.path.exists(_dst):
                try:
                    shutil.copy2(os.path.join(_old_projects, _name), _dst)
                except Exception:
                    pass
except Exception:
    pass


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
        with open(API_KEY_PATH, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


# ── Airtable API key (separate file — NEVER use the Anthropic key for Airtable) ──
AIRTABLE_KEY_PATH = os.path.join(dropbox_root, 'Scripts', 'airtable_api_key.txt')

def read_airtable_api_key():
    try:
        with open(AIRTABLE_KEY_PATH, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("[airtable] API key not found at %s", AIRTABLE_KEY_PATH)
        return None


def srt_to_ass(srt_text, width, height):
    """
    Convert SRT text to a full ASS script with PlayRes set to the actual video
    size, so font sizes are TRUE pixels. This fixes the long-standing
    giant-caption bug: SRT via the subtitles filter is styled against libass's
    default 384x288 canvas and scaled up (6.7x on a 1080x1920 video).
    Sizing: vertical ~3.4% of height, horizontal ~5.5%, square ~4.5%.
    """
    aspect = width / height if height else 1.78
    if aspect < 0.75:
        font_size = max(28, int(height * 0.034))
        margin_v = int(height * 0.12)
    elif aspect > 1.4:
        font_size = max(24, int(height * 0.055))
        margin_v = int(height * 0.06)
    else:
        font_size = max(26, int(height * 0.045))
        margin_v = int(height * 0.08)
    margin_h = int(width * 0.05)

    def t2ass(t):
        t = t.strip().replace('.', ',')
        h, m, rest = t.split(':')
        s, ms = (rest.split(',') + ['0'])[:2]
        return f"{int(h)}:{m}:{s}.{int(ms.ljust(3, '0')[:3]) // 10:02d}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H7F000000,-1,0,0,0,100,100,0,0,1,3,0,2,{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for block in re.split(r'\n\s*\n', srt_text.strip()):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        ti = next((i for i, l in enumerate(lines) if '-->' in l), -1)
        if ti < 0:
            continue
        start, end = [x.strip() for x in lines[ti].split('-->')]
        text = '\\N'.join(lines[ti + 1:]).replace('{', '(').replace('}', ')')
        events.append(f"Dialogue: 0,{t2ass(start)},{t2ass(end)},Default,,0,0,0,,{text}")
    return header + '\n'.join(events) + '\n'


ASS_CAPTION_FONTS = {"Arial", "Arial Black", "Impact", "Verdana", "Tahoma",
                     "Trebuchet MS", "Georgia"}


def hex_to_ass_color(hex_color):
    """#RRGGBB -> &H00BBGGRR& (ASS is BGR)."""
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return "&H0000A5F5&"  # amber fallback
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}&".upper()


def spec_to_ass(spec, width, height):
    """
    Styled caption spec from the in-frame editor -> full ASS script.
    spec = { font, emphasis_color, mode, all_caps, emphasis_caps, pop,
             pos_bottom, groups: [{start, end, words:[{t,s,e}], emphasis}] }
    Modes: 'group' (whole group), 'karaoke' (active word colored as spoken,
    one Dialogue per word window), 'word' (one word at a time, pop-in).
    Words may be plain strings (legacy) or {t,s,e} dicts. Times are on the
    OUTPUT clip timeline. True-pixel sizing (PlayRes = real size).
    """
    aspect = width / height if height else 1.78
    mode = spec.get("mode") or "group"
    if aspect < 0.75:
        font_size = max(28, int(height * (0.06 if mode == "word" else 0.034)))
        default_margin = 0.12
    elif aspect > 1.4:
        font_size = max(24, int(height * (0.085 if mode == "word" else 0.055)))
        default_margin = 0.06
    else:
        font_size = max(26, int(height * (0.07 if mode == "word" else 0.045)))
        default_margin = 0.08
    try:
        pos_bottom = float(spec.get("pos_bottom"))
    except (TypeError, ValueError):
        pos_bottom = default_margin
    pos_bottom = max(0.02, min(0.75, pos_bottom))
    margin_v = int(height * pos_bottom)
    margin_h = int(width * 0.05)

    # Size scale (70-160% from the editor slider)
    try:
        size_scale = float(spec.get("size_scale") or 1)
    except (TypeError, ValueError):
        size_scale = 1.0
    font_size = max(16, int(font_size * max(0.5, min(3.0, size_scale))))

    font = spec.get("font") or "Arial"
    if font not in ASS_CAPTION_FONTS:
        font = "Arial"
    emph_color = hex_to_ass_color(spec.get("emphasis_color") or "#F5A623")
    all_caps = bool(spec.get("all_caps"))
    emphasis_caps = bool(spec.get("emphasis_caps", True))
    emphasis_color_on = spec.get("emphasis_color_on", True) is not False
    emphasis_larger = bool(spec.get("emphasis_larger"))
    emphasis_pulse = bool(spec.get("emphasis_pulse"))
    pop = bool(spec.get("pop"))
    # Letter spacing (editor slider, 0-20 = % of font size).
    try:
        ls_pct = float(spec.get("letter_spacing")) if spec.get("letter_spacing") is not None else 1.0
    except (TypeError, ValueError):
        ls_pct = 1.0
    letter_spacing = max(0, round(font_size * max(0.0, min(30.0, ls_pct)) / 100.0))
    # Outline thickness (editor slider, 0-10 level; 0 = no outline).
    try:
        ow_level = float(spec.get("outline_width")) if spec.get("outline_width") is not None else 3.0
    except (TypeError, ValueError):
        ow_level = 3.0
    outline_px = max(0, round(font_size * max(0.0, min(12.0, ow_level)) * 0.02))

    def t2ass(t):
        t = max(0.0, float(t))
        h = int(t // 3600); m2 = int(t // 60) % 60; s = int(t % 60)
        cs = int(round((t % 1) * 100))
        if cs >= 100: cs = 99
        return f"{h}:{m2:02d}:{s:02d}.{cs:02d}"

    def norm_words(g):
        out = []
        n = len(g.get("words") or [])
        for i, w in enumerate(g.get("words") or []):
            if isinstance(w, dict):
                out.append({"t": str(w.get("t", "")), "s": float(w.get("s", g.get("start", 0))),
                            "e": float(w.get("e", g.get("end", 0))), "br": bool(w.get("br"))})
            else:
                # legacy plain strings: spread evenly across the group window
                gs, ge = float(g.get("start", 0)), float(g.get("end", 0))
                span = (ge - gs) / max(1, n)
                out.append({"t": str(w), "s": gs + i * span, "e": gs + (i + 1) * span})
        return [w for w in out if w["t"].strip()]

    def disp(text, emphasized):
        text = text.replace("{", "(").replace("}", ")")
        if all_caps or (emphasized and emphasis_caps):
            text = text.upper()
        return text

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H7F000000,-1,0,0,0,100,100,{letter_spacing},0,1,{outline_px},0,2,{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    pop_tag = "{\\t(0,90,\\fscx112\\fscy112)\\t(90,180,\\fscx100\\fscy100)}" if pop else ""
    pulse_tag = "{\\t(0,180,\\fscx118\\fscy118)\\t(180,360,\\fscx100\\fscy100)}"

    def styled_run(txt, emphasized, active):
        """Inline ASS tags for one word per the emphasis options."""
        open_tags = []
        close_tags = []
        if (emphasized or active) and emphasis_color_on:
            open_tags.append("\\1c" + emph_color)
            close_tags.append("\\1c&H00FFFFFF&")
        if emphasized and emphasis_larger:
            open_tags.append("\\fscx118\\fscy118")
            close_tags.append("\\fscx100\\fscy100")
        if active and not emphasis_color_on:
            open_tags.append("\\fscx112\\fscy112")
            close_tags.append("\\fscx100\\fscy100")
        prefix = ("{" + "".join(open_tags) + "}") if open_tags else ""
        suffix = ("{" + "".join(close_tags) + "}") if close_tags else ""
        if emphasized and emphasis_pulse:
            prefix = pulse_tag + prefix
        return prefix + txt + suffix

    events = []
    for g in spec.get("groups") or []:
        words = norm_words(g)
        if not words:
            continue
        emphasis = set(int(i) for i in (g.get("emphasis") or []) if isinstance(i, (int, float)))
        g_start, g_end = float(g.get("start", 0)), float(g.get("end", 0))

        def join_runs(runs_with_words):
            # Single space + slight letter spacing (two spaces read too wide);
            # \\N at line breaks
            out = []
            for run, w in runs_with_words:
                out.append(run)
                out.append("\\N" if w.get("br") else " ")
            return "".join(out[:-1]) if out else ""

        if mode == "word":
            for i, w in enumerate(words):
                txt = disp(w["t"], i in emphasis)
                run = styled_run(txt, i in emphasis, False)
                end_t = words[i + 1]["s"] if i + 1 < len(words) else g_end
                events.append(f"Dialogue: 0,{t2ass(w['s'])},{t2ass(max(w['s'] + 0.08, end_t))},Default,,0,0,0,,{pop_tag}{run}")
            continue

        if mode == "karaoke":
            for i in range(len(words)):
                runs = []
                for j, w in enumerate(words):
                    txt = disp(w["t"], j in emphasis)
                    runs.append((styled_run(txt, j in emphasis, j == i), w))
                start_t = words[i]["s"] if i > 0 else g_start
                end_t = words[i + 1]["s"] if i + 1 < len(words) else g_end
                if end_t - start_t < 0.04:
                    end_t = start_t + 0.04
                events.append(f"Dialogue: 0,{t2ass(start_t)},{t2ass(end_t)},Default,,0,0,0,," + join_runs(runs))
            continue

        # mode == 'group'
        runs = []
        for j, w in enumerate(words):
            txt = disp(w["t"], j in emphasis)
            runs.append((styled_run(txt, j in emphasis, False), w))
        events.append(f"Dialogue: 0,{t2ass(g_start)},{t2ass(g_end)},Default,,0,0,0,," + join_runs(runs))
    return header + "\n".join(events) + "\n"

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
        logger.info('[dimensions] %s: %sx%s', video_path, w, h)
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
        logger.warning('[dimensions] ffprobe failed, using default 1920x1080: %s', e)
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
    # BACKEND_BUILD lets the frontend detect a stale backend (running code
    # from before the latest deploy) and tell the user to restart the
    # launcher — version skew once burned Python dict reprs into captions.
    return jsonify({"status": "ok", "version": "1.0.0", "build": BACKEND_BUILD})


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

        # Detect video dimensions for true-pixel caption sizing
        width, height = get_video_dimensions(input_path, FFMPEG_EXE)
        logger.info('[captions] Video %sx%s — using ASS true-pixel sizing', width, height)

        # Transcribe with Whisper medium
        model  = get_whisper_model()
        result = model.transcribe(input_path)
        write_srt(result["segments"], srt_path)

        # Burn subtitles with ffmpeg as a full ASS script (PlayRes = video size).
        # The old force_style approach was scaled against libass's default
        # 384x288 canvas — on vertical video that multiplied font sizes ~6.7x,
        # which is why captions ran off the frame for years (fixed June 11, 2026).
        with open(srt_path, 'r', encoding='utf-8') as f:
            srt_content = f.read()
        ass_text = srt_to_ass(srt_content, width, height)
        if position == "top":
            # Alignment 8 = top-center (ASS numpad layout)
            ass_text = ass_text.replace(",1,3,0,2,", ",1,3,0,8,")
        if text_color == "yellow":
            ass_text = ass_text.replace("&H00FFFFFF,&H00FFFFFF", "&H0000FFFF,&H0000FFFF")
        ass_path = os.path.join(tmp_dir, "captions.ass")
        with open(ass_path, 'w', encoding='utf-8') as f:
            f.write(ass_text)

        cmd = [
            FFMPEG_EXE, "-y",
            "-i", input_path,
            "-vf", "subtitles=filename=captions.ass",
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


def iso_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def slugify_project_name(value):
    normalized = unicodedata.normalize('NFKD', value or '')
    ascii_value = normalized.encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', ascii_value).strip('-').lower()
    return slug or 'project'


def derive_project_name(filename):
    stem = bare_stem(os.path.basename(filename or '')).replace('_', ' ').strip()
    stem = re.sub(r'\s+', ' ', stem)
    parts = [part.strip() for part in re.split(r'\s+-\s+', stem) if part.strip()]
    if len(parts) >= 2:
        return ' - '.join(parts[:2])
    return stem or 'Untitled Project'


def make_source_key(source_path=None, filename=None):
    identity = (source_path or filename or '').strip().lower()
    return hashlib.sha1(identity.encode('utf-8')).hexdigest()


def get_project_path(project_id):
    return os.path.join(project_store_dir, f'{project_id}.json')


def compact_clip_candidate(item):
    return {
        'start_time': item.get('start_time'),
        'end_time': item.get('end_time'),
        'duration': item.get('duration'),
        'hook_score': item.get('hook_score'),
        'hook_line': item.get('hook_line'),
        'why_it_works': item.get('why_it_works'),
        'label': item.get('label'),
        'part': item.get('part'),
        'mode': item.get('mode'),
    }


def normalize_text_box(item, fallback_id='text-1'):
    item = item or {}
    return {
        'id': item.get('id') or fallback_id,
        'text': item.get('text') or '',
        'color': item.get('color') or '#ffffff',
        'background_color': item.get('background_color') or '#111111',
        'background_opacity': int(item.get('background_opacity') or 0),
        'font_family': item.get('font_family') or 'Montserrat',
        'font_size': int(item.get('font_size') or 64),
        'x': item.get('x'),
        'y': item.get('y'),
        'shadow': bool(item.get('shadow', True)),
        'align': item.get('align') or 'center',
        'width': item.get('width'),
        'height': item.get('height'),
        'letter_spacing': item.get('letter_spacing') or 0,
        'line_spacing': item.get('line_spacing') or 1.25,
        'bg_pill': bool(item.get('bg_pill', False)),
        'runs': item.get('runs'),
    }


def normalize_thumbnail_draft(payload):
    payload = payload or {}
    text_boxes = payload.get('text_boxes') or []
    normalized_boxes = [
        normalize_text_box(box, f'text-{index + 1}')
        for index, box in enumerate(text_boxes)
    ]
    if not normalized_boxes:
        normalized_boxes = [normalize_text_box({
            'id': payload.get('selected_text_box_id') or 'text-1',
            'text': payload.get('title') or '',
            'color': payload.get('text_color') or '#ffffff',
            'background_color': payload.get('background_color') or '#111111',
            'background_opacity': payload.get('background_opacity') or 0,
            'font_family': payload.get('font_family') or 'Montserrat',
            'font_size': payload.get('font_size') or 64,
            'x': payload.get('text_x'),
            'y': payload.get('text_y'),
            'shadow': payload.get('shadow_on', True),
        })]

    selected_text_box_id = payload.get('selected_text_box_id') or normalized_boxes[0]['id']
    available_frames = payload.get('available_frames') or payload.get('frames') or []
    suggested_titles = payload.get('suggested_titles') or payload.get('titles') or []
    draft_id = payload.get('draft_id') or make_source_key(
        payload.get('source_path') or '',
        '|'.join([
            str(payload.get('entry_point') or ''),
            str(payload.get('context') or ''),
            str(payload.get('clip_start') or ''),
            str(payload.get('clip_end') or ''),
        ]),
    )[:16]

    return {
        'draft_id': draft_id,
        'entry_point': payload.get('entry_point') or payload.get('context') or 'shared_composer',
        'source_filename': payload.get('source_filename'),
        'source_path': payload.get('source_path'),
        'clip_start': payload.get('clip_start'),
        'clip_end': payload.get('clip_end'),
        'style': payload.get('style') or 'lower_third',
        'target_format': payload.get('target_format') or 'instagram',
        'selected_frame_index': int(payload.get('selected_frame_index') or 0),
        'selected_text_box_id': selected_text_box_id,
        'text_boxes': normalized_boxes,
        'logo': {
            'enabled': bool((payload.get('logo') or {}).get('enabled', False)),
            'placement': (payload.get('logo') or {}).get('placement') or 'top_right',
            'asset': (payload.get('logo') or {}).get('asset'),
            'x': (payload.get('logo') or {}).get('x'),
            'y': (payload.get('logo') or {}).get('y'),
            'width': (payload.get('logo') or {}).get('width'),
        },
        'suggested_titles': suggested_titles[:12],
        'ig_caption': (payload.get('ig_caption') or ''),
        'available_frames': available_frames[:12],
        'video_info': payload.get('video_info') or {},
        # June 11, 2026: these were silently dropped before, losing shapes/
        # brightness/zoom on every saved draft.
        'graphic_elements': (payload.get('graphic_elements') or [])[:40],
        'brightness': int(payload.get('brightness') or 100),
        'frame_zoom': float(payload.get('frame_zoom') or 1),
        'frame_offset_x': float(payload.get('frame_offset_x') or 0),
        'frame_offset_y': float(payload.get('frame_offset_y') or 0),
        'saved_at': iso_now(),
    }


def summarize_project(project):
    source_video = project.get('source_video') or {}
    transcript = project.get('transcript') or {}
    return {
        'id': project.get('id'),
        'project_name': project.get('project_name'),
        'source_filename': source_video.get('filename'),
        'source_path': source_video.get('path'),
        'last_modified': project.get('updated_at'),
        'last_opened_at': project.get('last_opened_at'),
        'counts': {
            'clip_candidates': len(project.get('clip_candidates') or []),
            'edited_clips': len(project.get('edited_clips') or []),
            'thumbnail_drafts': len(project.get('thumbnail_drafts') or []),
            'exports': len(project.get('exports') or []),
        },
        'transcript': {
            'json_filename': transcript.get('json_filename'),
            'word_count': transcript.get('word_count', 0),
        },
        'meta': project.get('meta') or {},
    }


def get_project_source_path(project):
    source_video = project.get('source_video') or {}
    source_path = (source_video.get('path') or '').strip()
    if source_path and os.path.isfile(source_path):
        return source_path
    return ''


def load_transcript_words_from_json(json_path):
    if not json_path or not os.path.isfile(json_path):
        return []
    with open(json_path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get('words'), list):
            return data.get('words') or []
        if isinstance(data.get('segments'), list):
            words = []
            for segment in data.get('segments') or []:
                words.extend(segment.get('words') or [])
            return words
    return []


def load_project(project_id):
    path = get_project_path(project_id)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def save_project(project):
    path = get_project_path(project['id'])
    project['updated_at'] = iso_now()
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(project, handle, ensure_ascii=False, indent=2)


def create_project_record(project_id, filename, source_path):
    timestamp = iso_now()
    clean_filename = os.path.basename(source_path or filename or '')
    return {
        'id': project_id,
        'slug': slugify_project_name(derive_project_name(clean_filename)),
        'project_name': derive_project_name(clean_filename),
        'created_at': timestamp,
        'updated_at': timestamp,
        'last_opened_at': timestamp,
        'source_video': {
            'filename': clean_filename,
            'path': source_path,
            'bare_stem': bare_stem(clean_filename),
        },
        'transcript': {},
        'clip_candidates': [],
        'edited_clips': [],
        'thumbnail_drafts': [],
        'exports': [],
        'meta': {
            'last_view': 'clips',
            'last_clip_mode': 'viral',
        },
    }


def sanitize_export_folder_name(name):
    cleaned = re.sub(r'[<>:"/\\|?*]+', ' ', (name or '').strip())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().strip('.')
    return cleaned or 'Untitled Project'


def get_social_media_clips_root():
    clips_root = os.path.join(dropbox_root, 'Social Media Clips')
    os.makedirs(clips_root, exist_ok=True)
    return clips_root


SOCIAL_MEDIA_PARENT_RULES = [
    {
        'folder': 'Unstuck Clips',
        'prefixes': ['UNST'],
        'terms': ['unstuck', 'everyday stories'],
        'path_terms': ['\\unstuck\\', '/unstuck/'],
        'clip_types': ['unstuck'],
    },
    {
        'folder': 'TheoEd Clips',
        'prefixes': ['THEO', 'TED'],
        'terms': ['theoed', 'theo ed', 'theo-ed'],
        'path_terms': ['\\theoed\\', '/theoed/'],
        'clip_types': ['theoed'],
    },
    {
        'folder': 'On Demand Promo',
        'prefixes': ['OD', 'ODP', 'OND'],
        'terms': ['on demand', 'promo'],
        'path_terms': ['\\on demand\\', '/on demand/'],
        'clip_types': ['promo', 'on demand'],
    },
    {
        'folder': 'Podcast Clips',
        'prefixes': ['POD'],
        'terms': ['podcast', 'scholar, unscripted', 'scholar unscripted', 'womanist series'],
        'path_terms': ['\\podcast\\', '/podcast/'],
        'clip_types': ['podcast'],
    },
]


def sanitize_social_media_relative_path(value):
    parts = re.split(r'[\\/]+', str(value or '').strip())
    clean_parts = []
    for part in parts:
        safe = sanitize_export_folder_name(part)
        if safe and safe not in {'.', '..'}:
            clean_parts.append(safe)
    return '/'.join(clean_parts)


def split_social_media_relative_path(value):
    relative = sanitize_social_media_relative_path(value)
    return [part for part in relative.split('/') if part]


def build_social_media_local_path(relative_folder):
    path = get_social_media_clips_root()
    for part in split_social_media_relative_path(relative_folder):
        path = os.path.join(path, part)
    return path


def list_social_media_clip_folders():
    clips_root = get_social_media_clips_root()
    try:
        names = [
            name for name in os.listdir(clips_root)
            if os.path.isdir(os.path.join(clips_root, name))
        ]
    except FileNotFoundError:
        return []
    return sorted(names, key=lambda value: value.lower())


def list_social_media_existing_project_folders():
    clips_root = get_social_media_clips_root()
    top_level = list_social_media_clip_folders()
    parent_names = [rule['folder'] for rule in SOCIAL_MEDIA_PARENT_RULES]
    leaf_folders = []

    for name in top_level:
        full_path = os.path.join(clips_root, name)
        if name in parent_names:
            try:
                child_names = [
                    child for child in os.listdir(full_path)
                    if os.path.isdir(os.path.join(full_path, child))
                ]
            except FileNotFoundError:
                child_names = []
            for child in child_names:
                relative = sanitize_social_media_relative_path(os.path.join(name, child))
                leaf_folders.append({
                    'relative_path': relative,
                    'parent_folder': name,
                    'folder_name': child,
                    'legacy_root': False,
                })
        else:
            leaf_folders.append({
                'relative_path': sanitize_social_media_relative_path(name),
                'parent_folder': '',
                'folder_name': name,
                'legacy_root': True,
            })

    return sorted(
        leaf_folders,
        key=lambda item: (
            item['legacy_root'],
            item['parent_folder'].lower(),
            item['folder_name'].lower(),
        ),
    )


def infer_social_media_parent_folder(project_name='', source_filename='', source_path='', clip_type=''):
    combined = ' '.join([
        project_name or '',
        source_filename or '',
        clip_type or '',
    ]).strip()
    combined_lower = combined.lower()
    source_path_lower = (source_path or '').lower()
    stem = bare_stem(os.path.basename(source_filename or project_name or '')).upper()
    prefix_match = re.match(r'^([A-Z]{2,5})[\s\-_]', stem)
    prefix = prefix_match.group(1) if prefix_match else ''

    best = None
    for rule in SOCIAL_MEDIA_PARENT_RULES:
        score = 0
        reasons = []
        if prefix and prefix in rule['prefixes']:
            score += 10
            reasons.append(f'code prefix {prefix}')
        if any(term in combined_lower for term in rule['terms']):
            score += 5
            reasons.append('name/context match')
        if any(term in source_path_lower for term in rule['path_terms']):
            score += 6
            reasons.append('source path match')
        if any(term in (clip_type or '').lower() for term in rule['clip_types']):
            score += 4
            reasons.append('clip type match')
        if score and (best is None or score > best['score']):
            best = {
                'folder': rule['folder'],
                'score': score,
                'reason': ', '.join(reasons),
            }

    if best:
        confidence = 'high' if best['score'] >= 10 else 'medium'
        return best['folder'], confidence, best['reason']

    return 'Podcast Clips', 'low', 'fallback default'


def find_matching_social_media_folder(project_name='', source_filename='', source_path='', clip_type=''):
    parent_folder, confidence, reason = infer_social_media_parent_folder(
        project_name=project_name,
        source_filename=source_filename,
        source_path=source_path,
        clip_type=clip_type,
    )
    suggested_folder_name = sanitize_export_folder_name(
        project_name or derive_project_name(source_filename)
    )
    candidates = []
    for value in [
        suggested_folder_name,
        sanitize_export_folder_name(derive_project_name(source_filename)),
        sanitize_export_folder_name(bare_stem(os.path.basename(source_filename or ''))),
    ]:
        if value and value.lower() not in {item.lower() for item in candidates}:
            candidates.append(value)

    existing_folders = list_social_media_existing_project_folders()
    matched = None
    alternates = []
    for folder in existing_folders:
        folder_name_lower = folder['folder_name'].lower()
        candidate_match = any(folder_name_lower == candidate.lower() for candidate in candidates)
        if not candidate_match:
            continue
        if (
            not folder['legacy_root']
            and folder['parent_folder'].lower() == parent_folder.lower()
            and matched is None
        ):
            matched = folder
        else:
            alternates.append(folder)

    return {
        'suggested_folder_name': suggested_folder_name,
        'suggested_parent_folder': parent_folder,
        'suggested_relative_folder': sanitize_social_media_relative_path(
            os.path.join(parent_folder, suggested_folder_name)
        ),
        'parent_confidence': confidence,
        'parent_reason': reason,
        'matched_folder': matched,
        'alternate_matches': alternates,
        'available_folders': existing_folders[:200],
    }


def get_dropbox_client():
    import dropbox as dbx_module

    creds_path = os.path.join(dropbox_root, 'Scripts', 'dropbox_credentials.json')
    with open(creds_path, 'r', encoding='utf-8') as handle:
        creds = json.load(handle)
    return dbx_module.Dropbox(
        oauth2_refresh_token=creds['refresh_token'],
        app_key=creds.get('app_key') or creds.get('appkey'),
        app_secret=creds.get('app_secret') or creds.get('appsecret'),
    )


def _background_link_and_patch(dbx_path, airtable_record_id, field_name):
    """
    Big clips lose the race: the share-link is requested seconds after ffmpeg
    writes the file, before the Dropbox desktop client finishes uploading it,
    so the API says not_found. This daemon thread polls until the file exists
    in the cloud (up to 30 min), creates the link, and patches the Airtable
    record's URL field.
    """
    import urllib.request as _req
    import time

    def worker():
        try:
            dbx = get_dropbox_client()
            deadline = time.time() + 1800
            link = None
            while time.time() < deadline:
                try:
                    dbx.files_get_metadata(dbx_path)
                    link = get_or_create_shared_link(dbx, dbx_path)
                    break
                except Exception:
                    time.sleep(15)
            if not link:
                logger.warning("[dropbox-retry] Gave up waiting for %s to sync", dbx_path)
                return
            logger.info("[dropbox-retry] Link ready for %s", dbx_path)
            if airtable_record_id:
                airtable_key = read_airtable_api_key()
                if airtable_key:
                    payload = json.dumps({"fields": {field_name: link}}).encode("utf-8")
                    at_req = _req.Request(
                        "https://api.airtable.com/v0/appiL0Z2RilcAT2Cw/tbll0KDqmrAlwQuAx/" + airtable_record_id,
                        data=payload,
                        headers={"Authorization": "Bearer " + airtable_key,
                                 "Content-Type": "application/json"},
                        method="PATCH",
                    )
                    try:
                        with _req.urlopen(at_req, timeout=15):
                            logger.info("[dropbox-retry] Patched Airtable %s with %s", airtable_record_id, field_name)
                    except Exception as exc:
                        logger.warning("[dropbox-retry] Airtable patch failed: %s", exc)
        except Exception as exc:
            logger.warning("[dropbox-retry] Worker failed: %s", exc)

    threading.Thread(target=worker, daemon=True).start()


def get_or_create_shared_link(dbx, dbx_path):
    import dropbox as dbx_module

    try:
        result = dbx.sharing_create_shared_link_with_settings(dbx_path)
        return result.url
    except dbx_module.exceptions.ApiError:
        links = dbx.sharing_list_shared_links(path=dbx_path)
        if links.links:
            return links.links[0].url
        return None


def build_social_media_dropbox_path(folder_name, filename):
    relative_parts = split_social_media_relative_path(folder_name)
    return '/Social Media Clips/' + '/'.join([
        *(part.replace('\\', '/') for part in relative_parts),
        filename.replace('\\', '/'),
    ])


def resolve_project_for_source(filename='', source_path=None):
    clean_filename = (filename or '').strip()
    resolved_path = (source_path or '').strip() or video_path_cache.get(clean_filename)
    if resolved_path and not os.path.exists(resolved_path):
        resolved_path = ''
    if not resolved_path and clean_filename:
        resolved_path = find_video_in_dropbox(clean_filename) or ''
    project_id = make_source_key(resolved_path or None, clean_filename)
    with project_store_lock:
        project = load_project(project_id)
        existed = project is not None
        if not project:
            project = create_project_record(project_id, clean_filename, resolved_path or None)
        source_video = project.setdefault('source_video', {})
        if clean_filename:
            source_video['filename'] = clean_filename
            source_video['bare_stem'] = bare_stem(clean_filename)
        if resolved_path:
            source_video['path'] = resolved_path
        project['project_name'] = derive_project_name(source_video.get('filename') or clean_filename)
        project['slug'] = slugify_project_name(project['project_name'])
        project['last_opened_at'] = iso_now()
        save_project(project)
    return project, existed, resolved_path or None


def upsert_project_list_item(items, new_item, key_fields):
    for index, existing in enumerate(items):
        if all(existing.get(field) == new_item.get(field) for field in key_fields):
            items[index] = {**existing, **new_item}
            return
    items.append(new_item)


@app.route('/projects/recent', methods=['GET'])
def recent_projects():
    try:
        projects = []
        with project_store_lock:
            for name in os.listdir(project_store_dir):
                if not name.endswith('.json'):
                    continue
                path = os.path.join(project_store_dir, name)
                try:
                    with open(path, 'r', encoding='utf-8') as handle:
                        projects.append(json.load(handle))
                except Exception:
                    logger.warning('[projects] Skipping unreadable project file: %s', path)
        projects.sort(
            key=lambda project: project.get('updated_at') or project.get('last_opened_at') or '',
            reverse=True,
        )
        return jsonify({'projects': [summarize_project(project) for project in projects[:12]]})
    except Exception as exc:
        logger.exception('[projects] Failed to list recent projects')
        return jsonify({'error': str(exc), 'projects': []}), 500


@app.route('/thumbnails/list', methods=['GET'])
def list_thumbnails():
    """Every saved thumbnail draft across all projects, slimmed to one frame
    each so the Thumbnails-tab gallery can render previews client-side."""
    try:
        items = []
        with project_store_lock:
            for name in os.listdir(project_store_dir):
                if not name.endswith('.json'):
                    continue
                path = os.path.join(project_store_dir, name)
                try:
                    with open(path, 'r', encoding='utf-8') as handle:
                        project = json.load(handle)
                except Exception:
                    logger.warning('[thumbnails] Skipping unreadable project file: %s', path)
                    continue
                source_video = project.get('source_video') or {}
                pid = project.get('id')
                for draft in (project.get('thumbnail_drafts') or []):
                    frames = draft.get('available_frames') or []
                    idx = int(draft.get('selected_frame_index') or 0)
                    if not (0 <= idx < len(frames)):
                        idx = 0
                    sel = frames[idx] if frames else None
                    slim = dict(draft)
                    slim['available_frames'] = [sel] if sel else []
                    slim['selected_frame_index'] = 0
                    items.append({
                        'project_id': pid,
                        'project_name': project.get('project_name'),
                        'source_filename': draft.get('source_filename') or source_video.get('filename'),
                        'updated_at': project.get('updated_at') or project.get('last_opened_at'),
                        'draft_id': draft.get('draft_id'),
                        'target_format': draft.get('target_format') or 'instagram',
                        'has_frame': bool(sel),
                        'draft': slim,
                    })
        items.sort(key=lambda x: x.get('updated_at') or '', reverse=True)
        return jsonify({'thumbnails': items})
    except Exception as exc:
        logger.exception('[thumbnails] Failed to list thumbnails')
        return jsonify({'error': str(exc), 'thumbnails': []}), 500


@app.route('/thumbnails/delete', methods=['POST'])
def delete_thumbnail():
    """Remove a single saved thumbnail draft from its project (does not touch
    the source video or any exported clips)."""
    try:
        data = request.get_json(force=True) or {}
        pid = (data.get('project_id') or '').strip()
        draft_id = (data.get('draft_id') or '').strip()
        if not pid or not re.match(r'^[A-Za-z0-9_\-]+$', pid):
            return jsonify({'error': 'invalid project_id'}), 400
        if not draft_id:
            return jsonify({'error': 'draft_id is required'}), 400
        with project_store_lock:
            project = load_project(pid)
            if not project:
                return jsonify({'error': 'project not found'}), 404
            drafts = project.get('thumbnail_drafts') or []
            kept = [d for d in drafts if (d.get('draft_id') or '') != draft_id]
            if len(kept) == len(drafts):
                return jsonify({'error': 'thumbnail not found'}), 404
            project['thumbnail_drafts'] = kept
            save_project(project)
        logger.info('[thumbnails] Deleted draft %s from project %s', draft_id, pid)
        return jsonify({'success': True})
    except Exception as exc:
        logger.exception('[thumbnails] delete failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/projects/delete', methods=['POST'])
def delete_project():
    try:
        data = request.get_json(force=True) or {}
        pid = (data.get('project_id') or '').strip()
        if not pid or not re.match(r'^[A-Za-z0-9_\-]+$', pid):
            return jsonify({'error': 'invalid project_id'}), 400
        path = get_project_path(pid)
        with project_store_lock:
            if os.path.isfile(path):
                os.remove(path)
        logger.info('[projects] Deleted project %s', pid)
        return jsonify({'success': True})
    except Exception as exc:
        logger.exception('[projects] delete failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/projects/open_source', methods=['POST'])
def open_project_for_source():
    try:
        data = request.get_json(force=True) or {}
        filename = (data.get('filename') or '').strip()
        source_path = (data.get('source_path') or '').strip()
        if not filename and not source_path:
            return jsonify({'error': 'filename or source_path is required'}), 400
        project, existed, resolved_path = resolve_project_for_source(filename, source_path)
        logger.info(
            '[projects] %s project %s for %s',
            'Resumed' if existed else 'Created',
            project.get('id'),
            project.get('source_video', {}).get('filename'),
        )
        return jsonify({
            'project': project,
            'summary': summarize_project(project),
            'existing_project': existed,
            'resolved_source_path': resolved_path,
        })
    except Exception as exc:
        logger.exception('[projects] Failed to open source project')
        return jsonify({'error': str(exc)}), 500


@app.route('/projects/open_saved', methods=['POST'])
def open_saved_project():
    try:
        data = request.get_json(force=True) or {}
        project_id = (data.get('project_id') or '').strip()
        if not project_id:
            return jsonify({'error': 'project_id is required'}), 400

        with project_store_lock:
            project = load_project(project_id)
            if not project:
                return jsonify({'error': 'Project not found'}), 404

            source_path = get_project_source_path(project)
            transcript = project.get('transcript') or {}
            transcript_path = (transcript.get('json_path') or '').strip()
            transcript_words = load_transcript_words_from_json(transcript_path)

            project['last_opened_at'] = iso_now()
            save_project(project)

        source_video = project.get('source_video') or {}
        source_filename = source_video.get('filename') or os.path.basename(source_path or '')
        if source_filename and source_path:
            video_path_cache[source_filename] = source_path

        return jsonify({
            'project': project,
            'summary': summarize_project(project),
            'source_available': bool(source_path),
            'source_path': source_path or source_video.get('path') or '',
            'source_filename': source_filename,
            'source_stream_url': (
                f'http://localhost:5000/projects/source_video/{project_id}'
                if source_path else ''
            ),
            'expected_filename': source_filename,
            'transcript_filename': os.path.basename(transcript_path) if transcript_path else '',
            'transcript_words': transcript_words,
        })
    except Exception as exc:
        logger.exception('[projects] Failed to open saved project')
        return jsonify({'error': str(exc)}), 500


@app.route('/projects/source_video/<project_id>', methods=['GET'])
def stream_project_source_video(project_id):
    with project_store_lock:
        project = load_project(project_id)
    if not project:
        abort(404)

    source_path = get_project_source_path(project)
    if not source_path:
        abort(404)

    mime_type, _ = mimetypes.guess_type(source_path)
    return send_file(
        source_path,
        mimetype=mime_type or 'video/mp4',
        conditional=True,
        etag=False,
        last_modified=None,
    )


@app.route('/projects/update', methods=['POST'])
def update_project():
    try:
        data = request.get_json(force=True) or {}
        project_id = (data.get('project_id') or '').strip()
        event_type = (data.get('event_type') or '').strip()
        payload = data.get('payload') or {}
        if not project_id or not event_type:
            return jsonify({'error': 'project_id and event_type are required'}), 400

        with project_store_lock:
            project = load_project(project_id)
            if not project:
                return jsonify({'error': 'Project not found'}), 404

            project.setdefault('meta', {})
            project['last_opened_at'] = iso_now()

            if event_type == 'source_selected':
                if payload.get('view'):
                    project['meta']['last_view'] = payload['view']
                if payload.get('source_path'):
                    project.setdefault('source_video', {})['path'] = payload['source_path']
            elif event_type == 'transcript_loaded':
                project['transcript'] = {
                    'json_filename': payload.get('json_filename'),
                    'json_path': payload.get('json_path'),
                    'word_count': int(payload.get('word_count') or 0),
                    'loaded_at': iso_now(),
                }
            elif event_type == 'clip_candidates_updated':
                mode = payload.get('mode') or 'viral'
                project['meta']['last_clip_mode'] = mode
                candidates = payload.get('candidates') or []
                compacted = [
                    compact_clip_candidate({**candidate, 'mode': mode})
                    for candidate in candidates[:20]
                ]
                # Viral candidates and sequential split parts are stored SEPARATELY so the
                # two modes never overwrite each other for the same source video.
                if mode == 'split':
                    project['split_parts'] = compacted
                else:
                    project['clip_candidates'] = compacted
            elif event_type == 'clip_selected':
                def _clean_ranges(items):
                    out = []
                    for item in (items or [])[:40]:
                        try:
                            out.append({'start': float(item['start']), 'end': float(item['end'])})
                        except Exception:
                            continue
                    return out
                clip_entry = {
                    'start_time': payload.get('start_time'),
                    'end_time': payload.get('end_time'),
                    'hook_line': payload.get('hook_line'),
                    'mode': payload.get('mode') or project['meta'].get('last_clip_mode') or 'viral',
                    'cut_ranges': _clean_ranges(payload.get('cut_ranges')),
                    'bleep_ranges': _clean_ranges(payload.get('bleep_ranges')),
                    # Caption styling rides along (style dict, per-group text
                    # overrides, per-group emphasis indices) — stored as-is,
                    # bounded for sanity.
                    'caption_style': payload.get('caption_style') if isinstance(payload.get('caption_style'), dict) else None,
                    'caption_overrides': dict(list((payload.get('caption_overrides') or {}).items())[:60]),
                    'caption_emphasis': dict(list((payload.get('caption_emphasis') or {}).items())[:60]),
                    'caption_breaks': [int(b) for b in (payload.get('caption_breaks') or [])[:200] if isinstance(b, (int, float))],
                    'ig_caption': (payload.get('ig_caption') or '')[:3000],
                    'updated_at': iso_now(),
                }
                # Key by hook_line so re-editing the same clip UPDATES the entry
                # (keying by times created duplicates whenever trims changed).
                upsert_project_list_item(
                    project.setdefault('edited_clips', []),
                    clip_entry,
                    ['hook_line', 'mode'] if clip_entry['hook_line'] else ['start_time', 'end_time'],
                )
            elif event_type in {'thumbnail_saved', 'thumbnail_draft_saved'}:
                thumb_entry = normalize_thumbnail_draft(payload)
                upsert_project_list_item(
                    project.setdefault('thumbnail_drafts', []),
                    thumb_entry,
                    ['draft_id'],
                )
            elif event_type == 'export_created':
                export_entry = {
                    'filename': payload.get('filename'),
                    'clip_type': payload.get('clip_type'),
                    'clip_dropbox_url': payload.get('clip_dropbox_url'),
                    'thumbnail_dropbox_url': payload.get('thumbnail_dropbox_url'),
                    'thumbnail_mode': payload.get('thumbnail_mode'),
                    'thumbnail_draft_id': payload.get('thumbnail_draft_id'),
                    'exported_at': iso_now(),
                }
                upsert_project_list_item(
                    project.setdefault('exports', []),
                    export_entry,
                    ['filename'],
                )
            else:
                return jsonify({'error': f'Unsupported event_type: {event_type}'}), 400

            save_project(project)

        return jsonify({'project': project, 'summary': summarize_project(project)})
    except Exception as exc:
        logger.exception('[projects] Failed to update project')
        return jsonify({'error': str(exc)}), 500


@app.route('/project_export_folder/check', methods=['POST'])
def check_project_export_folder():
    try:
        data = request.get_json(force=True) or {}
        project_name = (data.get('project_name') or '').strip()
        source_filename = (data.get('source_filename') or '').strip()
        source_path = (data.get('source_path') or '').strip()
        clip_type = (data.get('clip_type') or '').strip()
        lookup = find_matching_social_media_folder(
            project_name=project_name,
            source_filename=source_filename,
            source_path=source_path,
            clip_type=clip_type,
        )
        matched = lookup['matched_folder']
        alternates = lookup['alternate_matches']
        return jsonify({
            'suggested_folder_name': lookup['suggested_folder_name'],
            'suggested_parent_folder': lookup['suggested_parent_folder'],
            'suggested_relative_folder': lookup['suggested_relative_folder'],
            'parent_confidence': lookup['parent_confidence'],
            'parent_reason': lookup['parent_reason'],
            'folder_exists': bool(matched),
            'matched_folder_name': matched['folder_name'] if matched else '',
            'matched_parent_folder': matched['parent_folder'] if matched else '',
            'matched_relative_folder': matched['relative_path'] if matched else '',
            'available_folders': [
                item['relative_path'] for item in lookup['available_folders']
            ],
            'alternate_matches': [
                item['relative_path'] for item in alternates[:20]
            ],
            'can_create_automatically': not matched and lookup['parent_confidence'] != 'low',
        })
    except Exception as exc:
        logger.exception('[export] Failed to check project folder')
        return jsonify({'error': str(exc)}), 500


@app.route('/project_export_folder/create', methods=['POST'])
def create_project_export_folder():
    try:
        data = request.get_json(force=True) or {}
        relative_folder = sanitize_social_media_relative_path(data.get('relative_folder'))
        if not relative_folder:
            folder_name = sanitize_export_folder_name(data.get('folder_name'))
            parent_folder = sanitize_export_folder_name(data.get('parent_folder'))
            relative_folder = sanitize_social_media_relative_path(
                os.path.join(parent_folder, folder_name) if parent_folder else folder_name
            )
        parts = split_social_media_relative_path(relative_folder)
        if not parts:
            return jsonify({'error': 'relative folder is required'}), 400
        folder_path = build_social_media_local_path(relative_folder)
        os.makedirs(folder_path, exist_ok=True)
        logger.info('[export] Ensured project folder exists: %s', relative_folder)
        return jsonify({
            'folder_name': parts[-1],
            'parent_folder': parts[-2] if len(parts) > 1 else '',
            'relative_folder': relative_folder,
        })
    except Exception as exc:
        logger.exception('[export] Failed to create project folder')
        return jsonify({'error': str(exc)}), 500


@app.route('/project_export_folder/list_all', methods=['GET'])
def list_all_project_export_folders():
    """Flat list of all available relative folder paths inside Social Media Clips.
    Required by CANONICAL.md section 17 (save modal "Other - browse all folders")."""
    try:
        folders = list_social_media_existing_project_folders()
        return jsonify({'folders': [item['relative_path'] for item in folders]})
    except Exception as exc:
        logger.exception('[export] Failed to list all export folders')
        return jsonify({'error': str(exc), 'folders': []}), 500


@app.route('/find_json', methods=['POST'])
def find_json():
    try:
        data = request.json
        filename = data.get('filename', '')
        logger.info('[find_json] Looking for video: %s', filename)

        # Check cache first; fall back to os.walk
        video_path = video_path_cache.get(filename)
        if video_path and not os.path.exists(video_path):
            logger.info('[find_json] Cached path stale, re-walking: %s', video_path)
            video_path = None

        if not video_path:
            video_path = find_video_in_dropbox(filename)

        if not video_path:
            logger.warning('[find_json] Video file not found in Dropbox: %s', filename)
            return jsonify({'json_found': False, 'error': 'Video file not found in Dropbox'})

        # Cache for later use by /thumbnail and other routes
        video_path_cache[filename] = video_path
        logger.info('[cache] Stored path for %s', filename)

        video_folder = os.path.dirname(video_path)
        logger.info('[find_json] Found video at: %s', video_path)
        logger.debug('[find_json] Searching folder: %s', video_folder)
        logger.debug('[find_json] Files in folder: %s', os.listdir(video_folder))

        # Search same folder for any .json file
        matches = [
            os.path.join(video_folder, f)
            for f in os.listdir(video_folder)
            if f.lower().endswith('.json')
        ]
        logger.debug('[find_json] .json files in same folder: %s', matches)

        if not matches:
            logger.warning('[find_json] No .json file found in %s', video_folder)
            return jsonify({'json_found': False, 'error': 'No transcript found near this video'})

        json_path = matches[0]
        logger.info('[find_json] Using transcript JSON: %s', json_path)

        with open(json_path, 'r', encoding='utf-8') as f:
            content = json.load(f)

        return jsonify({
            'json_found': True,
            'video_path': video_path,
            'json_path': json_path,
            'json_filename': os.path.basename(json_path),
            'json_content': content
        })

    except Exception as e:
        logger.exception('[find_json] ERROR')
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
    Accepts multipart/form-data: file (MP4) or source_path.
    Runs Whisper medium, returns { "words": [...] }.
    """
    file = request.files.get("file")
    source_path = (request.form.get("source_path") or "").strip()
    if not file and (not source_path or not os.path.isfile(source_path)):
        return jsonify({"error": "No file provided"}), 400

    tmp_dir = tempfile.mkdtemp()
    try:
        source_name = file.filename if file else os.path.basename(source_path or 'video.mp4')
        src_ext    = os.path.splitext(source_name or 'video.mp4')[1].lower() or '.mp4'
        input_path = os.path.join(tmp_dir, 'source' + src_ext)
        if file:
            file.save(input_path)
        else:
            shutil.copy2(source_path, input_path)

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
                timestamps = [start_t + i * (end_t - start_t) / 29 for i in range(30)]
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
                    mean_brightness = float(np.mean(arr))
                    # Hard-reject completely black frames (cuts/fades)
                    if mean_brightness < 15:
                        thumb_logger.debug(
                            'Job %s frame %s ts=%.1fs SKIPPED black frame brightness=%.1f',
                            job_id, i, ts, mean_brightness,
                        )
                        continue
                    # Composite score: penalize dark frames strongly
                    # brightness_weight: 0.15 (very dark) → 1.0 (well-lit, brightness >= 80)
                    brightness_weight = min(1.0, max(0.15, (mean_brightness - 20) / 60.0))
                    composite = sharpness * brightness_weight
                    thumb_logger.debug(
                        'Job %s frame %s ts=%.1fs sharpness=%.1f brightness=%.1f weight=%.2f score=%.1f',
                        job_id, i, ts, sharpness, mean_brightness, brightness_weight, composite,
                    )
                    scored.append((composite, fp))
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
                        "Rank these frames best to worst for a YouTube/social media thumbnail. "
                        "These are from a stage or auditorium setting where speakers are the primary subject. "
                        "Prefer: speaker face clearly visible and well-lit, eyes open and engaged, "
                        "natural confident expression (not mid-word or grimacing), sharp focus, good posture. "
                        "Strongly avoid: dark silhouette frames, motion-blurred frames, "
                        "frames showing a cut/transition, frames where the speaker is mostly in shadow, "
                        "partially cropped off-frame, or far from center. "
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


@app.route('/thumbnail_titles', methods=['POST'])
def thumbnail_titles():
    """
    Title + Instagram-caption generation for the manual thumbnail flow.
    Accepts JSON: { "clip_transcript": "...", "broader_transcript": "..." }
    Returns:      { "titles": [...], "caption": "..." }
    Forgiving: returns whatever it can ([] / "") on failure.
    """
    try:
        data = request.get_json(force=True) or {}
        clip_transcript = (data.get('clip_transcript') or '').strip()[:3000]
        broader_transcript = (data.get('broader_transcript') or '').strip()[:9000]
        if not clip_transcript:
            return jsonify({'titles': [], 'caption': ''})

        thumb_logger.info('[titles] Generating titles+caption (%s clip / %s broader chars)',
                          len(clip_transcript), len(broader_transcript))
        client = anthropic.Anthropic(api_key=read_api_key())
        part_index = data.get('part_index'); part_total = data.get('part_total')
        today = datetime.date.today()
        date_str = f"{today.strftime('%B')} {today.day}, {today.year}"
        series_note = ""
        try:
            pi = int(part_index); pt = int(part_total)
            if pi and pt and pt > 1:
                series_note = f" This Reel is PART {pi} OF {pt} of a sequential series cut from one longer video."
        except Exception:
            pass
        context_block = ''
        if broader_transcript:
            context_block = (
                "\n\nBroader context — transcript around / from the FULL source video, for "
                "understanding the overall topic only. The caption must be about the CLIP:\n"
                + broader_transcript
            )
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1100,
            messages=[{"role": "user", "content": (
                "The Candler Foundry produces faith-based video content for clergy, scholars, "
                "and the spiritually curious public. Voice: human, honest, direct. "
                "Avoid jargon, church-speak, or academic language.\n"
                f"Today's date is {date_str}.{series_note}\n\n"
                "From the CLIP transcript below, produce two things:\n\n"
                "1) titles: exactly 8 thumbnail title options.\n"
                "   - MAX 60 characters each\n"
                "   - Short, punchy, emotionally direct: a provocative question or bold statement\n"
                "   - No filler phrases, no colons that only pad length\n\n"
                "2) caption: ONE Instagram caption for a Reel of this clip.\n"
                "   - SHORT and punchy (about 1-3 sentences), built to drive engagement\n"
                "   - Open with a scroll-stopping hook in the first line\n"
                "   - Use natural, searchable language and the words a viewer would actually "
                "care about or type, so the caption aids discovery on Instagram\n"
                "   - Minimal, tasteful emoji (zero to two)\n"
                "   - Where it fits naturally, end with a genuine question the audience can "
                "answer in the comments (a light call-to-action is also fine; neither is mandatory)\n"
                + ("   - Clearly signal this is Part %d of %d (e.g. \"Part %d/%d\") and invite viewers to watch the other part(s) for the full conversation.\n" % (pi, pt, pi, pt) if series_note else "")
                + "   - End with 3-5 relevant hashtags on their OWN line at the very end: a couple specific to the clip's topic or theme, one for the show/speaker if apt, and a seasonally-appropriate tag for the current date ONLY if genuinely relevant. Use real, on-brand hashtags; do NOT invent fake or speculative trending tags.\n"
                + "   - Do not wrap the whole caption in quotation marks.\n\n"
                "Return ONLY a JSON object, no markdown, of this exact shape:\n"
                '{"titles": ["..."], "caption": "..."}\n\n'
                f"CLIP transcript ({len(clip_transcript)} chars):\n{clip_transcript}"
                + context_block
            )}]
        )
        raw = msg.content[0].text.strip()
        titles, caption = [], ''
        st = raw.find('{'); en = raw.rfind('}')
        if st != -1 and en != -1:
            try:
                obj = json.loads(raw[st:en + 1])
                parsed = obj.get('titles') or []
                titles = [str(t) for t in parsed if len(str(t)) <= 60][:8]
                if len(titles) < 5:
                    titles = [str(t)[:60] for t in parsed[:8]]
                caption = str(obj.get('caption') or '').strip()
            except Exception as pe:
                thumb_logger.warning('[titles] JSON object parse failed: %s', pe)
        if not titles:   # fallback: a bare JSON array of titles
            a = raw.find('['); b = raw.rfind(']')
            if a != -1 and b != -1:
                try:
                    titles = [str(t)[:60] for t in json.loads(raw[a:b + 1])][:8]
                except Exception:
                    pass
        thumb_logger.info('[titles] Generated %s titles, caption=%s chars', len(titles), len(caption))
        return jsonify({'titles': titles, 'caption': caption})
    except Exception as exc:
        thumb_logger.warning('[titles] Title/caption generation failed: %s', exc)
        return jsonify({'titles': [], 'caption': ''})


@app.route('/detect_speakers', methods=['POST'])
def detect_speakers():
    """Sample one frame and ask Claude Vision if it is a 2-person side-by-side shot.
    Returns {two_speakers, checked, left, right}. checked=False => could not determine
    (frame/api unavailable) so the frontend keeps its POD default. Forgiving on all errors."""
    import base64
    try:
        data = request.get_json(force=True) or {}
        filename    = (data.get('filename') or '').strip()
        source_path = (data.get('source_path') or '').strip()
        try:
            t = max(0.0, float(data.get('timestamp') or 0))
        except Exception:
            t = 0.0
        path = source_path if (source_path and os.path.isfile(source_path)) else (find_video_in_dropbox(filename) or '')
        if not path or not os.path.isfile(path) or not FFMPEG_EXE:
            return jsonify({'two_speakers': False, 'checked': False})
        tmp = tempfile.mkdtemp()
        fp = os.path.join(tmp, 'f.jpg')
        subprocess.run([FFMPEG_EXE, '-y', '-ss', f'{t:.2f}', '-i', path,
                        '-frames:v', '1', '-vf', 'scale=640:-1', '-q:v', '4', fp],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        if not os.path.isfile(fp):
            shutil.rmtree(tmp, ignore_errors=True)
            return jsonify({'two_speakers': False, 'checked': False})
        with open(fp, 'rb') as fh:
            b64 = base64.b64encode(fh.read()).decode()
        shutil.rmtree(tmp, ignore_errors=True)
        key = read_api_key()
        if not key:
            return jsonify({'two_speakers': False, 'checked': False})
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=150,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": (
                    "This is one frame from a video. Are there EXACTLY TWO people seated SIDE BY SIDE "
                    "(a two-person interview / podcast two-shot, both roughly facing the camera)? "
                    "A single speaker, an audience, or a solo stage talk is NOT two side-by-side people. "
                    "If yes, estimate each person's horizontal CENTER as a fraction of the frame width "
                    "(0.0 = left edge, 1.0 = right edge). "
                    "Reply with ONLY JSON: {\"two\": true, \"left\": 0.30, \"right\": 0.70} or {\"two\": false}.")}
            ]}])
        raw = msg.content[0].text.strip()
        st = raw.find('{'); en = raw.rfind('}')
        out = {'two_speakers': False, 'checked': True}
        if st != -1 and en != -1:
            obj = json.loads(raw[st:en + 1])
            if obj.get('two'):
                try:
                    lx = min(0.95, max(0.05, float(obj.get('left', 0.30))))
                    rx = min(0.95, max(0.05, float(obj.get('right', 0.70))))
                except Exception:
                    lx, rx = 0.30, 0.70
                out = {'two_speakers': True, 'checked': True, 'left': lx, 'right': rx}
        logger.info("[detect_speakers] %s -> %s", filename or os.path.basename(path), out)
        return jsonify(out)
    except Exception as exc:
        logger.warning("[detect_speakers] %s", exc)
        return jsonify({'two_speakers': False, 'checked': False})


@app.route('/caption_emphasis', methods=['POST'])
def caption_emphasis():
    """
    AI pre-emphasis for the in-frame caption editor: Claude picks the feature
    words (emotional peaks, key nouns/verbs, numbers, names) that deserve
    caps/color emphasis. Accepts JSON {"clip_transcript": "..."}.
    Returns {"words": [...]} — forgiving: [] on any failure.
    """
    try:
        data = request.get_json(force=True) or {}
        clip_transcript = (data.get('clip_transcript') or '').strip()[:3000]
        if not clip_transcript:
            return jsonify({'words': []})
        client = anthropic.Anthropic(api_key=read_api_key())
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": (
                "You are styling social-media video captions (like OpusClip). "
                "From the clip transcript below, pick the FEATURE WORDS that deserve "
                "visual emphasis (color + ALL CAPS): emotional peaks, surprising or "
                "counterintuitive words, key nouns and verbs, numbers, and names. "
                "Pick roughly 1 word per sentence, 5-15 words total. Choose ONLY single "
                "words that appear verbatim in the transcript. "
                "Return ONLY a JSON array of the words, no markdown.\n\n"
                f"Transcript:\n{clip_transcript}"
            )}]
        )
        raw = msg.content[0].text.strip()
        s = raw.find('['); e = raw.rfind(']')
        words = []
        if s != -1 and e != -1:
            parsed = json.loads(raw[s:e+1])
            words = [str(w) for w in parsed if str(w).strip()][:20]
        logger.info('[emphasis] Picked %s feature words', len(words))
        return jsonify({'words': words})
    except Exception as exc:
        logger.warning('[emphasis] Failed: %s', exc)
        return jsonify({'words': []})


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

HARD CONSTRAINT — OPEN ON THE HOOK (non-negotiable):
The first ~3 seconds decide whether a viewer keeps watching. Choose start_time so the clip OPENS on its single strongest line — the hook_line below must be the FIRST sentence of the clip (at most preceded by one short clause needed for it to make sense).
- Trim all setup, runup, and throat-clearing before the hook. Never open with "So," "And," "As I was saying," or context the viewer does not yet have.
- The opening sentence must stand alone as a scroll-stopper: a bold or counterintuitive claim, a provocative question, a raw admission, or a high-stakes statement that creates a curiosity gap the viewer needs resolved.
- If a segment's best line cannot be moved to the front without losing meaning, pick a different moment. A great line buried later in the clip is NOT usable — the clip must hook immediately.

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
- hook_score: integer 1-10 (score the OPENING line specifically — 10 = the first sentence alone stops a scroll)
- hook_line: string (the single most compelling sentence, quoted verbatim — and it MUST be the clip's opening line)
- why_it_works: string (1 sentence — name the specific technique: vulnerability, surprise, strong hook, emotional peak, etc.)

Find 8 clips, all between 30 and 90 seconds. Rank by hook_score descending.

Transcript:
{transcript_text}"""
        }]
    )
    raw = message.content[0].text.strip()
    logger.debug("[clips] Claude raw (first 300 chars): %s", raw[:300])
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
        logger.info("[clips] Transcript length: %s chars", len(transcript_text))
        logger.debug("[clips] Preview: %s", transcript_text[:200])

        api_key = read_api_key()
        client = anthropic.Anthropic(api_key=api_key)

        # Retry loop: require ≥ 3 clips with valid duration (25–95s) before returning
        valid = []
        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
            try:
                candidates = _call_clips_claude(transcript_text, client)
            except Exception as ce:
                logger.warning("[clips] Attempt %s Claude call failed: %s", attempt + 1, ce)
                candidates = []

            valid = [
                c for c in candidates
                if 25 <= (c.get('end_time', 0) - c.get('start_time', 0)) <= 95
            ]
            logger.info(
                "[clips] Attempt %s: %s total, %s valid (25-95s)",
                attempt + 1, len(candidates), len(valid),
            )

            if len(valid) >= 3:
                break
            if attempt < MAX_RETRIES - 1:
                logger.info("[clips] Too few valid clips; retrying")

        # Highest hook_score first (10s at the top)
        valid.sort(key=lambda c: c.get('hook_score') or 0, reverse=True)

        return jsonify({'candidates': valid})

    except Exception as e:
        logger.exception("ERROR in /clips")
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
        logger.info("[split] Transcript length: %s chars", len(transcript_text))

        # Validate n_parts
        if n_parts is not None:
            try:
                n_parts = int(n_parts)
                n_parts = max(2, min(6, n_parts))
            except (ValueError, TypeError):
                n_parts = None

        api_key = read_api_key()
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
        logger.debug("[split] Claude raw (first 300 chars): %s", raw[:300])
        parts = _parse_claude_json(raw)
        logger.info("[split] Parsed %s parts", len(parts))

        # Sort by part number to guarantee order
        parts.sort(key=lambda p: p.get('part', 0))

        return jsonify({'parts': parts})

    except Exception as e:
        logger.exception("ERROR in /split")
        return jsonify({'error': str(e), 'parts': []})


def compute_kept_segments(starttime, endtime, cut_ranges_raw):
    """
    Subtract user cut-out ranges from [starttime, endtime].
    cut_ranges_raw is a JSON string like "[[12.4, 15.0], [40.2, 41.8]]" (absolute seconds).
    Returns a list of (start, end) kept segments. Invalid/empty input returns the
    full [starttime, endtime] range (legacy single-segment behavior).
    """
    full = [(starttime, endtime)]
    if not cut_ranges_raw:
        return full
    try:
        parsed = json.loads(cut_ranges_raw)
        cuts = []
        for item in parsed:
            cs = max(float(item[0]), starttime)
            ce = min(float(item[1]), endtime)
            if ce - cs > 0.05:
                cuts.append((cs, ce))
        if not cuts:
            return full
        cuts.sort()
        # Merge overlapping cuts
        merged = [cuts[0]]
        for cs, ce in cuts[1:]:
            if cs <= merged[-1][1] + 0.01:
                merged[-1] = (merged[-1][0], max(merged[-1][1], ce))
            else:
                merged.append((cs, ce))
        # Complement inside [starttime, endtime]
        kept = []
        cursor = starttime
        for cs, ce in merged:
            if cs - cursor > 0.05:
                kept.append((cursor, cs))
            cursor = max(cursor, ce)
        if endtime - cursor > 0.05:
            kept.append((cursor, endtime))
        if not kept:
            return full
        return kept[:20]  # sanity cap on segment count
    except Exception as exc:
        logger.warning("[export] Ignoring invalid cut_ranges payload: %s", exc)
        return full


def parse_bleep_ranges(starttime, endtime, bleep_ranges_raw):
    """
    Parse user bleep (mute) ranges. Returns a sorted, merged list of
    (start, end) absolute-second tuples clamped to [starttime, endtime].
    Forgiving: invalid input returns [] (export proceeds without bleeps).
    """
    if not bleep_ranges_raw:
        return []
    try:
        parsed = json.loads(bleep_ranges_raw)
        ranges = []
        for item in parsed:
            bs = max(float(item[0]), starttime)
            be = min(float(item[1]), endtime)
            if be - bs > 0.02:
                ranges.append((bs, be))
        if not ranges:
            return []
        ranges.sort()
        merged = [ranges[0]]
        for bs, be in ranges[1:]:
            if bs <= merged[-1][1] + 0.01:
                merged[-1] = (merged[-1][0], max(merged[-1][1], be))
            else:
                merged.append((bs, be))
        return merged[:30]
    except Exception as exc:
        logger.warning("[export] Ignoring invalid bleep_ranges payload: %s", exc)
        return []


def build_bleep_audio_chain(bleep_ranges, time_offset=0.0):
    """
    Returns an ffmpeg audio-filter string that mutes the given absolute-time
    ranges, expressed relative to a stream that starts at `time_offset`.
    Empty string when there is nothing to mute. (The censor tone is mixed in
    separately — see build_bleep_tone_expr.)
    """
    parts = []
    for bs, be in bleep_ranges:
        parts.append(
            f"volume=0:enable='between(t,{bs - time_offset:.3f},{be - time_offset:.3f})'"
        )
    return ",".join(parts)


def build_bleep_tone_expr(bleep_ranges, time_offset=0.0):
    """
    Volume expression that gates a 1 kHz sine to the bleep windows
    (audible censor tone — Emily prefers it over silence).
    """
    if not bleep_ranges:
        return None
    terms = "+".join(
        f"between(t,{bs - time_offset:.3f},{be - time_offset:.3f})"
        for bs, be in bleep_ranges
    )
    return f"volume='0.25*({terms})':eval=frame"


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
    source_path = (request.form.get("source_path") or "").strip()
    if not file and (not source_path or not os.path.isfile(source_path)):
        return jsonify({"error": "No file provided"}), 400

    try:
        starttime       = float(request.form.get("starttime", 0))
        endtime         = float(request.form.get("endtime",   0))
        suggested_name  = request.form.get("suggested_name",  "clip.mp4")
        clip_type       = request.form.get("clip_type",       "Podcast Clip")
        hook_line       = request.form.get("hook_line",       "")
        content_title   = request.form.get("content_title",   "").strip()
        clip_transcript = request.form.get("clip_transcript", "")
        item_code       = request.form.get("item_code",       "")
        target_folder   = sanitize_social_media_relative_path(request.form.get("target_folder", ""))
        cut_ranges_raw  = request.form.get("cut_ranges",      "").strip()
        bleep_ranges_raw = request.form.get("bleep_ranges",   "").strip()
        captions_srt    = request.form.get("captions_srt",    "")
        captions_spec_raw = request.form.get("captions_spec",  "").strip()
        cover_frame     = request.form.get("cover_frame",     "").strip() in {"1", "true", "yes"}
        ig_caption      = request.form.get("ig_caption",      "").strip()
        # Playback speed burned into the export, from the editor speed slider.
        # Continuous 1.0-2.0 (clamped); 1.0 keeps the original render path untouched.
        try:
            speed = float(request.form.get("speed", 1) or 1)
        except (ValueError, TypeError):
            speed = 1.0
        speed = max(1.0, min(2.0, round(speed, 2)))  # clip-speed slider: continuous 1.0-2.0 (atempo valid range; >2.0 would need 2-stage)
        split_screen    = request.form.get("split_screen", "").strip() in {"1", "true", "yes"}
        split_swap      = request.form.get("split_swap",   "").strip() in {"1", "true", "yes"}
        # Manual per-cell crop regions (fractions of the SOURCE frame): {x,y,w,h} in 0..1.
        # When present they override the auto left/right framing; w/h carry the 9:8 cell aspect.
        split_top = split_bot = None
        try:
            _st = request.form.get("split_top", "").strip()
            _sb = request.form.get("split_bot", "").strip()
            if _st: split_top = json.loads(_st)
            if _sb: split_bot = json.loads(_sb)
        except Exception as _e:
            logger.warning("[export] bad split crop regions, using auto: %s", _e)
        if split_screen:
            logger.info("[export] Split-screen vertical 2-up (swap=%s, manual=%s)", split_swap, bool(split_top and split_bot))
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid parameters: {e}"}), 400

    if endtime <= starttime:
        return jsonify({"error": "endtime must be greater than starttime"}), 400

    thumbnail_file = request.files.get("thumbnail")

    # Normalise output filename to .mp4
    output_name = suggested_name if suggested_name.lower().endswith(".mp4") else suggested_name + ".mp4"
    base_name   = os.path.splitext(output_name)[0]
    folder_name = target_folder or sanitize_social_media_relative_path(base_name)

    # Destination: ~/Dropbox/Social Media Clips/<source-video project>/
    clips_folder = build_social_media_local_path(folder_name)
    os.makedirs(clips_folder, exist_ok=True)
    output_path  = os.path.join(clips_folder, output_name)

    tmp_dir = tempfile.mkdtemp()
    try:
        source_name = file.filename if file else os.path.basename(source_path or 'video.mp4')
        src_ext    = os.path.splitext(source_name or 'video.mp4')[1].lower() or '.mp4'
        input_path = os.path.join(tmp_dir, 'source' + src_ext)
        temp_clip  = os.path.join(tmp_dir, 'temp_clip.mp4')
        if file:
            file.save(input_path)
        else:
            shutil.copy2(source_path, input_path)

        # ── Optional: burn clip captions (from the Words JSON, sized for 9:16) ──
        # Frontend remaps caption times onto the output timeline (cuts removed,
        # bleeped words masked), so the SRT applies AFTER trim/stitch.
        caption_filter = ""
        ass_text = None
        if captions_spec_raw:
            # Styled spec from the in-frame caption editor (fonts + emphasis).
            try:
                ass_text = spec_to_ass(json.loads(captions_spec_raw), 1080, 1920)
                logger.info("[export] Burning styled captions (%s groups)",
                            len(json.loads(captions_spec_raw).get('groups') or []))
            except Exception as exc:
                logger.warning("[export] Invalid captions_spec; falling back: %s", exc)
                ass_text = None
        if ass_text is None and captions_srt.strip():
            ass_text = srt_to_ass(captions_srt, 1080, 1920)
            logger.info("[export] Burning clip captions (%s chars of SRT)", len(captions_srt))
        if ass_text:
            ass_path = os.path.join(tmp_dir, "captions.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_text)
            caption_filter = ",subtitles=filename=captions.ass"

        # ── Speed (1 / 1.5 / 2x) ───────────────────────
        # Applied at the END of the video chain (after subtitles) so burned
        # captions compress in lock-step with the footage — no need to rescale
        # the ASS timestamps. Audio gets a matching atempo (valid 0.5-2.0, so
        # 1.5 and 2.0 are single-stage). speed == 1.0 leaves the chains untouched.
        # ,fps=30 re-times the sped video to a clean 30fps CFR whose duration MATCHES the
        # atempo-stretched audio. Without it, setpts kept the source fps and the video ran ~1-2%
        # longer than the audio, so the A/V drift accumulated and streaming players (Dropbox web)
        # crackled/stuttered on sped clips even though the audio samples were clean.
        speed_v_suffix = "" if speed == 1.0 else f",setpts=PTS/{speed:.4f},fps=30"
        if speed != 1.0:
            logger.info("[export] Speeding clip to %.2fx (video setpts + audio atempo)", speed)

        def _reframe(in_label):
            # in_label is the source video pad, e.g. "[0:v]" or "[vcat]". Returns a
            # filter_complex segment ending in [vout], with captions + speed appended.
            if not split_screen:
                return f"{in_label}crop=ih*9/16:ih,scale=1080:1920{caption_filter}{speed_v_suffix}[vout]"
            # Vertical split-screen: two side-by-side people stacked into 9:16. Each cell
            # is 1080x960 (9:8); crop a 9:8 region at full height anchored to each edge
            # (left side -> top cell, right side -> bottom; swap flips them).
            def _clip01(v):
                try: return max(0.0, min(1.0, float(v)))
                except Exception: return None
            def _cell(region, fallback):
                if isinstance(region, dict) and all(k in region for k in ("x", "y", "w", "h")):
                    x, y, w, h = (_clip01(region[k]) for k in ("x", "y", "w", "h"))
                    if None not in (x, y, w, h) and w > 0 and h > 0:
                        # keep the crop inside the frame
                        x = min(x, 1.0 - w); y = min(y, 1.0 - h)
                        return (f"crop=iw*{w:.5f}:ih*{h:.5f}:iw*{x:.5f}:ih*{y:.5f},"
                                f"scale=1080:960,setsar=1")
                return fallback
            L = "crop=ih*9/8:ih:0:0,scale=1080:960,setsar=1"
            R = "crop=ih*9/8:ih:iw-ih*9/8:0,scale=1080:960,setsar=1"
            auto_top, auto_bot = (R, L) if split_swap else (L, R)
            top_src = _cell(split_top, auto_top)
            bot_src = _cell(split_bot, auto_bot)
            return (f"{in_label}split=2[ssA][ssB];"
                    f"[ssA]{top_src}[sstop];[ssB]{bot_src}[ssbot];"
                    f"[sstop][ssbot]vstack=inputs=2{caption_filter}{speed_v_suffix}[vout]")

        # ── Step A: 9:16 vertical reframe ──
        kept_segments = compute_kept_segments(starttime, endtime, cut_ranges_raw)
        bleep_ranges  = parse_bleep_ranges(starttime, endtime, bleep_ranges_raw)
        if bleep_ranges:
            logger.info("[export] Muting %s bleep range(s)", len(bleep_ranges))

        if len(kept_segments) > 1:
            # Stitched export: user cut out word ranges mid-clip.
            # Single ffmpeg pass: trim/atrim each kept segment, concat, then crop/scale.
            logger.info(
                "[export] Stitched export: %s kept segments from cuts %s",
                len(kept_segments), cut_ranges_raw[:200],
            )
            bleep_chain = build_bleep_audio_chain(bleep_ranges, time_offset=0.0)
            tone_expr   = build_bleep_tone_expr(bleep_ranges, time_offset=0.0)
            filter_parts = []
            audio_src = "0:a"
            if bleep_chain and tone_expr:
                # Build a bleeped full-timeline audio track once, then split it
                # so each kept segment can atrim from it.
                filter_parts.append(f"[0:a]{bleep_chain}[mutefull]")
                filter_parts.append(f"[1:a]{tone_expr}[tonefull]")
                filter_parts.append(
                    f"[mutefull][tonefull]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[bfull]"
                )
                filter_parts.append(
                    f"[bfull]asplit={len(kept_segments)}" +
                    "".join(f"[bs{i}]" for i in range(len(kept_segments)))
                )
            concat_pads = ""
            for i, (seg_s, seg_e) in enumerate(kept_segments):
                filter_parts.append(
                    f"[0:v]trim=start={seg_s:.3f}:end={seg_e:.3f},setpts=PTS-STARTPTS[v{i}]"
                )
                a_in = f"[bs{i}]" if (bleep_chain and tone_expr) else f"[{audio_src}]"
                filter_parts.append(
                    f"{a_in}atrim=start={seg_s:.3f}:end={seg_e:.3f},asetpts=PTS-STARTPTS[a{i}]"
                )
                concat_pads += f"[v{i}][a{i}]"
            filter_parts.append(
                f"{concat_pads}concat=n={len(kept_segments)}:v=1:a=1[vcat][acat]"
            )
            filter_parts.append(_reframe("[vcat]"))
            audio_out = "[acat]"
            if speed != 1.0:
                filter_parts.append(f"[acat]atempo={speed:.4f},alimiter=limit=0.97[aout]")
                audio_out = "[aout]"
            stitched_cmd = [FFMPEG_EXE, "-y", "-i", input_path]
            if bleep_chain and tone_expr:
                stitched_cmd += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]
            stitched_cmd += [
                "-filter_complex", ";".join(filter_parts),
                "-map", "[vout]", "-map", audio_out,
                "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                "-c:a", "aac",
                output_path]
            r2 = subprocess.run(
                stitched_cmd,
                capture_output=True, creationflags=CREATE_NO_WINDOW,
                cwd=tmp_dir,
            )
            if r2.returncode != 0:
                err = r2.stderr.decode("utf-8", errors="replace")[-800:]
                return jsonify({"error": "ffmpeg stitched export failed: " + err}), 500
        else:
            # Single-segment export (no cuts). ONE frame-accurate pass straight
            # from the original input via an input-seek (-ss/-t) re-encode. The
            # old path stream-copied the segment first (-ss -to -c copy), which
            # can only cut on a keyframe, so the clip started a fraction of a
            # second early and every bleep / caption landed early (CANONICAL §36).
            # Input-seek while re-encoding is sample-accurate and resets PTS to 0,
            # so the clip starts exactly at starttime; bleep/tone use
            # offset=starttime (local clip time = absolute - starttime).
            seg_dur = endtime - starttime
            bleep_chain = build_bleep_audio_chain(bleep_ranges, time_offset=starttime)
            tone_expr   = build_bleep_tone_expr(bleep_ranges, time_offset=starttime)
            if bleep_chain and tone_expr:
                # Censor tone: mute the speech and mix in a gated 1 kHz sine.
                # When sped up, the amix feeds an atempo stage before [aout].
                amix_tail = "[aout]" if speed == 1.0 else "[amx];[amx]atempo=%.4f,alimiter=limit=0.97[aout]" % speed
                fc = (
                    _reframe("[0:v]") + ";"
                    f"[0:a]{bleep_chain}[mute];"
                    f"[1:a]{tone_expr}[tone];"
                    f"[mute][tone]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{amix_tail}"
                )
                pass2_cmd = [FFMPEG_EXE, "-y",
                             "-ss", f"{starttime:.3f}", "-t", f"{seg_dur:.3f}", "-i", input_path,
                             "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                             "-filter_complex", fc,
                             "-map", "[vout]", "-map", "[aout]",
                             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                             "-c:a", "aac",
                             output_path]
            else:
                pass2_cmd = [FFMPEG_EXE, "-y",
                             "-ss", f"{starttime:.3f}", "-t", f"{seg_dur:.3f}", "-i", input_path]
                if split_screen:
                    pass2_cmd += ["-filter_complex", _reframe("[0:v]"),
                                  "-map", "[vout]", "-map", "0:a?"]
                else:
                    pass2_cmd += ["-vf", f"crop=ih*9/16:ih,scale=1080:1920{caption_filter}{speed_v_suffix}"]
                if speed != 1.0:
                    pass2_cmd += ["-af", f"atempo={speed:.4f},alimiter=limit=0.97"]
                pass2_cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                              "-c:a", "aac",
                              output_path]
            r2 = subprocess.run(
                pass2_cmd,
                capture_output=True, creationflags=CREATE_NO_WINDOW,
                cwd=tmp_dir,
            )
            if r2.returncode != 0:
                err = r2.stderr.decode("utf-8", errors="replace")[-800:]
                return jsonify({"error": "ffmpeg reframe failed: " + err}), 500

        # ── Step B: Save thumbnail (if provided) ──
        thumb_local_path  = None
        thumb_dbx_path    = None
        if thumbnail_file and thumbnail_file.filename:
            thumb_name       = base_name + " - Thumbnail.png"
            thumb_dir        = os.path.join(clips_folder, "Thumbnails")
            os.makedirs(thumb_dir, exist_ok=True)
            thumb_local_path = os.path.join(thumb_dir, thumb_name)
            thumbnail_file.save(thumb_local_path)
            thumb_dbx_path   = build_social_media_dropbox_path(folder_name + "/Thumbnails", thumb_name)

        # ── Step B2: optional Instagram cover burn ──
        # The basic Zapier "Publish Video" action has no cover-URL field, and
        # Instagram uses frame zero as the reel cover. Prepending the thumbnail
        # as ONE frame (~1/30s) makes the posted reel show it as the cover
        # while staying imperceptible during playback.
        if cover_frame and thumb_local_path:
            burned_path = os.path.join(tmp_dir, "burned.mp4")
            rb = subprocess.run(
                [FFMPEG_EXE, "-y",
                 "-loop", "1", "-framerate", "30", "-t", "0.10", "-i", thumb_local_path,
                 "-i", output_path,
                 "-f", "lavfi", "-t", "0.10", "-i", "anullsrc=r=48000:cl=stereo",
                 "-filter_complex",
                 "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                 "crop=1080:1920,setsar=1,format=yuv420p[cv];"
                 "[1:v]setsar=1[mv];"
                 "[2:a]aformat=sample_rates=48000:channel_layouts=stereo[ca];"
                 "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[ma];"
                 "[cv][ca][mv][ma]concat=n=2:v=1:a=1[v][a]",
                 "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                 "-c:a", "aac",
                 burned_path],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
            if rb.returncode == 0 and os.path.isfile(burned_path):
                shutil.move(burned_path, output_path)
                logger.info("[export] Burned thumbnail as cover frame")
            else:
                err = rb.stderr.decode("utf-8", errors="replace")[-400:]
                logger.warning("[export] Cover-frame burn failed (clip kept without it): %s", err)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Step C: Dropbox shared links ──
    clip_url  = None
    thumb_url = None
    dropbox_error = None
    try:
        dbx = get_dropbox_client()
        clip_url  = get_or_create_shared_link(dbx, build_social_media_dropbox_path(folder_name, output_name))
        if thumb_dbx_path:
            thumb_url = get_or_create_shared_link(dbx, thumb_dbx_path)

    except FileNotFoundError:
        dropbox_error = "dropbox_credentials.json not found in Dropbox/Scripts"
        logger.warning("dropbox_credentials.json not found; skipping Dropbox link generation")
    except ImportError:
        dropbox_error = "the 'dropbox' Python package is not installed (pip install dropbox)"
        logger.warning("dropbox package not installed; skipping link generation")
    except Exception as de:
        dropbox_error = str(de)[:300]
        logger.warning("Dropbox error during shared-link generation: %s", de)

    # ── Step D: Search source video record in Airtable (by item code) ──
    airtable_key      = read_airtable_api_key()
    source_record_id  = None
    source_speaker_name = None
    source_link_error = None
    source_link_field = "Full-Length Video"

    if airtable_key and item_code:
        # Podcast originals live in the POD & YouTube table and link via
        # "Full-Length Podcast"; everything else links "Full-Length Video".
        if item_code.upper().startswith("POD"):
            source_table      = "tbloVdhcMFMaMw5KC"   # POD & YouTube
            source_link_field = "Full-Length Podcast"
        else:
            source_table      = "tblS1Bk29cXyGGUdo"   # 3MB, UNST, TheoEd, OND
        formula  = '{{Code}}="{}"'.format(item_code)
        params   = urllib.parse.urlencode({"filterByFormula": formula})
        at_url   = "https://api.airtable.com/v0/appiL0Z2RilcAT2Cw/" + source_table + "?" + params
        at_req   = urllib.request.Request(
            at_url, headers={"Authorization": "Bearer " + airtable_key}
        )
        try:
            with urllib.request.urlopen(at_req, timeout=10) as resp:
                records = json.loads(resp.read()).get("records", [])
                if records:
                    source_record_id = records[0]["id"]
                    rec_fields = records[0].get("fields", {})
                    source_speaker_name = rec_fields.get("Instructor/Speaker")
                    if not source_speaker_name:
                        fp = rec_fields.get("Featured Participant")
                        if isinstance(fp, list) and fp:
                            source_speaker_name = fp[0]
                        elif isinstance(fp, str):
                            source_speaker_name = fp
                    logger.info("[airtable] Linked source %s -> %s (%s, speaker=%s)", item_code, source_record_id, source_link_field, source_speaker_name)
                else:
                    source_link_error = f"no record with Code '{item_code}' in the source table"
                    logger.warning("[airtable] No source record found for code %s in %s", item_code, source_table)
        except Exception as ae:
            try:
                import urllib.error
                if isinstance(ae, urllib.error.HTTPError):
                    source_link_error = f"HTTP {ae.code}: " + ae.read().decode("utf-8", errors="replace")[:200]
                else:
                    source_link_error = str(ae)[:200]
            except Exception:
                source_link_error = str(ae)[:200]
            logger.warning("Airtable source lookup failed: %s", source_link_error)
    elif airtable_key and not item_code:
        source_link_error = "no item code found in the source filename"

    # ── Step E: Create Airtable record (Video Shorts & Social) ──
    airtable_record_id = None
    airtable_url       = None
    airtable_error     = None

    # Prefer "Full Speaker Name — first 4 words…" when we found the source
    # record (the filename only carries the last name).
    try:
        if source_speaker_name and clip_transcript:
            first4 = " ".join(clip_transcript.split()[:4]).rstrip(".,;:!?")
            content_title = f"{source_speaker_name} \u2014 {first4}\u2026"[:120]
    except Exception:
        pass

    if not airtable_key:
        airtable_error = "Airtable API key not found at Dropbox/Scripts/airtable_api_key.txt"

    if airtable_key:
        # Only include writable fields — never lookup/rollup/formula/AI/button
        fields = {"Status": "Draft"}
        if clip_type:
            fields["Type"] = clip_type
        if content_title:
            fields["Content Title"] = content_title
        elif hook_line:
            fields["Content Title"] = hook_line
        if clip_url:
            fields["Clip - Dropbox URL"] = clip_url
        if thumb_url:
            fields["Thumbnail - Dropbox URL"] = thumb_url
        if clip_transcript:
            fields["Clip Transcript"] = clip_transcript
        if ig_caption:
            fields["IG Social Media Caption"] = ig_caption
        if source_record_id:
            fields[source_link_field] = [source_record_id]

        def _create_shorts_record(fields_payload):
            payload = json.dumps({"fields": fields_payload}).encode("utf-8")
            at_req = urllib.request.Request(
                "https://api.airtable.com/v0/appiL0Z2RilcAT2Cw/tbll0KDqmrAlwQuAx",
                data=payload,
                headers={
                    "Authorization": "Bearer " + airtable_key,
                    "Content-Type":  "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(at_req, timeout=15) as resp:
                return json.loads(resp.read()).get("id")

        def _describe_airtable_error(exc):
            try:
                import urllib.error
                if isinstance(exc, urllib.error.HTTPError):
                    body = exc.read().decode("utf-8", errors="replace")[:300]
                    return f"HTTP {exc.code}: {body}"
            except Exception:
                pass
            return str(exc)[:300]

        try:
            airtable_record_id = _create_shorts_record(fields)
        except Exception as ae:
            # An invalid single-select Type (e.g. "Other") fails the whole
            # record — retry once without it so the record is still created.
            if "Type" in fields:
                logger.warning("Airtable record creation failed (%s); retrying without Type", ae)
                fields.pop("Type", None)
                try:
                    airtable_record_id = _create_shorts_record(fields)
                except Exception as ae2:
                    airtable_error = _describe_airtable_error(ae2)
                    logger.warning("Airtable record creation failed: %s", airtable_error)
            else:
                airtable_error = _describe_airtable_error(ae)
                logger.warning("Airtable record creation failed: %s", airtable_error)

        if airtable_record_id:
            airtable_url = (
                "https://airtable.com/appiL0Z2RilcAT2Cw"
                "/tbll0KDqmrAlwQuAx/" + airtable_record_id
            )

    if not clip_url and not dropbox_error:
        # Link failed without a hard error — almost always the sync race on a
        # big file. Retry in the background and patch Airtable when ready.
        _background_link_and_patch(
            build_social_media_dropbox_path(folder_name, output_name),
            airtable_record_id,
            "Clip - Dropbox URL",
        )
        dropbox_error = ("clip is still syncing to Dropbox — the share link will be "
                         "added to Airtable automatically within a few minutes")
    elif not clip_url and dropbox_error and "not_found" in dropbox_error:
        _background_link_and_patch(
            build_social_media_dropbox_path(folder_name, output_name),
            airtable_record_id,
            "Clip - Dropbox URL",
        )
        dropbox_error = ("clip is still syncing to Dropbox — the share link will be "
                         "added to Airtable automatically within a few minutes")

    return jsonify({
        "success":               True,
        "output_filename":       output_name,
        "folder_name":           folder_name,
        "clip_dropbox_url":      clip_url,
        "thumbnail_dropbox_url": thumb_url,
        "airtable_record_id":    airtable_record_id,
        "airtable_url":          airtable_url,
        "airtable_error":        airtable_error,
        "dropbox_error":         dropbox_error,
        "source_record_id":      source_record_id,
        "source_link_error":     source_link_error,
    })


@app.route("/export_thumbnail", methods=["POST"])
def export_thumbnail():
    import urllib.request

    thumbnail_file = request.files.get("thumbnail")
    if not thumbnail_file or not thumbnail_file.filename:
        return jsonify({"error": "No thumbnail provided"}), 400

    suggested_name = request.form.get("suggested_name", "clip.mp4")
    folder_name = sanitize_social_media_relative_path(request.form.get("target_folder", ""))
    airtable_record_id = (request.form.get("airtable_record_id") or "").strip()
    if not folder_name:
        return jsonify({"error": "target_folder is required"}), 400

    cover_frame = (request.form.get("cover_frame") or "").strip() in {"1", "true", "yes"}
    # June 12 (Emily): standalone thumbnail PNGs are OFF by default — the
    # image exists only to burn the Instagram cover. save_png=1 restores the
    # old behavior (Thumbnails/ subfolder + shared link + Airtable patch).
    save_png = (request.form.get("save_png") or "0").strip() in {"1", "true", "yes"}
    cover_burned = False

    output_name = suggested_name if suggested_name.lower().endswith(".mp4") else suggested_name + ".mp4"
    base_name = os.path.splitext(output_name)[0]
    thumb_name = base_name + " - Thumbnail.png"
    clips_folder = build_social_media_local_path(folder_name)
    thumb_dbx_path = None
    if save_png:
        thumb_dir = os.path.join(clips_folder, "Thumbnails")
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_local_path = os.path.join(thumb_dir, thumb_name)
        thumb_dbx_path = build_social_media_dropbox_path(folder_name + "/Thumbnails", thumb_name)
    else:
        thumb_local_path = os.path.join(tempfile.mkdtemp(), thumb_name)
    thumbnail_file.save(thumb_local_path)

    # ── Instagram cover burn (primary flow) ──
    # Thumbnails are attached AFTER the clip is saved, so the cover burn lives
    # here: prepend the thumbnail as one ~1/30s frame onto the already-saved
    # clip. The Dropbox shared link is path-based, so it stays valid.
    clip_path = os.path.join(clips_folder, output_name)
    if cover_frame and FFMPEG_EXE and os.path.isfile(clip_path):
        tmp_dir = tempfile.mkdtemp()
        try:
            burned_path = os.path.join(tmp_dir, "burned.mp4")
            rb = subprocess.run(
                [FFMPEG_EXE, "-y",
                 "-loop", "1", "-framerate", "30", "-t", "0.10", "-i", thumb_local_path,
                 "-i", clip_path,
                 "-f", "lavfi", "-t", "0.10", "-i", "anullsrc=r=48000:cl=stereo",
                 "-filter_complex",
                 "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                 "crop=1080:1920,setsar=1,format=yuv420p[cv];"
                 "[1:v]setsar=1[mv];"
                 "[2:a]aformat=sample_rates=48000:channel_layouts=stereo[ca];"
                 "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[ma];"
                 "[cv][ca][mv][ma]concat=n=2:v=1:a=1[v][a]",
                 "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                 "-c:a", "aac",
                 burned_path],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
            if rb.returncode == 0 and os.path.isfile(burned_path):
                shutil.move(burned_path, clip_path)
                cover_burned = True
                logger.info("[export] Burned thumbnail as cover frame onto %s", output_name)
            else:
                err = rb.stderr.decode("utf-8", errors="replace")[-400:]
                logger.warning("[export] Cover-frame burn failed (clip kept without it): %s", err)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # First frame of the saved clip — returned so the app can SHOW the user the
    # embedded cover without posting anywhere.
    first_frame_b64 = None
    if FFMPEG_EXE and os.path.isfile(clip_path):
        try:
            import base64 as _b64
            _ftmp = tempfile.mkdtemp()
            _fp = os.path.join(_ftmp, "frame0.jpg")
            subprocess.run(
                [FFMPEG_EXE, "-y", "-i", clip_path, "-frames:v", "1", "-vf", "scale=320:-1", "-q:v", "4", _fp],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
            if os.path.isfile(_fp):
                with open(_fp, "rb") as _fh:
                    first_frame_b64 = "data:image/jpeg;base64," + _b64.b64encode(_fh.read()).decode("ascii")
            shutil.rmtree(_ftmp, ignore_errors=True)
        except Exception as _e:
            logger.warning("[export] first-frame preview failed: %s", _e)

    thumb_url = None
    if save_png and thumb_dbx_path:
        try:
            dbx = get_dropbox_client()
            thumb_url = get_or_create_shared_link(dbx, thumb_dbx_path)
        except FileNotFoundError:
            logger.warning("dropbox_credentials.json not found; skipping Dropbox link generation")
        except Exception as exc:
            logger.warning("Dropbox error during thumbnail shared-link generation: %s", exc)

    if airtable_record_id and thumb_url:
        airtable_key = read_airtable_api_key()
        if airtable_key:
            payload = json.dumps({
                "fields": {
                    "Thumbnail - Dropbox URL": thumb_url,
                }
            }).encode("utf-8")
            at_req = urllib.request.Request(
                "https://api.airtable.com/v0/appiL0Z2RilcAT2Cw/tbll0KDqmrAlwQuAx/" + airtable_record_id,
                data=payload,
                headers={
                    "Authorization": "Bearer " + airtable_key,
                    "Content-Type": "application/json",
                },
                method="PATCH",
            )
            try:
                with urllib.request.urlopen(at_req, timeout=15):
                    pass
            except Exception as exc:
                logger.warning("Airtable thumbnail update failed: %s", exc)

    if not save_png:
        try:
            shutil.rmtree(os.path.dirname(thumb_local_path), ignore_errors=True)
        except Exception:
            pass

    return jsonify({
        "success": True,
        "thumbnail_filename": thumb_name,
        "thumbnail_dropbox_url": thumb_url,
        "cover_burned": cover_burned,
        "first_frame_b64": first_frame_b64,
        "folder_name": folder_name,
    })


if __name__ == "__main__":
    logger.info("Foundry Video Editor backend starting on http://localhost:5000")
    api_key = read_api_key()
    if api_key:
        logger.info("API key loaded.")
    else:
        logger.warning("API key not found at %s", API_KEY_PATH)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
