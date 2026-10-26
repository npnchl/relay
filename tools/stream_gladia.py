"""Stream a 16 kHz mono WAV to Gladia live (solaria-1).

POST /v2/live -> WebSocket -> base64 PCM chunks at true real-time pace ->
stop_recording. Writes the same artifacts as stream.py so compare.py
works unchanged.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

import websockets

CHUNK_MS = 100
BYTES_PER_SEC = 32000  # 16 kHz * 2 bytes, mono
LIVE_URL = "https://api.gladia.io/v2/live"
DEFAULT_MODEL = "solaria-1"


@dataclass(frozen=True)
class Word:
    t: float
    text: str


@dataclass(frozen=True)
class RunSummary:
    name: str
    provider: str
    model: str
    audio_secs: float
    wall_secs: float
    first_word_secs: float | None
    word_count: int

    @property
    def speed_x(self) -> float:
        return self.audio_secs / self.wall_secs if self.wall_secs > 0 else 0.0


def load_pcm(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit WAV (run prep.py)")
        raw = w.readframes(w.getnframes())
    return raw, len(raw) / BYTES_PER_SEC


def create_session(key: str, model: str) -> str:
    config: dict[str, object] = {
        "encoding": "wav/pcm",
        "bit_depth": 16,
        "sample_rate": 16000,
        "channels": 1,
        "model": model,
        "language_config": {"languages": ["en"], "code_switching": False},
        "messages_config": {
            "receive_partial_transcripts": False,
            "receive_final_transcripts": True,
            "receive_errors": True,
        },
    }
    req = urllib.request.Request(
        LIVE_URL,
        data=json.dumps(config).encode(),
        headers={"Content-Type": "application/json", "x-gladia-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body: dict[str, object] = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise SystemExit(f"gladia live init failed: {err.code} {err.read()!r}") from err
    url = body.get("url")
    if not isinstance(url, str) or not url:
        raise SystemExit(f"gladia live init gave no websocket url: {body}")
    return url


async def run_gladia(
    wav: Path, name: str, out_dir: Path, model: str = DEFAULT_MODEL
) -> RunSummary:
    key = os.environ.get("GLADIA_API_KEY")
    if not key:
        raise SystemExit("GLADIA_API_KEY not set (run via --env-file .env)")
    pcm, audio_secs = load_pcm(wav)
    chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)
    url = create_session(key, model)

    words: list[Word] = []
    transcript_parts: list[str] = []
    first_at: float | None = None
    start = perf_counter()

    async with websockets.connect(url, max_size=None) as ws:
        async def sender() -> None:
            for i in range(0, len(pcm), chunk_bytes):
                chunk = base64.b64encode(pcm[i : i + chunk_bytes]).decode()
                await ws.send(
                    json.dumps({"type": "audio_chunk", "data": {"chunk": chunk}})
                )
                await asyncio.sleep(CHUNK_MS / 1000.0)
            await ws.send(json.dumps({"type": "stop_recording"}))

        async def receiver() -> None:
            nonlocal first_at
            while True:
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed:
                    return  # server closed the session; that's the end
                if isinstance(raw, bytes):
                    continue
                msg: dict[str, object] = json.loads(raw)
                kind = msg.get("type")
                if kind == "transcript":
                    data = msg.get("data")
                    if not isinstance(data, dict) or not data.get("is_final"):
                        continue
                    if first_at is None:
                        first_at = perf_counter() - start
                    utterance = data.get("utterance")
                    if not isinstance(utterance, dict):
                        continue
                    for w in utterance.get("words", []):
                        words.append(
                            Word(float(w.get("start", 0.0)), str(w.get("word", "")))
                        )
                    transcript_parts.append(str(utterance.get("text", "")))
                elif kind == "error":
                    raise SystemExit(f"gladia error: {msg}")

        await asyncio.gather(sender(), receiver())

    wall_secs = perf_counter() - start
    out_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("words", "transcripts", "summaries"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    words_path = out_dir / "words" / f"{name}.words.jsonl"
    with words_path.open("w") as f:
        for w in words:
            f.write(json.dumps({"t": round(w.t, 3), "w": w.text}) + "\n")
    (out_dir / "transcripts" / f"{name}.txt").write_text(" ".join(transcript_parts))

    summary = RunSummary(
        name=name,
        provider="gladia",
        model=model,
        audio_secs=audio_secs,
        wall_secs=wall_secs,
        first_word_secs=first_at,
        word_count=len(words),
    )
    (out_dir / "summaries" / f"{name}.summary.json").write_text(
        json.dumps(asdict(summary) | {"speed_x": summary.speed_x}, indent=2) + "\n"
    )
    print(
        f"[{name}] audio {audio_secs:.1f}s | wall {wall_secs:.1f}s "
        f"| first final {'+' + f'{first_at:.2f}s' if first_at is not None else 'none'} "
        f"| words {len(words)}"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="16 kHz mono WAV")
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args()
    asyncio.run(run_gladia(args.wav, args.name, args.out, args.model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
