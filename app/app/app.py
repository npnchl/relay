"""live compare: interactive A/B of Speechmatics vs Soniox on the sermon.

Play the full sermon audio in the browser, hit Start, and both providers
receive the same audio from your current position at true realtime pace.
Partials/non-final tokens render in grey (refining live); finals in solid.

Run:  cd app && uv run reflex run --env-file ../.env  (or: reflex dev)
"""

import asyncio
import json
import os
import wave
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import reflex as rx
import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

AUDIO_PATH = Path(__file__).resolve().parents[1] / "assets" / "sermon-16k.wav"
CHUNK_MS = 100
BYTES_PER_SEC = 32000
SM_URL = "wss://eu.rt.speechmatics.com/v2"
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


def read_pcm() -> bytes:
    with wave.open(str(AUDIO_PATH), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("need 16 kHz mono 16-bit WAV at assets/sermon-16k.wav")
        return w.readframes(w.getnframes())


PCM = read_pcm()


def fmt_mmss(secs: float) -> str:
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"


class CaptionState(rx.State):
    running: bool = False
    start_pos: float = 0.0

    sm_status: str = "idle"
    soniox_status: str = "idle"
    sm_finals: list[str] = []
    sm_pending: str = ""
    soniox_finals: list[str] = []
    soniox_pending: str = ""
    sm_first_partial_ms: str = "-"
    sm_first_final_ms: str = "-"
    soniox_first_token_ms: str = "-"
    soniox_first_final_ms: str = "-"

    _stop: bool = False

    def read_position_and_start(self) -> rx.event.EventSpec:
        """Fetch the player's current time, then start both sessions."""
        return rx.call_script(
            "document.getElementById('sermon-audio').currentTime",
            callback=CaptionState.start_from_position,
        )

    def start_from_position(self, pos: float) -> None:
        pos = max(0.0, min(float(pos), len(PCM) / BYTES_PER_SEC - 0.1))
        if self.running:
            return
        self.start_pos = pos
        self._stop = False
        return CaptionState.stream_both

    @rx.event(background=True)
    async def stream_both(self) -> None:
        async with self:
            self.running = True
            self.sm_status = "connecting..."
            self.soniox_status = "connecting..."
            self.sm_finals = []
            self.sm_pending = ""
            self.soniox_finals = []
            self.soniox_pending = ""
            self.sm_first_partial_ms = "-"
            self.sm_first_final_ms = "-"
            self.soniox_first_token_ms = "-"
            self.soniox_first_final_ms = "-"
        await asyncio.gather(
            self.run_speechmatics(), self.run_soniox(), return_exceptions=True
        )
        async with self:
            self.running = False
            if self.sm_status.startswith("live"):
                self.sm_status = "stopped"
            if self.soniox_status.startswith("live"):
                self.soniox_status = "stopped"

    def stop_streams(self) -> None:
        self._stop = True

    @rx.var
    def start_pos_label(self) -> str:
        return fmt_mmss(self.start_pos)

    # ---------- Speechmatics ----------

    async def run_speechmatics(self) -> None:
        key = os.environ.get("SPEECHMATICS_API_KEY")
        if not key:
            async with self:
                self.sm_status = "no SPEECHMATICS_API_KEY"
            return
        chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)
        offset_bytes = int(self.start_pos * BYTES_PER_SEC)
        first_partial: float | None = None
        first_final: float | None = None
        start = perf_counter()

        async def sender(ws) -> None:  # type: ignore[no-untyped-def]
            i = 0
            next_at = perf_counter()
            while offset_bytes + i * chunk_bytes < len(PCM):
                if self._stop:
                    return
                await ws.send(
                    PCM[offset_bytes + i * chunk_bytes : offset_bytes + (i + 1) * chunk_bytes]
                )
                i += 1
                next_at += CHUNK_MS / 1000.0
                delay = next_at - perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            await ws.send(json.dumps({"message": "EndOfStream"}))

        try:
            async with websockets.connect(
                SM_URL,
                additional_headers={"Authorization": f"Bearer {key}"},
                max_size=None,
            ) as ws:
                await ws.send(
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
                while True:
                    reply = json.loads(await ws.recv())
                    msg = reply.get("message")
                    if msg == "RecognitionStarted":
                        break
                    if msg == "Error":
                        raise RuntimeError(str(reply))
                async with self:
                    self.sm_status = "live"
                sender_task = asyncio.create_task(sender(ws))
                while True:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    except asyncio.TimeoutError:
                        if sender_task.done() and self._stop:
                            break
                        if sender_task.done():
                            # drain: keep reading until server closes or stop
                            if self._stop:
                                break
                        continue
                    kind = msg.get("message")
                    if kind == "AddTranscript":
                        finals_now: list[str] = []
                        pending_now: list[str] = []
                        for result in msg.get("results", []):
                            alts = result.get("alternatives", [])
                            if not alts:
                                continue
                            content = alts[0].get("content", "")
                            if result.get("is_final"):
                                finals_now.append(content)
                            else:
                                pending_now.append(content)
                        if pending_now:
                            if first_partial is None:
                                first_partial = perf_counter() - start
                            async with self:
                                self.sm_first_partial_ms = f"{first_partial * 1000:.0f}ms"
                                self.sm_pending = " ".join(pending_now)
                        if finals_now:
                            if first_final is None:
                                first_final = perf_counter() - start
                            stamp = fmt_mmss(self.start_pos + (perf_counter() - start))
                            async with self:
                                self.sm_first_final_ms = f"{first_final * 1000:.0f}ms"
                                self.sm_pending = ""
                                self.sm_finals.append(f"[{stamp}] " + " ".join(finals_now))
                    elif kind == "EndOfTranscript":
                        break
                    elif kind == "Error":
                        raise RuntimeError(str(msg))
                sender_task.cancel()
        except Exception as err:  # noqa: BLE001 - surface in UI
            async with self:
                self.sm_status = f"error: {err}"
            return
        async with self:
            self.sm_status = "stopped"

    # ---------- Soniox ----------

    async def run_soniox(self) -> None:
        key = os.environ.get("SONIOX_API_KEY")
        if not key:
            async with self:
                self.soniox_status = "no SONIOX_API_KEY"
            return
        chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)
        offset_bytes = int(self.start_pos * BYTES_PER_SEC)
        first_token: float | None = None
        first_final: float | None = None
        finals: list[str] = []
        pending: list[str] = []
        start = perf_counter()

        def assemble(tokens: list[dict]) -> tuple[list[str], list[str]]:
            words: list[str] = []
            pieces: list[str] = []
            for tok in tokens:
                text = str(tok.get("text", ""))
                if not text:
                    continue
                if text[0].isspace():
                    if pieces:
                        words.append("".join(pieces))
                        pieces = []
                    stripped = text.strip()
                    if stripped:
                        pieces.append(stripped)
                else:
                    pieces.append(text)
            if pieces:
                words.append("".join(pieces))
            return words, []

        async def sender(ws) -> None:  # type: ignore[no-untyped-def]
            i = 0
            next_at = perf_counter()
            while offset_bytes + i * chunk_bytes < len(PCM):
                if self._stop:
                    return
                await ws.send(
                    PCM[offset_bytes + i * chunk_bytes : offset_bytes + (i + 1) * chunk_bytes]
                )
                i += 1
                next_at += CHUNK_MS / 1000.0
                delay = next_at - perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            await ws.send("")

        try:
            async with websockets.connect(
                SONIOX_URL,
                additional_headers={"Authorization": f"Bearer {key}"},
                max_size=None,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "model": "stt-rt-v5",
                            "language_hints": ["en"],
                            "audio_format": "pcm_s16le",
                            "sample_rate": 16000,
                            "num_channels": 1,
                            "enable_endpoint_detection": True,
                            "client_reference_id": f"live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
                        }
                    )
                )
                async with self:
                    self.soniox_status = "live"
                sender_task = asyncio.create_task(sender(ws))
                while True:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    except asyncio.TimeoutError:
                        if self._stop or sender_task.done():
                            continue
                        continue
                    if msg.get("error_code"):
                        raise RuntimeError(
                            f"{msg.get('error_code')}: {msg.get('error_message')}"
                        )
                    tokens = msg.get("tokens", [])
                    final_tokens = [t for t in tokens if t.get("is_final")]
                    nonfinal = [t for t in tokens if not t.get("is_final")]
                    if tokens and first_token is None:
                        first_token = perf_counter() - start
                        async with self:
                            self.soniox_first_token_ms = f"{first_token * 1000:.0f}ms"
                    if nonfinal:
                        pending = [str(t.get("text", "")) for t in nonfinal]
                        async with self:
                            self.soniox_pending = "".join(pending).strip()
                    if final_tokens:
                        if first_final is None:
                            first_final = perf_counter() - start
                        words, _ = assemble(final_tokens)
                        if words:
                            stamp = fmt_mmss(self.start_pos + (perf_counter() - start))
                            async with self:
                                self.soniox_first_final_ms = f"{first_final * 1000:.0f}ms"
                                self.soniox_pending = ""
                                finals.append(" ".join(words))
                                self.soniox_finals.append(f"[{stamp}] " + " ".join(words))
                    if msg.get("finished"):
                        break
                sender_task.cancel()
        except Exception as err:  # noqa: BLE001 - surface in UI
            async with self:
                self.soniox_status = f"error: {err}"
            return
        async with self:
            self.soniox_status = "stopped"


