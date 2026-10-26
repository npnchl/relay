# nemotron-mlx-accuracy

Latency vs accuracy comparison for NVIDIA's
`nemotron-speech-streaming-en-0.6b` running on Apple Silicon via
[mlx-audio](https://github.com/Blaizzy/mlx-audio).

The question this project answers: **what is the fastest streaming delay
(80 / 160 / 560 / 1120 ms) whose accuracy holds up against the
solaria-3 baseline?**

## Layout

```
sources/   pristine originals - full-sermon.WAV + its solaris-3 transcript
patches/   cut audio + matching baseline slices (derived, gitignored)
runs/      outputs, organised:
          reports/<section>/   one folder per sermon section, one report
                               per contender inside (nemotron, speechmatics,
                               gladia) - plus sweep-report.txt at the root
          transcripts/ plain transcripts per run
          words/       word-timing logs (input to compare.py)
          summaries/  run metrics (speed, first word)
tools/     the five tools
```

## Tools

| tool | job |
|---|---|
| `prep.py` | convert a recording to 16 kHz mono WAV |
| `patch.py` | cut stratified patches (reading / preaching / children / application / creed) and slice the matching baseline text |
| `stream.py` | feed a WAV to the model as simulated live audio; write word-timed transcript + run summary |
| `compare.py` | word-for-word diff against the solaris-3 baseline; critical-term flags; report for manual ear-checking |
| `sweep.py` | run every patch at every delay, print the verdict table |

## Running

```bash
uv sync                                   # once
uv run python tools/prep.py  sources/full-sermon.WAV patches/sermon-16k.wav
uv run python tools/patch.py patches/sermon-16k.wav sources/full-sermon-transcript.json
uv run python tools/sweep.py              # ~10 minutes; writes runs/reports/sweep-report.txt
```

Single runs and comparisons:

```bash
uv run python tools/stream.py patches/preaching.wav --name preaching-560 --delay 560
uv run python tools/compare.py patches/preaching.baseline.json \
    runs/words/preaching-560.words.jsonl --name preaching-560 --out runs/reports/preaching/nemotron-560.txt
```

Type checks: `uv run mypy tools/` (strict).

## Reading the results

- `runs/reports/sweep-report.txt` — the verdict table (speed, first-word latency,
  differing sentences, critical flags per patch and delay).
- `runs/reports/compare-<patch>-<delay>ms.txt` — the diff report for one run:
  every sentence where the model disagrees with the baseline, the exact
  word differences, `[CRITICAL]` marks on theologically important words,
  and the audio timestamp to listen and judge.

Deviation is measured *against solaris-3*, which is itself a model - the
diff log is the deliverable and a human is the final judge.
