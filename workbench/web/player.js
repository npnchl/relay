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
  let suppressGraph = null; // {ctx, src, hp, dest} - created once, bypassed when off

  // the button owns the state, like the model Listen buttons
  const setBtn = () => {
    const playing = !audio.paused;
    playBtn.innerHTML = (playing ? ICON_PAUSE : ICON_PLAY) + `<span>${playing ? "Pause" : "Play"}</span>`;
    playBtn.classList.toggle("on", playing);
    playBtn.setAttribute("aria-label", playing ? "Pause source audio" : "Play source audio");
    statusEl.textContent = "";
    statusEl.className = "pstatus";
  };
  const updateTime = () => {
    timeEl.textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration || 0)}`;
    if (!scrubbing) scrub.value = audio.currentTime;
  };

  playBtn.addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
  audio.addEventListener("play", setBtn);
  audio.addEventListener("pause", setBtn);
  audio.addEventListener("ended", setBtn);
  audio.addEventListener("timeupdate", updateTime);
  audio.addEventListener("loadedmetadata", () => {
    scrub.max = audio.duration || 0;
    updateTime();
    setBtn();
  });
  setBtn();
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

  // One shared Suppress toggle (lives on the sim card) also filters this source
  // player, so you can hear what suppression does - works mid-playback too.
  // Currently a band-pass (250 Hz - 3.4 kHz, like a cleaned-up speech channel);
  // the real noise-suppression algorithm slots into this graph later.
  function setSuppress(on) {
    const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!AC) return false;
    if (!suppressGraph) {
      const ctx = new AC();
      const src = ctx.createMediaElementSource(audio);
      const hp = ctx.createBiquadFilter();
      hp.type = "highpass";
      hp.frequency.value = 250;
      const lp = ctx.createBiquadFilter();
      lp.type = "lowpass";
      lp.frequency.value = 3400;
      const dest = ctx.destination;
      src.connect(hp);
      hp.connect(lp);
      lp.connect(dest);
      suppressGraph = { ctx, src, hp, lp, dest };
    }
    const g = suppressGraph;
    if (g.ctx.state === "suspended") g.ctx.resume(); // autoplay policy: must resume on the user gesture
    try { g.src.disconnect(); } catch { /* not connected */ }
    if (on) g.src.connect(g.hp); // through the filter chain
    else g.src.connect(g.dest); // bypass
    return on;
  }

  return {
    setSuppress,
    loadSource(src) {
      audio.src = `/audio/${src.file}`;
      scrub.max = src.duration;
      updateTime();
      setBtn();
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
  let ended = false;
  let pending = false;

  // the button owns the state, like the model Listen buttons
  const setBtn = () => {
    playBtn.disabled = pending;
    playBtn.classList.remove("on");
    if (pending) {
      playBtn.innerHTML = `${ICON_PLAY}<span>${running ? "Stopping…" : "Starting…"}</span>`;
    } else if (running) {
      playBtn.innerHTML = `${ICON_PAUSE}<span>Running</span>`;
      playBtn.classList.add("on");
      playBtn.setAttribute("aria-label", "Stop simulated mic");
    } else {
      playBtn.innerHTML = `${ICON_PLAY}<span>${ended ? "Replay" : "Start"}</span>`;
      playBtn.setAttribute("aria-label", "Start simulated mic");
    }
    if (!statusEl.classList.contains("err")) {
      statusEl.textContent = "";
      statusEl.className = "pstatus";
    }
  };

  playBtn.addEventListener("click", async () => {
    if (pending) return;
    pending = true; // immediate feedback; cleared by the next state event or timeout
    setBtn();
    setTimeout(() => { if (pending) { pending = false; setBtn(); } }, 12000);
    try {
      if (running) await postJSON("/sim/stop");
      else await postJSON("/sim/start");
    } catch (e) {
      statusEl.textContent = String(e.message || e);
      statusEl.className = "pstatus err";
      pending = false;
      setBtn();
    }
  });
  scrub.addEventListener("input", () => {
    scrubbing = true;
    timeEl.textContent = `${fmtTime(scrub.value)} / ${fmtTime(scrub.max)}`;
  });
  scrub.addEventListener("change", async () => {
    scrubbing = false;
    statusEl.textContent = "seeking…"; // cleared by the next state event
    statusEl.className = "pstatus";
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
    ended = !!st.ended;
    pending = false;
    setBtn();
    const dur = st.source ? st.source.duration : 0;
    scrub.max = dur;
    if (!scrubbing) scrub.value = st.position;
    timeEl.textContent = `${fmtTime(st.position)} / ${fmtTime(dur)}`;
  }
  setBtn();

  return { applyState };
}