def caption_panel(title: str, status: rx.Var, finals: rx.Var, pending: rx.Var,
                  first_a_label: str, first_a: rx.Var,
                  first_b_label: str, first_b: rx.Var) -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.heading(title, size="4"),
            rx.badge(status),
            rx.spacer(),
            rx.text(f"{first_a_label}: ", rx.code(first_a), size="2"),
            rx.text(f"{first_b_label}: ", rx.code(first_b), size="2"),
            width="100%",
        ),
        rx.divider(),
        rx.scroll_area(
            rx.vstack(
                rx.foreach(finals, lambda line: rx.text(line, size="3")),
                rx.text(pending, size="3", color=rx.color("gray", 9), font_style="italic"),
                width="100%",
                align="start",
            ),
            width="100%",
            height="55vh",
        ),
        width="100%",
    )


def index() -> rx.Component:
    return rx.container(
        rx.heading("relay — live caption compare", size="6"),
        rx.text(
            "Play the audio, seek anywhere, hit Start — both providers "
            "transcribe from that point at realtime pace. Grey = partial "
            "(refining), solid = final. Press Stop, seek, Start again to "
            "re-sync after scrubbing.",
            size="2",
            color=rx.color("gray", 11),
        ),
        rx.el.audio(
            src="/sermon-16k.wav",
            id="sermon-audio",
            controls=True,
            width="100%",
        ),
        rx.hstack(
            rx.button(
                "Start captions",
                on_click=CaptionState.read_position_and_start,
                disabled=CaptionState.running,
            ),
            rx.button(
                "Stop",
                on_click=CaptionState.stop_streams,
                disabled=~CaptionState.running,
                color_scheme="red",
            ),
            rx.cond(
                CaptionState.running,
                rx.text("streaming from ", rx.code(CaptionState.start_pos_label)),
                rx.text("idle"),
            ),
        ),
        rx.grid(
            caption_panel(
                "Speechmatics Enhanced",
                CaptionState.sm_status,
                CaptionState.sm_finals,
                CaptionState.sm_pending,
                "1st partial", CaptionState.sm_first_partial_ms,
                "1st final", CaptionState.sm_first_final_ms,
            ),
            caption_panel(
                "Soniox stt-rt-v5",
                CaptionState.soniox_status,
                CaptionState.soniox_finals,
                CaptionState.soniox_pending,
                "1st token", CaptionState.soniox_first_token_ms,
                "1st final", CaptionState.soniox_first_final_ms,
            ),
            columns="2",
            width="100%",
        ),
        max_width="1200px",
        margin_y="4",
    )


app = rx.App()
app.add_page(index, route="/")
