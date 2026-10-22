"""Stream a 16 kHz mono WAV through a nemotron-speech-streaming session.

Simulates live audio (100 ms chunks), collects words with their times,
and writes: a word-timed JSONL, a transcript, and a run summary JSON.
"""

import argparse
import json
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

CHUNK_MS = 100
DEFAULT_MODEL = "nvidia/nemotron-speech-streaming-en-0.6b"


class StreamingSession(Protocol):
    """The slice of the mlx-audio streaming contract we rely on."""

    @property
    def done(self) -> bool: ...

    def feed(self, samples: NDArray[np.float32]) -> None: ...

    def step(self, *, max_decode_tokens: int = 4) -> list[str]: ...

    def close(self) -> None: ...


class StreamingModel(Protocol):
    def create_streaming_session(
        self, *, transcription_delay_ms: int = 560
    ) -> StreamingSession: ...


@dataclass(frozen=True)
class Word:
    t: float
    text: str


@dataclass(frozen=True)
class RunSummary:
    name: str
    model: str
    delay_ms: int
    audio_secs: float
    wall_secs: float
    first_word_secs: float | None
    word_count: int

    @property
    def speed_x(self) -> float:
        return self.audio_secs / self.wall_secs if self.wall_secs > 0 else 0.0


class WordCollector:
    """Groups decoded characters into words, stamped when their text arrived."""

    def __init__(self) -> None:
        self._cur: list[str] = []
        self._cur_t: float = 0.0

    def add(self, text: str, t: float) -> list[Word]:
        finished: list[Word] = []
        for ch in text:
            if ch == " ":
                word = "".join(self._cur).strip()
                if word:
                    finished.append(Word(self._cur_t, word))
                self._cur = []
            else:
                if not self._cur:
                    self._cur_t = t
                self._cur.append(ch)
        return finished

    def flush(self) -> list[Word]:
        word = "".join(self._cur).strip()
        self._cur = []
        if word:
            return [Word(self._cur_t, word)]
        return []


def load_wav(path: Path) -> NDArray[np.float32]:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise SystemExit(f"{path}: need 16 kHz mono WAV (run prep.py first)")
        raw = w.readframes(w.getnframes())
    samples: NDArray[np.float32] = (
        np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    )
    return samples


def make_session(model: StreamingModel, delay_ms: int) -> StreamingSession:
    try:
        return model.create_streaming_session(transcription_delay_ms=delay_ms)
    except TypeError:
        # some model factories may not accept the delay option
        return model.create_streaming_session()


def run_stream(
    wav: Path,
    name: str,
    out_dir: Path,
    delay_ms: int,
    model_name: str = DEFAULT_MODEL,
) -> RunSummary:
    from mlx_audio.stt import load  # deferred: heavy import

    samples = load_wav(wav)
    audio_secs = float(len(samples)) / 16000.0
    chunk = int(16000 * CHUNK_MS / 1000)

    t0 = perf_counter()
    model = load(model_name)
    load_secs = perf_counter() - t0
    print(f"[{name}] model loaded in {load_secs:.1f}s")

    session = make_session(model, delay_ms)
    collector = WordCollector()
    words: list[Word] = []
    transcript_parts: list[str] = []
    first_word_at: float | None = None

    start = perf_counter()
    fed = 0
    for i in range(0, len(samples), chunk):
        session.feed(samples[i : i + chunk])
        fed += len(samples[i : i + chunk])
        text = "".join(session.step(max_decode_tokens=8))
        if text:
            if first_word_at is None:
                first_word_at = perf_counter() - start
            t = fed / 16000.0 - delay_ms / 1000.0
            new_words = collector.add(text, t)
            words.extend(new_words)
            transcript_parts.extend(w.text + " " for w in new_words)
            print(text, end="", flush=True)
    session.close()
    while not session.done:
        text = "".join(session.step(max_decode_tokens=8))
        if text:
            t = audio_secs - delay_ms / 1000.0
            new_words = collector.add(text, t)
            words.extend(new_words)
            transcript_parts.extend(w.text + " " for w in new_words)
            print(text, end="", flush=True)
    words.extend(collector.flush())
    wall_secs = perf_counter() - start

    out_dir.mkdir(parents=True, exist_ok=True)
    words_path = out_dir / f"{name}.words.jsonl"
    with words_path.open("w") as f:
        for w in words:
            f.write(json.dumps({"t": round(w.t, 3), "w": w.text}) + "\n")

    transcript_path = out_dir / f"{name}.txt"
    transcript_path.write_text("".join(transcript_parts))

    summary = RunSummary(
        name=name,
        model=model_name,
        delay_ms=delay_ms,
        audio_secs=audio_secs,
        wall_secs=wall_secs,
        first_word_secs=first_word_at,
        word_count=len(words),
    )
    summary_path = out_dir / f"{name}.summary.json"
    summary_path.write_text(
        json.dumps(asdict(summary) | {"speed_x": summary.speed_x}, indent=2) + "\n"
    )
    print(
        f"\n[{name}] audio {audio_secs:.1f}s | wall {wall_secs:.1f}s "
        f"| speed {summary.speed_x:.2f}x | first word "
        f"{'+' + f'{first_word_at:.2f}s' if first_word_at is not None else 'none'} "
        f"| words {len(words)}"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="16 kHz mono WAV")
    parser.add_argument("--name", required=True, help="run name (output prefix)")
    parser.add_argument("--delay", type=int, default=560, help="streaming delay ms")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args()
    run_stream(args.wav, args.name, args.out, args.delay, args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
