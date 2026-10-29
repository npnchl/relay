# Translation evaluation — running findings

Evidence log for the translation phase, newest first. Each finding was
observed on the live Speechmatics stream; the numbers come from
diagnostic runs of the same sermon passage (~86s–183s, Matthew 3,
"at least 25 years after chapter two").

## What the app does now

`app/` (Reflex): a virtual player streams the sermon at true realtime
pace (wall-clock driven, no drift) chunk-by-chunk to one Speechmatics
EU session that does STT + Hindi translation together
(`transcription_config`: enhanced, finals-only, max_delay flexible;
`translation_config`: target hi). Hindi sentences render with the
English words they translate (matched by span overlap in the original
audio) and an Azure TTS play button each (Aarav voice, UK South
resource `ebenezer-tts-test`) for ear-grading. Rows containing a run
of 3+ English words get an amber "untranslated" badge.

All audio properties derive from the WAV header (byte rate, duration);
nothing is hardcoded. Pause/seek close the Speechmatics session
(billing stops) and clear all buffers; Start reopens fresh at the
playhead.

## Findings

### 1. Speechmatics passes untranslated English through on long units — deterministic

- The translator runs incrementally: it emits small spans as finals
  commit, NOT sentence-buffered drafts. No re-emission, no revision —
  each span arrives exactly once, ever.
- Every unit longer than ~10 s came back as raw English on the Hindi
  channel. Every unit under ~9 s translated fine. Observed twice in a
  95 s window (10.8 s and 11.9 s spans) — ~11-12 s of sermon lost per
  failure, delivered in English to Hindi speakers.
- Also observed once: a *partial* passthrough (English head + Hindi
  tail in one unit) — mid-process failure of the same kind.

**max_delay made no difference:** identical passthrough at 1.5, 2.0
and 4.0 on the same span. The vendor's translation is not gated by our
transcript latency; this is a defect on long units, not a latency
artifact. No knob on our side fixes it.

### 2. Translation units are not aligned to transcript fragments

The vendor's AddTranslation spans differ from the AddTranscript
fragment boundaries, so a Hindi sentence can cover different words
than the English fragment displayed beside it. The app pairs them by
timestamp overlap; do not judge a Hindi row against an English
fragment without the paired text.

### 3. Long pauses mid-sentence produce false full stops

When the pastor pauses before finishing a sentence, Speechmatics
commits a transcript final ending in a period that isn't a real
sentence end ("...confessing their sins ."). These fake stops also
arrive as separate punctuation-only fragments, and some of the odd "
." spacing in the English panel is our fragment joining (known,
deliberately not yet fixed). The fake periods likely fragment the
vendor translator's context too — input we cannot change for their
arm, but fully under our control in an LLM arm.

### 4. Early translation-quality notes (ear-grading, casual)

- "camel's hair" → चमड़े के बालों ("leather hair") — mistranslation.
- "locusts" → लुक (garbled transliteration; should be टिड्डी).
- Long-unit passthrough (finding 1) is the dominant defect so far.

## Architecture insight (from a conversation with a friend)

Quality real-time translation is unavoidably ~1 sentence behind the
speaker — you need the whole sentence to translate it well. Church
captions tolerate that lag (people read at their own pace). Therefore:
- For a sentence-buffered pipeline (e.g. our own LLM arm),
  transcript max_delay is nearly free — hidden behind the sentence
  wait — and we control chunking, so findings 1 and 3 disappear by
  construction.
- Speechmatics' incremental translator does NOT benefit: it consumes
  finals as they commit, so their latency advantage is real but their
  quality ceiling is low for our use case.

## Tuning plan (in progress)

Speechmatics input-side settings, aimed at the exact defects above.
Diagnostic window: 95 s of the same passage (absolute 86-181 s),
"passthroughs" = untranslated English on the Hindi channel.

| config | passthroughs | notes |
|---|---|---|
| delay 1.5/2.0/4.0, sensitivity 0.5 | 2 | false stop after "infancy" persists at every delay |
| delay 8.0, sensitivity 0.5 | 2 | delay is NOT the punctuation lever (docs cap says 4; server accepts 8 silently) |
| delay 1.5, sensitivity 0.12 | 1 | false stop gone, but real sentences merge (ear-graded too sparse) |
| delay 1.5, sensitivity 0.3 | 2 | isolation run: sensitivity alone does NOT fix it - both classic spans fail again |
| delay 4.0, sensitivity 0.25 | 0 | false stop gone, but "why say in those days?" lost its ? |
| **delay 4.0, sensitivity 0.3** | **0** | **current setting**; ? restored in English AND Hindi; full coverage kept |

**Why the combo matters:** at 1.5 s the model must commit at a pause
before hearing the continuation, so pauses still get false periods;
at 4.0 s it has runway past the pause to see the sentence continue
and punctuate honestly — which is what lets moderate sensitivity
(0.3) keep real marks without inventing fake ones. The knobs are
coupled: delay buys the punctuation model honesty, sensitivity
chooses how often it marks.

**Variance check (5 identical runs, 4.0 + 0.3):** 0 passthroughs in
all five (15 messages each). Compare sensitivity 0.5, where the
same two spans failed deterministically at every delay. So the
tuned config moved those spans from *certain failure* to *no failure
observed in 5 runs*. Caveat: the maintainer's own app session did
catch a partial passthrough on a "So in those days..." unit at this
config once — so the true rate is rare-but-nonzero, not zero.

Applied field shapes (server-validated):
- `punctuation_overrides: {"sensitivity": 0.25}` inside transcription_config
- `transcript_filtering_config: {"remove_disfluencies": true}` — NOT
  directly in transcription_config (server rejects it there)
- disfluency removal kept on at every sensitivity

Open items:
1. Single-run numbers so far; rerun the window at the chosen setting
   to check run-to-run variance.
2. Hindi quality is ear-graded, not script-graded (early note:
   "internal combustion engine" -> आंतरिक जलन इंजन looked literal).
3. `output_locale: en-GB` — free, matches the (British) pastor.
4. STT accuracy regression check on transcription-accuracy (its
   harness normalises punctuation, so punctuation itself must be
   judged by ear here).
5. If vendor defects resurface: LLM translation arm, sentence-buffered.

## Current uncommitted state

- `app/app/providers.py` max_delay is 2.0 (test setting); the
  committed verdict value is 1.5. Confirm which to keep.
