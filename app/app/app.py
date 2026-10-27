"""relay sermon player — the whole sermon in memory, one audio player.

Deliberately minimal: no providers, no caption logic yet. The full PCM
sits in memory (SERMON_PCM) ready for streaming when that comes back.
"""

import wave
from pathlib import Path

import reflex as rx

AUDIO_PATH = Path(__file__).resolve().parents[1] / "assets" / "sermon-16k.wav"

# entire sermon loaded into memory once at startup
with wave.open(str(AUDIO_PATH), "rb") as _w:
    SERMON_PCM = _w.readframes(_w.getnframes())
DURATION_SECS = len(SERMON_PCM) // 32000  # 16 kHz * 2 bytes, mono


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
                rx.el.audio(
                    src="/sermon-16k.wav",
                    id="sermon-audio",
                    controls=True,
                    width="100%",
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
