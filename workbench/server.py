"""Relay A/B workbench - backend only (no frontend).

A simulated live microphone streams the cached 16 kHz mono PCM to whichever
provider arms are listening, through the OFFICIAL native clients
(speechmatics-rt / gladiaio-sdk, see providers.py), and transcripts are stored
server-side and served over REST.

The simulated mic reads the file the way a capture library reads a real mic:
one 40 ms chunk every 40 ms of wall clock, gapless, never faster than realtime,
never skipping bytes. If a send stalls, the stream lags (logged past 2 s) -
exactly like a real mic over a congested network.

Run:  cd workbench && uv run python server.py     -> http://127.0.0.1:8000

REST API (all bodies/returns JSON):
  GET  /health
  GET  /sources                          selectable sources (full + 60 s segments)
  POST /source            {"id": "full"|"seg_XX"|"random"}
  GET  /audio/{name}                     playback wavs (full.wav, seg_XX.wav)
  GET  /state                            {source, running, position, listening}
  POST /sim/start         {"offset"?}    start/resume the mic; re-anchors if offset differs
  POST /sim/stop                         pause the mic, keep position + transcripts
  POST /listen/start      {"model": "A"|"B", "offset"?}   open that arm's provider session
  POST /listen/stop       {"model": "A"|"B"}              close it (sim auto-pauses if none left)

If the PROVIDER drops a session (idle timeout, error), that arm is marked
not-listening - we respect the drop and never silently reconnect. Resume
explicitly with POST /listen/start (fresh session at the current sim position).
POST /sim/stop never closes sessions: the mic just goes quiet ("muted mic").
  GET  /configs
  POST /config            {"model": "A"|"B", "config": {...}}   validated; restarts a listening arm
  GET  /transcripts?model=A&partial=0    stored transcript lines (also persisted to results/*.jsonl)
  POST /transcripts/clear {"model": "A"|"B"}
  GET  /events                           SSE push stream: state changes, provider events
                                         (drops/errors), transcript lines
"""

import asyncio
import json
import os
import random
import time
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

import audio
from providers import DEFAULT_CONFIGS, ProviderSession

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
KEYS = {
    "A": os.environ.get("SPEECHMATICS_API_KEY", ""),
    "B": os.environ.get("GLADIA_API_KEY", ""),
}

BASE = Path(__file__).resolve().parent
OUT = BASE / "audio_out"
RESULTS = BASE / "results"
CHUNK = 640  # 40 ms of 16 kHz s16le mono - the cadence a real capture client sends
LAG_WARN = 2.0  # warn if the simulated mic falls more than this far behind wall clock

state = {
    "manifest": None,
    "source": None,  # {"id","label","start","duration","file"} start = offset in full file
    "position": 0.0,
    "running": False,  # simulated mic is streaming
    "listening": {"A": False, "B": False},
    "sessions": {"A": None, "B": None},
    "configs": {"A": DEFAULT_CONFIGS["A"], "B": DEFAULT_CONFIGS["B"]},
    "sender": None,
    "log": {"A": [], "B": []},
}

SENT_END = (".", "!", "?", "।")  # sentence-final punctuation incl. Hindi danda
pending = {"A": None, "B": None}  # per-model sentence accumulator

# SSE subscribers: front ends connect to GET /events and get pushed state
# changes, provider events (incl. drops), and transcript lines in real time
subscribers: set[asyncio.Queue] = set()


def log(msg: str) -> None:
    print(msg, flush=True)


def broadcast(msg: dict) -> None:
    for q in list(subscribers):
        q.put_nowait(msg)


def broadcast_state() -> None:
    broadcast({"type": "state", **state_json()})


# ------------------------------------------------------------ transcripts

def persist(line: dict) -> None:
    try:
        RESULTS.mkdir(exist_ok=True)
        src = state["source"]["id"] if state["source"] else "none"
        with (RESULTS / f"live-{src}.jsonl").open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"persist failed: {e}")


def store_line(model: str, t0: float, t1: float, text: str, partial: bool) -> dict:
    line = {"wall": round(time.time(), 3), "model": model, "t0": t0, "t1": t1,
            "text": text, "partial": partial}
    state["log"][model].append(line)
    persist(line)
    broadcast({"type": "line", "line": line})
    return line


