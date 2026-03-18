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
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CREATE_NO_WINDOW = 0x08000000

# ── API key ──
API_KEY_PATH = r"C:\Users\esavant\Dropbox\3MB\api_key.txt"

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
    Accepts: { "file_path": "C:/path/to/video.mp4", "options": {} }
    Returns: { "status": "ok", "output_path": "...(Captioned).mp4" }
    Session 2: wire ffmpeg burn here.
    """
    data = request.get_json(force=True)
    file_path = data.get("file_path", "")
    base = os.path.splitext(file_path)[0]
    output_path = base + " (Captioned).mp4"

    return jsonify({
        "status": "stub",
        "message": "Caption burn not yet implemented — wiring in Session 2",
        "input": file_path,
        "output_path": output_path,
    })


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
    starttime  = data.get("starttime")
    endtime    = data.get("endtime")

    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    api_key = read_api_key()
    if not api_key:
        return jsonify({"error": f"API key not found at {API_KEY_PATH}"}), 500

    # Optionally focus Claude on a user-selected time range
    range_note = ""
    if starttime is not None and endtime is not None:
        range_note = (
            f"\n\nThe user has highlighted a range from {starttime:.1f}s to {endtime:.1f}s. "
            "Prioritise candidates that fall within or near this window, but still return "
            "the 3-5 best clips overall."
        )

    prompt = (
        "You are an expert social media video editor. Analyse this transcript and identify "
        "3-5 segments that would make excellent standalone short-form clips for Instagram, "
        "TikTok, or YouTube Shorts.\n\n"
        "Look for:\n"
        "- Quotable, self-contained statements\n"
        "- High-energy or emotionally resonant moments\n"
        "- Segments with a clear narrative arc (setup \u2192 payoff)\n"
        "- Hooks that grab attention in the first 3 seconds\n\n"
        "TRANSCRIPT:\n"
        + transcript
        + range_note
        + "\n\n"
        "Return ONLY a valid JSON array with 3-5 objects. Each object must have exactly:\n"
        '- "starttime": number (seconds)\n'
        '- "endtime": number (seconds)\n'
        '- "hookscore": integer 1-10 (10 = highest viral potential)\n'
        '- "hookline": string (the single strongest sentence from that segment)\n'
        '- "reason": string (1-2 sentences on why this makes a great clip)\n\n'
        "Return ONLY the JSON array — no markdown fences, no explanation."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = message.content[0].text.strip()

        # Extract the JSON array even if Claude adds surrounding text
        match = re.search(r'\[[\s\S]*\]', response_text)
        candidates = json.loads(match.group() if match else response_text)

        return jsonify({"candidates": candidates})

    except json.JSONDecodeError as e:
        return jsonify({
            "error": f"Could not parse Claude response as JSON: {e}",
            "raw": response_text
        }), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Export Clip ──
FFMPEG = r"C:\Users\esavant\Dropbox\FFMPEG\ffmpeg.exe"

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
