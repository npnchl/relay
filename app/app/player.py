"""The virtual player state: a wall-clock-paced timeline, no audio.

Each tick advances the playhead one step at true realtime pace and
reads the matching 100 ms chunk from the in-memory sermon - the same
chunks that will later be fed to a provider. Start resumes (from the
top if the sermon had ended); Stop pauses in place.
"""

import asyncio

import reflex as rx

from . import audio


class PlayerState(rx.State):
    position: float = 0.0
    playing: bool = False

    # backend-only: bumping this invalidates the in-flight loop
    _gen: int = 0

    # ---- controls ----

    def start(self) -> rx.event.EventSpec | None:
        if self.position >= audio.DURATION_SECS:
            self.position = 0.0
        self._gen += 1
        self.playing = True
        return PlayerState.run(self._gen, self.position)

    def stop(self) -> None:
        self.playing = False
        self._gen += 1

    # ---- the virtual clock ----

    @rx.event(background=True)
    async def run(self, gen: int, start_pos: float) -> None:
        """Advance the playhead at true realtime pace until stopped.

        Paced against the wall clock (not sleep durations), so the
        playhead cannot drift no matter how long the session runs.
        """
        t0 = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(audio.CHUNK_MS / 1000)
            async with self:
                if self._gen != gen or not self.playing:
                    return
                elapsed = asyncio.get_running_loop().time() - t0
                self.position = min(start_pos + elapsed, audio.DURATION_SECS)
                if self.position >= audio.DURATION_SECS:
                    self.playing = False
                    return

    # ---- readouts ----

    @rx.var
    def chunks_read(self) -> int:
        return int(self.position * 1000 / audio.CHUNK_MS)

    @rx.var
    def position_label(self) -> str:
        return f"{audio.fmt_mmss(self.position)} / {audio.fmt_mmss(audio.DURATION_SECS)}"

    @rx.var
    def progress_percent(self) -> int:
        return int(self.position / audio.DURATION_SECS * 100)

    @rx.var
    def chunk_label(self) -> str:
        return f"{self.chunks_read} chunks read ({audio.CHUNK_MS} ms each)"
