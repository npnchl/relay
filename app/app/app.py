"""relay live compare — sermon in memory, player-driven chunk streamer,
caption panels for Speechmatics and Soniox.

The player reports its position continuously (on_time_update). The
chunk streamer reads 100 ms chunks straight from the in-memory sermon
and only sends audio the player has actually reached - locked to the
player's own clock, so it cannot drift, and pause/seek stop/restart it
exactly. Each chunk fans out to both provider sessions (providers.py);
their captions render in the panels below the player.
"""

import asyncio
import os
import wave
from pathlib import Path

import reflex as rx
from reflex_components_react_player.audio import Audio
from reflex_components_react_player.react_player import ReactPlayerEvent

from .providers import SonioxSession, SpeechmaticsSession

APP_DIR = Path(__file__).resolve().parents[1]
AUDIO_PATH = APP_DIR / "assets" / "sermon-16k.wav"
ENV_PATH = APP_DIR.parent / ".env"

BYTES_PER_SEC = 32000  # 16 kHz * 2 bytes, mono
CHUNK_MS = 100
CHUNK_BYTES = int(BYTES_PER_SEC * CHUNK_MS / 1000)


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_env()

# entire sermon loaded into memory once at startup
with wave.open(str(AUDIO_PATH), "rb") as _w:
    SERMON_PCM = _w.readframes(_w.getnframes())
DURATION_SECS = len(SERMON_PCM) // BYTES_PER_SEC


# The bundled rx.audio component's event signatures read
# event.target.api.playerInfo.*, which does not exist in the pinned
# react-player 3.4.0 (a plain native <audio> element) - every value
# extracted as 0. These signatures read the native element directly.
def _current_time(event: ReactPlayerEvent) -> list:
    return [event["target"]["currentTime"].to(float)]  # type: ignore[attr-defined]


def _duration(event: ReactPlayerEvent) -> list:
    return [event["target"]["duration"].to(float)]  # type: ignore[attr-defined]


class FixedAudio(Audio):
    """rx.audio with event signatures matching react-player 3.4.0."""


_triggers = dict(Audio.get_event_triggers())
_triggers["on_time_update"] = _current_time
_triggers["on_duration_change"] = _duration
_triggers["on_seeked"] = _current_time
FixedAudio._event_triggers_cache = _triggers


def fmt_mmss(secs: float) -> str:
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"


async def _noop(text: str) -> None:
    """Partials disabled: finals only, per the captions decision."""


