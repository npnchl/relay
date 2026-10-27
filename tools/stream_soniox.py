"""Stream a 16 kHz mono WAV to Soniox real-time STT (stt-rt-v5).

Mirrors stream_speechmatics.py: one WebSocket, raw PCM frames at true
real-time pace, then an empty frame to signal end-of-audio. Soniox returns
a token stream (subword pieces); final tokens are assembled into words so
the artifacts (words.jsonl, transcript, summary) match the other tools
and compare.py works unchanged.
"""

import argparse
import asyncio
import json
import os
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from datetime import datetime, timezone
from time import perf_counter

import websockets

CHUNK_MS = 100
URL = "wss://stt-rt.soniox.com/transcribe-websocket"
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
    client_reference_id: str

    @property
    def speed_x(self) -> float:
        return self.audio_secs / self.wall_secs if self.wall_secs > 0 else 0.0


def load_pcm(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit WAV (run prep.py)")
        raw = w.readframes(w.getnframes())
    return raw, len(raw) / BYTES_PER_SEC


def start_message(ref_id: str) -> dict[str, object]:
    return {
        "model": "stt-rt-v5",
        "language_hints": ["en"],
        "audio_format": "pcm_s16le",
        "sample_rate": 16000,
        "num_channels": 1,
        # tags this session's entry in Soniox usage logs so per-run cost
        # can be read back from GET /v1/usage-logs (see cost.py); unique
        # per run so re-runs never aggregate into one metered figure
        "client_reference_id": ref_id,
    }


class WordAssembler:
    """Turns Soniox final tokens into Speechmatics-shaped word entries.

    Soniox tokens are subword pieces: a token whose text starts with
    whitespace begins a new word, all following tokens concatenate onto
    it (" morn" + "ing" = "morning"). Punctuation with a leading space
    becomes its own entry (" ." -> "."), punctuation without one attaches
    to the current word ("?" after "doing" -> "doing?") - both normalise
    to the same thing in compare.py. A word's timestamp is the start_ms
    of its first token.
    """

    def __init__(self) -> None:
        self.words: list[Word] = []
        self.parts: list[str] = []
        self.start_t: float | None = None
        self.last_t: float | None = None

    def _flush(self) -> None:
        if self.parts:
            t = self.start_t if self.start_t is not None else (self.last_t or 0.0)
            self.words.append(Word(t, "".join(self.parts)))
        self.parts = []
        self.start_t = None

    def add(self, token: dict[str, object]) -> None:
        text = str(token.get("text", ""))
        if not text:
            return
        raw_ms = token.get("start_ms")
        t = float(raw_ms) / 1000.0 if isinstance(raw_ms, (int, float)) else None
        if t is not None:
            self.last_t = t
        if text[0].isspace():
            self._flush()
            stripped = text.strip()
            if stripped:
                self.parts.append(stripped)
                self.start_t = t
        else:
            self.parts.append(text)
            if self.start_t is None:
                self.start_t = t

    def finish(self) -> list[Word]:
        self._flush()
        return self.words


async def run_soniox(
    wav: Path, name: str, out_dir: Path, model: str = "stt-rt-v5"
) -> RunSummary:
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        raise SystemExit("SONIOX_API_KEY not set (get a key at console.soniox.com)")
    pcm, audio_secs = load_pcm(wav)
    chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)
    ref_id = f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    assembler = WordAssembler()
    first_at: float | None = None
    final_seen = False
    start = perf_counter()

    async with websockets.connect(
        URL, additional_headers={"Authorization": f"Bearer {key}"}, max_size=None
    ) as ws:
        await ws.send(json.dumps(start_message(ref_id)))

        async def sender() -> None:
            for i in range(0, len(pcm), chunk_bytes):
                await ws.send(pcm[i : i + chunk_bytes])
                await asyncio.sleep(CHUNK_MS / 1000.0)
            await ws.send("")  # empty frame signals end-of-audio

        async def receiver() -> None:
            nonlocal first_at, final_seen
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("error_code"):
                    raise SystemExit(f"soniox error: {msg.get('error_code')}: {msg.get('error_message')}")
                for token in msg.get("tokens", []):
                    if token.get("is_final"):
                        if first_at is None:
                            first_at = perf_counter() - start
                        final_seen = True
                        assembler.add(token)
                if msg.get("finished"):
                    return

        await asyncio.gather(sender(), receiver())

    wall_secs = perf_counter() - start
    words = assembler.finish()
    out_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("words", "transcripts", "summaries"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    words_path = out_dir / "words" / f"{name}.words.jsonl"
    with words_path.open("w") as f:
        for w in words:
            f.write(json.dumps({"t": round(w.t, 3), "w": w.text}) + "\n")
    (out_dir / "transcripts" / f"{name}.txt").write_text(
        " ".join(w.text for w in words)
    )

    summary = RunSummary(
        name=name,
        provider="soniox",
        operating_point=model,
        audio_secs=audio_secs,
        wall_secs=wall_secs,
        first_word_secs=first_at,
        word_count=len(words),
        client_reference_id=ref_id,
    )
    (out_dir / "summaries" / f"{name}.summary.json").write_text(
        json.dumps(asdict(summary) | {"speed_x": summary.speed_x}, indent=2) + "\n"
    )
    print(
        f"[{name}] audio {audio_secs:.1f}s | wall {wall_secs:.1f}s "
        f"| first final {'+' + f'{first_at:.2f}s' if first_at is not None else 'none'} "
        f"| words {len(words)} | finals seen: {final_seen}"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="16 kHz mono WAV")
    parser.add_argument("--name", required=True)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args()
    asyncio.run(run_soniox(args.wav, args.name, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
