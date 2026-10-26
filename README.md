# relay — translation accuracy

Phase 2 of evaluating the pipeline for **relay**: a church
live-translation captions system (sermon audio → live English
transcription → live translation → captions on phones).

**Hardware pivot:** all processing now happens in the cloud. relay hosts
the API and delegates transcription and translation to external
providers. (The original plan — local ASR on a Mac Mini — was abandoned;
local models remain useful only as a free quality yardstick.)

## Findings carried over from the transcription phase (branch: nemotron-mlx-accuracy)

**Test method that worked:**
- Stratified patches of a real 30-min sermon (Matthew 3, Garry Williams):
  reading / preaching / children's talk / application / creed — 228
  baseline sentences total.
- Reference transcript: Gladia solaris-3 offline (fallible! several
  "differences" proved to be baseline errors, not candidate errors).
- Comparison: one global word-sequence alignment over the whole
  transcript (immune to timestamp drift), with a normalization layer
  (hyphens, contractions, possessives, number words, compound joins)
  so formatting style never counts as a hearing error. Per-sentence
  diff reports + a critical-term glossary (names, theological terms)
  + human ear arbitration at listed timestamps.

**Result (sentences differing vs baseline, of 228):**
| Contender | Differing | Critical | Cost/h | Notes |
|---|---|---|---|---|
| Speechmatics Enhanced (EU) | **17** | 1 | ~$0.40 | fully punctuated output; ~500 ms partials (Pipecat: 495 ms median TTFS); custom dictionary available |
| Nemotron 0.6B local (free) | 22 | 2 | free | the yardstick; occasional name errors (profit→prophet) |
| Gladia live solaria-1 | 41 | 1 | ~€0.70 | live model clearly below its own offline baseline; one meaning-changing mishearing ("2000 years" → "20 years") |

**Decisions:**
- STT provider: **Speechmatics Enhanced**, EU endpoint pinned (data
  residency; JWT auth not needed for server-side — Bearer key works).
- Both cloud providers handled names that beat local models
  ("Pontius Pilate" — the failure case that started the evaluation).
- Remaining STT error classes: rare dropped one-word utterances
  ("locusts"), and homophone name errors on other contenders —
  targeted by Speechmatics' custom dictionary, untested so far.

**Costs observed:** cloud realtime bills per second of audio; ~9 min of
testing ≈ $0.06-0.08. Speechmatics gives $100 evaluation credit.

## This phase: translation accuracy

**No gold baseline exists for translation** — evaluation is by judgment:
1. **Hindi arm: graded directly by the maintainer**, who understands
   spoken Hindi but does not read Devanagari. Grade via transliteration
   into Roman script (Hinglish) or TTS-audio review — a genuine human
   faithfulness check, not a proxy.
2. **Other languages: LLM-as-judge** (MQM-style: omissions, additions,
   mistranslations; specifically checking theological terms, names,
   numbers), calibrated against the maintainer's Hindi rulings.

**Arms to test:**
| Arm | Why |
|---|---|
| Speechmatics realtime translation | frontrunner: STT + translation in one WebSocket stream (target languages set at session start) — the single-vendor pipeline |
| Gladia translation add-on | €0.05/h flat, pairs with their STT |
| Google Translate API or an LLM API | general-purpose arms |

DeepL is excluded — no Hindi support.

**Live vs batch:** test translation both on the live caption stream
(hard mode, the deployment reality) and on final transcripts.

## Open inputs needed
- The church's actual target language list (at minimum: Hindi first,
  since it can be graded directly).
- Speechmatics translation: confirm supported target languages cover
  the list, and its cost model.
