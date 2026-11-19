"""End-to-end smoke test against a running server (http://127.0.0.1:8000).

Selects segment 1, plays ~8s so both providers stream, pauses, seeks (must
clear), replays from the seek point. Prints every message it receives.

Run:  cd workbench && uv run python smoke_test.py
"""

import asyncio

import aiohttp

BASE = "http://127.0.0.1:8000"


async def drain(ws, dur, tag):
    got = []
    loop = asyncio.get_event_loop()
    end = loop.time() + dur
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.receive_json(), timeout=max(0.1, end - loop.time()))
        except asyncio.TimeoutError:
            break
        got.append(m)
        if m["type"] == "transcript":
            print(f"[{tag}] {m['model']} t={m['t0']:6.1f} partial={int(m.get('partial', False))} :: {m['text'][:100]}")
        elif m["type"] in ("error", "closed", "state", "cleared", "source"):
            extra = m.get("message") or m.get("model") or ("playing=" + str(m.get("playing"))) or m.get("source", {}).get("id", "")
            print(f"[{tag}] {m['type']}: {extra}")
    return got


async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{BASE}/ws") as ws:
            hello = await ws.receive_json()
            assert hello["type"] == "hello", hello
            print("HELLO source =", hello["source"]["id"], "| segments =", len(hello["manifest"]["segments"]))

            await ws.send_json({"cmd": "select", "id": "seg_00"})
            msgs = await drain(ws, 2, "select")
            assert any(m["type"] == "source" and m["source"]["id"] == "seg_00" for m in msgs), "no source msg"

            await ws.send_json({"cmd": "play", "offset": 0})
            msgs = await drain(ws, 15, "play")
            ta = [m for m in msgs if m["type"] == "transcript" and m["model"] == "A"]
            tb = [m for m in msgs if m["type"] == "transcript" and m["model"] == "B"]
            print(f"PLAY transcripts: A={len(ta)} B={len(tb)}")

            await ws.send_json({"cmd": "pause"})
            await drain(ws, 1, "pause")

            await ws.send_json({"cmd": "seek", "offset": 30})
            msgs = await drain(ws, 4, "seek")
            assert any(m["type"] == "cleared" and m["model"] == "both" for m in msgs), "seek did not clear"
            print("SEEK cleared both viewports OK")

            await ws.send_json({"cmd": "play", "offset": 30})
            msgs = await drain(ws, 6, "replay")
            tb2 = [m for m in msgs if m["type"] == "transcript"]
            print(f"REPLAY transcripts: {len(tb2)} (t0 should be >= 30: {all(m['t0'] >= 29.9 for m in tb2) if tb2 else 'n/a'})")

            await ws.send_json({"cmd": "pause"})
            await drain(ws, 1, "done")
            ok = bool(ta) and bool(tb)
            print("SMOKE OK (both arms transcribed)" if ok else "SMOKE DONE (missing transcripts: A=%d B=%d, check errors above)" % (len(ta), len(tb)))


asyncio.run(main())
