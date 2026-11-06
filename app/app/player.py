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
import re

import reflex as rx

from . import audio, azure_tts, opper
from .providers import SpeechmaticsSession

# three or more consecutive Latin-letter words inside a Hindi
# sentence: the vendor passed source text through untranslated
LATIN_RUN = re.compile(r"[A-Za-z']+(?:\s+[A-Za-z']+){2,}")


class PlayerState(rx.State):
    position: float = 0.0
    playing: bool = False
    # switchable audio source (see audio.SOURCE_WINDOWS)
    source: str = audio.DEFAULT_SOURCE
    duration_secs: float = audio.DURATION_SECS

    # captions from the Speechmatics session
    sm_status: str = ""
    en_fragments: list[dict] = []  # {"start", "end", "text"}
    hindi_sentences: list[dict] = []  # {"idx", "at", "start", "end", "text"}

    # translations from the Opper arm (one per finalised fragment)
    haiku_status: str = ""
    haiku_sentences: list[dict] = []  # same shape as hindi_sentences

    # TTS review state: which sentence is speaking
    tts_idx: int = -1

    # backend-only: bumping this invalidates the in-flight loop
    _gen: int = 0
    # backend-only: true while the slider thumb is being dragged
    _seeking: bool = False
    # backend-only: finals not yet forming a complete sentence
    # ({"text", "start", "end"}), and the (en, hi) pairs of translated
    # sentences used as context
    _en_pending: list[dict] = []
    _ctx: list[dict] = []

    # ---- controls ----

    def on_source_change(self, value: str) -> None:
        """Switch audio source: stop playback and clear ALL state -
        transcript, both translation panels, sentence buffer, context -
        so nothing from the old source carries over."""
        audio.load_source(value)
        self.source = value
        self.duration_secs = audio.DURATION_SECS
        self.playing = False
        self._gen += 1
        self.position = 0.0
        self.sm_status = ""
        self.en_fragments = []
        self.hindi_sentences = []
        self.haiku_status = ""
        self.haiku_sentences = []
        self._en_pending = []
        self._ctx = []
        self.tts_idx = -1

    def toggle(self) -> rx.event.EventSpec | None:
        """One button: play when paused, pause when playing."""
        if self.playing:
            self.stop()
            return None
        return self.start()

    def start(self) -> rx.event.EventSpec | None:
        if self.position >= self.duration_secs:
            self.position = 0.0
        if self.position == 0.0:
            # fresh run from the top: clear the old captions
            self.sm_status = ""
            self.en_fragments = []
            self.hindi_sentences = []
            self.haiku_status = ""
            self.haiku_sentences = []
            self._en_pending = []
            self._ctx = []
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
        self.position = min(values[0], self.duration_secs)

    def seek(self, values: list[float]) -> None:
        """Seek committed on release: clear the caption buffers and
        restart the chunk stream clean at the new position - nothing
        carries over, so no gaps between old and new transcript."""
        self._seeking = False
        self.position = min(values[0], self.duration_secs)
        self.en_fragments = []
        self.hindi_sentences = []
        self.haiku_status = ""
        self.haiku_sentences = []
        self._en_pending = []
        self._ctx = []
        self.sm_status = ""
        self.tts_idx = -1
        if self.position >= self.duration_secs:
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
            # Finals arrive as small chunks, not sentences: buffer them
            # and only translate once a sentence-ending mark arrives
            flush = None
            async with self:
                if self._gen == gen and line.strip():
                    # Speechmatics timestamps are relative to the
                    # session start; shift into sermon time so
                    # captions from different sessions (pause/resume)
                    # share one clock and pair correctly
                    abs_start = round(start + start_pos, 2)
                    abs_end = round(end + start_pos, 2)
                    self.en_fragments.append(
                        {
                            "start": abs_start,
                            "end": abs_end,
                            "text": line,
                        }
                    )
                    self._en_pending.append(
                        {"text": line, "start": abs_start, "end": abs_end}
                    )
                    if line.rstrip().endswith((".", "!", "?")):
                        # the sentence spans its first fragment's start
                        # to its last fragment's end, so the paired
                        # English covers the whole sentence
                        span = (
                            self._en_pending[0]["start"],
                            self._en_pending[-1]["end"],
                        )
                        flush = (" ".join(p["text"] for p in self._en_pending), *span)
                        self._en_pending = []
            if flush is not None:
                sentence, abs_start, abs_end = flush
                # fire-and-forget: translate the whole sentence in a
                # worker thread while the stream continues
                asyncio.create_task(
                    self._translate_final(gen, sentence, abs_start, abs_end)
                )

        async def on_translation(text: str, start: float, end: float) -> None:
            async with self:
                if self._gen == gen and text.strip():
                    abs_start = start + start_pos
                    abs_end = end + start_pos
                    self.hindi_sentences.append(
                        {
                            "idx": len(self.hindi_sentences),
                            "at": audio.fmt_mmss(abs_start),
                            "start": round(abs_start, 2),
                            "end": round(abs_end, 2),
                            "text": text,
                            "flag": bool(LATIN_RUN.search(text)),
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
                    pos = min(start_pos + elapsed, self.duration_secs)
                    self.position = pos
                    ended = pos >= self.duration_secs
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
                    # translate whatever is left in the sentence buffer
                    async with self:
                        if self._gen == gen and self._en_pending:
                            span = (
                                self._en_pending[0]["start"],
                                self._en_pending[-1]["end"],
                            )
                            rest = " ".join(p["text"] for p in self._en_pending)
                            self._en_pending = []
                            asyncio.create_task(
                                self._translate_final(gen, rest, *span)
                            )
                    return
        finally:
            if session is not None:
                await session.close()
            async with self:
                if self._gen == gen:
                    self.playing = False

    async def _translate_final(
        self, gen: int, sentence: str, abs_start: float, abs_end: float
    ) -> None:
        """Translate one complete sentence with Haiku 5.5 via Opper,
        passing recent sentence pairs as context, and append it to the
        Haiku panel. The call takes ~1 s, so it must never block the
        audio stream - it runs detached here."""
        async with self:
            ctx = [(c["en"], c["hi"]) for c in self._ctx]
        try:
            hindi = await asyncio.to_thread(opper.translate, sentence, ctx)
        except Exception as err:  # noqa: BLE001
            async with self:
                if self._gen == gen:
                    self.haiku_status = f"error: {err}"
            return
        async with self:
            if self._gen == gen and hindi:
                self.haiku_sentences.append(
                    {
                        "idx": len(self.haiku_sentences),
                        "at": audio.fmt_mmss(abs_start),
                        "start": abs_start,
                        "end": abs_end,
                        "text": hindi,
                        # the subtext is exactly the sentence that was
                        # sent as to_translate
                        "en": sentence,
                        "flag": bool(LATIN_RUN.search(hindi)),
                    }
                )
                self._ctx.append({"en": sentence, "hi": hindi})

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

    async def play_haiku(self, idx: int) -> rx.event.EventSpec:
        """Play a sentence from the Opper/Haiku panel (same TTS path as
        the Speechmatics panel, reading haiku_sentences instead)."""
        if idx >= len(self.haiku_sentences):
            return []
        text = self.haiku_sentences[idx]["text"]
        self.tts_idx = idx
        data = await asyncio.to_thread(azure_tts.synthesize, text)
        b64 = base64.b64encode(data).decode()
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
    def haiku_paired(self) -> list[dict]:
        """Haiku-panel sentences; each row's "en" is the exact sentence
        that was sent to the model as to_translate."""
        return list(self.haiku_sentences)

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
        return "-" + audio.fmt_mmss(self.duration_secs - self.position)

    @rx.var
    def chunk_label(self) -> str:
        return f"{self.chunks_read} chunks read ({audio.CHUNK_MS} ms each)"
