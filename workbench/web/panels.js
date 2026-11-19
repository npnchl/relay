// One model panel: header (status dot + text, name, listen/stop, settings, scroll, clear)
// + transcript viewport + settings popover (schema form + raw JSON).
import { fmtTime, postJSON } from "./api.js";

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
      <span class="model-name">${label}</span>
      <span class="spacer"></span>
      <button class="btn listen" aria-pressed="false" title="Start/stop this model's session">Listen</button>
      <button class="btn scroll on" aria-pressed="true" title="Auto-scroll transcript">Scroll</button>
      <button class="btn gear" title="Settings for ${label}">Settings</button>
      <button class="btn trash danger" title="Clear transcript ${model}">Clear</button>
      <span class="pstatus" role="status">idle</span>
      <span class="dot" data-state="idle" role="img" aria-label="status: idle" title="idle"></span>
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
