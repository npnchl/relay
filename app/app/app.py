"""relay sermon player — sermon in memory, player-driven chunk streamer.

The player reports its position continuously (on_time_update). The
chunk streamer reads 100 ms chunks straight from the in-memory sermon
and only sends audio the player has actually reached - the stream is
locked to the player's own clock, so it cannot drift, and pause/seek
stop/restart it exactly.

The sink is pluggable: today it counts chunks (free to test); provider
WebSockets attach at the same point later.
"""

import asyncio
import wave
from pathlib import Path

import reflex as rx
from reflex_components_react_player.audio import Audio
from reflex_components_react_player.react_player import ReactPlayerEvent

AUDIO_PATH = Path(__file__).resolve().parents[1] / "assets" / "sermon-16k.wav"

BYTES_PER_SEC = 32000  # 16 kHz * 2 bytes, mono
CHUNK_MS = 100
CHUNK_BYTES = int(BYTES_PER_SEC * CHUNK_MS / 1000)

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


class PlayerState(rx.State):
    position: float = 0.0
    duration: float = float(DURATION_SECS)

    # chunk streamer state (player-driven)
    streaming: bool = False
    stream_pos: float = 0.0
    chunks_sent: int = 0

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
            # restart the stream from the new spot
            self._gen += 1
            return PlayerState.stream_chunks(self._gen, position)
        return None

    # ---- chunk streamer: player-locked, pluggable sink ----

    @rx.event(background=True)
    async def stream_chunks(self, gen: int, start_pos: float) -> None:
        async with self:
            self.streaming = True
            self.stream_pos = start_pos
            self.chunks_sent = 0
        cursor = int(start_pos * BYTES_PER_SEC)
        try:
            while cursor < len(SERMON_PCM):
                async with self:
                    if self._gen != gen:
                        return
                    player_byte = int(self.position * BYTES_PER_SEC)
                # never run ahead of what the listener has heard:
                # stream only what the player has reached (within one chunk)
                if cursor > player_byte + CHUNK_BYTES:
                    await asyncio.sleep(CHUNK_MS / 2000)
                    continue
                chunk = SERMON_PCM[cursor : cursor + CHUNK_BYTES]
                # ---- pluggable sink: providers attach here ----
                await self.sink(chunk)
                cursor += CHUNK_BYTES
                async with self:
                    if self._gen == gen:
                        self.chunks_sent += 1
                        self.stream_pos = cursor / BYTES_PER_SEC
        finally:
            async with self:
                if self._gen == gen:
                    self.streaming = False

    async def sink(self, chunk: bytes) -> None:
        """Where chunks go. Today: nowhere (counting only, free to test).
        Speechmatics/Soniox WebSockets attach here later."""
        await asyncio.sleep(0)  # keep it a coroutine from day one

    @rx.var
    def position_label(self) -> str:
        return f"{fmt_mmss(self.position)} / {fmt_mmss(self.duration)}"

    @rx.var
    def stream_label(self) -> str:
        if not self.streaming:
            return "not streaming"
        return (
            f"streamed {fmt_mmss(self.stream_pos)} · {self.chunks_sent} chunks"
        )


def index() -> rx.Component:
    return rx.theme(
        rx.el.div(
            rx.vstack(
                rx.heading("relay — sermon player", size="6"),
                rx.text(
                    f"full sermon in memory · {DURATION_SECS // 60} min",
                    size="3",
                    color=rx.color("gray", 11),
                ),
                FixedAudio.create(
                    src="/sermon-16k.wav",
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
                    font_family="ui-monospace, Menlo, monospace",
                ),
                rx.text(
                    PlayerState.stream_label,
                    size="3",
                    color=rx.color("gray", 11),
                    font_family="ui-monospace, Menlo, monospace",
                ),
                spacing="6",
                width="100%",
                align="center",
            ),
            style={
                "max_width": "900px",
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
