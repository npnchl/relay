// One model panel: header (status dot + text, name, listen/stop, settings, scroll, clear)
// + transcript viewport + settings popover (schema form + raw JSON).
import { fmtTime, postJSON } from "./api.js";

const ICON_GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/></svg>';
const ICON_SCROLL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11"/><path d="m8 11 4 4 4-4"/><path d="M5 20h14"/></svg>';

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setPath(obj, path, v) {
  const keys = path.split(".");
  let o = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (typeof o[keys[i]] !== "object" || o[keys[i]] === null) o[keys[i]] = {};
    o = o[keys[i]];
  }
  o[keys[keys.length - 1]] = v;
}

// build form inputs for a schema {fields} into a container; values live in `config`
function buildFields(container, fields, config, prefix) {
  for (const [name, spec] of Object.entries(fields)) {
    const path = prefix ? `${prefix}.${name}` : name;
    const cur = getPath(config, path);
    if (spec.type === "object" && spec.fields) {
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.textContent = name;
      det.appendChild(sum);
      buildFields(det, spec.fields, config, path);
      container.appendChild(det);
      continue;
    }
    const wrap = document.createElement("label");
    wrap.className = "field";
    const lab = document.createElement("span");
    lab.className = "flabel";
    lab.textContent = name;
    wrap.appendChild(lab);
    let inp;
    if (spec.type === "enum") {
      inp = document.createElement("select");
      for (const v of spec.enum) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = v;
        inp.appendChild(o);
      }
      inp.value = cur ?? spec.enum[0];
    } else if (spec.type === "bool" || typeof cur === "boolean") {
      inp = document.createElement("input");
      inp.type = "checkbox";
      inp.className = "fcheck";
      inp.checked = !!cur;
    } else if (spec.type === "list" || Array.isArray(cur)) {
      inp = document.createElement("textarea");
      inp.className = "finput";
      inp.value = JSON.stringify(cur ?? spec.default ?? []);
    } else {
      inp = document.createElement("input");
      inp.className = "finput";
      inp.type = spec.type === "int" || spec.type === "float" ? "number" : "text";
      if (spec.type === "float" || spec.type === "int") inp.step = "any";
      inp.value = cur ?? spec.default ?? "";
    }
    inp.dataset.path = path;
    inp.dataset.type = spec.type;
    wrap.appendChild(inp);
    container.appendChild(wrap);
  }
}

function readForm(form, config) {
  const out = JSON.parse(JSON.stringify(config));
  for (const inp of form.querySelectorAll("[data-path]")) {
    const t = inp.dataset.type;
    let v;
    if (inp.type === "checkbox") v = inp.checked;
    else if (t === "enum") v = inp.value;
    else if (t === "list") { try { v = JSON.parse(inp.value); } catch { v = inp.value; } }
    else if (t === "int" || t === "float") v = inp.value === "" ? null : Number(inp.value);
    else v = inp.value === "" ? null : inp.value;
    setPath(out, inp.dataset.path, v);
  }
  return out;
}

