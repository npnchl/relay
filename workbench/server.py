"""Relay A/B workbench server.

One process serves the static frontend, the playback audio (with HTTP Range
support so the scrubber works), and a single browser websocket (/ws). It owns
the provider API keys, paces identical 16 kHz mono PCM to both providers at
realtime speed, and relays transcripts to the browser.

Run:  cd workbench && uv run python server.py     -> http://127.0.0.1:8000

Browser -> server commands (JSON over /ws):
  {"cmd":"select","id":"full"|"seg_XX"|"random"}
  {"cmd":"shuffle"}                       pick another random segment
  {"cmd":"play","offset":seconds}         start/resume streaming + sessions
  {"cmd":"pause"}                         stop streaming, keep sessions+transcripts
  {"cmd":"seek","offset":seconds}         close sessions, clear both viewports
  {"cmd":"config","model":"A"|"B","config":{...}}   provider config JSON
  {"cmd":"clear","model":"A"|"B"}

Server -> browser messages:
  hello, source, state, transcript, cleared, config_applied, config_error, error, closed
"""

import asyncio
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from dotenv import load_dotenv

import audio
from providers import DEFAULT_CONFIGS, ProviderSession

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
KEYS = {
    "A": os.environ.get("SPEECHMATICS_API_KEY", ""),
    "B": os.environ.get("GLADIA_API_KEY", ""),
}

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
OUT = BASE / "audio_out"
CHUNK = 3200  # 100 ms of 16 kHz s16le mono
PACE = 1.05  # send slightly slower than realtime; at exactly 1.0x the providers'
             # buffers can underrun on jitter and they silently drop speech

state = {
    "manifest": None,
    "source": None,  # {"id","label","url","duration","base"} base = offset in full file
    "position": 0.0,
    "playing": False,
    "sessions": {"A": None, "B": None},
    "configs": {"A": DEFAULT_CONFIGS["A"], "B": DEFAULT_CONFIGS["B"]},
    "sender": None,
    "log": {"A": [], "B": []},
    "browser": None,
    "http": None,
}


SENT_END = (".", "!", "?", "।")  # sentence-final punctuation incl. Hindi danda
pending = {"A": None, "B": None}  # per-model sentence accumulator


async def emit(msg: dict) -> None:
    ws = state["browser"]
    if ws is not None and not ws.closed:
        try:
            await ws.send_json(msg)
        except Exception:
            pass


def _log_line(model: str, t0: float, t1: float, text: str) -> None:
    state["log"][model].append({"t0": t0, "t1": t1, "text": text})


def log(msg: str) -> None:
    print(msg, flush=True)


async def on_provider_event(ev: dict) -> None:
    """Buffer provider finals into sentence lines and emit only finalized lines
    (sentence ends with punctuation, or flush on pause/end). Provider-level
    partials, if a config enables them, pass straight through to the UI."""
    if ev["type"] != "transcript":
        log(f"provider event: {ev.get('type')} {ev.get('model','')} {str(ev.get('message',''))[:160]}")
        await emit(ev)
        return
    m = ev["model"]
    if ev.get("partial"):
        await emit(ev)
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
        _log_line(m, p["t0"], p["t1"], text)
        log(f"final {m} t={p['t0']:.1f} :: {text[:80]}")
        await emit({"type": "transcript", "model": m, "text": text, "t0": p["t0"], "t1": p["t1"], "partial": False})
    else:
        # keep accumulating silently; only finalized sentence lines reach the UI.
        # Cap the buffer so lines still appear when the model emits long stretches
        # without sentence-ending punctuation (melia-1 has no punctuation knob).
        pending[m] = p
        if (p["t1"] - p["t0"] > 10) or len(text) > 160:
            pending[m] = None
            _log_line(m, p["t0"], p["t1"], text)
            await emit({"type": "transcript", "model": m, "text": text, "t0": p["t0"], "t1": p["t1"], "partial": False})


async def flush_pending() -> None:
    """Promote any unfinished sentence buffer to a final line (pause/end)."""
    for m in ("A", "B"):
        p = pending[m]
        if p and p["text"].strip():
            pending[m] = None
            _log_line(m, p["t0"], p["t1"], p["text"])
            await emit({"type": "transcript", "model": m, "text": p["text"], "t0": p["t0"], "t1": p["t1"], "partial": False})


def resolve_source(id: str) -> dict:
    m = state["manifest"]
    if id == "random":
        seg = random.choice(m["segments"])
        return {**seg, "base": seg["start"]}
    if id == "full":
        return {**m["full"], "base": 0.0}
    seg = next(s for s in m["segments"] if s["id"] == id)
    return {**seg, "base": seg["start"]}


async def close_sessions(fast: bool = False) -> None:
    for m in ("A", "B"):
        s = state["sessions"][m]
        if s is not None:
            await s.close(fast=fast)
        state["sessions"][m] = None
        pending[m] = None