def on_provider_event(ev: dict) -> None:
    """Sync callback from provider sessions. Buffers finals into sentence lines;
    only finalized lines are stored (sentence ends with punctuation, or flush
    on sim stop/end). Partials are stored too, flagged, if a config enables them.

    If the PROVIDER drops the session (closed/error, not our own close), that
    arm stops listening - we respect the drop instead of silently reconnecting;
    resume explicitly with POST /listen/start."""
    if ev["type"] in ("closed", "error"):
        m = ev.get("model")
        log(f"provider event: {ev.get('type')} {m} {str(ev.get('message',''))[:160]}")
        broadcast({"type": "provider", "event": ev})
        if m in ("A", "B") and not ev.get("local") and state["listening"][m]:
            s = state["sessions"][m]
            if s is None or not s.open:
                state["listening"][m] = False
                state["sessions"][m] = None
                log(f"provider dropped session {m} at pos {state['position']:.1f}s - "
                    f"arm {m} no longer listening; POST /listen/start to resume")
                broadcast_state()
        return
    if ev["type"] != "transcript":
        log(f"provider event: {ev.get('type')} {ev.get('model','')} {str(ev.get('message',''))[:160]}")
        return
    m = ev["model"]
    if ev.get("partial"):
        store_line(m, ev["t0"], ev["t1"], ev["text"], partial=True)
        return
    p = pending[m]
    if p is None:
        p = {"t0": ev["t0"], "t1": ev["t1"], "text": ev["text"].strip()}
    else:
        p["text"] = (p["text"].rstrip() + " " + ev["text"].strip()).strip()
        p["t1"] = ev["t1"]
    text = p["text"]
    if text and text[-1] in SENT_END:
        pending[m] = None
        line = store_line(m, p["t0"], p["t1"], text, partial=False)
        log(f"final {m} t={p['t0']:.1f} :: {text[:80]}")
    else:
        # keep accumulating silently; cap the buffer so lines still appear when
        # the model emits long stretches without sentence-ending punctuation
        pending[m] = p
        if (p["t1"] - p["t0"] > 10) or len(text) > 160:
            pending[m] = None
            store_line(m, p["t0"], p["t1"], text, partial=False)


def flush_pending() -> None:
    """Promote any unfinished sentence buffer to a stored line (sim stop/end)."""
    for m in ("A", "B"):
        p = pending[m]
        if p and p["text"].strip():
            pending[m] = None
            store_line(m, p["t0"], p["t1"], p["text"], partial=False)


# ------------------------------------------------------------ sim + sessions

def resolve_source(id: str) -> dict:
    m = state["manifest"]
    if id == "random":
        seg = random.choice(m["segments"])
        return {**seg}
    if id == "full":
        return {**m["full"]}
    seg = next(s for s in m["segments"] if s["id"] == id)
    return {**seg}


def start_sender() -> None:
    if state["sender"] is not None and not state["sender"].done():
        state["sender"].cancel()
    state["running"] = True
    state["sender"] = asyncio.create_task(sender_loop())


async def stop_sender() -> None:
    state["running"] = False
    if state["sender"] is not None:
        state["sender"].cancel()
        try:
            await state["sender"]
        except asyncio.CancelledError:
            pass
        state["sender"] = None


async def close_sessions(fast: bool = False) -> None:
    for m in ("A", "B"):
        s = state["sessions"][m]
        if s is not None:
            await s.close(fast=fast)
        state["sessions"][m] = None
        pending[m] = None


def session_healthy(s) -> bool:
    return (
        s is not None and s.open
        and (time.monotonic() - s.last_rx) < 45
    )


async def open_session(model: str, target: float) -> bool:
    """Open (or keep) this arm's provider session at absolute file position target.
    The caller must have stopped the sender first, so target is exactly the
    position of the first chunk this session will receive (audio sent before
    the provider confirms ready is dropped by the provider)."""
    s = state["sessions"][model]
    if session_healthy(s) and abs(s.expect_pos - target) < 0.5:
        return True  # healthy session already at this position: keep it
    if s is not None:
        await s.close(fast=True)
    try:
        s = ProviderSession(model, KEYS[model], state["configs"][model], target, on_provider_event)
        await s.start()
        s.expect_pos = target
        state["sessions"][model] = s
    except Exception as e:
        state["sessions"][model] = None
        log(f"session {model} start FAILED: {e}")
        return False
    log(f"session {model} listening at {target:.1f}s")
    return True


async def sender_loop() -> None:
    """The simulated mic. One CHUNK (40 ms of audio) every 40 ms of wall clock,
    gapless, sent only to listening arms. Tracks an integer byte offset: float
    second offsets drift (0.1 accumulates to 0.7999999999999999 etc.) and
    corrupt the PCM stream."""
    src = state["source"]
    byte_pos = int(round((src["start"] + state["position"]) * audio.BYTES_PER_SEC))
    next_t = time.monotonic()
    warned = False
    f = audio.RAW16K.open("rb")  # one persistent handle for the whole stream
    try:
        f.seek(byte_pos)
        while state["running"]:
            if state["source"] is not src:
                src = state["source"]
            pos = byte_pos / audio.BYTES_PER_SEC - src["start"]
            if pos >= src["duration"] - 1e-6:
                break
            chunk = f.read(CHUNK)
            if not chunk:
                break
            for m in ("A", "B"):
                if state["listening"][m]:
                    s = state["sessions"][m]
                    if s is not None and s.open:
                        await s.send_audio(chunk)
            byte_pos += len(chunk)
            dur = len(chunk) / 2 / audio.SAMPLE_RATE
            state["position"] = byte_pos / audio.BYTES_PER_SEC - src["start"]
            for m in ("A", "B"):
                s = state["sessions"][m]
                if s is not None:
                    s.expect_pos = src["start"] + state["position"]
            next_t += dur
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # fell behind: keep the stream gapless and let the lag persist;
                # re-anchor the schedule (timing only - no audio bytes are skipped)
                if -delay > LAG_WARN and not warned:
                    log(f"sim lagging {-delay:.1f}s behind wall clock")
                    warned = True
                next_t = time.monotonic()
    except asyncio.CancelledError:
        return
    finally:
        f.close()
    await end_source()


