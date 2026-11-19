"""Provider sessions on the OFFICIAL native clients (issue 3):

  A: speechmatics-rt  -> AsyncClient against wss://eu.rt.speechmatics.com/v2
                        (start_session waits for RecognitionStarted; send_audio
                        pushes each chunk over the websocket immediately)
  B: gladiaio-sdk     -> GladiaClient.live_v2_async().start_session(config dict)
                        (POST /v2/live + websocket; send_audio sends immediately,
                        buffering only until the socket opens, then flushes)

Both arms receive identical 16 kHz mono s16le PCM chunks via send_audio(), and
report events through the synchronous on_event callback:

  {"type": "transcript", "model": "A"|"B", "text": str, "t0": float, "t1": float,
   "partial": bool}          t0/t1 are absolute seconds in the full source file
  {"type": "error",  "model": ..., "message": str}
  {"type": "closed", "model": ...}
"""

import asyncio
import dataclasses
import json
import re
import time

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    Model,
    ServerMessageType,
    StaticKeyAuth,
    TranscriptionConfig,
)
from gladiaio_sdk import GladiaClient

SM_URL = "wss://eu.rt.speechmatics.com/v2"

DEFAULT_CONFIGS = {
    # A is a flat dict mapped onto speechmatics-rt's TranscriptionConfig
    # (melia-1 rejects max_delay / punctuation_overrides / transcript_filtering_config)
    "A": {
        "model": "melia-1",
        "language": "multi",
        "enable_partials": False,
    },
    # B is a gladiaio-sdk LiveV2InitRequest dict; the SDK sends a dict as-is
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


def _to_dict(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


class ProviderSession:
    """One live STT session. base_offset = absolute seconds in the source file
    at which this session's audio stream started (for timestamp mapping)."""

    def __init__(self, model: str, key: str, config: dict, base_offset: float, on_event):
        self.model = model
        self.key = key
        self.config = config
        self.base_offset = base_offset
        self.on_event = on_event  # sync callback
        self.open = False
        self.ready = asyncio.Event()
        # absolute source position the next audio chunk should have, and last
        # time we heard from the provider (to detect dead/half-open sessions)
        self.expect_pos = base_offset
        self.last_rx = time.monotonic()
        self._client: AsyncClient | None = None      # speechmatics-rt
        self._session = None                        # gladiaio-sdk live session
        self._sent_bytes = 0
        self._tail: list[str] = []                  # SM word-overlap dedup
        self.closed_by_us = False                   # False if the provider dropped us

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.model == "A":
            await self._start_sm()
        else:
            await self._start_gladia()
        self.open = True
        try:
            await asyncio.wait_for(self.ready.wait(), 8)
        except asyncio.TimeoutError:
            pass

    async def _start_sm(self) -> None:
        cfg = self.config.get("transcription_config", self.config)  # flat or nested
        fields = {f.name for f in dataclasses.fields(TranscriptionConfig)}
        kwargs = {}
        for k, v in cfg.items():
            if k not in fields:
                continue
            if k == "model" and isinstance(v, str):
                v = Model(v)
            kwargs[k] = v
        tc = TranscriptionConfig(**kwargs)
        af = AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=16000)
        c = AsyncClient(auth=StaticKeyAuth(self.key), url=SM_URL)
        self._client = c
        c.on(ServerMessageType.RECOGNITION_STARTED, lambda m: self.ready.set())
        c.on(ServerMessageType.ADD_TRANSCRIPT, lambda m: self._emit_sm(m, partial=False))
        c.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT, lambda m: self._emit_sm(m, partial=True))
        c.on(ServerMessageType.ERROR, lambda m: self.on_event(
            {"type": "error", "model": "A", "message": json.dumps(_to_dict(m))[:300]}))
        # start_session waits for RecognitionStarted internally (5 s timeout)
        await c.start_session(transcription_config=tc, audio_format=af)

    async def _start_gladia(self) -> None:
        client = GladiaClient(api_key=self.key)
        live = client.live_v2_async()
        s = live.start_session(self.config)  # dict options are sent as-is
        self._session = s
        s.on("connected", lambda m: self.ready.set())
        s.on("message", self._on_gladia_message)
        s.on("error", lambda e: self.on_event({"type": "error", "model": "B", "message": str(e)[:300]}))
        s.on("ended", lambda m: self._provider_ended())

    async def send_audio(self, chunk: bytes) -> None:
        if not self.open:
            return
        try:
            if self.model == "A":
                await self._client.send_audio(chunk)
            else:
                self._session.send_audio(chunk)  # sync; sends immediately when ws open
            self._sent_bytes += len(chunk)
        except Exception as e:
            self.open = False
            self.on_event({"type": "error", "model": self.model, "message": f"send: {e}"})

    def _provider_ended(self) -> None:
        """Provider closed the session on its own (idle timeout, error, ...)."""
        self.open = False
        self.on_event({"type": "closed", "model": self.model, "local": self.closed_by_us})

    async def close(self, fast: bool = False) -> None:
        """fast=True skips waiting for in-flight finals (used on re-anchor/select)."""
        self.closed_by_us = True
        self.open = False
        try:
            if self.model == "A":
                if fast:
                    await self._client.close()
                else:
                    try:
                        await asyncio.wait_for(self._client.stop_session(), 4)
                    except Exception:
                        await self._client.close()
            else:
                if not fast and self._session is not None:
                    self._session.stop_recording()
                    await asyncio.sleep(0.3)  # let in-flight finals arrive
                if self._session is not None:
                    self._session.end_session()
        except Exception:
            pass
        self.on_event({"type": "closed", "model": self.model, "local": True})

    # ------------------------------------------------------------- events

    def _on_gladia_message(self, m) -> None:
        self.last_rx = time.monotonic()
        m = _to_dict(m)
        kind = m.get("type")
        if kind == "transcript":
            data = m.get("data") or {}
            self._emit_gladia_utt(data.get("utterance") or {}, partial=not data.get("is_final", True))
        elif kind == "final_transcript":  # legacy shapes, kept as fallback
            self._emit_gladia_utt((m.get("data") or {}).get("utterance") or {}, partial=False)
        elif kind == "partial_transcript":
            self._emit_gladia_utt((m.get("data") or {}).get("utterance") or {}, partial=True)

    def _emit_sm(self, m: dict, partial: bool) -> None:
        self.last_rx = time.monotonic()
        results = m.get("results") or []
        words = [((r.get("alternatives") or [{}])[0].get("content", ""),
                  r.get("start_time", 0), r.get("end_time", 0)) for r in results]
        if not words:
            return
        # SM re-sends the tail of a phrase when it extends it ("In" -> "In those"
        # -> "In those days, ..."), with overlapping start times. Strip the
        # word-level overlap against what we already emitted and emit only the delta.
        tail = self._tail
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
        self.on_event({
            "type": "transcript", "model": "A", "text": text,
            "t0": self.base_offset + delta[0][1], "t1": self.base_offset + delta[-1][2],
            "partial": partial,
        })

    def _emit_gladia_utt(self, utt: dict, partial: bool) -> None:
        text = (utt.get("text") or "").strip()
        if not text:
            return
        self.on_event({
            "type": "transcript", "model": "B", "text": text,
            "t0": self.base_offset + utt.get("start", 0),
            "t1": self.base_offset + utt.get("end", 0),
            "partial": partial,
        })
