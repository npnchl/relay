// The two independent player cards docked at the bottom.
//  - source player: a local <audio> element over /audio/{file}; never talks to the sim.
//  - sim player: controls for the backend's simulated mic (REST only, no audio element).
import { fmtTime, postJSON } from "./api.js";

const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';

export function initSourcePlayer() {
  const audio = new Audio();
  const playBtn = document.getElementById("src-play");
  const scrub = document.getElementById("src-scrub");
  const timeEl = document.getElementById("src-time");
  const statusEl = document.getElementById("src-status");
  let scrubbing = false;

  const setIcon = (playing) => {
    playBtn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
    playBtn.setAttribute("aria-label", playing ? "Pause source audio" : "Play source audio");
  };
  const updateTime = () => {
    timeEl.textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration || 0)}`;
    if (!scrubbing) scrub.value = audio.currentTime;
  };
  const updateStatus = () => {
    statusEl.textContent = audio.paused ? "paused" : "playing";
    statusEl.className = "pstatus" + (audio.paused ? "" : " ok");
  };

  playBtn.addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
  audio.addEventListener("play", () => { setIcon(true); updateStatus(); });
  audio.addEventListener("pause", () => { setIcon(false); updateStatus(); });
  audio.addEventListener("ended", () => { setIcon(false); updateStatus(); });
  audio.addEventListener("timeupdate", updateTime);
  audio.addEventListener("loadedmetadata", () => {
    scrub.max = audio.duration || 0;
    updateTime();
    updateStatus();
  });
  scrub.addEventListener("input", () => {
    scrubbing = true;
    audio.currentTime = Number(scrub.value);
    updateTime();
  });
  scrub.addEventListener("change", () => {
    scrubbing = false;
    audio.currentTime = Number(scrub.value); // local only; the sim is unaffected
    updateTime();
  });
  document.addEventListener("keydown", (e) => {
    const tag = document.activeElement && document.activeElement.tagName;
    if (e.code === "Space" && !/TEXTAREA|INPUT|SELECT|BUTTON/.test(tag)) {
      e.preventDefault();
      playBtn.click();
    }
  });

  return {
    loadSource(src) {
      audio.src = `/audio/${src.file}`;
      scrub.max = src.duration;
      updateTime();
      updateStatus();
    },
    // jump to a source-relative position (transcript lines carry file-absolute t0)
    jumpTo(localT) {
      const dur = audio.duration || 0;
      audio.currentTime = Math.max(0, Math.min(Number(localT) || 0, dur || Number(localT) || 0));
      audio.play();
    },
  };
}

export function initSimPlayer() {
  const playBtn = document.getElementById("sim-play");
  const scrub = document.getElementById("sim-scrub");
  const timeEl = document.getElementById("sim-time");
  const statusEl = document.getElementById("sim-status");
  let scrubbing = false;
  let running = false;

  const setIcon = () => {
    playBtn.innerHTML = running ? ICON_PAUSE : ICON_PLAY;
    playBtn.setAttribute("aria-label", running ? "Stop simulated mic" : "Start simulated mic");
  };

  playBtn.addEventListener("click", async () => {
    playBtn.disabled = true;
    try {
      if (running) await postJSON("/sim/stop");
      else await postJSON("/sim/start");
    } catch (e) {
      statusEl.textContent = String(e.message || e);
      statusEl.className = "pstatus err";
    } finally {
      playBtn.disabled = false;
    }
  });
  scrub.addEventListener("input", () => {
    scrubbing = true;
    timeEl.textContent = `${fmtTime(scrub.value)} / ${fmtTime(scrub.max)}`;
  });
  scrub.addEventListener("change", async () => {
    scrubbing = false;
    try {
      await postJSON("/sim/seek", { offset: Number(scrub.value) });
    } catch (e) {
      statusEl.textContent = String(e.message || e);
      statusEl.className = "pstatus err";
    }
  });

  // called from SSE state events - backend is the source of truth for sim position
  function applyState(st) {
    running = !!st.running;
    setIcon();
    const dur = st.source ? st.source.duration : 0;
    scrub.max = dur;
    if (!scrubbing) scrub.value = st.position;
    timeEl.textContent = `${fmtTime(st.position)} / ${fmtTime(dur)}`;
    if (!statusEl.classList.contains("err")) {
      statusEl.textContent = st.ended ? "ended" : running ? "running" : "paused";
      statusEl.className = "pstatus" + (running ? " ok" : "");
    }
  }

  return { applyState };
}
