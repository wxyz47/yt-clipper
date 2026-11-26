#!/usr/bin/env python3
"""Transcription worker — run with the transcription venv's Python
(faster-whisper). Usage: transcribe_worker.py <media_file> <out_json>

Writes {"language":..., "segments":[{"start":..,"end":..,"text":..,
"words":[{"start":..,"end":..,"word":..}]}]} to <out_json>.

While transcribing, prints "PROGRESS <pct>" lines to stdout (flushed)
so the bot can show live progress: pct = last segment end / audio
duration. Set TRANSCRIBE_LANGUAGE (e.g. "id", "en") to force a language;
unset = auto-detect.

Audio is decoded with ffmpeg into a numpy array first: faster-whisper's
own decode_audio uses a PyAV keyword that newer PyAV removed, so letting
it open media files directly crashes.
"""
import json
import os
import subprocess
import sys

import numpy as np
from faster_whisper import WhisperModel

SR = 16000


def load_audio_ffmpeg(path, sr=SR):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le",
           "-ac", "1", "-ar", str(sr), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def main():
    src, out = sys.argv[1], sys.argv[2]
    language = os.environ.get("TRANSCRIBE_LANGUAGE") or None
    audio = load_audio_ffmpeg(src)
    duration = len(audio) / SR if len(audio) else 0.0
    print("PROGRESS 0", flush=True)
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        audio, language=language, word_timestamps=True, beam_size=1,
        vad_filter=True)
    data = {"language": info.language, "segments": []}
    last_pct = 0
    for s in segments:
        seg = {"start": s.start, "end": s.end, "text": s.text.strip(),
               "words": [{"start": w.start, "end": w.end, "word": w.word}
                         for w in (s.words or [])]}
        data["segments"].append(seg)
        if duration > 0:
            pct = int(min(100.0, s.end / duration * 100.0))
            if pct > last_pct:
                last_pct = pct
                print(f"PROGRESS {pct}", flush=True)
    print("PROGRESS 100", flush=True)
    with open(out, "w") as f:
        json.dump(data, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
