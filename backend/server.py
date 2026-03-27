"""
Foundry Video Editor — Local Flask Backend
Runs on localhost:5000
"""

import io
import os
import json
import platform
import re
import shutil
import subprocess
import tempfile
import anthropic
import whisper
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CREATE_NO_WINDOW = 0x08000000 if platform.system() == 'Windows' else 0

STYLE_STR = "Fontname=Arial,Outline=1,Shadow=0,BorderStyle=1,Spacing=1"

# ── Dropbox portable paths ──
dropbox_root = os.path.join(os.path.expanduser('~'), 'Dropbox')
API_KEY_PATH = os.path.join(dropbox_root, 'Scripts', 'api_key.txt')


def find_ffmpeg():
    candidates = [
        os.path.join(dropbox_root, 'Scripts', 'FFMPEG', 'ffmpeg.exe'),
        os.path.join(dropbox_root, 'Scripts', 'FFMPEG', 'bin', 'ffmpeg.exe'),
        os.path.join(dropbox_root, 'FFMPEG', 'ffmpeg.exe'),
        os.path.join(dropbox_root, 'ffmpeg', 'bin', 'ffmpeg.exe'),
        'ffmpeg',
    ]
    for path in candidates:
        try:
            result = subprocess.run([path, '-version'], capture_output=True,
                timeout=5, creationflags=CREATE_NO_WINDOW)
            if result.returncode == 0:
                print(f'[ffmpeg] Found at: {path}')
                return path
        except Exception:
            continue
    print('[ffmpeg] ERROR: ffmpeg not found in any expected location')
    return None

FFMPEG_EXE = find_ffmpeg()

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
        return jsonify({'error': 'ffmpeg not found'}), 500

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    try:
        font_size = int(request.form.get("font_size", 18))
    except (ValueError, TypeError):
        font_size = 18

    position   = request.form.get("position",   "bottom")
    text_color = request.form.get("text_color", "white")

    # Alignment: bottom-center=2, top-center=8 (ASS numpad layout)
    alignment = 8 if position == "top" else 2
    # PrimaryColour in ASS format (&HAABBGGRR)
    color_map = {"white": "&H00FFFFFF", "yellow": "&H0000FFFF"}
    primary_colour = color_map.get(text_color, "&H00FFFFFF")

    style = (
        f"Fontsize={font_size},{STYLE_STR},"
        f"Alignment={alignment},PrimaryColour={primary_colour}"
    )

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
    import glob as glob_module
    filename = request.json.get('filename', '')
    dropbox_root_local = os.path.join(os.path.expanduser('~'), 'Dropbox')

    print(f'[find_json] Searching for video: {filename}')

    # Find the video file anywhere in Dropbox by exact filename
    video_matches = glob_module.glob(
        os.path.join(dropbox_root_local, '**', filename), recursive=True
    )
    if not video_matches:
        print('[find_json] Video not found in Dropbox')
        return jsonify({'found': False, 'reason': 'Could not locate video in Dropbox'})

    video_path = video_matches[0]
    video_folder = os.path.dirname(video_path)
    print(f'[find_json] Found video at: {video_path}')
    print(f'[find_json] Video folder: {video_folder}')

    # Search for Words JSON using glob pattern only — no prefix matching
    json_matches = glob_module.glob(os.path.join(video_folder, '*Transcript (Words).json'))
    print(f'[find_json] JSON search result (same folder): {json_matches}')

    if not json_matches:
        # Try one level up
        parent = os.path.dirname(video_folder)
        json_matches = glob_module.glob(os.path.join(parent, '*Transcript (Words).json'))
        print(f'[find_json] JSON search result (parent folder): {json_matches}')

    if not json_matches:
        return jsonify({'found': False, 'reason': 'No transcript found near this video'})

    json_path = json_matches[0]
    with open(json_path, 'r', encoding='utf-8') as f:
        content = json.load(f)

    return jsonify({
        'found': True,
        'json_path': json_path,
        'json_filename': os.path.basename(json_path),
        'json_content': content
    })


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


