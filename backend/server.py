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
API_KEY_PATH   = os.path.join(dropbox_root, 'Scripts', 'api_key.txt')
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


@app.route('/find_json', methods=['POST'])
def find_json():
    import glob as glob_module
    filename = request.json.get('filename', '')
    dropbox_root_local = os.path.join(os.path.expanduser('~'), 'Dropbox')

    # Find the video file anywhere in Dropbox by exact filename
    video_matches = glob_module.glob(
        os.path.join(dropbox_root_local, '**', filename), recursive=True
    )
    if not video_matches:
        return jsonify({'found': False, 'reason': 'Could not locate video in Dropbox'})

    video_dir = os.path.dirname(video_matches[0])

    # Look for Words JSON in the same folder — no naming convention required
    json_matches = glob_module.glob(os.path.join(video_dir, '*Words*.json'))

    # Fallback: case-insensitive 'words' anywhere in filename
    if not json_matches:
        json_matches = [
            f for f in glob_module.glob(os.path.join(video_dir, '*.json'))
            if 'words' in os.path.basename(f).lower()
        ]

    # Last resort: any JSON in the folder
    if not json_matches:
        json_matches = glob_module.glob(os.path.join(video_dir, '*.json'))

    if not json_matches:
        return jsonify({'found': False, 'reason': 'No transcript JSON found in video folder'})

    # If multiple, pick most similar to video filename
    if len(json_matches) > 1:
        video_stem = os.path.splitext(filename)[0].lower()
        def similarity(path):
            return sum(1 for c in video_stem if c in os.path.basename(path).lower())
        json_matches.sort(key=similarity, reverse=True)

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
@app.route('/clips', methods=['POST'])
def find_clips():
    try:
        data = request.json
        transcript = data.get('transcript', [])

        if not transcript or len(transcript) < 10:
            return jsonify({'error': 'Transcript too short', 'candidates': []})

        # Build plain text from words array
        if isinstance(transcript[0], dict):
            transcript_text = ' '.join(w.get('word', '') for w in transcript)
        else:
            transcript_text = ' '.join(str(w) for w in transcript)

        # Log what we received
        print(f"Transcript word count: {len(transcript)}")
        print(f"Transcript preview: {transcript_text[:200]}")

        api_key_path = os.path.join(os.path.expanduser('~'), 'Dropbox', 'Scripts', 'api_key.txt')
        with open(api_key_path, 'r') as f:
            api_key = f.read().strip()

        client = anthropic.Anthropic(api_key=api_key)

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": f"""You are a social media clip expert. Analyze this transcript and identify 8-10 high-impact moments for viral short-form video clips (30-90 seconds each).

Return ONLY a valid JSON array. No markdown. No code fences. No explanation. Start your response with [ and end with ].

Each item must have exactly these fields:
- start_time: float (seconds from start of video)
- end_time: float (seconds from start of video)
- hook_score: integer 1-10
- hook_line: string (most compelling sentence from this segment)
- why_it_works: string (1-2 sentences)

Rank by hook_score descending.

Transcript: {transcript_text}"""
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
