"""
Foundry Video Editor — Local Flask Backend
Runs on localhost:5000
"""

import io
import os
import json
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

CREATE_NO_WINDOW = 0x08000000

# ── Dropbox portable paths ──
dropbox_root   = os.path.join(os.path.expanduser('~'), 'Dropbox')
API_KEY_PATH   = os.path.join(dropbox_root, '3MB', 'api_key.txt')
FFMPEG         = os.path.join(dropbox_root, 'FFMPEG', 'ffmpeg.exe')
FFMPEG_CAPTION = FFMPEG

# ── Whisper model (loaded once on first use) ──
_WHISPER_MODEL = None

def get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        _WHISPER_MODEL = whisper.load_model("medium")
    return _WHISPER_MODEL


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
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    try:
        font_size = int(request.form.get("font_size", 18))
    except (ValueError, TypeError):
        font_size = 18

    original_name = file.filename or "video.mp4"
    base_name     = os.path.splitext(os.path.basename(original_name))[0]
    output_name   = base_name + " (Captioned).mp4"

    tmp_dir = tempfile.mkdtemp()
    try:
        input_path  = os.path.join(tmp_dir, "source.mp4")
        srt_path    = os.path.join(tmp_dir, "captions.srt")
        output_path = os.path.join(tmp_dir, output_name)

        file.save(input_path)

        # Transcribe with Whisper medium
        model  = get_whisper_model()
        result = model.transcribe(input_path)
        write_srt(result["segments"], srt_path)

        # Burn subtitles with ffmpeg
        # Use cwd=tmp_dir and relative filename to avoid Windows path escaping issues
        style = (
            f"Fontname=Arial,Fontsize={font_size},"
            "Outline=1,Shadow=0,BorderStyle=1,Spacing=1"
        )
        cmd = [
            FFMPEG_CAPTION, "-y",
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


@app.route("/find_json", methods=["POST"])
def find_json():
    """
    Accepts: { "file_path": "..." } OR { "filename": "Video.mp4" }
    If file_path given and exists: searches same directory.
    If only filename given: walks Dropbox looking for a matching Words JSON.
    Returns: { "found": true, "json_path": "..." } or { "found": false }
    """
    data = request.get_json(force=True)
    file_path = data.get("file_path", "").strip()
    filename  = data.get("filename",  "").strip()

    # Determine search root and stem
    if file_path and os.path.isfile(file_path):
        search_dirs = [os.path.dirname(file_path)]
        target_bare = bare_stem(os.path.basename(file_path)).lower()
    elif filename:
        search_dirs = [dropbox_root]
        target_bare = bare_stem(filename).lower()
    else:
        return jsonify({"found": False})

    def search_dir_tree(roots):
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for entry in files:
                    if not entry.lower().endswith('.json'):
                        continue
                    entry_lower = entry.lower()
                    if target_bare in entry_lower and 'words' in entry_lower:
                        return os.path.join(dirpath, entry)
        return None

    result = search_dir_tree(search_dirs)
    if result:
        return jsonify({"found": True, "json_path": result})
    return jsonify({"found": False})


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
        clean_input = os.path.join(tmp_dir, 'source.mp4')
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
        input_path = os.path.join(tmp_dir, "source.mp4")
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
@app.route("/thumbnails", methods=["POST"])
def thumbnails():
    """
    Accepts: { "file_path": "...", "style": "warm_bar"|"bold_corner"|"kinetic_slash" }
    Returns: { "frames": [...], "title_suggestion": "..." }
    Session 3: wire frame extraction, Whisper tiny, Claude title generation here.
    """
    data = request.get_json(force=True)
    file_path = data.get("file_path", "")

    return jsonify({
        "status": "stub",
        "message": "Thumbnail generation not yet implemented — wiring in Session 3",
        "input": file_path,
        "frames": [],
        "title_suggestion": "",
    })


# ── Clips ──
@app.route("/clips", methods=["POST"])
def clips():
    """
    Accepts: { "transcript": "...", "starttime": float (optional), "endtime": float (optional) }
    Returns: { "candidates": [{ "starttime", "endtime", "hookscore", "hookline", "reason" }, ...] }
    """
    data = request.get_json(force=True)
    transcript = data.get("transcript", "").strip()

    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    api_key = read_api_key()
    if not api_key:
        return jsonify({"error": f"API key not found at {API_KEY_PATH}"}), 500

    prompt = (
        "You are an expert social media video editor for a seminary and theological education "
        "organization. Analyse this word-timed transcript and identify the 8-10 best segments "
        "that would make excellent standalone short-form clips (30-90 seconds each) for "
        "Instagram, TikTok, or YouTube Shorts.\n\n"
        "Look for:\n"
        "- Quotable, self-contained statements with a strong hook\n"
        "- High-energy or emotionally resonant moments\n"
        "- Segments with a clear narrative arc (setup \u2192 payoff)\n"
        "- Theological insight or wisdom that is accessible to a broad audience\n"
        "- The single best opening line that grabs attention in the first 3 seconds\n\n"
        "Each clip must be 30-90 seconds long. Prefer the higher end of that range.\n\n"
        "TRANSCRIPT (format: [start_seconds] word):\n"
        + transcript
        + "\n\n"
        "Return ONLY a valid JSON array of 8-10 objects sorted by hook_score descending. "
        "Each object must have exactly:\n"
        '- "starttime": number (seconds, from transcript timestamps)\n'
        '- "endtime": number (seconds, from transcript timestamps)\n'
        '- "hook_score": integer 1-10 (10 = highest viral potential)\n'
        '- "hookline": string (the single strongest opening sentence from that segment)\n'
        '- "why_it_works": string (1-2 sentences on why this makes a great clip)\n\n'
        "Return ONLY the JSON array — no markdown fences, no explanation."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = message.content[0].text.strip()

        # Extract the JSON array even if Claude adds surrounding text
        match = re.search(r'\[[\s\S]*\]', response_text)
        candidates = json.loads(match.group() if match else response_text)

        # Normalise field names: support both old (reason/hookscore) and new schema
        normalised = []
        for c in candidates:
            normalised.append({
                "starttime":    c.get("starttime", 0),
                "endtime":      c.get("endtime",   0),
                "hook_score":   c.get("hook_score", c.get("hookscore", 0)),
                "hookline":     c.get("hookline", ""),
                "why_it_works": c.get("why_it_works", c.get("reason", "")),
            })
        normalised.sort(key=lambda x: x["hook_score"], reverse=True)

        return jsonify({"candidates": normalised})

    except json.JSONDecodeError as e:
        return jsonify({
            "error": f"Could not parse Claude response as JSON: {e}",
            "raw": response_text
        }), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Export Clip ──
@app.route("/export_clip", methods=["POST"])
def export_clip():
    """
    Accepts multipart/form-data:
      file          — the source MP4
      starttime     — float, in-point seconds
      endtime       — float, out-point seconds
      suggested_name — desired output filename (e.g. "Talk - Clip (12.5-45.0).mp4")
    Returns the clipped MP4 as a download.
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    try:
        starttime      = float(request.form.get("starttime", 0))
        endtime        = float(request.form.get("endtime",   0))
        suggested_name = request.form.get("suggested_name", "clip.mp4")
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid parameters: {e}"}), 400

    if endtime <= starttime:
        return jsonify({"error": "endtime must be greater than starttime"}), 400

    # Write source to a clean temp path (avoids special characters in filename)
    tmp_dir = tempfile.mkdtemp()
    try:
        input_path  = os.path.join(tmp_dir, "source.mp4")
        output_name = suggested_name if suggested_name.lower().endswith(".mp4") else suggested_name + ".mp4"
        output_path = os.path.join(tmp_dir, output_name)

        file.save(input_path)

        cmd = [
            FFMPEG, "-y",
            "-ss", str(starttime),
            "-to", str(endtime),
            "-i", input_path,
            "-c", "copy",
            output_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace")[-600:]
            return jsonify({"error": "ffmpeg failed: " + err}), 500

        # Read into memory so we can clean up temp files immediately
        with open(output_path, "rb") as f:
            clip_bytes = f.read()

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return send_file(
        io.BytesIO(clip_bytes),
        as_attachment=True,
        download_name=output_name,
        mimetype="video/mp4",
    )


if __name__ == "__main__":
    print("Foundry Video Editor backend starting on http://localhost:5000")
    api_key = read_api_key()
    if api_key:
        print("API key loaded.")
    else:
        print(f"WARNING: API key not found at {API_KEY_PATH}")
    app.run(host="localhost", port=5000, debug=False)