export function createPanel(root, { model, label, schema, currentConfig, onJump, onListen, onClear }) {
  root.innerHTML = `
    <header class="panel-head">
      <span class="dot" data-state="idle" role="img" aria-label="status: idle" title="idle"></span>
      <span class="pstatus" data-role="status">idle</span>
      <span class="model-name">${label}</span>
      <span class="spacer"></span>
      <button class="btn listen" aria-pressed="false">Listen</button>
      <button class="icon-btn gear" aria-label="Settings for ${label}" title="Settings">${ICON_GEAR}</button>
      <button class="icon-btn scroll on" aria-label="Auto-scroll" aria-pressed="true" title="Auto-scroll">${ICON_SCROLL}</button>
      <button class="icon-btn trash" aria-label="Clear transcript ${model}" title="Clear">${ICON_TRASH}</button>
      <div class="popover" hidden>
        <div class="pop-title"><span>${label} settings</span><button class="btn json-toggle">edit as JSON</button></div>
        <div class="form"></div>
        <textarea class="finput json-edit" hidden rows="14" aria-label="Raw config JSON"></textarea>
        <div class="pop-row"><button class="apply">Apply</button><button class="reset">Reset</button><span class="cfg-status" role="status"></span></div>
      </div>
    </header>
    <div class="viewport" aria-live="polite" role="log"></div>`;

  const vp = root.querySelector(".viewport");
  const dot = root.querySelector(".dot");
  const statusEl = root.querySelector(".pstatus");
  const listenBtn = root.querySelector(".listen");
  const scrollBtn = root.querySelector(".scroll");
  const gear = root.querySelector(".gear");
  const trash = root.querySelector(".trash");
  const pop = root.querySelector(".popover");
  const form = root.querySelector(".form");
  const jsonEdit = root.querySelector(".json-edit");
  const jsonToggle = root.querySelector(".json-toggle");
  const cfgStatus = root.querySelector(".cfg-status");
  let autoScroll = true;
  let listening = false;

  buildFields(form, schema.fields, currentConfig, "");

  listenBtn.addEventListener("click", () => onListen(listening));
  trash.addEventListener("click", () => onClear());
  scrollBtn.addEventListener("click", () => {
    autoScroll = !autoScroll;
    scrollBtn.classList.toggle("on", autoScroll);
    scrollBtn.setAttribute("aria-pressed", String(autoScroll));
  });
  gear.addEventListener("click", (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !gear.contains(e.target)) pop.hidden = true;
  });
  jsonToggle.addEventListener("click", () => {
    const showJson = jsonEdit.hidden;
    jsonEdit.hidden = !showJson;
    form.style.display = showJson ? "none" : "";
    jsonToggle.textContent = showJson ? "edit as form" : "edit as JSON";
    if (showJson) jsonEdit.value = JSON.stringify(readForm(form, currentConfig), null, 2);
  });
  root.querySelector(".apply").addEventListener("click", async () => {
    cfgStatus.className = "cfg-status";
    cfgStatus.textContent = "applying…";
    try {
      const cfg = jsonEdit.hidden ? readForm(form, currentConfig) : JSON.parse(jsonEdit.value);
      await postJSON("/config", { model, config: cfg });
      Object.assign(currentConfig, cfg);
      cfgStatus.textContent = "applied";
    } catch (e) {
      cfgStatus.className = "cfg-status err";
      cfgStatus.textContent = "error: " + (e.data && e.data.error || e.message);
    }
  });
  root.querySelector(".reset").addEventListener("click", () => {
    buildFields(form, schema.fields, currentConfig, "");
    form.replaceChildren();
    buildFields(form, schema.fields, currentConfig, "");
    jsonEdit.value = JSON.stringify(currentConfig, null, 2);
    cfgStatus.textContent = "";
  });

  function setState(st) {
    listening = !!(st.listening && st.listening[model]);
    const open = !!(st.sessions && st.sessions[model]);
    listenBtn.textContent = listening ? "Stop" : "Listen";
    listenBtn.classList.toggle("on", listening);
    listenBtn.setAttribute("aria-pressed", String(listening));
    if (!statusEl.classList.contains("err")) {
      dot.dataset.state = listening ? (open ? "listening" : "dropped") : "idle";
      dot.title = statusEl.textContent = listening ? (open ? "listening" : "dropped") : "idle";
      dot.setAttribute("aria-label", "status: " + statusEl.textContent);
    }
  }

  function setNotice(text, isErr) {
    statusEl.textContent = text;
    statusEl.classList.toggle("err", !!isErr);
    dot.dataset.state = isErr ? "dropped" : "idle";
    dot.title = text;
    dot.setAttribute("aria-label", "status: " + text);
  }

  function addLine(line) {
    if (line.type === "error") return;
    const div = document.createElement("div");
    div.className = "line jump" + (line.partial ? " partial" : "");
    div.title = "Jump source player to this timestamp";
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = fmtTime(line.t0);
    const txt = document.createElement("span");
    txt.className = "txt";
    txt.textContent = line.text;
    div.append(ts, txt);
    div.addEventListener("click", () => onJump(line.t0));
    const last = vp.lastElementChild;
    if (last && last.classList.contains("partial")) last.replaceWith(div);
    else vp.appendChild(div);
    if (autoScroll) vp.scrollTop = vp.scrollHeight;
  }

  function clear() { vp.innerHTML = ""; }

  return { setState, setNotice, addLine, clear };
}
