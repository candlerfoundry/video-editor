"""
Foundry Video Editor — Local Flask Backend
Runs on localhost:5000
"""

import os
import json
import re
import subprocess
import anthropic
from flask import Flask, jsonify, request
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


if __name__ == "__main__":
    print("Foundry Video Editor backend starting on http://localhost:5000")
    api_key = read_api_key()
    if api_key:
        print("API key loaded.")
    else:
        print(f"WARNING: API key not found at {API_KEY_PATH}")
    app.run(host="localhost", port=5000, debug=False)
