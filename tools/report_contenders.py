"""Contender report: compare every available cloud run against its patch
baseline, and tabulate accuracy, speed, and cost side by side.

A run is 'available' when runs/words/<patch>-<contender>.words.jsonl
exists. Accuracy numbers are an index into the per-patch diff reports
(written next to this table) - the diffs are the actual evidence.

Costs come from provider usage APIs where metering exists:
- Soniox: exact, per run (each run is tagged client_reference_id=<run
  name>; cost fetched from GET /v1/usage-logs).
- Speechmatics: /v2/usage reports nothing for realtime WebSocket
  sessions, so costs are estimates (audio hours x published rate) -
  realtime spend is only visible in the portal.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from compare import run_compare
from cost import soniox_costs_by_reference

# contender id -> (label, $/hour realtime, metered?)
CONTENDERS: list[tuple[str, str, float, bool]] = [
    ("sm-enhanced", "Speechmatics Enhanced", 0.40, False),
    ("soniox", "Soniox stt-rt-v5", 0.12, True),
]


@dataclass(frozen=True)
class Row:
    patch: str
    contender: str
    report_path: str
    audio_secs: float
    speed_x: float
    first_word_secs: float
    cost_usd: float | None  # metered; None = not meterable
    est_cost_usd: float
    differing: int
    total: int
    critical: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patches", type=Path, default=Path("patches"))
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    args = parser.parse_args()

    patch_names = sorted(
        p.stem for p in args.patches.glob("*.wav")
        if (args.patches / f"{p.stem}.baseline.json").exists()
        and p.stem != "sermon-16k"
    )
    if not patch_names:
        raise SystemExit(f"no patches in {args.patches} (run patch.py first)")

    soniox_key = os.environ.get("SONIOX_API_KEY")
    metered: dict[str, float] = {}
    if soniox_key:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            metered = soniox_costs_by_reference(soniox_key, start, end)
        except Exception as err:  # noqa: BLE001 - degrade to estimates
            print(f"warning: soniox usage lookup failed ({err})", file=sys.stderr)

    rows: list[Row] = []
    diff_files: list[str] = []
    for patch in patch_names:
        baseline = args.patches / f"{patch}.baseline.json"
        for cid, label, rate, meterable in CONTENDERS:
            name = f"{patch}-{cid}"
            words_path = args.runs / "words" / f"{name}.words.jsonl"
            summary_path = args.runs / "summaries" / f"{name}.summary.json"
            if not words_path.exists() or not summary_path.exists():
                continue
            print(f"=== {name} ===", flush=True)
            summary = json.loads(summary_path.read_text())
            report_path = args.runs / "reports" / patch / f"{cid}.txt"
            total, differing, critical = run_compare(
                baseline, words_path, name, report_path, audio_label=patch
            )
            diff_files.append(str(report_path))
            ref_id = str(summary.get("client_reference_id") or name)
            cost = metered.get(ref_id) if meterable else None
            rows.append(
                Row(
                    patch=patch,
                    contender=label,
                    report_path=str(report_path),
                    audio_secs=float(summary["audio_secs"]),
                    speed_x=float(summary["speed_x"]),
                    first_word_secs=float(summary.get("first_word_secs") or 0.0),
                    cost_usd=cost,
                    est_cost_usd=float(summary["audio_secs"]) / 3600.0 * rate,
                    differing=differing,
                    total=total,
                    critical=critical,
                )
            )

    if not rows:
        raise SystemExit("no runs found (stream a contender first)")

    def cost_cell(r: Row) -> str:
        if r.cost_usd is not None:
            return f"${r.cost_usd:.4f}"
        return f"~${r.est_cost_usd:.3f} est"

    report: list[str] = [
        "CONTENDER COMPARISON vs solaris-3 baseline (per patch)",
        "",
        "accuracy numbers index the per-patch diff reports listed below -",
        "read the diffs (and ear-check the flagged timestamps) to judge.",
        "",
        f"{'patch':<12} {'contender':<22} {'speed':>6} {'1st final':>9} "
        f"{'differing':>9} {'of':>4} {'crit':>4} {'cost':>12}",
    ]
    for r in rows:
        report.append(
            f"{r.patch:<12} {r.contender:<22} {r.speed_x:>5.1f}x "
            f"{r.first_word_secs:>8.2f}s {r.differing:>9} {r.total:>4} "
            f"{r.critical:>4} {cost_cell(r):>12}"
        )
    report.append("")
    report.append("totals per contender:")
    for _, label, _, meterable in CONTENDERS:
        c_rows = [r for r in rows if r.contender == label]
        if not c_rows:
            continue
        audio = sum(r.audio_secs for r in c_rows)
        if meterable and all(r.cost_usd is not None for r in c_rows):
            spend = sum(r.cost_usd or 0.0 for r in c_rows)
            cost_txt = f"metered ${spend:.4f}"
        else:
            cost_txt = f"~${sum(r.est_cost_usd for r in c_rows):.3f} est"
        report.append(
            f"  {label:<22} differing {sum(r.differing for r in c_rows)}/"
            f"{sum(r.total for r in c_rows)}, critical {sum(r.critical for r in c_rows)}, "
            f"{cost_txt} ({audio / 60:.0f} min audio)"
        )
    report.append("")
    report.append("diff reports (the actual evidence):")
    for p in diff_files:
        report.append(f"  {p}")
    report.append("")
    report.append(
        "costs: soniox = metered from usage API; speechmatics = estimate\n"
        "(its usage API reports nothing for realtime sessions - check the\n"
        "portal for actual realtime spend)"
    )

    text = "\n".join(report) + "\n"
    print()
    print(text, end="")
    out_path = args.runs / "reports" / "contenders-report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