# ── Thumbnails ──
@app.route('/thumbnail', methods=['POST'])
def thumbnail():
    """
    Accepts multipart/form-data:
      file             — MP4 or MOV
      titles_only      — "1" to skip frame extraction and just redo titles
      clip_transcript  — optional clip text to augment title generation

    Returns JSON:
      { "frames": ["base64...", ...up to 8], "titles": [...8], "clip_transcript": str }
      or if titles_only=1: { "titles": [...], "clip_transcript": str }
    """
    import base64
    from PIL import Image as PILImage
    import numpy as np

    if not FFMPEG_EXE:
        return jsonify({'error': 'ffmpeg not found'}), 500

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    titles_only              = request.form.get("titles_only", "0") == "1"
    incoming_clip_transcript = request.form.get("clip_transcript", "")

    src_ext    = os.path.splitext(file.filename or 'video.mp4')[1].lower() or '.mp4'
    tmp_dir    = tempfile.mkdtemp()
    frame_b64s = None

    try:
        input_path = os.path.join(tmp_dir, 'source' + src_ext)
        file.save(input_path)

        # Read API key + Anthropic client
        api_key_path = os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'api_key.txt')
        with open(api_key_path, 'r') as fk:
            api_key = fk.read().strip()
        client = anthropic.Anthropic(api_key=api_key)

        if not titles_only:
            # ── Step A: get video duration via ffprobe ──
            ffprobe_exe = FFMPEG_EXE.replace('ffmpeg.exe', 'ffprobe.exe')
            if not os.path.isfile(ffprobe_exe):
                ffprobe_exe = 'ffprobe'
            probe = subprocess.run(
                [ffprobe_exe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
            )
            try:
                duration = float(probe.stdout.strip())
            except Exception:
                duration = 90.0

            skip     = 17.0
            usable   = max(duration - skip - 3.0, 10.0)
            n_sample = 20
            timestamps = [skip + i * usable / n_sample for i in range(n_sample)]

            # ── Step B: extract frames via ffmpeg (PIL + numpy only, no OpenCV) ──
            scored = []
            for i, ts in enumerate(timestamps):
                fp = os.path.join(tmp_dir, f'frame_{i:03d}.jpg')
                subprocess.run(
                    [FFMPEG_EXE, '-y', '-ss', str(ts), '-i', input_path,
                     '-frames:v', '1', '-q:v', '2', fp],
                    capture_output=True, creationflags=CREATE_NO_WINDOW
                )
                if not os.path.isfile(fp):
                    continue
                try:
                    img = PILImage.open(fp).convert('L')
                    arr = np.array(img, dtype=np.float32)
                    sharpness = float(arr.var())
                    print(f'[thumbnail] frame {i} ts={ts:.1f}s sharpness={sharpness:.0f}')
                    scored.append((sharpness, fp))
                except Exception as fe:
                    print(f'[thumbnail] frame {i} load error: {fe}')

            if not scored:
                return jsonify({"error": "Could not extract usable frames from video"}), 500

            # Sort best-first, cap at 8
            scored.sort(reverse=True)
            scored = scored[:8]

            # Encode as 640×360 JPEG base64
            frame_b64s = []
            for _, fp in scored:
                try:
                    pil = PILImage.open(fp).convert('RGB').resize((640, 360), PILImage.LANCZOS)
                    buf = io.BytesIO()
                    pil.save(buf, 'JPEG', quality=85)
                    frame_b64s.append(base64.b64encode(buf.getvalue()).decode())
                except Exception as ee:
                    print(f'[thumbnail] encode error: {ee}')

            if not frame_b64s:
                return jsonify({"error": "Frame encoding failed"}), 500

            # ── Step C: Claude Vision ranking — reorder frame_b64s best-first ──
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
                        "Return ONLY a JSON array of ALL indices, best first. "
                        "Example: [2,0,3,1]"
                    )
                })
                rank_msg = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=120,
                    messages=[{"role": "user", "content": content}]
                )
                rank_text = rank_msg.content[0].text.strip()
                s = rank_text.find('['); e = rank_text.rfind(']')
                if s != -1 and e != -1:
                    ranks = json.loads(rank_text[s:e+1])
                    seen    = set()
                    ordered = []
                    for idx in ranks:
                        if isinstance(idx, int) and 0 <= idx < len(frame_b64s) and idx not in seen:
                            ordered.append(frame_b64s[idx])
                            seen.add(idx)
                    for idx in range(len(frame_b64s)):
                        if idx not in seen:
                            ordered.append(frame_b64s[idx])
                    frame_b64s = ordered
            except Exception as ve:
                print(f'[thumbnail] Vision ranking failed (using sharpness order): {ve}')

        # ── Step D: Whisper tiny + Claude title generation ──
        tiny     = get_whisper_tiny()
        w_result = tiny.transcribe(input_path)
        full_transcript = (w_result.get("text") or "")[:3000]

        titles = []
        try:
            context_parts = [f"Full video transcript:\n{full_transcript}"]
            if incoming_clip_transcript:
                context_parts.append(
                    f"Clip transcript (focus segment):\n{incoming_clip_transcript[:1000]}"
                )
            context = "\n\n".join(context_parts)

            title_msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": (
                        "The Candler Foundry produces faith-based video content for clergy, scholars, "
                        "and the spiritually curious public. The best thumbnail titles are short, surprising, "
                        "and emotionally resonant — a question or statement that makes someone stop scrolling. "
                        "Avoid jargon, church-speak, or academic language. Aim for human, honest, direct.\n\n"
                        "Generate exactly 8 title options. Rules:\n"
                        "- MAX 60 characters each — strip any over 60 chars\n"
                        "- Short, punchy, emotionally direct: a provocative question or bold statement\n"
                        "- No filler phrases, no colons that only pad length\n"
                        "Return ONLY a JSON array of 8 strings, no markdown.\n\n"
                        f"{context}"
                    )
                }]
            )
            raw = title_msg.content[0].text.strip()
            s = raw.find('['); e = raw.rfind(']')
            if s != -1 and e != -1:
                parsed = json.loads(raw[s:e+1])
                titles = [str(t) for t in parsed if len(str(t)) <= 60][:8]
                if len(titles) < 5:
                    titles = [str(t)[:60] for t in parsed[:8]]
        except Exception as te:
            print(f'[thumbnail] Title generation failed: {te}')
            titles = ["Add Your Title Here"] * 8

        result = {"titles": titles, "clip_transcript": full_transcript}
        if frame_b64s:
            result["frames"] = frame_b64s
        return jsonify(result)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Clips ──
