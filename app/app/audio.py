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

# switchable sources: the full sermon, plus its real sections (name ->
# (start secs, duration secs) in the full file; None = whole file).
# Boundaries read off the solaris-3 transcript: exposition starts ~148s
# ("sit up and listen"), children's map demo ~339s ("three pre-prepared
# helpers"), application ~1500s, creed ~1781s ("I believe in God").
SOURCE_WINDOWS: dict[str, tuple[float, float] | None] = {
    "Full sermon": None,
    "Reading (scripture)": (0, 148),
    "Preaching (exposition)": (148, 191),
    "Children's talk": (339, 1161),
    "Application": (1500, 281),
    "Creed": (1781, 19),
}
DEFAULT_SOURCE = "Full sermon"

with wave.open(str(AUDIO_PATH), "rb") as _w:
    _FULL_PCM = _w.readframes(_w.getnframes())
    BYTES_PER_SEC = _w.getframerate() * _w.getsampwidth() * _w.getnchannels()

CHUNK_BYTES = int(BYTES_PER_SEC * CHUNK_MS / 1000)

# current source (mutated by load_source)
SERMON_PCM = _FULL_PCM
DURATION_SECS = len(SERMON_PCM) / BYTES_PER_SEC


def load_source(name: str) -> None:
    """Switch the in-memory audio to a named source (sliced window or
    the full sermon). Times in the player are relative to the source."""
    global SERMON_PCM, DURATION_SECS
    window = SOURCE_WINDOWS[name]
    if window is None:
        SERMON_PCM = _FULL_PCM
    else:
        start, dur = window
        a = int(start * BYTES_PER_SEC)
        b = int((start + dur) * BYTES_PER_SEC)
        SERMON_PCM = _FULL_PCM[a:b]
    DURATION_SECS = len(SERMON_PCM) / BYTES_PER_SEC


def chunk_at(position: float) -> bytes:
    """The 100 ms chunk of audio starting at `position` (seconds)."""
    cursor = int(position * BYTES_PER_SEC)
    return SERMON_PCM[cursor : cursor + CHUNK_BYTES]


def fmt_mmss(secs: float) -> str:
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"
