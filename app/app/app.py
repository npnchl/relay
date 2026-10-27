"""live compare: interactive A/B of Speechmatics vs Soniox on the sermon.

The browser's audio player drives everything: press play and both
providers start transcribing from the current position at true realtime
pace; pause and the sessions stop. Partials render in grey (refining),
finals in solid with [MM:SS] sermon timestamps.

Run:  cd app && uv run reflex run   (keys loaded from ../.env)
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
from reflex_base.components.component import DEFAULT_TRIGGERS
from reflex_components_core.el.elements.media import Audio

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class MediaAudio(Audio):
    """rx.el.audio with the native media events Reflex omits (play/pause/ended)."""


MediaAudio._event_triggers_cache = dict(DEFAULT_TRIGGERS) | {
    "on_play": DEFAULT_TRIGGERS["on_mount"],
    "on_pause": DEFAULT_TRIGGERS["on_mount"],
    "on_ended": DEFAULT_TRIGGERS["on_mount"],
}

AUDIO_PATH = Path(__file__).resolve().parents[1] / "assets" / "sermon-16k.wav"
CHUNK_MS = 100
BYTES_PER_SEC = 32000
SM_URL = "wss://eu.rt.speechmatics.com/v2"
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


def read_pcm() -> bytes:
    with wave.open(str(AUDIO_PATH), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("need 16 kHz mono 16-bit WAV at app/assets/sermon-16k.wav")
        return w.readframes(w.getnframes())


PCM = read_pcm()


def fmt_mmss(secs: float) -> str:
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"


class CaptionState(rx.State):
    sm_status: str = ""
    soniox_status: str = ""
    sm_finals: list[str] = []
    sm_pending: str = ""
    soniox_finals: list[str] = []
    soniox_pending: str = ""

    # backend-only: bumping this invalidates any in-flight sessions
    _gen: int = 0

    def player_play(self) -> rx.event.EventSpec:
        return rx.call_script(
            "document.getElementById('sermon-audio').currentTime",
            callback=CaptionState.start_from_position,
        )

    def player_pause(self) -> None:
        self._gen += 1
        self.sm_pending = ""
        self.soniox_pending = ""

    def start_from_position(self, pos: float) -> rx.event.EventSpec | None:
        start_pos = max(0.0, min(float(pos), len(PCM) / BYTES_PER_SEC - 0.1))
        self._gen += 1
        return CaptionState.stream_both(self._gen, start_pos)

    @rx.event(background=True)
    async def stream_both(self, gen: int, start_pos: float) -> None:
        async with self:
            self.sm_status = ""
            self.soniox_status = ""
            self.sm_finals = []
            self.sm_pending = ""
            self.soniox_finals = []
            self.soniox_pending = ""
        await asyncio.gather(
            self.run_speechmatics(gen, start_pos),
            self.run_soniox(gen, start_pos),
            return_exceptions=True,
        )

    # ---------- Speechmatics ----------

    async def run_speechmatics(self, gen: int, start_pos: float) -> None:
        key = os.environ.get("SPEECHMATICS_API_KEY")
        if not key:
            async with self:
                self.sm_status = "no SPEECHMATICS_API_KEY"
            return
        chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)
        offset_bytes = int(start_pos * BYTES_PER_SEC)
        start = perf_counter()

        async def sender(ws) -> None:  # type: ignore[no-untyped-def]
            i = 0
            next_at = perf_counter()
            while offset_bytes + i * chunk_bytes < len(PCM):
                if self._gen != gen:
                    return
                await ws.send(
                    PCM[
                        offset_bytes
                        + i * chunk_bytes : offset_bytes
                        + (i + 1) * chunk_bytes
                    ]
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
                    if self._gen != gen:
                        return
                    reply = json.loads(await ws.recv())
                    msg = reply.get("message")
                    if msg == "RecognitionStarted":
                        break
                    if msg == "Error":
                        raise RuntimeError(str(reply))
                sender_task = asyncio.create_task(sender(ws))
                while True:
                    try:
                        if self._gen != gen:
                            break
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    except asyncio.TimeoutError:
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
                            async with self:
                                if self._gen == gen:
                                    self.sm_pending = " ".join(pending_now)
                        if finals_now:
                            stamp = fmt_mmss(start_pos + (perf_counter() - start))
                            async with self:
                                if self._gen == gen:
                                    self.sm_pending = ""
                                    self.sm_finals.append(
                                        f"[{stamp}] " + " ".join(finals_now)
                                    )
                    elif kind == "EndOfTranscript":
                        break
                    elif kind == "Error":
                        raise RuntimeError(str(msg))
                sender_task.cancel()
        except Exception as err:  # noqa: BLE001 - surface in UI
            async with self:
                if self._gen == gen:
                    self.sm_status = f"error: {err}"

    # ---------- Soniox ----------

    async def run_soniox(self, gen: int, start_pos: float) -> None:
        key = os.environ.get("SONIOX_API_KEY")
        if not key:
            async with self:
                self.soniox_status = "no SONIOX_API_KEY"
            return
        chunk_bytes = int(BYTES_PER_SEC * CHUNK_MS / 1000)
        offset_bytes = int(start_pos * BYTES_PER_SEC)
        start = perf_counter()

        def assemble(tokens: list[dict]) -> list[str]:
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
            return words

        async def sender(ws) -> None:  # type: ignore[no-untyped-def]
            i = 0
            next_at = perf_counter()
            while offset_bytes + i * chunk_bytes < len(PCM):
                if self._gen != gen:
                    return
                await ws.send(
                    PCM[
                        offset_bytes
                        + i * chunk_bytes : offset_bytes
                        + (i + 1) * chunk_bytes
                    ]
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
                            "client_reference_id": (
                                "live-"
                                + datetime.now(timezone.utc).strftime(
                                    "%Y%m%dT%H%M%S"
                                )
                            ),
                        }
                    )
                )
                sender_task = asyncio.create_task(sender(ws))
                while True:
                    try:
                        if self._gen != gen:
                            break
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    except asyncio.TimeoutError:
                        continue
                    if msg.get("error_code"):
                        raise RuntimeError(
                            f"{msg.get('error_code')}: {msg.get('error_message')}"
                        )
                    tokens = msg.get("tokens", [])
                    final_tokens = [t for t in tokens if t.get("is_final")]
                    nonfinal = [t for t in tokens if not t.get("is_final")]
                    if nonfinal:
                        pending = "".join(str(t.get("text", "")) for t in nonfinal)
                        async with self:
                            if self._gen == gen:
                                self.soniox_pending = pending.strip()
                    if final_tokens:
                        words = assemble(final_tokens)
                        if words:
                            stamp = fmt_mmss(
                                start_pos + (perf_counter() - start)
                            )
                            async with self:
                                if self._gen == gen:
                                    self.soniox_pending = ""
                                    self.soniox_finals.append(
                                        f"[{stamp}] " + " ".join(words)
                                    )
                    if msg.get("finished"):
                        break
                sender_task.cancel()
        except Exception as err:  # noqa: BLE001 - surface in UI
            async with self:
                if self._gen == gen:
                    self.soniox_status = f"error: {err}"


MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def caption_panel(
    title: str,
    accent: str,
    status: rx.Var,
    finals: rx.Var,
    pending: rx.Var,
) -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.heading(title, size="5", weight="medium"),
                rx.spacer(),
                # only surface problems; no idle/live chrome
                rx.cond(
                    status != "",
                    rx.badge(status, color_scheme="red", variant="soft"),
                ),
                width="100%",
                align="center",
            ),
            rx.scroll_area(
                rx.vstack(
                    rx.foreach(
                        finals,
                        lambda line: rx.text(
                            line,
                            size="3",
                            font_family=MONO,
                            line_height="1.7",
                            padding_left="0.5em",
                            border_left="2px solid color-mix(in srgb, "
                            + accent
                            + " 25%, transparent)",
                        ),
                    ),
                    rx.text(
                        pending,
                        size="3",
                        color=rx.color("gray", 10),
                        italic=True,
                        font_family=MONO,
                        line_height="1.7",
                    ),
                    width="100%",
                    align="start",
                    spacing="1",
                ),
                width="100%",
                height="60vh",
            ),
        ),
        width="100%",
        padding="5",
        size="3",
    )


def index() -> rx.Component:
    return rx.theme(
        rx.el.div(
            rx.vstack(
                rx.heading("relay — live caption compare", size="6"),
                MediaAudio.create(
                    src="/sermon-16k.wav",
                    id="sermon-audio",
                    controls=True,
                    width="100%",
                    on_play=CaptionState.player_play,
                    on_pause=CaptionState.player_pause,
                    on_ended=CaptionState.player_pause,
                ),
                rx.el.div(
                    caption_panel(
                        "Speechmatics Enhanced",
                        "var(--teal-9)",
                        CaptionState.sm_status,
                        CaptionState.sm_finals,
                        CaptionState.sm_pending,
                    ),
                    caption_panel(
                        "Soniox stt-rt-v5",
                        "var(--violet-9)",
                        CaptionState.soniox_status,
                        CaptionState.soniox_finals,
                        CaptionState.soniox_pending,
                    ),
                    style={
                        "display": "grid",
                        "grid_template_columns": "1fr 1fr",
                        "gap": "24px",
                        "width": "100%",
                    },
                ),
                spacing="6",
                width="100%",
                align="center",
            ),
            style={
                "max_width": "1160px",
                "margin": "0 auto",
                "padding": "48px 32px 64px 32px",
                "box_sizing": "border-box",
                "min_height": "100vh",
                "width": "100%",
            },
        ),
        appearance="dark",
        accent_color="teal",
        radius="large",
    )


app = rx.App()
app.add_page(index, route="/")
