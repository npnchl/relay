# Workbench to-do — issues to fix

## 1. Audio input doesn't fully behave like real live audio

- `server.py:49` — streaming runs at `PACE = 1.05`, i.e. deliberately 5% slower than realtime, and when the sender falls behind it resyncs the clock and bursts chunks. A real live stream is exactly 1.0x wall-clock and never bursts.
- `server.py:48` — chunks are 100 ms (`CHUNK = 3200` bytes), coarse compared to typical 20–50 ms live frames, so latency/behavior differs from a real mic stream.
- The browser `<audio>` element and the server's sender run on two separate clocks and drift apart over the file, so what you hear and what the models receive stop lining up.
- Pause keeps provider sessions open while sending nothing — a hybrid that matches neither a live stream nor a closed session.

## 2. Models listening is automated, not manual

- Pressing Play automatically starts provider sessions for both arms (`cmd_play` in `server.py`) — there is no way to make a model listen on your own.
- No per-model listen / stop-listening control; listening cannot be started without pressing play on the audio.

## 3. Not using the native provider clients

- `providers.py` uses raw `aiohttp` websockets for both Speechmatics and Gladia instead of the official SDKs (`speechmatics-rt`, `gladiaio-sdk`).
- The SDKs are not in `pyproject.toml` dependencies and not installed in the venv.

## 4. Not all provider settings are exposed

- `web/viewport.js` (`FIELDS`) exposes only 7 hand-picked fields per arm, and `buildConfig` can only emit that fixed shape — no raw-JSON editing.
- Many Speechmatics realtime settings are unreachable from the UI (e.g. operating point, diarization, entities, language identification, audio events, translation config, channel settings).
- Many Gladia live v2 settings are unreachable from the UI (e.g. encoding/bit depth/sample rate/channels, pre-processing beyond the enhancer toggle, custom vocabulary, custom spelling, translation, diarization, the other `messages_config` receive flags).