async def end_source() -> None:
    state["running"] = False
    await close_sessions()
    flush_pending()
    log(f"sim reached end of source at {state['position']:.1f}s")


# ------------------------------------------------------------ HTTP helpers

async def body(request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def state_json() -> dict:
    return {
        "source": state["source"],
        "running": state["running"],
        "position": state["position"],
        "listening": dict(state["listening"]),
        "sessions": {m: bool(state["sessions"][m] and state["sessions"][m].open) for m in ("A", "B")},
    }


def json_resp(data, status=200):
    return web.json_response(data, status=status)


async def bad(msg: str, status=400):
    return json_resp({"error": msg}, status=status)


# ------------------------------------------------------------ routes

async def health(request):
    return json_resp({"ok": True, "keys": {k: bool(v) for k, v in KEYS.items()}})


async def sources(request):
    return json_resp(state["manifest"])


async def source_post(request):
    b = await body(request)
    id = str(b.get("id", "full"))
    try:
        src = resolve_source(id)
    except StopIteration:
        return await bad(f"unknown source id: {id}")
    await stop_sender()
    await close_sessions(fast=True)
    flush_pending()
    state["log"] = {"A": [], "B": []}
    state["listening"] = {"A": False, "B": False}
    state["source"] = src
    state["position"] = 0.0
    log(f"source selected: {src['id']} ({src['label']})")
    broadcast_state()
    return json_resp({"source": src})


async def audio_file(request):
    name = request.match_info["name"]
    base = OUT.resolve()
    p = (base / name).resolve()
    if p.parent != base or not p.is_file():
        return await bad("no such file", 404)
    return web.FileResponse(p)


async def state_get(request):
    broadcast_state()
    return json_resp(state_json())


async def sim_start(request):
    b = await body(request)
    if state["source"] is None:
        return await bad("select a source first (POST /source)")
    offset = b.get("offset")
    if offset is not None:
        offset = min(max(0.0, float(offset)), state["source"]["duration"])
        if state["running"] and abs(state["position"] - offset) > 0.5:
            # re-anchor the whole sim at offset; re-open every listening arm there
            arms = [m for m in ("A", "B") if state["listening"][m]]
            await stop_sender()
            await close_sessions(fast=True)
            for m in ("A", "B"):
                state["listening"][m] = False
            state["position"] = offset
            for m in arms:
                state["listening"][m] = await open_session(m, state["source"]["start"] + offset)
            log(f"sim re-anchored at {offset:.1f}s")
        elif not state["running"]:
            state["position"] = offset
    if not state["running"]:
        # NOTE: we do NOT re-open dropped sessions here. If the provider dropped
        # a session (e.g. idle timeout during a sim pause), that arm was already
        # marked not-listening when the drop was seen; resume it explicitly with
        # POST /listen/start.
        for m in ("A", "B"):
            if state["listening"][m] and not session_healthy(state["sessions"][m]):
                log(f"arm {m} is marked listening but its session is gone - "
                    f'POST /listen/start {{"model": "{m}"}} to resume')
        start_sender()
        log(f"sim started at {state['position']:.1f}s")
    broadcast_state()
    return json_resp(state_json())


async def sim_stop(request):
    await stop_sender()
    flush_pending()
    log(f"sim paused at {state['position']:.1f}s")
    broadcast_state()
    return json_resp(state_json())


async def listen_start(request):
    b = await body(request)
    model = str(b.get("model", ""))
    if model not in ("A", "B"):
        return await bad('model must be "A" or "B"')
    if state["source"] is None:
        return await bad("select a source first (POST /source)")
    offset = b.get("offset")
    was_running = state["running"]
    if offset is not None and not was_running:
        state["position"] = min(max(0.0, float(offset)), state["source"]["duration"])
    if was_running:
        if state["listening"][model] and session_healthy(state["sessions"][model]):
            broadcast_state()
            return json_resp(state_json())  # already listening: no-op
        # pause the stream while the session connects so base_offset is exact
        await stop_sender()
    ok = await open_session(model, state["source"]["start"] + state["position"])
    state["listening"][model] = ok
    if was_running:
        start_sender()
    if not ok:
        broadcast_state()
        return json_resp({"error": f"session {model} failed to start", "state": state_json()}, status=502)
    broadcast_state()
    return json_resp(state_json())


async def listen_stop(request):
    b = await body(request)
    model = str(b.get("model", ""))
    if model not in ("A", "B"):
        return await bad('model must be "A" or "B"')
    state["listening"][model] = False
    s = state["sessions"][model]
    if s is not None:
        await s.close(fast=True)
        state["sessions"][model] = None
    pending[model] = None
    if not any(state["listening"].values()) and state["running"]:
        await stop_sender()  # no listeners: pause the sim, keep position + transcripts
        flush_pending()
    broadcast_state()
    return json_resp(state_json())


async def configs_get(request):
    return json_resp(state["configs"])


MELIA1_FORBIDDEN = {"max_delay", "max_delay_mode", "punctuation_overrides", "transcript_filtering_config"}


async def config_post(request):
    b = await body(request)
    model = str(b.get("model", ""))
    cfg = b.get("config")
    if model not in ("A", "B") or not isinstance(cfg, dict):
        return await bad('need {"model": "A"|"B", "config": {...}}')
    # reject Enhanced-only knobs on melia-1 up front with a clear message,
    # instead of letting the vendor return a protocol_error mid-session
    if model == "A" and (cfg.get("model") == "melia-1" or cfg.get("transcription_config", {}).get("model") == "melia-1"):
        tc = cfg.get("transcription_config", cfg)
        bad_keys = MELIA1_FORBIDDEN & set(tc.keys())
        if bad_keys:
            return await bad(f"melia-1 does not allow: {', '.join(sorted(bad_keys))} (Enhanced-only knobs)")
    state["configs"][model] = cfg
    if state["listening"][model] and state["source"] is not None:
        # restart this arm at the current position so the new config takes effect now
        was_running = state["running"]
        if was_running:
            await stop_sender()
        s = state["sessions"][model]
        if s is not None:
            await s.close(fast=True)
            state["sessions"][model] = None
        ok = await open_session(model, state["source"]["start"] + state["position"])
        state["listening"][model] = ok
        if was_running:
            start_sender()
    broadcast_state()
    return json_resp({"applied": model, "config": cfg})


async def transcripts_get(request):
    model = request.query.get("model")
    partial = request.query.get("partial")
    lines = []
    for m in ([model] if model in ("A", "B") else ["A", "B"]):
        for line in state["log"][m]:
            if partial is not None and str(line["partial"]) != partial:
                continue
            lines.append(line)
    lines.sort(key=lambda l: l["t0"])
    return json_resp({"count": len(lines), "lines": lines})


async def transcripts_clear(request):
    b = await body(request)
    model = str(b.get("model", ""))
    if model not in ("A", "B"):
        return await bad('model must be "A" or "B"')
    state["log"][model] = []
    pending[model] = None
    broadcast_state()
    return json_resp({"cleared": model})


async def events(request):
    """SSE stream: pushes {"type":"state",...} on every state change,
    {"type":"provider","event":...} for provider events (drops/errors),
    {"type":"line","line":...} for each stored transcript line."""
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)
    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
    )
    await resp.prepare(request)
    await resp.write(f"data: {json.dumps({'type': 'state', **state_json()})}\n\n".encode())
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                await resp.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")  # keep-alive comment
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        subscribers.discard(q)
    return resp


# ------------------------------------------------------------ app

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/sources", sources)
    app.router.add_post("/source", source_post)
    app.router.add_get("/audio/{name}", audio_file)
    app.router.add_get("/state", state_get)
    app.router.add_post("/sim/start", sim_start)
    app.router.add_post("/sim/stop", sim_stop)
    app.router.add_post("/listen/start", listen_start)
    app.router.add_post("/listen/stop", listen_stop)
    app.router.add_get("/configs", configs_get)
    app.router.add_post("/config", config_post)
    app.router.add_get("/transcripts", transcripts_get)
    app.router.add_post("/transcripts/clear", transcripts_clear)
    app.router.add_get("/events", events)
    return app


async def serve() -> None:
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8000)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        if e.errno == 48:
            raise SystemExit("port 8000 already in use - another server.py is probably running")
        raise
    log("workbench backend ready at http://127.0.0.1:8000")
    await asyncio.Event().wait()


def main() -> None:
    if not KEYS["A"] or not KEYS["B"]:
        raise SystemExit("missing SPEECHMATICS_API_KEY / GLADIA_API_KEY in ../.env")
    audio.ensure_16k_mono()
    state["manifest"] = audio.ensure_segments()
    state["source"] = resolve_source("full")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
