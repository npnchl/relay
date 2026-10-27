"""relay translation evaluation — the UI.

Thin layer: components and page wiring only. Behaviour lives in
player.py (state) and audio.py (the in-memory sermon).
"""

import reflex as rx

from . import audio
from .player import PlayerState

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def index() -> rx.Component:
    return rx.theme(
        rx.el.div(
            rx.vstack(
                rx.heading("relay — virtual player", size="6"),
                rx.hstack(
                    rx.button(
                        rx.cond(PlayerState.playing, "Pause", "Play"),
                        on_click=PlayerState.toggle,
                        size="3",
                        px="5",
                        font_weight="medium",
                    ),
                    rx.slider(
                        value=[PlayerState.position],
                        min=0,
                        max=audio.DURATION_SECS,
                        step=1,
                        on_change=PlayerState.drag,
                        on_value_commit=PlayerState.seek,
                        flex_grow="1",
                    ),
                    rx.text(
                        PlayerState.position_label,
                        size="2",
                        font_family=MONO,
                        white_space="nowrap",
                        color=rx.color("gray", 11),
                    ),
                    align="center",
                    width="100%",
                    gap="5",
                ),
                rx.text(
                    PlayerState.chunk_label,
                    size="3",
                    color=rx.color("gray", 11),
                    font_family=MONO,
                ),
                spacing="5",
                align="center",
                width="100%",
            ),
            style={
                "max_width": "640px",
                "margin": "0 auto",
                "padding": "48px 32px",
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
app.add_page(index)
