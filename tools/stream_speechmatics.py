"""Stream a 16 kHz mono WAV to Speechmatics realtime (Enhanced model).

Hand-rolled v2 WebSocket client: StartRecognition -> raw PCM frames at
true real-time pace -> AudioEnd. Writes the same artifacts as stream.py
(words.jsonl, transcript, summary) so compare.py works unchanged.
"""

import argparse
import asyncio
import json
import os
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

import websockets

CHUNK_MS = 100
EU_URL = "wss://eu.rt.speechmatics.com/v2"
BYTES_PER_SEC = 32000  # 16 kHz * 2 bytes, mono


@dataclass(frozen=True)
class Word:
    t: float
    text: str


@dataclass(frozen=True)
class RunSummary:
    name: str
    provider: str
    operating_point: str
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


def start_message() -> dict[str, object]:
    return {
        "message": "StartRecognition",
        "audio_format": {
            "type": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
        },
        "transcription_config": {
            "language": "en",
            "operating_point": "enhanced",
            "enable_partials": False,
        },
    }


async def run_speechmatics(
    wav: Path, name: str, out_dir: Path, operating_point: str = "enhanced"
) -> RunSummary:
    key = os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        raise SystemExit("SPEECHMATICS_API_KEY not set (run via --env-file .env)")
    pcm, audio_secs = load_pcm(wav)
    chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)

    words: list[Word] = []
    transcript_parts: list[str] = []
    first_at: float | None = None
    start = perf_counter()

    async with websockets.connect(
        EU_URL, additional_headers={"Authorization": f"Bearer {key}"}, max_size=None
    ) as ws:
        await ws.send(json.dumps(start_message()))

        # wait for the server to accept the session (Info frames come first)
        while True:
            reply: dict[str, object] = json.loads(await ws.recv())
            msg = reply.get("message")
            if msg == "RecognitionStarted":
                break
            if msg == "Error":
                raise SystemExit(f"speechmatics rejected the session: {reply}")

        sent_chunks = 0

        async def sender() -> None:
            nonlocal sent_chunks
            for i in range(0, len(pcm), chunk_bytes):
                await ws.send(pcm[i : i + chunk_bytes])
                sent_chunks += 1
                await asyncio.sleep(CHUNK_MS / 1000.0)
            await ws.send(
                json.dumps({"message": "EndOfStream", "last_seq_no": sent_chunks - 1})
            )

        async def receiver() -> None:
            nonlocal first_at
            while True:
                msg = json.loads(await ws.recv())
                kind = msg.get("message")
                if kind == "AddTranscript":
                    if first_at is None:
                        first_at = perf_counter() - start
                    for result in msg.get("results", []):
                        alts = result.get("alternatives", [])
                        if not alts:
                            continue
                        content = str(alts[0].get("content", ""))
                        t = float(
                            result.get(
                                "start_time", alts[0].get("start_time", 0.0)
                            )
                        )
                        words.append(Word(t, content))
                        transcript_parts.append(content)
                elif kind == "Error":
                    raise SystemExit(f"speechmatics error: {msg}")
                elif kind == "EndOfTranscript":
                    return

        await asyncio.gather(sender(), receiver())

    wall_secs = perf_counter() - start
    out_dir.mkdir(parents=True, exist_ok=True)
    words_path = out_dir / f"{name}.words.jsonl"
    with words_path.open("w") as f:
        for w in words:
            f.write(json.dumps({"t": round(w.t, 3), "w": w.text}) + "\n")
    (out_dir / f"{name}.txt").write_text(" ".join(transcript_parts))

    summary = RunSummary(
        name=name,
        provider="speechmatics",
        operating_point=operating_point,
        audio_secs=audio_secs,
        wall_secs=wall_secs,
        first_word_secs=first_at,
        word_count=len(words),
    )
    (out_dir / f"{name}.summary.json").write_text(
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
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args()
    asyncio.run(run_speechmatics(args.wav, args.name, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
