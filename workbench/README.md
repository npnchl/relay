# workbench/ — local A/B transcription workbench

Side-by-side live comparison of two STT providers over the same audio:

- **A**: Speechmatics realtime `melia-1` + `language: "multi"` (`wss://eu.rt.speechmatics.com/v2`)
- **B**: Gladia live v2 `solaria-1` + `code_switching` (`POST /v2/live`)

Default configs come from the probe-verified JSON in `../tuning/*.md` and can be
edited per arm in the UI (gear button on each viewport).

## Run

```bash
cd workbench
uv run python server.py
# open http://127.0.0.1:8000
```

First run converts `../sermon_full.WAV` (48 kHz stereo) to a cached 16 kHz mono
PCM file (`cache/`, gitignored) and writes playable files + `audio_out/manifest.json`
(full file and twelve 60 s segments). Keys are read from `../.env` and never
sent to the browser.

## Use

- Pick **Full file**, a **Segment**, or **Random 1-min segment** (shuffle button re-rolls).
- **Play** streams the audio out loud and feeds identical 16 kHz PCM to both
  providers at realtime pace. **Pause** stops streaming but keeps transcripts.
- **Scrub** closes both provider sessions and clears both viewports; pressing
  play again resumes from the new position. **Trash** clears one viewport.
- Each viewport auto-scrolls while its toggle is on. Sessions are saved to
  `../results/ab-<timestamp>.json` on pause and at end of source.

## Smoke test

```bash
cd workbench && uv run python server.py &   # in another shell:
uv run python smoke_test.py
```

`probe_gladia.py` is a standalone diagnostic that streams 12 s of audio straight
to Gladia and prints every protocol message (useful when changing the live config).

## Layout

- `server.py` — static hosting, range-served audio, `/ws` protocol, session orchestration
- `providers.py` — raw-websocket provider sessions (Speechmatics arm A, Gladia arm B)
- `audio.py` — 48k stereo to 16k mono conversion, segment cutting, manifest
- `web/` — vanilla HTML/CSS/ES-modules frontend, no build step
  (`app.js` glue, `transport.js` player controls, `viewport.js` panels, `ws.js` socket)
