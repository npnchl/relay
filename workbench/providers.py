"""Provider sessions for the two A/B arms.

Raw websockets with the probe-verified configs from tuning/ (2026-11-19):
  A: Speechmatics realtime v2, melia-1 + language "multi"
  B: Gladia live v2, solaria-1 + code_switching

Both receive identical 16 kHz mono s16le PCM chunks. The official SDKs
(speechmatics-python, gladiaio-sdk) can replace these classes later; the
message handling here mirrors their payloads.
"""

import asyncio
import json
import os
import re
import time

from aiohttp import WSMsgType

SM_URL = "wss://eu.rt.speechmatics.com/v2"
GLADIA_INIT = "https://api.gladia.io/v2/live"

DEFAULT_CONFIGS = {
    "A": {
        # melia-1 has a reduced feature set: max_delay, max_delay_mode,
        # punctuation_overrides and transcript_filtering_config are rejected
        # by the API on this model (verified 2026-11-19), so they are omitted.
        "message": "StartRecognition",
        "audio_format": {"type": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
        "transcription_config": {
            "model": "melia-1",
            "language": "multi",
            "enable_partials": False,
        },
    },
    "B": {
        "encoding": "wav/pcm",
        "bit_depth": 16,
        "sample_rate": 16000,
        "channels": 1,
        "model": "solaria-1",
        "language_config": {"languages": ["en", "de"], "code_switching": True},
        "endpointing": 0.5,
        "maximum_duration_without_endpointing": 5,
        "messages_config": {
            "receive_partial_transcripts": False,
            "receive_final_transcripts": True,
        },
        "custom_metadata": {"experiment": "ab-workbench"},
    },
}


class ProviderSession:
    """One live STT session. base_offset = absolute seconds in the source file
    at which this session's audio stream started (for timestamp mapping)."""

    def __init__(self, model: str, key: str, config: dict, base_offset: float, on_event):
        self.model = model
        self.key = key
        self.config = config
        self.base_offset = base_offset
        self.on_event = on_event
        self.ws = None
        self.open = False
        self._seq = 0
        self._recv = None
        self.ready = asyncio.Event()  # set when the provider confirms the session is live
        # absolute source position the next audio chunk should have, and last
        # time we heard from the provider (to detect dead/half-open sessions)
        self.expect_pos = base_offset
        self.last_rx = time.monotonic()

    async def start(self, http) -> None:
        if self.model == "A":
            self.ws = await http.ws_connect(
                SM_URL,
                headers={"Authorization": f"Bearer {self.key}"},
                heartbeat=30,
            )
            await self.ws.send_str(json.dumps(self.config))
        else:
            async with http.post(
                GLADIA_INIT, json=self.config, headers={"x-gladia-key": self.key}
            ) as r:
                data = await r.json()
                if r.status not in (200, 201):
                    raise RuntimeError(f"Gladia init {r.status}: {json.dumps(data)[:300]}")
            self.ws = await http.ws_connect(data["url"], heartbeat=30)
        self.open = True
        self._recv = asyncio.create_task(self._recv_loop())
        # don't stream audio until the provider confirms the session is live;
        # audio sent before that is silently dropped and the opening is lost
        try:
            await asyncio.wait_for(self.ready.wait(), 5)
        except asyncio.TimeoutError:
            pass

    async def send_audio(self, chunk: bytes) -> None:
        if not self.open or self.ws is None:
            return
        try:
            self._seq += 1
            self._sent_bytes = getattr(self, "_sent_bytes", 0) + len(chunk)
            await self.ws.send_bytes(chunk)
        except Exception as e:
            self.open = False
            await self.on_event({"type": "error", "model": self.model, "message": f"send: {e}"})

    async def close(self, fast: bool = False) -> None:
        """fast=True skips waiting for in-flight finals (used on seek/select,
        where the viewports are cleared anyway)."""
        self.open = False
        if self.ws is not None:
            try:
                if self.model == "A":
                    await self.ws.send_str(
                        json.dumps({"message": "EndOfStream", "last_seq_no": self._seq})
                    )
                else:
                    await self.ws.send_str(json.dumps({"type": "stop_recording"}))
            except Exception:
                pass
            if self._recv is not None:
                if fast:
                    self._recv.cancel()
                else:
                    try:
                        await asyncio.wait_for(self._recv, 4)
                    except Exception:
                        self._recv.cancel()
            try:
                await self.ws.close()
            except Exception:
                pass

    async def _recv_loop(self) -> None:
        try:
            async for msg in self.ws:
                self.last_rx = time.monotonic()
                if msg.type == WSMsgType.TEXT:
                    await self._handle(json.loads(msg.data))
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self.on_event(
                {"type": "error", "model": self.model, "message": f"recv: {type(e).__name__}: {e}"}
            )
        finally:
            self.open = False
            await self.on_event({"type": "closed", "model": self.model})

    async def _handle(self, m: dict) -> None:
        kind = m.get("message") or m.get("type")
        if os.environ.get("WB_DEBUG_RAW"):
            import sys
            print(f"RAW {time.monotonic():.1f} {self.model} {kind} {json.dumps(m, ensure_ascii=False)[:150]}", file=sys.stderr, flush=True)
        if kind in ("RecognitionStarted", "started", "listening"):
            self.ready.set()
        if self.model == "A":
            if kind == "AddTranscript":
                await self._emit_sm(m, partial=False)
            elif kind == "AddPartialTranscript":
                await self._emit_sm(m, partial=True)
            elif kind == "Error":
                await self.on_event(
                    {"type": "error", "model": "A", "message": json.dumps(m)[:300]}
                )
        else:
            # Gladia live v2 sends {"type":"transcript","data":{"is_final":bool,"utterance":{...}}}
            # (verified 2026-11-19 probe); legacy final_transcript/partial_transcript kept as fallback
            if kind == "transcript":
                data = m.get("data") or {}
                await self._emit_gladia_utt(data.get("utterance") or {}, partial=not data.get("is_final", True))
            elif kind == "final_transcript":
                await self._emit_gladia(m, partial=False)
            elif kind == "partial_transcript":
                await self._emit_gladia(m, partial=True)

    async def _emit_sm(self, m: dict, partial: bool) -> None:
        results = m.get("results") or []
        words = [((r.get("alternatives") or [{}])[0].get("content", ""),
                  r.get("start_time", 0), r.get("end_time", 0)) for r in results]
        if not words:
            return
        # SM re-sends the tail of a phrase when it extends it ("In" -> "In those"
        # -> "In those days, ..."), with overlapping start times. Strip the
        # word-level overlap against what we already emitted and emit only the delta.
        tail = getattr(self, "_tail", [])
        k = 0
        for cand in range(min(len(tail), len(words)), 0, -1):
            if tail[-cand:] == [w[0] for w in words[:cand]]:
                k = cand
                break
        delta = words[k:]
        self._tail = (tail + [w[0] for w in words])[-12:]
        if not delta:
            return
        text = " ".join(w[0] for w in delta).strip()
        text = re.sub(r"\s+([.,!?;:])", r"\1", text)  # "6 ." -> "6."
        if not text:
            return
        t0 = self.base_offset + delta[0][1]
        t1 = self.base_offset + delta[-1][2]
        await self.on_event(
            {"type": "transcript", "model": "A", "text": text, "t0": t0, "t1": t1, "partial": partial}
        )

    async def _emit_gladia(self, m: dict, partial: bool) -> None:
        await self._emit_gladia_utt((m.get("data") or {}).get("utterance") or {}, partial)

    async def _emit_gladia_utt(self, utt: dict, partial: bool) -> None:
        text = (utt.get("text") or "").strip()
        if not text:
            return
        await self.on_event({
            "type": "transcript",
            "model": "B",
            "text": text,
            "t0": self.base_offset + utt.get("start", 0),
            "t1": self.base_offset + utt.get("end", 0),
            "partial": partial,
        })
