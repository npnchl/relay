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

## Tuning plan (next)

Speechmatics input-side settings to test, aimed at the exact defects
above (tuning happens on this branch, ear-graded; STT accuracy
regression checked on transcription-accuracy against the solaris-3
baseline — note: that harness normalises punctuation, so fake stops
won't show there; punctuation work belongs here):

1. `transcription_config.punctuation_overrides` — sensitivity /
   permitted marks, targeting finding 3.
2. Disfluency removal ("um/uh/you know") — cleaner translation input.
3. `output_locale: en-GB` — free, matches the (British) pastor.
4. Audio filtering — low priority; recording is clean, real church
   acoustics would be the reason.
5. If vendor defects persist: LLM translation arm, sentence-buffered —
   our own chunking, one sentence behind by design.

## Current uncommitted state

- `app/app/providers.py` max_delay is 2.0 (test setting); the
  committed verdict value is 1.5. Confirm which to keep.