class PlayerState(rx.State):
    position: float = 0.0
    duration: float = float(DURATION_SECS)

    streaming: bool = False
    stream_pos: float = 0.0
    chunks_sent: int = 0

    sm_status: str = ""
    sm_text: str = ""
    sm_pending: str = ""
    soniox_status: str = ""
    soniox_text: str = ""
    soniox_pending: str = ""

    # backend-only: bumping this invalidates the in-flight streamer
    _gen: int = 0

    # ---- player events ----

    def time_update(self, position: float) -> None:
        self.position = position

    def duration_change(self, duration: float) -> None:
        self.duration = duration

    def on_play(self) -> rx.event.EventSpec | None:
        self._gen += 1
        return PlayerState.stream_chunks(self._gen, self.position)

    def on_pause(self) -> None:
        self._gen += 1

    def on_seeked(self, position: float) -> None:
        self.position = position
        if self.streaming:
            self._gen += 1
            return PlayerState.stream_chunks(self._gen, position)
        return None

    # ---- player-locked chunk streamer fanning out to providers ----

    @rx.event(background=True)
    async def stream_chunks(self, gen: int, start_pos: float) -> None:
        async with self:
            self.streaming = True
            self.stream_pos = start_pos
            self.chunks_sent = 0
            self.sm_status = ""
            self.sm_text = ""
            self.sm_pending = ""
            self.soniox_status = ""
            self.soniox_text = ""
            self.soniox_pending = ""

        async def on_sm_final(line: str) -> None:
            async with self:
                if self._gen == gen and line.strip():
                    self.sm_text = (self.sm_text + " " + line).strip()
                    self.sm_pending = ""

        async def on_sm_pending(text: str) -> None:
            async with self:
                if self._gen == gen and text.strip():
                    self.sm_pending = text

        async def on_sx_pending(text: str) -> None:
            async with self:
                if self._gen == gen and text.strip():
                    self.soniox_pending = text

        async def on_sx_final(line: str) -> None:
            async with self:
                if self._gen == gen and line.strip():
                    self.soniox_text = (self.soniox_text + " " + line).strip()
                    self.soniox_pending = ""



        async def on_sm_error(msg: str) -> None:
            async with self:
                if self._gen == gen:
                    self.sm_status = msg

        async def on_sx_error(msg: str) -> None:
            async with self:
                if self._gen == gen:
                    self.soniox_status = msg

        sessions: list = []
        sm_key = os.environ.get("SPEECHMATICS_API_KEY")
        sx_key = os.environ.get("SONIOX_API_KEY")
        if sm_key:
            sm = SpeechmaticsSession(sm_key)
            sessions.append((sm, on_sm_final, on_sm_pending, on_sm_error))
        if sx_key:
            sx = SonioxSession(sx_key)
            sessions.append((sx, on_sx_final, on_sx_pending, on_sx_error))
        async with self:
            if not sm_key:
                self.sm_status = "no SPEECHMATICS_API_KEY"
            if not sx_key:
                self.soniox_status = "no SONIOX_API_KEY"

        try:
            for session, on_final, on_pending, on_error in sessions:
                await session.open(on_final, on_pending, on_error)
        except Exception as err:  # noqa: BLE001
            for session, _, _, _ in sessions:
                await session.close()
            async with self:
                if self._gen == gen:
                    self.streaming = False
                    self.sm_status = self.sm_status or f"error: {err}"
            return

        cursor = int(start_pos * BYTES_PER_SEC)
        try:
            while cursor < len(SERMON_PCM):
                async with self:
                    if self._gen != gen:
                        return
                    player_byte = int(self.position * BYTES_PER_SEC)
                # never run ahead of what the listener has heard
                if cursor > player_byte + CHUNK_BYTES:
                    await asyncio.sleep(CHUNK_MS / 2000)
                    continue
                chunk = SERMON_PCM[cursor : cursor + CHUNK_BYTES]
                for session, _, _, _ in sessions:
                    await session.send(chunk)
                cursor += CHUNK_BYTES
                async with self:
                    if self._gen == gen:
                        self.chunks_sent += 1
                        self.stream_pos = cursor / BYTES_PER_SEC
            # audio exhausted: tell both providers and drain their finals
            for session, _, _, _ in sessions:
                await session.finish()
            for session, _, _, _ in sessions:
                await session.wait_done()
        finally:
            for session, _, _, _ in sessions:
                await session.close()
            async with self:
                if self._gen == gen:
                    self.streaming = False

    @rx.var
    def position_label(self) -> str:
        return f"{fmt_mmss(self.position)} / {fmt_mmss(self.duration)}"

    @rx.var
    def stream_label(self) -> str:
        if not self.streaming:
            return "not streaming"
        return f"streamed {fmt_mmss(self.stream_pos)} · {self.chunks_sent} chunks"


MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def caption_panel(
    title: str,
    status: rx.Var,
    text: rx.Var,
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
                    rx.text(
                        text,
                        size="3",
                        font_family=MONO,
                        line_height="1.7",
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
                height="45vh",
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
                FixedAudio.create(
                    src="/sermon-16k.wav",
                    id="sermon-audio",
                    controls=True,
                    width="100%",
                    on_time_update=PlayerState.time_update,
                    on_duration_change=PlayerState.duration_change,
                    on_play=PlayerState.on_play,
                    on_pause=PlayerState.on_pause,
                    on_ended=PlayerState.on_pause,
                    on_seeked=PlayerState.on_seeked,
                ),
                rx.text(
                    PlayerState.position_label,
                    size="4",
                    font_family=MONO,
                ),
                rx.text(
                    PlayerState.stream_label,
                    size="3",
                    color=rx.color("gray", 11),
                    font_family=MONO,
                ),
                rx.el.div(
                    caption_panel(
                        "Speechmatics Enhanced",
                        PlayerState.sm_status,
                        PlayerState.sm_text,
                        PlayerState.sm_pending,
                    ),
                    caption_panel(
                        "Soniox stt-rt-v5",
                        PlayerState.soniox_status,
                        PlayerState.soniox_text,
                        PlayerState.soniox_pending,
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
