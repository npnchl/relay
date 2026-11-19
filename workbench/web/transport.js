const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';

export function fmtTime(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function initTransport({ audio, playBtn, scrub, timeEl, send }) {
  let scrubbing = false;

  const setIcon = (playing) => {
    playBtn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
    playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
  };

  const updateTime = () => {
    timeEl.textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration || 0)}`;
    if (!scrubbing) scrub.value = audio.currentTime;
  };

  playBtn.addEventListener("click", () => {
    if (audio.paused) {
      audio.play();
      send({ cmd: "play", offset: audio.currentTime });
    } else {
      audio.pause();
      send({ cmd: "pause" });
    }
  });

  audio.addEventListener("play", () => setIcon(true));
  audio.addEventListener("pause", () => setIcon(false));
  audio.addEventListener("ended", () => setIcon(false));
  audio.addEventListener("timeupdate", updateTime);
  audio.addEventListener("loadedmetadata", () => {
    scrub.max = audio.duration || 0;
    updateTime();
  });

  scrub.addEventListener("input", () => {
    scrubbing = true;
    audio.currentTime = Number(scrub.value);
    updateTime();
  });
  scrub.addEventListener("change", () => {
    scrubbing = false;
    const off = Number(scrub.value);
    audio.pause(); // reposition; transcription restarts from here on next play
    send({ cmd: "seek", offset: off });
  });

  document.addEventListener("keydown", (e) => {
    const tag = document.activeElement && document.activeElement.tagName;
    if (e.code === "Space" && !/TEXTAREA|INPUT|SELECT/.test(tag)) {
      e.preventDefault();
      playBtn.click();
    }
  });

  return { updateTime, setIcon };
}
