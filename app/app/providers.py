"""Provider WebSocket sessions for the live compare app.

Each session owns one realtime STT connection and pushes caption
updates through callbacks: on_final(line), on_pending(text),
on_error(message). Pure provider logic - no Reflex imports, so the
module is testable on its own.

Speechmatics partials are deltas (accumulate them); Soniox sends
subword pieces that can straddle messages (buffer them across
messages). Both lessons learned the hard way in tools/.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import websockets

SM_URL = "wss://eu.rt.speechmatics.com/v2"

PUNCT = (",", ".", "!", "?", ";", ":")
# Soniox internal markers (endpoint detection etc.), not caption text
MARKER_RE = re.compile(r"^<[a-z]+>$")


def soniox_url() -> str:
    region = os.environ.get("SONIOX_REGION", "us").lower()
    host = "stt-rt.eu.soniox.com" if region == "eu" else "stt-rt.soniox.com"
    return f"wss://{host}/transcribe-websocket"


def fix_punct(tokens: list[str]) -> str:
    out = " ".join(tokens)
    for p in PUNCT:
        out = out.replace(" " + p, p)
    return out


class SpeechmaticsSession:
    """One realtime session on the EU endpoint (enhanced, partials on)."""

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._partial_words: list[str] = []
        self._done = asyncio.Event()
        self._seq = -1  # audio frame sequence number, for EndOfStream

    async def open(self, on_final, on_pending, on_error) -> None:  # type: ignore[no-untyped-def]
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
                        "operating_point": "enhanced",
                        "enable_partials": True,
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
        self._receiver = asyncio.create_task(self._receive(on_final, on_pending, on_error))

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

    async def _receive(self, on_final, on_pending, on_error) -> None:  # type: ignore[no-untyped-def]
        try:
            while True:
                msg = json.loads(await self._ws.recv())
                kind = msg.get("message")
                # the message TYPE distinguishes partial from final;
                # results carry no is_final flags (verified against EU v2)
                if kind == "AddPartialTranscript":
                    # each partial carries the full current window
                    words = [
                        r.get("alternatives", [{}])[0].get("content", "")
                        for r in msg.get("results", [])
                        if r.get("alternatives")
                    ]
                    await on_pending(fix_punct(words))
                elif kind == "AddTranscript":
                    words = [
                        r.get("alternatives", [{}])[0].get("content", "")
                        for r in msg.get("results", [])
                        if r.get("alternatives")
                    ]
                    await on_final(fix_punct(words))
                elif kind == "EndOfTranscript":
                    return
                elif kind == "Error":
                    raise RuntimeError(str(msg))
        except Exception as err:  # noqa: BLE001
            await on_error(f"error: {err}")
        finally:
            self._done.set()


class SonioxSession:
    """One realtime session (stt-rt-v5, endpoint by SONIOX_REGION)."""

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._pieces: list[str] = []
        self._done = asyncio.Event()

    async def open(self, on_final, on_pending, on_error) -> None:  # type: ignore[no-untyped-def]
        self._ws = await websockets.connect(
            soniox_url(),
            additional_headers={"Authorization": f"Bearer {self._key}"},
            max_size=None,
        )
        await self._ws.send(
            json.dumps(
                {
                    "model": "stt-rt-v5",
                    "language_hints": ["en"],
                    "audio_format": "pcm_s16le",
                    "sample_rate": 16000,
                    "num_channels": 1,
                    "enable_endpoint_detection": True,
                    "client_reference_id": (
                        "live-"
                        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                    ),
                }
            )
        )
        self._receiver = asyncio.create_task(self._receive(on_final, on_pending, on_error))

    async def send(self, chunk: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(chunk)

    async def finish(self) -> None:
        if self._ws is not None:
            await self._ws.send("")  # empty frame = end of audio

    async def wait_done(self) -> None:
        await self._done.wait()

    async def close(self) -> None:
        self._done.set()
        if self._receiver is not None:
            self._receiver.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _receive(self, on_final, on_pending, on_error) -> None:  # type: ignore[no-untyped-def]
        try:
            while True:
                msg = json.loads(await self._ws.recv())
                if msg.get("error_code"):
                    raise RuntimeError(
                        f"{msg.get('error_code')}: {msg.get('error_message')}"
                    )
                tokens = msg.get("tokens", [])
                nonfinal = [
                    t
                    for t in tokens
                    if not t.get("is_final") and not MARKER_RE.match(str(t.get("text", "")).strip())
                ]
                finals = [
                    t
                    for t in tokens
                    if t.get("is_final") and not MARKER_RE.match(str(t.get("text", "")).strip())
                ]
                if nonfinal:
                    # each response carries the full non-final window
                    pending = "".join(str(t.get("text", "")) for t in nonfinal)
                    await on_pending(pending.strip())
                if finals:
                    # subword pieces can straddle messages: keep the
                    # unfinished word in self._pieces between messages
                    words: list[str] = []
                    for tok in finals:
                        text = str(tok.get("text", ""))
                        if not text:
                            continue
                        if text[0].isspace():
                            if self._pieces:
                                words.append("".join(self._pieces))
                                self._pieces = []
                            stripped = text.strip()
                            if stripped:
                                self._pieces.append(stripped)
                        else:
                            self._pieces.append(text)
                    if words:
                        await on_final(" ".join(words))
                if msg.get("finished"):
                    # flush any half-assembled word left in the buffer
                    if self._pieces:
                        await on_final("".join(self._pieces))
                        self._pieces = []
                    return
        except Exception as err:  # noqa: BLE001
            await on_error(f"error: {err}")
        finally:
            self._done.set()
