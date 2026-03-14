"""
Foundry Video Editor — Local Flask Backend
Runs on localhost:5000
"""

import os
import json
import subprocess
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
    Accepts: { "file_path": "..." }
    Returns: { "candidates": [{ "start": 0.0, "end": 30.0, "reason": "..." }, ...] }
    Session 4: wire Whisper medium + Claude clip detection here.
    """
    data = request.get_json(force=True)
    file_path = data.get("file_path", "")

    return jsonify({
        "status": "stub",
        "message": "Clip detection not yet implemented — wiring in Session 4",
        "input": file_path,
        "candidates": [],
    })


if __name__ == "__main__":
    print("Foundry Video Editor backend starting on http://localhost:5000")
    api_key = read_api_key()
    if api_key:
        print("API key loaded.")
    else:
        print(f"WARNING: API key not found at {API_KEY_PATH}")
    app.run(host="localhost", port=5000, debug=False)
