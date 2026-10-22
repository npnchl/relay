"""Latency sweep: run every patch at every streaming delay, compare each
against its solaris-3 baseline, and print the verdict table.

The question this answers: what is the fastest delay setting (80/160/560/
1120 ms) whose accuracy does not fall apart?
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from compare import run_compare
from stream import run_stream

DELAYS_MS: list[int] = [80, 160, 560, 1120]


@dataclass(frozen=True)
class SweepRow:
    patch: str
    delay_ms: int
    speed_x: float
    first_word_secs: float
    differing: int
    total_chunks: int
    critical_flags: int


@dataclass(frozen=True)
class RunOutcome:
    speed_x: float
    first_word_secs: float


def load_outcome(runs: Path, name: str) -> RunOutcome:
    data = json.loads((runs / f"{name}.summary.json").read_text())
    return RunOutcome(
        speed_x=float(data["speed_x"]),
        first_word_secs=float(data.get("first_word_secs") or 0.0),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patches", type=Path, default=Path("patches"))
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="reuse existing run summaries, only re-run the comparisons",
    )
    args = parser.parse_args()

    # only wavs that have a matching baseline are patches
    patch_names = sorted(
        p.stem for p in args.patches.glob("*.wav")
        if (args.patches / f"{p.stem}.baseline.json").exists()
    )
    if not patch_names:
        raise SystemExit(f"no patches in {args.patches} (run patch.py first)")

    rows: list[SweepRow] = []
    for patch in patch_names:
        wav = args.patches / f"{patch}.wav"
        baseline = args.patches / f"{patch}.baseline.json"
        for delay in DELAYS_MS:
            name = f"{patch}-{delay}ms"
            print(f"\n=== {name} ===", flush=True)
            if args.report_only:
                outcome = load_outcome(args.runs, name)
            else:
                kwargs: dict[str, object] = {}
                if args.model is not None:
                    kwargs["model_name"] = args.model
                summary = run_stream(wav, name, args.runs, delay, **kwargs)  # type: ignore[arg-type]
                outcome = RunOutcome(
                    speed_x=summary.speed_x,
                    first_word_secs=summary.first_word_secs or 0.0,
                )
            total, differing, critical = run_compare(
                baseline,
                args.runs / f"{name}.words.jsonl",
                name,
                args.runs / f"compare-{name}.txt",
                audio_label=patch,
            )
            rows.append(
                SweepRow(
                    patch=patch,
                    delay_ms=delay,
                    speed_x=outcome.speed_x,
                    first_word_secs=outcome.first_word_secs,
                    differing=differing,
                    total_chunks=total,
                    critical_flags=critical,
                )
            )

    report: list[str] = [
        "LATENCY SWEEP - nemotron-speech-streaming vs solaris-3 baseline",
        "",
        f"{'patch':<12} {'delay':>6} {'speed':>7} {'1st word':>9} "
        f"{'differing':>10} {'of':>5} {'critical':>9}",
    ]
    for r in rows:
        report.append(
            f"{r.patch:<12} {r.delay_ms:>4}ms {r.speed_x:>6.1f}x "
            f"{r.first_word_secs:>8.2f}s {r.differing:>10} {r.total_chunks:>5} "
            f"{r.critical_flags:>9}"
        )
    # per-delay totals for the verdict
    report.append("")
    report.append("per-delay totals across patches:")
    for delay in DELAYS_MS:
        d_rows = [r for r in rows if r.delay_ms == delay]
        report.append(
            f"  {delay:>4}ms: differing {sum(r.differing for r in d_rows)}/"
            f"{sum(r.total_chunks for r in d_rows)}, "
            f"critical {sum(r.critical_flags for r in d_rows)}, "
            f"slowest speed {min(r.speed_x for r in d_rows):.1f}x"
        )
    text = "\n".join(report) + "\n"
    args.runs.mkdir(parents=True, exist_ok=True)
    (args.runs / "sweep-report.txt").write_text(text)
    print("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
