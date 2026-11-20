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
  GET  /                                 the frontend (web/)
  GET  /static/{name}                    frontend assets (no-store, cache-busted)
  GET  /health
  GET  /sources                          selectable sources (full + 60 s segments)
  POST /source            {"id": "full"|"seg_XX"|"random"}
  GET  /audio/{name}                     playback wavs (full.wav, seg_XX.wav)
  GET  /state                            {source, running, position, listening}
  POST /sim/start         {"offset"?}    start/resume the mic; re-anchors if offset differs
  POST /sim/stop                         pause the mic, keep position + transcripts
  POST /sim/seek          {"offset"}     scrub the mic position (re-anchors listening arms)
  POST /listen/start      {"model": "A"|"B", "offset"?}   open that arm's provider session
  POST /listen/stop       {"model": "A"|"B"}              close it (sim auto-pauses if none left)

If the PROVIDER drops a session (idle timeout, error), that arm is marked
not-listening - we respect the drop and never silently reconnect. Resume
explicitly with POST /listen/start (fresh session at the current sim position).
POST /sim/stop never closes sessions: the mic just goes quiet ("muted mic").
  GET  /configs
  GET  /config/schema                    all provider settings, from the SDK config dataclasses
  POST /config            {"model": "A"|"B", "config": {...}}   validated; restarts a listening arm
  GET  /transcripts?model=A&partial=0    stored transcript lines (also persisted to results/*.jsonl)
  POST /transcripts/clear {"model": "A"|"B"}
  GET  /events                           SSE push stream: state changes, provider events
                                         (drops/errors), transcript lines
"""

import asyncio
import dataclasses
import enum
import hashlib
import json
import os
import random
import re
import time
import typing
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from gladiaio_sdk import LiveV2InitRequest
from speechmatics.rt import TranscriptionConfig

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
WEB = BASE / "web"
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
    "ended": False,  # sim reached the end of the source
    "suppress": False,  # noise-suppression toggle from the sim card; sim-side DSP
                       # algorithm is intentionally not implemented yet - the flag
                       # is stored + broadcast so it is ready when it lands
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
        log(f"provider event: {ev.get('type')} {m} local={ev.get('local')} "
            f"pos={state['position']:.1f} {str(ev.get('message',''))[:160]}")
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
    last_bcast = 0.0
    f = audio.RAW16K.open("rb")  # one persistent handle for the whole stream
    try:
        f.seek(byte_pos)
        while state["running"]:
            if time.monotonic() - last_bcast >= 1.0:
                last_bcast = time.monotonic()
                broadcast_state()  # live sim position for the UI (~1 Hz)
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
    state["ended"] = True
    await close_sessions()
    flush_pending()
    log(f"sim reached end of source at {state['position']:.1f}s")
    broadcast_state()


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
        "ended": state["ended"],
        "suppress": state["suppress"],
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
    state["ended"] = False
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


async def reanchor(offset: float) -> None:
    """Move the sim position to `offset`: stop the mic, close all sessions and
    re-open every listening arm at the new position. A provider session cannot
    teleport - its base_offset must match the first chunk it receives, so any
    position change (seek, or start with a different offset) re-anchors arms."""
    arms = [m for m in ("A", "B") if state["listening"][m]]
    was_running = state["running"]
    await stop_sender()
    flush_pending()
    await close_sessions(fast=True)
    for m in ("A", "B"):
        state["listening"][m] = False
    state["position"] = offset
    state["ended"] = False
    for m in arms:
        state["listening"][m] = await open_session(m, state["source"]["start"] + offset)
    if was_running:
        start_sender()
    log(f"sim re-anchored at {offset:.1f}s (was_running={was_running})")


async def sim_start(request):
    b = await body(request)
    if state["source"] is None:
        return await bad("select a source first (POST /source)")
    offset = b.get("offset")
    if offset is None and state["ended"]:
        offset = 0.0  # Replay: sim reached the end, so restart from the beginning
    if offset is not None:
        offset = min(max(0.0, float(offset)), state["source"]["duration"])
        if abs(state["position"] - offset) > 0.5:
            await reanchor(offset)
    if not state["running"]:
        # NOTE: we do NOT re-open dropped sessions here. If the provider dropped
        # a session, that arm was already marked not-listening when the drop was
        # seen; resume it explicitly with POST /listen/start.
        state["ended"] = False
        start_sender()
        log(f"sim started at {state['position']:.1f}s")
    broadcast_state()
    return json_resp(state_json())


async def sim_seek(request):
    """Scrub the simulated mic position (running or paused)."""
    b = await body(request)
    if state["source"] is None:
        return await bad("select a source first (POST /source)")
    if b.get("offset") is None:
        return await bad('need {"offset": seconds}')
    offset = min(max(0.0, float(b["offset"])), state["source"]["duration"])
    await reanchor(offset)
    broadcast_state()
    return json_resp(state_json())


async def sim_stop(request):
    await stop_sender()
    flush_pending()
    log(f"sim paused at {state['position']:.1f}s")
    broadcast_state()
    return json_resp(state_json())


async def sim_suppress(request):
    """Toggle noise suppression. Source-player filtering is client-side; the sim
    side stores the flag for now (DSP algorithm deliberately not implemented yet)."""
    b = await body(request)
    state["suppress"] = bool(b.get("on"))
    broadcast_state()
    return json_resp({"suppress": state["suppress"]})


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
    state["listening"][model] = True
    broadcast_state()  # intent immediately - UIs update without waiting for the connect
    ok = await open_session(model, state["source"]["start"] + state["position"])
    state["listening"][model] = ok
    if was_running:
        start_sender()
    broadcast_state()
    if not ok:
        return json_resp({"error": f"session {model} failed to start", "state": state_json()}, status=502)
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
    errs = _validate_config(model, cfg)
    if errs:
        return await bad("invalid config: " + "; ".join(errs[:6]))
    state["configs"][model] = cfg
    broadcast_state()  # config stored; session restart below may take seconds
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
    broadcast({"type": "cleared", "model": model})
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


# ------------------------------------------------------------ static frontend

NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def asset_version() -> str:
    """Hash of the web/ files' names+mtimes+sizes, so asset URLs change whenever
    any frontend file is edited (busts browser caches without a restart)."""
    h = hashlib.sha1()
    if WEB.is_dir():
        for p in sorted(WEB.iterdir()):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:12]


async def index(request):
    html = (WEB / "index.html").read_text()
    html = re.sub(r"(/static/[a-z]+\.(?:js|css))", rf"\1?v={asset_version()}", html)
    return web.Response(text=html, content_type="text/html", headers=NO_CACHE)


async def static_handler(request):
    name = request.match_info["name"]
    p = (WEB / name).resolve()
    if WEB.resolve() not in p.parents or not p.is_file():
        return await bad("no such file", 404)
    return web.FileResponse(p, headers=NO_CACHE)


# ------------------------------------------------------------ config schema

def _enum_entry(vals: list) -> dict:
    e = {"type": "enum", "enum": vals}
    if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        e["vtype"] = "number"  # frontend must emit numbers, not strings
    return e


def _type_entry(tp, depth: int) -> dict:
    args = typing.get_args(tp)
    origin = typing.get_origin(tp)
    if origin is type(None) or tp is type(None):
        return {"type": "null"}
    if type(None) in args:  # unwrap Optional[X]
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_entry(non_none[0], depth)
    if origin is typing.Literal:  # Literal["a","b"] -> select
        return _enum_entry(list(args))
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return _enum_entry([m.value for m in tp])
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        lits = [v for a in non_none if typing.get_origin(a) is typing.Literal for v in typing.get_args(a)]
        if lits and all(typing.get_origin(a) is typing.Literal or a is str for a in non_none):
            entry = _enum_entry(lits)
            if str in non_none:  # e.g. Literal["solaria-1"] | str -> suggestions, but any string ok
                entry["free_str"] = True
            return entry
        if len(non_none) == 1:
            return _type_entry(non_none[0], depth)
        return {"type": "any"}
    if origin is dict or tp is dict:
        return {"type": "dict"}
    if origin is list or tp is list:
        return {"type": "list"}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp) and depth < 3:
        return {"type": "object", "fields": _field_schema(tp, depth + 1)}
    return {"type": getattr(tp, "__name__", str(tp))}


def _field_schema(cls, depth: int = 0) -> dict:
    """Serialize a dataclass into a UI schema: fields with type/enum/default and
    nested object fields (recursive). This is what exposes ALL provider settings."""
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}
    out = {}
    for f in dataclasses.fields(cls):
        if f.name.startswith("_"):
            continue
        tp = hints.get(f.name, f.type)
        entry = _type_entry(tp, depth)
        if f.default is not dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            try:
                json.dumps(f.default)
                entry["default"] = f.default.value if isinstance(f.default, enum.Enum) else f.default
            except Exception:
                pass
        out[f.name] = entry
    return out


def _validate_config(model: str, cfg: dict) -> list[str]:
    """Type-check a config against the provider's SDK schema; returns error strings."""
    cls = TranscriptionConfig if model == "A" else LiveV2InitRequest
    fields = _field_schema(cls)
    errs: list[str] = []

    def walk(flds, obj, prefix):
        if not isinstance(obj, dict):
            errs.append(f"{prefix or 'config'}: expected an object, got {type(obj).__name__}")
            return
        for k, v in obj.items():
            spec = flds.get(k)
            if spec is None:
                continue
            path = f"{prefix}.{k}" if prefix else k
            t = spec["type"]
            if t == "object" and "fields" in spec:
                if not isinstance(v, dict):
                    errs.append(f"{path}: expected an object, got {type(v).__name__}")
                else:
                    walk(spec["fields"], v, path)
            elif t == "dict" and not isinstance(v, dict):
                errs.append(f"{path}: expected an object, got {type(v).__name__}")
            elif t == "list" and not isinstance(v, list):
                errs.append(f"{path}: expected a list, got {type(v).__name__}")
            elif t == "enum":
                ev = v
                if spec.get("vtype") == "number" and isinstance(ev, str):
                    # tolerate numeric strings from hand-rolled API clients
                    try:
                        ev = int(ev) if ev.strip().lstrip("+-").isdigit() else float(ev)
                    except ValueError:
                        pass
                if v is not None and not spec.get("free_str") and ev not in spec["enum"]:
                    errs.append(f"{path}: expected one of {spec['enum']}, got {v!r}")
                elif spec.get("free_str") and v is not None and not isinstance(v, str):
                    errs.append(f"{path}: expected a string, got {type(v).__name__}")
            elif t in ("int", "float") and v is not None:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    errs.append(f"{path}: expected a number, got {type(v).__name__}")
                elif t == "int" and isinstance(v, float) and not v.is_integer():
                    errs.append(f"{path}: expected an integer, got {v}")
            elif t == "bool" and not isinstance(v, bool):
                errs.append(f"{path}: expected a boolean, got {type(v).__name__}")
            elif t == "str" and v is not None and not isinstance(v, str):
                errs.append(f"{path}: expected a string, got {type(v).__name__}")

    walk(fields, cfg, "")
    return errs


async def config_schema(request):
    return json_resp({
        "A": {"root": "TranscriptionConfig (speechmatics-rt)", "fields": _field_schema(TranscriptionConfig)},
        "B": {"root": "LiveV2InitRequest (gladiaio-sdk)", "fields": _field_schema(LiveV2InitRequest)},
    })


# ------------------------------------------------------------ app

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/static/{name:.+}", static_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/sources", sources)
    app.router.add_post("/source", source_post)
    app.router.add_get("/audio/{name}", audio_file)
    app.router.add_get("/state", state_get)
    app.router.add_post("/sim/start", sim_start)
    app.router.add_post("/sim/stop", sim_stop)
    app.router.add_post("/sim/suppress", sim_suppress)
    app.router.add_post("/sim/seek", sim_seek)
    app.router.add_post("/listen/start", listen_start)
    app.router.add_post("/listen/stop", listen_stop)
    app.router.add_get("/configs", configs_get)
    app.router.add_get("/config/schema", config_schema)
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
