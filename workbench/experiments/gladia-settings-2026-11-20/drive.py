import asyncio, json, sys, aiohttp
BASE = "http://127.0.0.1:8000"
OUT = "/tmp/exp"
CONFIGS = json.load(open(f"{OUT}/configs.json"))

async def post(s, path, payload=None):
    async with s.post(BASE + path, json=payload or {}) as r:
        return r.status, await r.json()

async def get(s, path):
    async with s.get(BASE + path) as r:
        return await r.json()

async def run_one(s, c):
    label, cfg, seg = c["label"], c["config"], c.get("seg", "seg_00")
    st, r = await post(s, "/source", {"id": seg})
    assert st == 200, r
    await post(s, "/transcripts/clear", {"model": "A"})
    await post(s, "/transcripts/clear", {"model": "B"})
    st, r = await post(s, "/config", {"model": "B", "config": cfg})
    assert st == 200, (label, r)
    st, r = await post(s, "/listen/start", {"model": "A", "offset": 0})
    assert st == 200 and r["listening"]["A"], (label, r)
    st, r = await post(s, "/listen/start", {"model": "B", "offset": 0})
    assert st == 200 and r["listening"]["B"], (label, r)
    await post(s, "/sim/start")
    st = {}
    for _ in range(55):  # up to ~110 s
        await asyncio.sleep(2)
        st = await get(s, "/state")
        if st["ended"] or not st["running"]:
            break
    t = await get(s, "/transcripts")
    json.dump({"label": label, "seg": seg, "lines": t["lines"]}, open(f"{OUT}/{label}_{seg}.json", "w"), indent=1)
    finals = [l for l in t["lines"] if not l["partial"]]
    print(f"[{label}] {seg} done pos={st.get('position',0):.1f} finals A={sum(1 for l in finals if l['model']=='A')} B={sum(1 for l in finals if l['model']=='B')}", flush=True)
    for l in finals:
        if l["model"] == "B":
            print(f"   B t={l['t0']:5.1f}-{l['t1']:5.1f} :: {l['text'][:110]}", flush=True)

async def main():
    labels = sys.argv[1:]
    async with aiohttp.ClientSession() as s:
        for c in CONFIGS:
            if labels and c["label"] not in labels:
                continue
            await run_one(s, c)

asyncio.run(main())
