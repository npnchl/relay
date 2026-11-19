// Relay A/B workbench frontend - a pure view of backend state.
// SSE /events pushes state/line/provider/cleared; user actions are REST POSTs.
import { getJSON, postJSON, connectEvents } from "./api.js";
import { initSourcePlayer, initSimPlayer } from "./player.js";
import { createPanel } from "./panels.js";

const $ = (s) => document.querySelector(s);

const sourceSel = $("#source");
const shuffleBtn = $("#shuffle");
const conn = $("#conn");
const connText = $("#conn-text");

let manifest = null;
let currentSource = null;
let selectValue = "full";
let schema = null;
let configs = null;

const sourcePlayer = initSourcePlayer();
const simPlayer = initSimPlayer();

const panels = {};

function makePanels() {
  for (const [model, label] of [["A", "Speechmatics"], ["B", "Gladia"]]) {
    panels[model] = createPanel($(`#panel-${model}`), {
      model,
      label,
      schema: schema[model],
      currentConfig: configs[model],
      onJump: (t0) => {
        if (currentSource) sourcePlayer.jumpTo(Math.max(0, t0 - currentSource.start));
      },
      onListen: (wasListening) => {
        postJSON(wasListening ? "/listen/stop" : "/listen/start", { model }).catch((e) =>
          panels[model].setDrop(String(e.message || e))
        );
      },
      onClear: () => postJSON("/transcripts/clear", { model }).catch(() => {}),
    });
  }
}

function fillSources() {
  const opts = [
    `<option value="full">${manifest.full.label}</option>`,
    ...manifest.segments.map((s) => `<option value="${s.id}">${s.label}</option>`),
    `<option value="random">Random 60 s segment</option>`,
  ];
  sourceSel.innerHTML = opts.join("");
  sourceSel.value = selectValue;
}

function applyState(st) {
  if (st.source && (!currentSource || currentSource.id !== st.source.id || currentSource.file !== st.source.file)) {
    currentSource = st.source;
    sourcePlayer.loadSource(st.source);
    if (selectValue !== "random" && [...sourceSel.options].some((o) => o.value === st.source.id)) {
      sourceSel.value = st.source.id;
    }
    shuffleBtn.hidden = selectValue !== "random";
  }
  simPlayer.applyState(st);
  for (const m of ["A", "B"]) panels[m].setState(st);
}

function handleEvent(msg) {
  switch (msg.type) {
    case "state":
      applyState(msg);
      break;
    case "line":
      if (panels[msg.line.model]) panels[msg.line.model].addLine(msg.line);
      break;
    case "cleared":
      if (panels[msg.model]) panels[msg.model].clear();
      break;
    case "provider": {
      const ev = msg.event;
      const p = panels[ev.model];
      if (!p) break;
      if (ev.type === "closed" && !ev.local) p.setDrop("provider dropped the session - press re-listen");
      else if (ev.type === "error") p.setDrop(String(ev.message || "error").slice(0, 120));
      break;
    }
  }
}

async function boot() {
  [manifest, schema, configs] = await Promise.all([
    getJSON("/sources"),
    getJSON("/config/schema"),
    getJSON("/configs"),
  ]);
  fillSources();
  makePanels();
  applyState(await getJSON("/state"));
  connectEvents(handleEvent, (on) => {
    conn.dataset.state = on ? "on" : "off";
    connText.textContent = on ? "connected" : "offline";
    conn.setAttribute("aria-label", "connection: " + (on ? "connected" : "offline"));
  });
}

sourceSel.addEventListener("change", () => {
  selectValue = sourceSel.value;
  shuffleBtn.hidden = selectValue !== "random";
  postJSON("/source", { id: selectValue }).catch((e) => console.warn(e));
});
shuffleBtn.addEventListener("click", () => {
  selectValue = "random";
  sourceSel.value = "random";
  shuffleBtn.hidden = false;
  postJSON("/source", { id: "random" }).catch((e) => console.warn(e));
});

boot().catch((e) => {
  conn.dataset.state = "off";
  connText.textContent = "offline";
  console.error(e);
});
