# Progress so far (2026-11-12, branch transcription-accuracy)

## Goal
Develop the **most accurate live transcription setup with multilingual
capacity** — the speaker code-switches between English and Hindi mid-
sermon, so the pipeline must handle that natively. **Diarization is
optional** (nice to have, not required). Accuracy is judged by A/B
listening tests (see test strategy below), not by vendor benchmarks
alone.

## What was verified live
- `SPEECHMATICS_API_KEY` works on `wss://eu.rt.speechmatics.com/v2`
  (session accepted, streams, EndOfTranscript).
- `OPPER_API_KEY` works: `GET api.opper.ai/v3/models`; realtime STT via
  `wss://api.opper.ai/v3/realtime` (`session.start` → `audio.append`
  base64 pcm16 16 kHz → `text.delta` / `transcript.committed`).
- Voxtral model on Opper (EU-hosted): `mistral/voxtral-mini-transcribe-realtime-3-0`.
  Probe-tested accepted config: `transcription_language_codes: []` (auto),
  `transcription_mode: "smart"`, `turn_detection: {type: "semantic_vad",
  eagerness: auto|medium|high}` (low rejected; diarize incompatible with
  semantic_vad), `transcription_custom_vocabulary` OK, `config.parameters`
  NOT supported, audio modality rejected. `audio.commit` errors if no new
  audio since last flush.
- A/B workbench (Reflex app, since removed) streamed the sermon to both
  providers simultaneously; results saved in `results/ab-*.json`.

## Findings from the head-to-head
- Speechmatics RT Enhanced (translation-branch settings: max_delay 4.0
  flexible, punctuation 0.3, remove_disfluencies) produced clean
  punctuated sentences; missed "was" ("he who has spoken of").
- Voxtral RT3: good punctuation, got "was" right, but repeatedly
  misheard the cold opening ("Matthew three verses 1 to 6" → "That's his
  three…", "This is free…", "Versus one to six") — flagged severe in
  `results/ab-20261112-102625.json`. Also batches output: with semantic
  VAD it commits at turn ends, so several sentences arrive per commit.
- Voxtral latency numbers needed span-end at last text.delta, not the
  send cursor.

## Decision
Hit a wall with Voxtral realtime accuracy (esp. cold-start and
numbers/names). Keep Speechmatics realtime as the live baseline.

## Test strategy (for the next iteration)
A/B workbench: play a sermon segment (full file, pre-made 1-min
segments, or a random 1-min batch) while **both providers stream it
simultaneously** at realtime pace. Each finalized sentence appears as a
line per side with its audio timestamp; the tester can add an **inline
note** on any sentence (plus button → text box → submit, editable
later) and **tag the severity of the issue** (partial / severe) per
sentence. Sessions are saved as JSON (transcripts + spans + notes +
severity flags + provider config) for post-analysis: normalized WER,
model-vs-model disagreement flags, and human ear-checks as the final
judge.

## Next steps
1. Test **Speechmatics offline/batch** API accuracy vs realtime — if the
   gap is big, build the offline cascade: cheap RT model (or semantic
   VAD) finds sentence boundaries → accumulated chunk goes to the strong
   offline model with the previous transcript as context.
2. Multilingual: Speechmatics pinned `en` garbles Hindi; compare
   Speechmatics language auto-detection / translation config vs Voxtral
   auto-detect (`language_codes: []`) on mixed-language segments.
3. Keep a small set of 1-min segments (incl. code-switching spans) as a
   repeatable benchmark; score with normalized WER + disagreement flags
   + human ear-checks.

## Workbench rebuild + Gladia live settings experiment (2026-11-20)

### Workbench state
Backend-only aiohttp service (`workbench/server.py`, run with
`cd workbench && uv run python server.py` → http://127.0.0.1:8000) + vanilla JS
frontend (`workbench/web/`, GitHub Dark Dimmed). Native clients only:
arm A = `speechmatics-rt` AsyncClient (melia-1, wss://eu.rt.speechmatics.com/v2),
arm B = `gladiaio-sdk` live v2 (solaria-1 — the only model the SDK pins for live;
solaria-3/solaria-fusion are prerecorded-only). Server-side simulated mic streams the
cached 16 kHz mono PCM in 40 ms chunks at strict 1× wall-clock pace (gapless, never
skips bytes). Listen start/stop per arm is manual and independent of sim
start/stop/seek; a provider drop marks the arm not-listening (no silent reconnect);
Replay restarts from 0 after the source ends. All SDK settings are exposed via
`/config/schema` (two-tier form: Core + collapsed "All provider options") and
validated server-side (400 with field paths). Transcripts live in memory +
`workbench/results/live-*.jsonl`, pushed over SSE `/events`. The Normalize toggle is a
view-only sentence re-segmentation of the in-memory lines (raw output untouched), one
button per panel.

### Gladia live v2 settings experiment
Method: sim streamed seg_00 (00:00–01:00, Matthew 3:1–6) and seg_02 (05:00–06:00) at 1×
with arm A (Speechmatics melia-1) as reference segmentation and arm B across a config
grid. Driver + raw outputs: `workbench/experiments/gladia-settings-2026-11-20/`.

Findings (seg_00 unless noted):
- `endpointing 0.5` / `max_duration 10` → over-splits mid-sentence (11 finals).
- `maximum_duration_without_endpointing 8` → the cap force-splits long sentences and
  garbles the merge; needs to be ≥ ~15.
- `endpointing 1.0–1.2` → merges across verse boundaries (v1 + start of v3).
- `speech_threshold 1.0` → enhancer treats quiet speech as silence → fake gaps inside
  sentences → verse 4 shattered into 3 fragments + garbled v4/v5 merge.
- `speech_threshold 0.5` → clean verse-per-final breaks, but quiet background noise gets
  transcribed as words ("Thanks." at 00:05; Speechmatics never emitted it).
- **`speech_threshold 0.8` is the sweet spot**: no noise pickup AND each verse one whole
  final ("…locusts and wild honey." complete). thr 0.7 still leaks one noise fragment.
- Speechmatics melia-1 over-splits more than any Gladia config on this material
  (seg_00: v1 broken as "…came preaching in the" / "wilderness of Judea…").

### Recommended Gladia live config (raw output — no Normalize needed)
```json
{
  "encoding": "wav/pcm", "bit_depth": 16, "sample_rate": 16000, "channels": 1,
  "model": "solaria-1",
  "language_config": { "languages": ["en", "de"], "code_switching": true },
  "endpointing": 0.8,
  "maximum_duration_without_endpointing": 15,
  "pre_processing": { "audio_enhancer": true, "speech_threshold": 0.8 },
  "messages_config": { "receive_partial_transcripts": false, "receive_final_transcripts": true }
}
```
Set via the Gladia settings panel (Core section) and Apply.
