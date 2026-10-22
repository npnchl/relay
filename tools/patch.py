"""Cut test patches from the full sermon and slice the matching baseline.

Each patch is a short WAV plus the solaris-3 utterances that fall inside
its time window (times shifted to be patch-relative). The baseline keeps
the same JSON shape as the original, so compare.py treats patches and the
full sermon identically.
"""

import argparse
import json
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

# stratified sections of the Matthew 3 sermon (name, start secs, duration secs)
DEFAULT_WINDOWS: list[tuple[str, int, int]] = [
    ("reading", 0, 120),        # scripture read aloud - the easy case
    ("preaching", 160, 120),    # core exposition - the normal case
    ("children", 350, 120),     # interactive map demo - the hard case
    ("application", 1530, 120), # application on repentance
    ("creed", 1740, 60),        # Apostle's Creed recitation
]


@dataclass(frozen=True)
class Window:
    name: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


class Utterance(TypedDict):
    start: float
    end: float
    text: str


def load_baseline(path: Path) -> list[Utterance]:
    data = json.loads(path.read_text())
    return list(data["result"]["transcription"]["utterances"])


def slice_audio(wav: Path, window: Window, out_path: Path) -> None:
    with wave.open(str(wav), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise SystemExit(f"{wav}: need 16 kHz mono WAV (run prep.py first)")
        first = int(window.start * 16000)
        last = min(int(window.end * 16000), w.getnframes())
        w.setpos(first)
        raw = w.readframes(last - first)
    with wave.open(str(out_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(raw)


def slice_baseline(
    utterances: list[Utterance], window: Window, out_path: Path
) -> None:
    sliced: list[Utterance] = []
    for u in utterances:
        if u["start"] < window.end and u["end"] > window.start:
            sliced.append(
                Utterance(
                    start=u["start"] - window.start,
                    end=u["end"] - window.start,
                    text=u["text"],
                )
            )
    payload = {"result": {"transcription": {"utterances": sliced}}}
    out_path.write_text(json.dumps(payload, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sermon", type=Path, help="full sermon, 16 kHz mono WAV")
    parser.add_argument("baseline", type=Path, help="solaris-3 json of the full sermon")
    parser.add_argument("--out", type=Path, default=Path("patches"))
    parser.add_argument(
        "--window",
        action="append",
        default=[],
        help="extra window as name,start,duration (seconds); repeatable",
    )
    args = parser.parse_args()

    windows: list[Window] = [
        Window(name, float(start), float(dur))
        for name, start, dur in DEFAULT_WINDOWS
    ]
    for spec in args.window:
        name, start, dur = spec.split(",")
        windows.append(Window(name, float(start), float(dur)))

    utterances = load_baseline(args.baseline)
    args.out.mkdir(parents=True, exist_ok=True)
    for w in windows:
        wav_out = args.out / f"{w.name}.wav"
        base_out = args.out / f"{w.name}.baseline.json"
        slice_audio(args.sermon, w, wav_out)
        slice_baseline(utterances, w, base_out)
        n = len(load_baseline(base_out))
        print(f"{w.name}: {wav_out} + baseline ({n} utterances)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