async def stop_sender() -> None:
    state["playing"] = False
    if state["sender"] is not None:
        state["sender"].cancel()
        try:
            await state["sender"]
        except asyncio.CancelledError:
            pass
        state["sender"] = None


async def cmd_play(offset: float) -> None:
    if state["source"] is None:
        return
    offset = min(max(0.0, offset), state["source"]["duration"])
    target = state["source"]["base"] + offset  # absolute position in the full file
    for m in ("A", "B"):
        s = state["sessions"][m]
        if (
            s is not None
            and s.open
            and s.ws is not None
            and not s.ws.closed
            and abs(s.expect_pos - target) < 0.5
            and (time.monotonic() - s.last_rx) < 45
        ):
            continue  # healthy session already at this position: resume it
        if s is not None:
            await s.close(fast=True)  # stale/dead session or wrong position: replace it
        try:
            s = ProviderSession(
                m, KEYS[m], state["configs"][m], target, on_provider_event,
            )
            await s.start(state["http"])
            s.expect_pos = target
            state["sessions"][m] = s
        except Exception as e:
            state["sessions"][m] = None
            log(f"session {m} start FAILED: {e}")
            await emit({"type": "error", "model": m, "message": f"start: {e}"})
        else:
            log(f"session {m} started at {offset:.1f}s")
            await emit({"type": "status", "model": m, "active": True})
    state["position"] = offset
    state["playing"] = True
    if state["sender"] is not None and not state["sender"].done():
        state["sender"].cancel()
    state["sender"] = asyncio.create_task(sender_loop())
    await emit({"type": "state", "playing": True, "position": offset})


async def cmd_pause() -> None:
    await stop_sender()
    await flush_pending()
    for m in ("A", "B"):
        await emit({"type": "status", "model": m, "active": False})  # sessions stay open, but nothing is processing
    await emit({"type": "state", "playing": False, "position": state["position"]})


async def cmd_seek(offset: float) -> None:
    await stop_sender()
    await close_sessions(fast=True)
    offset = min(max(0.0, offset), state["source"]["duration"] if state["source"] else 0)
    state["position"] = offset
    await emit({"type": "cleared", "model": "both"})
    await emit({"type": "state", "playing": False, "position": offset})


async def cmd_select(id: str) -> None:
    await stop_sender()
    await close_sessions(fast=True)
    state["log"] = {"A": [], "B": []}
    state["source"] = resolve_source(id)
    state["position"] = 0.0
    await emit({"type": "source", "source": state["source"]})
    await emit({"type": "cleared", "model": "both"})
    await emit({"type": "state", "playing": False, "position": 0})


MELIA1_FORBIDDEN = {"max_delay", "max_delay_mode", "punctuation_overrides", "transcript_filtering_config"}


async def cmd_config(model: str, cfg) -> None:
    if model not in ("A", "B") or not isinstance(cfg, dict):
        await emit({"type": "config_error", "model": model, "message": "bad config"})
        return
    # reject Enhanced-only knobs on melia-1 up front with a clear message,
    # instead of letting the vendor return a protocol_error mid-session
    tc = cfg.get("transcription_config") or {}
    if model == "A" and tc.get("model") == "melia-1":
        bad = MELIA1_FORBIDDEN & set(tc.keys())
        if bad:
            await emit({
                "type": "config_error", "model": model,
                "message": f"melia-1 does not allow: {', '.join(sorted(bad))} (Enhanced-only knobs)",
            })
            return
    state["configs"][model] = cfg
    s = state["sessions"][model]
    if state["playing"] and state["source"] is not None:
        # restart this arm at the current position so the new config takes
        # effect now, even if the old session already errored and closed
        if s is not None:
            await s.close(fast=True)
        try:
            s = ProviderSession(
                model, KEYS[model], cfg,
                state["source"]["base"] + state["position"], on_provider_event,
            )
            await s.start(state["http"])
            state["sessions"][model] = s
        except Exception as e:
            state["sessions"][model] = None
            await emit({"type": "config_error", "model": model, "message": f"restart: {e}"})
            return
    await emit({"type": "config_applied", "model": model})


async def handle_cmd(cmd: dict) -> None:
    c = cmd.get("cmd")
    log(f"cmd {c} {str(cmd)[:120]}")
    if c == "play":
        await cmd_play(float(cmd.get("offset", 0)))
    elif c == "pause":
        await cmd_pause()
    elif c == "seek":
        await cmd_seek(float(cmd.get("offset", 0)))
    elif c == "select":
        await cmd_select(str(cmd.get("id", "full")))
    elif c == "shuffle":
        await cmd_select("random")
    elif c == "config":
        await cmd_config(cmd.get("model"), cmd.get("config"))
    elif c == "clear":
        m = cmd.get("model")
        if m in ("A", "B"):
            state["log"][m] = []
            pending[m] = None
            await emit({"type": "cleared", "model": m})


