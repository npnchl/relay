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
