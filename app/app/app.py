"""relay translation evaluation — the UI.

Thin layer: components and page wiring only. Behaviour lives in
player.py (clock, captions, TTS), providers.py (Speechmatics) and
audio.py / azure_tts.py (sermon and speech).
"""

import reflex as rx

from . import audio
from .player import PlayerState

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def hindi_row(sent: rx.Var) -> rx.Component:
    """One translated sentence: play button, timestamp, Devanagari text."""
    return rx.hstack(
        rx.icon_button(
            rx.icon("volume-2"),
            on_click=PlayerState.play_sentence(sent["idx"]),
            size="1",
            variant="soft",
            color_scheme=(
                rx.cond(PlayerState.tts_idx == sent["idx"], "teal", "gray")
            ),
        ),
        rx.text(
            sent["at"],
            size="1",
            font_family=MONO,
            color=rx.color("gray", 10),
            white_space="nowrap",
        ),
        rx.text(
            sent["text"],
            size="3",
            width="100%",
        ),
        align="start",
        gap="3",
        width="100%",
    )


def caption_panel(title: str, status: rx.Var, body: rx.Component) -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.heading(title, size="4", weight="medium"),
                rx.spacer(),
                # only surface problems; no idle/live chrome
                rx.cond(
                    status != "",
                    rx.badge(status, color_scheme="red", variant="soft"),
                ),
                width="100%",
                align="center",
            ),
            body,
            spacing="3",
            width="100%",
        ),
        width="100%",
        padding="4",
        size="3",
    )


def english_panel() -> rx.Component:
    return caption_panel(
        "English — Speechmatics Enhanced",
        PlayerState.sm_status,
        rx.scroll_area(
            rx.text(
                rx.cond(
                    PlayerState.en_text != "",
                    PlayerState.en_text,
                    "…",
                ),
                size="2",
                font_family=MONO,
                line_height="1.7",
            ),
            width="100%",
            height="20vh",
        ),
    )


def hindi_panel() -> rx.Component:
    return caption_panel(
        "हिन्दी — translation, spoken by Azure TTS",
        rx.cond(
            PlayerState.tts_loading,
            rx.badge("speaking…", color_scheme="teal", variant="soft"),
            PlayerState.sm_status,
        ),
        rx.scroll_area(
            rx.vstack(
                rx.foreach(PlayerState.hindi_sentences, hindi_row),
                rx.cond(
                    PlayerState.hindi_sentences.length() == 0,
                    rx.text("…", size="2", color=rx.color("gray", 10)),
                ),
                spacing="3",
                width="100%",
            ),
            width="100%",
            height="32vh",
        ),
    )


def index() -> rx.Component:
    return rx.theme(
        rx.el.div(
            rx.vstack(
                rx.heading("relay — translation eval", size="6"),
                rx.vstack(
                    rx.hstack(
                        rx.icon_button(
                            rx.cond(
                                PlayerState.playing,
                                rx.icon("pause"),
                                rx.icon("play"),
                            ),
                            on_click=PlayerState.toggle,
                        ),
                        rx.text(
                            PlayerState.elapsed_label,
                            size="2",
                            font_family=MONO,
                            white_space="nowrap",
                            color=rx.color("gray", 11),
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
                            PlayerState.remaining_label,
                            size="2",
                            font_family=MONO,
                            white_space="nowrap",
                            color=rx.color("gray", 11),
                        ),
                        align="center",
                        width="100%",
                        gap="4",
                    ),
                    rx.text(
                        PlayerState.chunk_label,
                        size="1",
                        font_family=MONO,
                        color=rx.color("gray", 10),
                        align_self="end",
                    ),
                    spacing="1",
                    width="100%",
                ),
                english_panel(),
                hindi_panel(),
                # the TTS audio player: invisible, driven by play buttons
                rx.cond(
                    PlayerState.tts_src != "",
                    rx.audio(
                        src=PlayerState.tts_src,
                        auto_play=True,
                        style={"display": "none"},
                    ),
                ),
                spacing="5",
                align="center",
                width="100%",
            ),
            style={
                "max_width": "760px",
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
