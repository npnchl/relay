# workbench/ — backend-only A/B transcription workbench

Side-by-side live comparison of two STT providers over the same audio, driven
entirely by REST. A **simulated microphone** streams the cached 16 kHz mono PCM
at a strict 1.0× wall-clock pace (40 ms chunks, gapless, never faster than
realtime, never skipping bytes) to whichever arms are listening — the same way
a real capture client feeds a live mic to the providers.

- **A**: Speechmatics realtime via the native `speechmatics-rt` SDK (`AsyncClient`,
  `wss://eu.rt.speechmatics.com/v2`), default `melia-1` + `language: "multi"`
- **B**: Gladia live v2 via the native `gladiaio-sdk` (`GladiaClient.live_v2_async()`),
  default `solaria-1` + code switching

## Run

```bash
cd workbench
uv run python server.py        # http://127.0.0.1:8000
```

First run converts `../sermon_full.WAV` (48 kHz stereo) to a cached 16 kHz mono
PCM file (`cache/`, gitignored) and writes playable wavs + `audio_out/manifest.json`.
Keys are read from `../.env`.

## API

```
GET  /health                          ok + which API keys are present
GET  /sources                         full file + 60 s segments (id, start, duration)
POST /source            {"id": "full"|"seg_XX"|"random"}
GET  /audio/{name}                    playback wav (full.wav, seg_XX.wav) - human source of truth
GET  /state                           {source, running, position, listening:{A,B}}
POST /sim/start         {"offset"?}   start/resume the simulated mic (re-anchors if offset differs)
POST /sim/stop                        pause the mic, keep position + transcripts
POST /listen/start      {"model":"A"|"B", "offset"?}   open that arm's native-SDK session
POST /listen/stop       {"model":"A"|"B"}              close it (sim auto-pauses if none left)
GET  /configs
POST /config            {"model":"A"|"B", "config":{...}}   validated; restarts a listening arm
GET  /transcripts?model=A&partial=0   stored transcript lines (file-absolute t0/t1)
POST /transcripts/clear {"model":"A"|"B"}
GET  /events                          SSE push: state changes, provider events (drops),
                                      transcript lines - connect to watch /state live
```

Typical flow: `POST /source {"id":"seg_00"}` → `POST /listen/start {"model":"A","offset":0}`
(same for B) → `POST /sim/start` → poll `GET /state` + `GET /transcripts` →
`POST /sim/stop`. Transcript lines carry file-absolute timestamps, so you can
jump to `t0` in any external player of the wav. Every stored line is also
appended to `results/live-<source>.jsonl`.

Semantics: `sim/stop` never closes provider sessions — the mic just goes quiet
("muted mic") and `sim/start` resumes feeding the same sessions. If the
**provider** drops a session (idle timeout, error), that arm is marked
not-listening in `/state` (`listening` + `sessions` flags); we respect the drop
and never silently reconnect — resume explicitly with `POST /listen/start`.

## Smoke test

```bash
cd workbench && uv run python server.py &   # in another shell:
uv run python smoke_test.py
```

Drives the full flow over REST against both live providers and checks
timestamps after re-anchoring at 30 s.

## Layout

- `server.py` — REST API, simulated-mic sender loop, session orchestration, transcript store
- `providers.py` — provider sessions on the native SDKs (speechmatics-rt, gladiaio-sdk)
- `audio.py` — 48k stereo → 16k mono cache, segment wavs, manifest
- `smoke_test.py` — end-to-end REST test against a running server
- `TODO.md` — open issues
