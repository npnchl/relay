"""One-off probe: stream 12s of seg_00 PCM straight to Gladia live v2 and
print every message, to diagnose why arm B produced no transcripts.

Run:  cd workbench && uv run python probe_gladia.py
"""

import asyncio
import json
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
KEY = os.environ["GLADIA_API_KEY"]

CFG = {
    "encoding": "wav/pcm",
    "bit_depth": 16,
    "sample_rate": 16000,
    "channels": 1,
    "model": "solaria-1",
    "language_config": {"languages": ["en", "hi"], "code_switching": True},
    "endpointing": 0.5,
    "maximum_duration_without_endpointing": 5,
    "messages_config": {
        "receive_partial_transcripts": False,
        "receive_final_transcripts": True,
    },
    "custom_metadata": {"experiment": "ab-workbench-probe"},
}


async def main():
    pcm = Path("cache/sermon_16k_mono.s16").read_bytes()[: 16000 * 2 * 12]  # 12 s
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.gladia.io/v2/live", json=CFG, headers={"x-gladia-key": KEY}
        ) as r:
            print("init status", r.status)
            data = await r.json()
            print("init body", json.dumps(data)[:300])
            if r.status not in (200, 201):
                return
        ws = await s.ws_connect(data["url"])

        async def send():
            for i in range(0, len(pcm), 3200):
                await ws.send_bytes(pcm[i : i + 3200])
                await asyncio.sleep(0.1)
            await ws.send_str(json.dumps({"type": "stop_recording"}))

        t = asyncio.create_task(send())
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                print("MSG", m.get("type"), json.dumps(m, ensure_ascii=False)[:250])
            else:
                print("WS", msg.type)
                break
        await t


asyncio.run(main())
