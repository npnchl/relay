"""End-to-end smoke test against a running backend (http://127.0.0.1:8000).

Selects seg_00, opens both arms via /listen/start, starts the simulated mic,
polls /state + /transcripts until both arms have lines, stops listening
(sim auto-pauses, position kept), re-listens at 30s, starts the sim, and
checks that new transcripts carry file-absolute t0 >= 30. Also verifies the
results/*.jsonl persistence.

Run:  cd workbench && uv run python smoke_test.py   (with server.py running)
"""

import asyncio
import json
from pathlib import Path

import aiohttp

BASE = "http://127.0.0.1:8000"


async def get(s, path):
    async with s.get(f"{BASE}{path}") as r:
        return r.status, await r.json()


async def post(s, path, payload=None):
    async with s.post(f"{BASE}{path}", json=payload or {}) as r:
        return r.status, await r.json()


async def poll(s, seconds, tag):
    """Poll state+transcripts for `seconds`; return (state, lines_by_model)."""
    end = asyncio.get_event_loop().time() + seconds
    st = None
    while asyncio.get_event_loop().time() < end:
        _, st = await get(s, "/state")
        await asyncio.sleep(1)
    _, tr = await get(s, "/transcripts")
    lines = {"A": [], "B": []}
    for line in tr["lines"]:
        if not line["partial"]:
            lines[line["model"]].append(line)
    print(f"[{tag}] state running={st['running']} pos={st['position']:.1f} "
          f"listening={st['listening']} finals A={len(lines['A'])} B={len(lines['B'])}")
    for m in ("A", "B"):
        for line in lines[m][:4]:
            print(f"[{tag}] {m} t={line['t0']:6.1f} :: {line['text'][:90]}")
    return st, lines


async def main():
    async with aiohttp.ClientSession() as s:
        st, h = await get(s, "/health")
        assert st == 200 and h["ok"], h
        assert h["keys"]["A"] and h["keys"]["B"], "API keys missing"

        st, srcs = await get(s, "/sources")
        print(f"sources: full={srcs['full']['duration']:.0f}s segments={len(srcs['segments'])}")

        st, r = await post(s, "/source", {"id": "seg_00"})
        assert st == 200 and r["source"]["id"] == "seg_00", r

        # open both arms at 0s, then start the mic
        st, r = await post(s, "/listen/start", {"model": "A", "offset": 0})
        assert st == 200 and r["listening"]["A"], r
        st, r = await post(s, "/listen/start", {"model": "B", "offset": 0})
        assert st == 200 and r["listening"]["B"], r
        st, r = await post(s, "/sim/start")
        assert st == 200 and r["running"], r

        st, lines = await poll(s, 25, "phase1")
        assert st["running"], "sim not running"
        assert st["position"] > 5, f"sim did not advance: {st['position']}"
        assert lines["A"] and lines["B"], f"missing transcripts: A={len(lines['A'])} B={len(lines['B'])}"

        # stop listening -> sim auto-pauses, position kept
        await post(s, "/listen/stop", {"model": "A"})
        st, r = await post(s, "/listen/stop", {"model": "B"})
        assert st == 200 and not r["running"], "sim did not pause after last listener stopped"
        kept = r["position"]
        print(f"sim paused at {kept:.1f}s (position kept)")
        assert kept > 5

        # re-listen at 30s and restart the mic there
        await post(s, "/listen/start", {"model": "A", "offset": 30})
        await post(s, "/listen/start", {"model": "B", "offset": 30})
        st, r = await post(s, "/sim/start")
        assert st == 200 and r["running"] and abs(r["position"] - 30) < 0.6, r

        st, lines = await poll(s, 20, "phase2")
        a30 = [l for l in lines["A"] if l["t0"] >= 29]
        b30 = [l for l in lines["B"] if l["t0"] >= 29]
        print(f"phase2 transcripts with t0>=29: A={len(a30)} B={len(b30)}")
        assert a30 and b30, "no correctly-timestamped transcripts after re-anchor at 30s"

        # scrub the sim while running -> re-anchors all listening arms at 45s
        st, r = await post(s, "/sim/seek", {"offset": 45})
        assert st == 200 and abs(r["position"] - 45) < 0.6 and r["running"], r
        assert r["listening"] == {"A": True, "B": True}, r
        assert r["sessions"] == {"A": True, "B": True}, "seek must re-open listening arms"
        _, lines2 = await poll(s, 12, "seek45")
        a45 = [l for l in lines2["A"] if l["t0"] >= 44]
        b45 = [l for l in lines2["B"] if l["t0"] >= 44]
        print(f"after sim/seek to 45s: transcripts t0>=44: A={len(a45)} B={len(b45)}")
        assert a45 and b45, "no correctly-timestamped transcripts after sim seek"

        # stop the sim; transcripts endpoint still serves everything
        await post(s, "/sim/stop")
        st, tr = await get(s, "/transcripts?model=A")
        assert st == 200 and tr["count"] > 0, tr
        print(f"GET /transcripts?model=A -> {tr['count']} lines")

        # persistence file exists and is valid JSONL
        jf = Path(__file__).parent / "results" / "live-seg_00.jsonl"
        assert jf.exists(), f"missing {jf}"
        n = sum(1 for _ in jf.open())
        rows = [json.loads(l) for l in jf.open()]
        assert all({"model", "t0", "t1", "text", "partial"} <= set(r) for r in rows)
        print(f"results/live-seg_00.jsonl -> {n} valid JSONL rows")

        print("SMOKE TEST OK")


if __name__ == "__main__":
    asyncio.run(main())