@app.route('/clips', methods=['POST'])
def find_clips():
    try:
        data = request.json
        transcript = data.get('transcript', '')

        if not transcript or len(transcript) < 10:
            return jsonify({'error': 'Transcript too short', 'candidates': []})

        # transcript is already a pre-formatted timestamped string from the frontend:
        # "[0.0] word [0.4] another [0.9] word ..."
        transcript_text = transcript if isinstance(transcript, str) else ' '.join(str(w) for w in transcript)

        # Log what we received
        print(f"Transcript length: {len(transcript_text)} chars")
        print(f"Transcript preview: {transcript_text[:200]}")

        api_key_path = os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'api_key.txt')
        with open(api_key_path, 'r') as f:
            api_key = f.read().strip()

        client = anthropic.Anthropic(api_key=api_key)

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{
                "role": "user",
                "content": f"""You are an expert short-form video editor for a faith-based educational media organization called The Candler Foundry at Emory University. Your job is to find the best clip moments from theological talks and personal testimonials for Instagram Reels, YouTube Shorts, and TikTok.

The transcript below uses the format: [timestamp_in_seconds] word [timestamp] word ...
Use these timestamps directly as start_time and end_time values — do not calculate or estimate them.

WHAT PERFORMS WELL ON THESE PLATFORMS for this content type:
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
- start_time: float (copy the timestamp number directly from the transcript — e.g. if the segment starts at [143.2], use 143.2)
- end_time: float (timestamp of the last word in the segment)
- hook_score: integer 1-10 (10 = most likely to stop a scroll)
- hook_line: string (the single most compelling sentence in this segment, quoted verbatim from the transcript)
- why_it_works: string (1 sentence — name the specific technique: vulnerability, surprise, strong hook, emotional peak, etc.)

Find 8 clips. Rank by hook_score descending.

Transcript:
{transcript_text}"""
            }]
        )

        raw = message.content[0].text.strip()
        print(f"Claude raw response (first 300 chars): {raw[:300]}")

        # Strip any markdown code fences defensively
        raw = re.sub(r'^```json\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'^```\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()

        # Find the JSON array even if there's extra text around it
        start = raw.find('[')
        end = raw.rfind(']')
        if start != -1 and end != -1:
            raw = raw[start:end+1]

        candidates = json.loads(raw)
        print(f"Parsed {len(candidates)} candidates")
        return jsonify({'candidates': candidates})

    except Exception as e:
        print(f"ERROR in /clips: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'candidates': []})


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
        return jsonify({'error': 'ffmpeg not found'}), 500

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
    print("Foundry Video Editor backend starting on http://localhost:5000")
    api_key = read_api_key()
    if api_key:
        print("API key loaded.")
    else:
        print(f"WARNING: API key not found at {API_KEY_PATH}")
    app.run(host="localhost", port=5000, debug=False)
