"""Speechmatics realtime session: English STT + Hindi translation.

One realtime connection to the EU endpoint, using the phase-1
transcription settings (enhanced model, finals-only, max_delay 1.5
s, flexible - tested at 2.0 and 4.0 too; the vendor's untranslated
passthrough on long units is identical at all delays) plus a
translation config targeting Hindi. The vendor streams translated
text as AddTranslation messages, so the simultaneous-translation
tradeoffs are handled inside their model - we just display what
arrives.

Pushes updates through callbacks with their spans in the original
audio: on_final(en_line, start, end), on_translation(hi_text,
start, end), on_error(message). Pure provider logic - no Reflex
imports, testable on its own.
"""

import asyncio
import json

import websockets

from . import env  # noqa: F401  (loads the repo .env)

SM_URL = "wss://eu.rt.speechmatics.com/v2"
TARGET_LANG = "hi"

PUNCT = (",", ".", "!", "?", ";", ":", "\u0964", "\u0965")  # danda + double danda


def fix_punct(tokens: list[str]) -> str:
    out = " ".join(tokens)
    for p in PUNCT:
        out = out.replace(" " + p, p)
    return out.strip()


def span_start(results: list[dict]) -> float:
    """When this unit begins in the original audio."""
    return results[0].get("start_time", 0.0) if results else 0.0


def span_end(results: list[dict]) -> float:
    """When this unit ends in the original audio."""
    if not results:
        return 0.0
    return results[-1].get("end_time", span_start(results))


class SpeechmaticsSession:
    """One realtime session on the EU endpoint: STT + Hindi translation."""

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._done = asyncio.Event()
        self._seq = -1  # audio frame sequence number, for EndOfStream

    async def open(self, on_final, on_translation, on_error) -> None:  # type: ignore[no-untyped-def]
        self._ws = await websockets.connect(
            SM_URL,
            additional_headers={"Authorization": f"Bearer {self._key}"},
            max_size=None,
        )
        await self._ws.send(
            json.dumps(
                {
                    "message": "StartRecognition",
                    "audio_format": {
                        "type": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": 16000,
                    },
                    "transcription_config": {
                        "language": "en",
                        "model": "enhanced",
                        "enable_partials": False,
                        "max_delay": 4.0,
                        "max_delay_mode": "flexible",
                        # 0.12 merged real sentences; 0.5 seeded false
                        # stops that broke the vendor's translation units
                        "punctuation_overrides": {"sensitivity": 0.3},
                        "transcript_filtering_config": {
                            "remove_disfluencies": True,
                        },
                    },
                    "translation_config": {
                        "target_languages": [TARGET_LANG],
                        "enable_partials": False,
                    },
                }
            )
        )
        # wait for the server to accept the session
        while True:
            reply = json.loads(await self._ws.recv())
            msg = reply.get("message")
            if msg == "RecognitionStarted":
                break
            if msg == "Error":
                raise RuntimeError(str(reply))
        self._receiver = asyncio.create_task(
            self._receive(on_final, on_translation, on_error)
        )

    async def send(self, chunk: bytes) -> None:
        if self._ws is not None:
            self._seq += 1
            await self._ws.send(chunk)

    async def finish(self) -> None:
        """No more audio: tell the server and let the receiver drain."""
        if self._ws is not None:
            await self._ws.send(
                json.dumps({"message": "EndOfStream", "last_seq_no": self._seq})
            )

    async def wait_done(self) -> None:
        await self._done.wait()

    async def close(self) -> None:
        """Hard stop (pause/seek): disconnect; billing stops immediately."""
        self._done.set()
        if self._receiver is not None:
            self._receiver.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _receive(self, on_final, on_translation, on_error) -> None:  # type: ignore[no-untyped-def]
        try:
            while True:
                msg = json.loads(await self._ws.recv())
                kind = msg.get("message")
                if kind == "AddTranscript":
                    results = msg.get("results", [])
                    words = [
                        r.get("alternatives", [{}])[0].get("content", "")
                        for r in results
                        if r.get("alternatives")
                    ]
                    await on_final(
                        fix_punct(words),
                        span_start(results),
                        span_end(results),
                    )
                elif kind == "AddTranslation":
                    if msg.get("language") != TARGET_LANG:
                        continue
                    results = msg.get("results", [])
                    contents = [r.get("content", "") for r in results]
                    await on_translation(
                        fix_punct(contents),
                        span_start(results),
                        span_end(results),
                    )
                elif kind == "EndOfTranscript":
                    return
                elif kind == "Error":
                    raise RuntimeError(str(msg))
        except Exception as err:  # noqa: BLE001
            await on_error(f"error: {err}")
        finally:
            self._done.set()
