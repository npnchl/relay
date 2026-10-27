"""relay sermon player — the whole sermon in memory, one audio player.

rx.audio (react-player) reports its state straight into Python state:
position via on_time_update, duration via on_duration_change, and
play/pause/seeked as events. No JS bridges - the player's own clock
is the source of truth for anything layered on top (e.g. captions).
"""

import wave
from pathlib import Path

import reflex as rx
from reflex_components_react_player.audio import Audio
from reflex_components_react_player.react_player import ReactPlayerEvent

AUDIO_PATH = Path(__file__).resolve().parents[1] / "assets" / "sermon-16k.wav"

# entire sermon loaded into memory once at startup
with wave.open(str(AUDIO_PATH), "rb") as _w:
    SERMON_PCM = _w.readframes(_w.getnframes())
DURATION_SECS = len(SERMON_PCM) // 32000  # 16 kHz * 2 bytes, mono


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

    def time_update(self, position: float) -> None:
        """The player tells us where it is, continuously."""
        self.position = position

    def duration_change(self, duration: float) -> None:
        self.duration = duration

    @rx.var
    def position_label(self) -> str:
        return f"{fmt_mmss(self.position)} / {fmt_mmss(self.duration)}"


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
                ),
                rx.text(
                    PlayerState.position_label,
                    size="4",
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
