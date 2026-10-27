"""The virtual player state: clock, Speechmatics stream, TTS review.

The wall-clock-paced timeline walks the sermon at real listening pace
and streams each 100 ms chunk the playhead passes to Speechmatics
(English STT + Hindi translation in one session, EU endpoint). Pause
and seek close the session - billing stops - and Start reopens one
clean at the new position, so no stale chunks carry over.

Translated Hindi sentences each get a play button in the UI; clicking
it synthesises the sentence with Azure TTS (UK South) and plays it in
the browser for ear-grading.
"""

import asyncio
import base64
import os

import reflex as rx

from . import audio, azure_tts
from .providers import SpeechmaticsSession


class PlayerState(rx.State):
    position: float = 0.0
    playing: bool = False

    # captions from the Speechmatics session
    sm_status: str = ""
    en_fragments: list[dict] = []  # {"start", "end", "text"}
    hindi_sentences: list[dict] = []  # {"idx", "at", "start", "end", "text"}

    # TTS review state: which sentence is speaking
    tts_idx: int = -1

    # backend-only: bumping this invalidates the in-flight loop
    _gen: int = 0
    # backend-only: true while the slider thumb is being dragged
    _seeking: bool = False

    # ---- controls ----

    def toggle(self) -> rx.event.EventSpec | None:
        """One button: play when paused, pause when playing."""
        if self.playing:
            self.stop()
            return None
        return self.start()

    def start(self) -> rx.event.EventSpec | None:
        if self.position >= audio.DURATION_SECS:
            self.position = 0.0
        if self.position == 0.0:
            # fresh run from the top: clear the old captions
            self.sm_status = ""
            self.en_fragments = []
            self.hindi_sentences = []
        self._gen += 1
        self.playing = True
        return PlayerState.run(self._gen, self.position)

    def stop(self) -> None:
        self.playing = False
        self._gen += 1

    # ---- seeking ----

    def drag(self, values: list[float]) -> None:
        """Live preview while the thumb moves: move the playhead only."""
        self._seeking = True
        self.position = min(values[0], audio.DURATION_SECS)

    def seek(self, values: list[float]) -> None:
        """Seek committed on release: clear the caption buffers and
        restart the chunk stream clean at the new position - nothing
        carries over, so no gaps between old and new transcript."""
        self._seeking = False
        self.position = min(values[0], audio.DURATION_SECS)
        self.en_fragments = []
        self.hindi_sentences = []
        self.sm_status = ""
        self.tts_idx = -1
        if self.position >= audio.DURATION_SECS:
            self.playing = False
            self._gen += 1
            return
        if self.playing:
            self._gen += 1
            return PlayerState.run(self._gen, self.position)

    # ---- the virtual clock and the chunk streamer ----

    @rx.event(background=True)
    async def run(self, gen: int, start_pos: float) -> None:
        """Advance the playhead at true realtime pace and stream every
        chunk it passes to Speechmatics - never ahead of the clock."""
        loop = asyncio.get_running_loop()

        async def on_final(line: str, start: float, end: float) -> None:
            async with self:
                if self._gen == gen and line.strip():
                    self.en_fragments.append(
                        {
                            "start": round(start, 2),
                            "end": round(end, 2),
                            "text": line,
                        }
                    )

        async def on_translation(text: str, start: float, end: float) -> None:
            async with self:
                if self._gen == gen and text.strip():
                    self.hindi_sentences.append(
                        {
                            "idx": len(self.hindi_sentences),
                            "at": audio.fmt_mmss(start),
                            "start": round(start, 2),
                            "end": round(end, 2),
                            "text": text,
                        }
                    )

        async def on_error(msg: str) -> None:
            async with self:
                if self._gen == gen:
                    self.sm_status = msg

        # open the provider session (pause/seek close it; Start reopens)
        session = None
        key = os.environ.get("SPEECHMATICS_API_KEY")
        if key:
            session = SpeechmaticsSession(key)
            try:
                await session.open(on_final, on_translation, on_error)
            except Exception as err:  # noqa: BLE001
                await session.close()
                session = None
                async with self:
                    if self._gen == gen:
                        self.sm_status = f"error: {err}"
        if session is None:
            async with self:
                if self._gen == gen and not self.sm_status:
                    self.sm_status = "no SPEECHMATICS_API_KEY"

        t0 = loop.time()
        cursor = int(start_pos * audio.BYTES_PER_SEC)
        try:
            while True:
                await asyncio.sleep(audio.CHUNK_MS / 1000)
                async with self:
                    if self._gen != gen or not self.playing:
                        return
                    if self._seeking:
                        # a drag is in flight: don't fight it with clock
                        # writes - the seek will restart this loop anyway
                        continue
                    elapsed = loop.time() - t0
                    pos = min(start_pos + elapsed, audio.DURATION_SECS)
                    self.position = pos
                    ended = pos >= audio.DURATION_SECS
                # stream every chunk the playhead has now passed
                target_byte = int(pos * audio.BYTES_PER_SEC)
                while cursor < target_byte:
                    if session is not None:
                        await session.send(
                            audio.SERMON_PCM[
                                cursor : cursor + audio.CHUNK_BYTES
                            ]
                        )
                    cursor += audio.CHUNK_BYTES
                if ended:
                    # audio exhausted: flush and drain the last finals
                    if session is not None:
                        await session.finish()
                        await session.wait_done()
                    return
        finally:
            if session is not None:
                await session.close()
            async with self:
                if self._gen == gen:
                    self.playing = False

    # ---- TTS review ----

    async def play_sentence(self, idx: int) -> rx.event.EventSpec:
        """Speak one Hindi sentence straight from the click: Azure TTS
        synthesises it, then JS plays the audio directly - no visible
        audio player involved."""
        if idx >= len(self.hindi_sentences):
            return []
        text = self.hindi_sentences[idx]["text"]
        self.tts_idx = idx
        # synth in a worker thread so the UI stays responsive
        data = await asyncio.to_thread(azure_tts.synthesize, text)
        b64 = base64.b64encode(data).decode()
        # one shared Audio object: a new click swaps the src, which
        # also stops whatever was playing
        return rx.call_script(
            "window._tts = window._tts || new Audio();"
            f"window._tts.src = 'data:audio/mpeg;base64,{b64}';"
            "window._tts.play();"
        )

    # ---- readouts ----

    @rx.var
    def en_text(self) -> str:
        """The English transcript as flowing text."""
        return " ".join(f["text"] for f in self.en_fragments)

    @rx.var
    def paired_sentences(self) -> list[dict]:
        """Hindi sentences with the English words they translate
        attached: the fragments whose span overlaps the sentence's
        span in the original audio."""
        out: list[dict] = []
        for h in self.hindi_sentences:
            en = " ".join(
                f["text"]
                for f in self.en_fragments
                if f["end"] > h["start"] and f["start"] < h["end"]
            )
            out.append({**h, "en": en})
        return out

    @rx.var
    def chunks_read(self) -> int:
        return int(self.position * 1000 / audio.CHUNK_MS)

    @rx.var
    def elapsed_label(self) -> str:
        return audio.fmt_mmss(self.position)

    @rx.var
    def remaining_label(self) -> str:
        return "-" + audio.fmt_mmss(audio.DURATION_SECS - self.position)

    @rx.var
    def chunk_label(self) -> str:
        return f"{self.chunks_read} chunks read ({audio.CHUNK_MS} ms each)"
