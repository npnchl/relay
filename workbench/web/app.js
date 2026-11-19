import { connect } from "./ws.js";
import { initTransport } from "./transport.js";
import { createViewport } from "./viewport.js";

const $ = (s) => document.querySelector(s);

const audio = new Audio();
const sourceSel = $("#source");
const shuffleBtn = $("#shuffle");
let selectValue = "full";

let send = () => {};
let defaultConfigs = null;

// one shared auto-scroll toggle for both viewports
const autoScrollAll = $("#autoscroll-all");
let autoScroll = autoScrollAll.checked;
autoScrollAll.addEventListener("change", () => {
  autoScroll = autoScrollAll.checked;
});

const viewports = {
  A: createViewport($("#panel-A"), "A", "Speechmatics melia-1", {
    onConfig: (cfg) => send({ cmd: "config", model: "A", config: cfg }),
    onClear: () => send({ cmd: "clear", model: "A" }),
    onReset: () => {
      if (!defaultConfigs) return;
      viewports.A.setDefaultConfig(defaultConfigs.A);
      send({ cmd: "config", model: "A", config: defaultConfigs.A });
    },
    getAutoScroll: () => autoScroll,
  }),
  B: createViewport($("#panel-B"), "B", "Gladia solaria-1", {
    onConfig: (cfg) => send({ cmd: "config", model: "B", config: cfg }),
    onClear: () => send({ cmd: "clear", model: "B" }),
    onReset: () => {
      if (!defaultConfigs) return;
      viewports.B.setDefaultConfig(defaultConfigs.B);
      send({ cmd: "config", model: "B", config: defaultConfigs.B });
    },
    getAutoScroll: () => autoScroll,
  }),
};

function fillSources(manifest) {
  const opts = [
    `<option value="full">${manifest.full.label}</option>`,
    ...manifest.segments.map((s) => `<option value="${s.id}">${s.label}</option>`),
    `<option value="random">Random 1-min segment</option>`,
  ];
  sourceSel.innerHTML = opts.join("");
  sourceSel.value = selectValue;
}

function applySource(src) {
  audio.src = src.url;
  $("#scrub").max = src.duration;
  if (selectValue === "random") sourceSel.value = "random";
  else if ([...sourceSel.options].some((o) => o.value === src.id)) sourceSel.value = src.id;
  shuffleBtn.hidden = selectValue !== "random";
  viewports.A.clear();
  viewports.B.clear();
  transport.updateTime();
}

function onMessage(msg) {
  switch (msg.type) {
    case "hello":
      fillSources(msg.manifest);
      defaultConfigs = msg.configs;
      viewports.A.setDefaultConfig(msg.configs.A);
      viewports.B.setDefaultConfig(msg.configs.B);
      if (msg.source) applySource(msg.source);
      break;
    case "source":
      applySource(msg.source);
      break;
    case "transcript":
      viewports[msg.model].addLine(msg);
      break;
    case "cleared":
      (msg.model === "both" ? ["A", "B"] : [msg.model]).forEach((m) => viewports[m].clear());
      break;
    case "state":
      break; // icon follows the audio element events
    case "config_applied":
      viewports[msg.model].setStatus("applied");
      break;
    case "config_error":
      viewports[msg.model].setStatus("error: " + msg.message);
      break;
    case "error":
      if (msg.model && viewports[msg.model]) {
        viewports[msg.model].addLine(msg);
        viewports[msg.model].setDot("error");
      } else {
        console.warn(msg.message);
        $("#conn").textContent = msg.message;
      }
      break;
    case "status":
      if (viewports[msg.model]) viewports[msg.model].setDot(msg.active ? "active" : "idle");
      break;
    case "closed":
      if (msg.model && viewports[msg.model]) viewports[msg.model].setDot("idle");
      break;
  }
}

const transport = initTransport({
  audio,
  playBtn: $("#play"),
  scrub: $("#scrub"),
  timeEl: $("#time"),
  send: (o) => send(o),
});

sourceSel.addEventListener("change", () => {
  selectValue = sourceSel.value;
  shuffleBtn.hidden = selectValue !== "random";
  send({ cmd: "select", id: selectValue });
});
shuffleBtn.addEventListener("click", () => send({ cmd: "shuffle" }));

const conn = connect(
  `ws://${location.host}/ws`,
  onMessage,
  () => { $("#conn").textContent = ""; },
  () => { $("#conn").textContent = "disconnected - is the server running?"; }
);
send = conn.send;
