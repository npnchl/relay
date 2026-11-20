# Gladia live v2 settings experiment (2026-11-20)

Findings and the recommended config are documented in `progress.md` (repo root,
section "Workbench rebuild + Gladia settings experiment").

## Contents
- `drive.py` — experiment driver: talks to the workbench backend REST API, runs each
  config in `configs*.json` over a segment with both arms listening (arm A = Speechmatics
  as reference), saves raw finals to `<label>_<seg>.json`.
- `configs.json` — main grid (seg_00); `configs_seg2.json` — confirmation on seg_02;
  `configs_thrsweep.json` — speech_threshold sweep 0.7/0.8 on seg_00.
- `*_seg_00.json` / `*_seg_02.json` — raw transcript lines (A + B) per run.

## Reproduce
```bash
cd workbench && uv run python server.py &   # http://127.0.0.1:8000
uv run python experiments/gladia-settings-2026-11-20/drive.py <label> [<label>...]
```
