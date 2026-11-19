"""Audio prep for the workbench.

Converts the 48 kHz stereo sermon WAV to a cached 16 kHz mono s16le raw PCM
file (the exact bytes streamed to both providers), and writes playable WAVs
(full file + pre-cut 1-minute segments) plus a manifest for the UI.
"""

import json
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parent.parent
SRC_WAV = ROOT / "sermon_full.WAV"
CACHE = Path(__file__).resolve().parent / "cache"
OUT = Path(__file__).resolve().parent / "audio_out"
RAW16K = CACHE / "sermon_16k_mono.s16"
MANIFEST = OUT / "manifest.json"

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # s16le mono
SEGMENT_SECONDS = 60
SEGMENT_STRIDE = 150  # one 60s segment every 150s across the file


def fmt(s: float) -> str:
    s = int(s)
    return f"{s // 60:02d}:{s % 60:02d}"


def source_info() -> dict:
    with wave.open(str(SRC_WAV)) as w:
        return {
            "channels": w.getnchannels(),
            "rate": w.getframerate(),
            "frames": w.getnframes(),
            "duration": w.getnframes() / w.getframerate(),
        }


def ensure_16k_mono() -> float:
    """Create the cached 16 kHz mono PCM file if missing. Returns duration in s."""
    if RAW16K.exists() and RAW16K.stat().st_size > 0:
        return RAW16K.stat().st_size / BYTES_PER_SEC
    CACHE.mkdir(exist_ok=True)
    info = source_info()
    rate, ch = info["rate"], info["channels"]
    print(f"converting {SRC_WAV.name} ({rate} Hz, {ch} ch, {fmt(info['duration'])}) to 16 kHz mono...")
    with wave.open(str(SRC_WAV)) as w:
        raw = w.readframes(info["frames"])
    data = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
    mono = data.mean(axis=1).astype(np.float32)
    del data, raw
    if rate != SAMPLE_RATE:
        g = np.gcd(rate, SAMPLE_RATE)
        mono = resample_poly(mono, SAMPLE_RATE // g, rate // g).astype(np.float32)
    pcm = np.clip(mono, -32768, 32767).astype(np.int16)
    del mono
    tmp = RAW16K.with_suffix(".tmp")
    pcm.tofile(tmp)
    tmp.replace(RAW16K)
    print(f"cached {RAW16K} ({pcm.size / SAMPLE_RATE:.1f}s)")
    return pcm.size / SAMPLE_RATE


def read_chunk(offset_s: float, nbytes: int) -> bytes:
    """Read nbytes of 16 kHz mono PCM at the given offset (seconds)."""
    with RAW16K.open("rb") as f:
        f.seek(int(offset_s * BYTES_PER_SEC))
        return f.read(nbytes)


def total_seconds() -> float:
    return RAW16K.stat().st_size / BYTES_PER_SEC


def ensure_segments() -> dict:
    """Write full.wav + seg_XX.wav playback files and manifest.json."""
    OUT.mkdir(exist_ok=True)
    total = total_seconds()
    raw = RAW16K.read_bytes()

    full = OUT / "full.wav"
    if not full.exists():
        with wave.open(str(full), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(raw)

    segments = []
    n = int(total // SEGMENT_STRIDE)
    for i in range(n):
        start = i * SEGMENT_STRIDE
        dur = min(SEGMENT_SECONDS, total - start)
        if dur < 10:
            break
        name = f"seg_{i:02d}.wav"
        p = OUT / name
        if not p.exists():
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(raw[int(start * BYTES_PER_SEC): int((start + dur) * BYTES_PER_SEC)])
        segments.append({
            "id": f"seg_{i:02d}",
            "label": f"Segment {i + 1} ({fmt(start)}-{fmt(start + dur)})",
            "start": start,
            "duration": dur,
            "url": f"/audio/{name}",
        })

    manifest = {
        "full": {
            "id": "full",
            "label": f"Full file ({fmt(total)})",
            "duration": total,
            "url": "/audio/full.wav",
        },
        "segments": segments,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    return manifest