async def sender_loop() -> None:
    """Pace PCM chunks to both providers at realtime speed.
    Tracks an integer byte offset: float second offsets drift (0.1 accumulates
    to 0.7999999999999999 etc.) and truncated seeks corrupt the PCM stream."""
    next_t = time.monotonic()
    src = state["source"]
    byte_pos = int(round((src["base"] + state["position"]) * audio.BYTES_PER_SEC))
    try:
        while state["playing"]:
            src = state["source"]
            if src is None:
                break
            pos = byte_pos / audio.BYTES_PER_SEC - src["base"]
            if pos >= src["duration"] - 1e-6:
                break
            chunk = audio.read_chunk_bytes(byte_pos, CHUNK)
            if not chunk:
                break
            for m in ("A", "B"):
                s = state["sessions"][m]
                if s is not None and s.open:
                    await s.send_audio(chunk)
            byte_pos += len(chunk)
            dur = len(chunk) / 2 / audio.SAMPLE_RATE
            state["position"] = byte_pos / audio.BYTES_PER_SEC - src["base"]
            if int(state["position"] / 5) != int(pos / 5):
                sent = {m: getattr(s, "_sent_bytes", 0) if s else 0 for m, s in state["sessions"].items()}
                log(f"sender wall={time.monotonic():.1f} pos={state['position']:.1f}s bytes={sent}")
            for m in ("A", "B"):
                s = state["sessions"][m]
                if s is not None:
                    s.expect_pos = src["base"] + state["position"]
            next_t += dur * PACE
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = time.monotonic()  # fell behind; resync clock
    except asyncio.CancelledError:
        return
    await end_source()


async def end_source() -> None:
    state["playing"] = False
    await close_sessions()
    await flush_pending()
    await emit({"type": "state", "playing": False, "ended": True, "position": state["position"]})


NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def asset_version() -> str:
    """Hash of the web/ files' names+mtimes+sizes, so the asset URLs change
    whenever any frontend file is edited, even without a server restart."""
    h = hashlib.sha1()
    for p in sorted(WEB.iterdir()):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:12]


async def index(request):
    # version the asset URLs so no browser cache (incl. VS Code's internal
    # browser, which is known to ignore no-store) can serve stale JS/CSS
    html = (WEB / "index.html").read_text()
    html = re.sub(r"(/static/[a-z]+\.(?:js|css))", rf"\1?v={asset_version()}", html)
    return web.Response(text=html, content_type="text/html", headers=NO_CACHE)


async def static_handler(request):
    # no-store so browsers always refetch after a server restart (no stale JS/CSS)
    name = request.match_info["name"]
    p = (WEB / name).resolve()
    if WEB.resolve() not in p.parents or not p.is_file():
        return web.Response(status=404)
    return web.FileResponse(p, headers=NO_CACHE)


async def audio_handler(request):
    name = request.match_info["name"]
    base = OUT.resolve()
    p = (base / name).resolve()
    if p.parent != base or not p.is_file():
        return web.Response(status=404)
    size = p.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Content-Type": "audio/wav"}
    rng = request.headers.get("Range")
    if rng:
        try:
            _, _, spec = rng.partition("=")
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else 0
            end = min(int(end_s) if end_s else size - 1, size - 1)
            length = end - start + 1
            h = dict(headers)
            h["Content-Range"] = f"bytes {start}-{end}/{size}"
            h["Content-Length"] = str(length)
            resp = web.StreamResponse(status=206, headers=h)
            await resp.prepare(request)
            try:
                with p.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        buf = f.read(min(1 << 20, remaining))
                        if not buf:
                            break
                        await resp.write(buf)
                        remaining -= len(buf)
                await resp.write_eof()
            except (ConnectionError, asyncio.CancelledError):
                pass  # browser aborted the range request (e.g. scrubbing)
            return resp
        except ValueError:
            return web.Response(status=416)
    return web.FileResponse(p, headers=headers)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    state["browser"] = ws
    log("browser connected")
    await emit({
        "type": "hello",
        "manifest": state["manifest"],
        "configs": state["configs"],
        "source": state["source"],
        "playing": state["playing"],
        "position": state["position"],
    })
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                await handle_cmd(json.loads(msg.data))
            except Exception as e:
                await emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        if state["browser"] is ws:
            state["browser"] = None
        # browser gone: stop streaming and release provider sessions so no
        # audio keeps flowing (and billing) with nobody watching
        await stop_sender()
        await close_sessions(fast=True)
        log("browser disconnected")
    return ws


async def serve() -> None:
    state["http"] = aiohttp.ClientSession()
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/audio/{name}", audio_handler)
    app.router.add_get("/static/{name:.+}", static_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8000)
    try:
        await site.start()
    except OSError as e:
        await state["http"].close()
        await runner.cleanup()
        if e.errno == 48:
            raise SystemExit(
                "port 8000 already in use - another server.py is probably running "
                "(check with: lsof -i :8000 | grep LISTEN)"
            )
        raise
    log("workbench ready at http://127.0.0.1:8000")
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
