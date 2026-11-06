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

# switchable sources: real WAV files - the full sermon plus one file
# per sermon section, pre-cut into assets/sections/ (boundaries read
# off the solaris-3 transcript). name -> wav path.
SOURCE_FILES: dict[str, Path] = {
    "Full sermon": AUDIO_PATH,
    "Reading (scripture)": APP_DIR / "assets" / "sections" / "reading.wav",
    "Exposition (part 1)": APP_DIR / "assets" / "sections" / "exposition.wav",
    "Children's talk": APP_DIR / "assets" / "sections" / "children.wav",
    "Exposition (part 2)": APP_DIR / "assets" / "sections" / "exposition-2.wav",
    "Application": APP_DIR / "assets" / "sections" / "application.wav",
    "Creed": APP_DIR / "assets" / "sections" / "creed.wav",
}
DEFAULT_SOURCE = "Full sermon"


def load_source(name: str) -> None:
    """Load a named source WAV into memory (module globals below)."""
    global SERMON_PCM, BYTES_PER_SEC, DURATION_SECS
    with wave.open(str(SOURCE_FILES[name]), "rb") as w:
        SERMON_PCM = w.readframes(w.getnframes())
        BYTES_PER_SEC = w.getframerate() * w.getsampwidth() * w.getnchannels()
    DURATION_SECS = len(SERMON_PCM) / BYTES_PER_SEC


CHUNK_MS = 100
load_source(DEFAULT_SOURCE)
CHUNK_BYTES = int(BYTES_PER_SEC * CHUNK_MS / 1000)


def chunk_at(position: float) -> bytes:
    """The 100 ms chunk of audio starting at `position` (seconds)."""
    cursor = int(position * BYTES_PER_SEC)
    return SERMON_PCM[cursor : cursor + CHUNK_BYTES]


def fmt_mmss(secs: float) -> str:
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"
