"""The sermon as in-memory PCM, plus every number derived from it.

Pure module - no Reflex imports, so it can be tested on its own.
Nothing about the audio is hardcoded: byte rate, chunk size and
duration all follow from the WAV file's own header.
"""

import wave
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
AUDIO_PATH = APP_DIR / "assets" / "sermon-16k.wav"

CHUNK_MS = 100

# the sermon is loaded into memory once at import time
with wave.open(str(AUDIO_PATH), "rb") as _w:
    SERMON_PCM = _w.readframes(_w.getnframes())
    BYTES_PER_SEC = _w.getframerate() * _w.getsampwidth() * _w.getnchannels()

DURATION_SECS = len(SERMON_PCM) / BYTES_PER_SEC
CHUNK_BYTES = int(BYTES_PER_SEC * CHUNK_MS / 1000)


def chunk_at(position: float) -> bytes:
    """The 100 ms chunk of audio starting at `position` (seconds)."""
    cursor = int(position * BYTES_PER_SEC)
    return SERMON_PCM[cursor : cursor + CHUNK_BYTES]


def fmt_mmss(secs: float) -> str:
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"
