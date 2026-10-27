# transcription-accuracy

Realtime STT accuracy evaluation for **relay** — a church
live-translation captions system (sermon audio → live English
transcription → live translation → captions on phones). This phase
picks the transcription provider and its settings.

**Verdict: Speechmatics Enhanced, finals-only, `max_delay: 1.5 s`.**

- Speechmatics Enhanced: **16/228** baseline sentences differing,
  1 critical-term error — the accuracy leader, confirmed by two
  independent runs (17/228 then 16/228).
- Soniox stt-rt-v5: 32/228, 4 critical — eliminated; the base model
  misheard core content ("Matthew three" → "massive strain") and
  silently paraphrases. Cheaper (~$0.11/h metered vs ~$0.40/h) but not
  good enough for the job.
- Nemotron 0.6B local (via mlx-audio): the free yardstick from the
  original evaluation (22/228) — kept as a tool, no longer a contender.
- Gladia live solaria-1: 41/228 in the original evaluation — eliminated
  early.

## Layout

```
sources/   pristine originals - full-sermon.WAV + its solaris-3 transcript
patches/   cut audio + matching baseline slices (derived, gitignored)
tools/     evaluation harness (see below)
app/       live tuning app - play the sermon, stream to a provider,
           watch captions (see "Live tuning app")
```

Run outputs (`runs/`) are generated, not tracked — regenerate with the
tools below. They were deleted in favour of the interactive app.

## Tools

| tool | job |
|---|---|
| `prep.py` | convert a recording to 16 kHz mono WAV |
| `patch.py` | cut stratified patches (reading / preaching / children / application / creed) and slice the matching baseline text |
| `stream.py` | feed a WAV to the local nemotron model as simulated live audio |
| `stream_speechmatics.py` | same, against Speechmatics realtime (Enhanced) |
| `stream_soniox.py` | same, against Soniox stt-rt-v5 (region via `SONIOX_REGION`) |
| `stream_gladia.py` | same, against Gladia realtime |
| `compare.py` | word-for-word diff against the solaris-3 baseline; critical-term flags; report for manual ear-checking |
| `sweep.py` | latency sweep for the local model across delays |
| `report_contenders.py` | per-patch accuracy/speed/cost table over all available runs; indexes the per-patch diff reports as the evidence |
| `cost.py` | metered per-run cost from the Soniox usage API; Speechmatics usage API verified to report nothing for realtime sessions (portal only) |

## Running

```bash
uv sync                                   # once
uv run python tools/prep.py  sources/full-sermon.WAV patches/sermon-16k.wav
uv run python tools/patch.py patches/sermon-16k.wav sources/full-sermon-transcript.json
```

Stream every contender over the patches, then produce the comparison
report (per-patch diffs land in `runs/reports/`, the table in
`runs/reports/contenders-report.txt`):

```bash
for p in reading preaching children application creed; do
  uv run python tools/stream_speechmatics.py patches/$p.wav --name $p-sm-enhanced
  uv run python tools/stream_soniox.py patches/$p.wav --name $p-soniox
done
uv run python tools/report_contenders.py
```

Type checks: `uv run mypy tools/` (strict).

## Speechmatics Enhanced run configuration

All Speechmatics runs (`tools/stream_speechmatics.py` and the live
tuning app) use these settings:

- **Endpoint:** `wss://eu.rt.speechmatics.com/v2` (EU region, for data
  residency), auth via `SPEECHMATICS_API_KEY` (Bearer token, from `.env`).
- **Language:** `en`
- **Operating point:** `enhanced` (the Enhanced realtime model)
- **Partials:** disabled (`enable_partials: false`) — only final results are
  kept, so text never rewrites. Finals arrive progressively (small chunks
  roughly 0.5-1 s behind the speech), not once per sentence. End-of-session
  flush takes another ~30 s after the last audio, irrelevant for live use.
- **`max_delay: 1.5` with `max_delay_mode: flexible`** — the chosen tuning,
  decided interactively via the live app: finals commit fast enough that
  latency isn't noticeable, with a bit more revision time than the
  snappier 0.7 s. `flexible` lets the model hold a final briefly so
  numbers/dates format properly.
- **No custom dictionary, no other options set** — the model runs with all
  defaults. `vocab_terms` remains the untested lever if name errors
  resurface.
- **Audio fed to the API:** 16 kHz mono 16-bit PCM (`prep.py` output), sent
  in 100 ms chunks (3,200 bytes each) at true realtime pace — the server
  sees a live stream, not a batch upload. Session closes with `EndOfStream`
  (which requires `last_seq_no`).

Known gap: arrival times per result are not logged, so end-to-end
caption latency is estimated, not measured exactly.

## Live tuning app (`app/`)

A Reflex app that plays the full sermon in the browser and streams
captions from Speechmatics Enhanced with the settings above. The
player's own clock drives the chunk streaming — only audio the player
has reached is sent, so captions cannot drift, and play/pause/seek
start/stop the provider session exactly. Used to settle the max_delay
choice. Run it:

```bash
cd app && uv run reflex run    # keys loaded from ../.env
```

Billing runs only while the audio is playing (~$0.40/h).

## Reading the results

- `runs/reports/contenders-report.txt` — the verdict table: per patch and
  contender, speed, first-final latency, differing sentences, critical
  flags, and cost (metered for Soniox, estimated for Speechmatics).
- `runs/reports/<patch>/<contender>.txt` — the per-patch diff report: every
  sentence where the model disagrees with the baseline, the exact word
  differences, `[CRITICAL]` marks on theologically important words, and the
  audio timestamp to listen and judge.

Deviation is measured *against solaris-3*, which is itself a model — the
diff log is the deliverable and a human is the final judge.
