// One model panel: header with icon+text buttons (Listen owns its state,
// Auto-scroll, Settings, Clear) + transcript viewport + form-only settings popover.
import { fmtTime, postJSON } from "./api.js";

const I = {
  play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  scroll: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m8 9 4-4 4 4"/><path d="m8 15 4 4 4-4"/></svg>',
  norm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
};

const MELIA1_FORBIDDEN = ["max_delay", "max_delay_mode", "punctuation_overrides", "transcript_filtering_config"];

// Two-tier settings: CORE = top-level fields that affect transcription output and
// stay visible in the main section; everything else lives under "All provider
// options" (collapsed). Everything the SDK exposes is still reachable.
const CORE = {
  A: new Set([
    "language", "model", "operating_point", "enable_partials",
    "max_delay", "max_delay_mode", "diarization", "speaker_diarization_config",
    "additional_vocab", "punctuation_overrides", "domain", "output_locale",
  ]),
  B: new Set([
    "encoding", "bit_depth", "sample_rate", "channels", "model",
    "endpointing", "maximum_duration_without_endpointing", "custom_metadata",
    "language_config", "pre_processing", "messages_config",
  ]),
};

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

function helperText(spec) {
  const parts = [];
  if (spec.enum) parts.push("one of: " + spec.enum.join(", "));
  if ("default" in spec) parts.push("default: " + JSON.stringify(spec.default));
  return parts.join(" · ");
}

// Build one vertical field row: label / control / helper
function buildField(container, name, spec, config, path) {
  const cur = getPath(config, path);
  const wrap = document.createElement("div");
  wrap.className = "field";
  wrap.dataset.path = path;
  const lab = document.createElement("label");
  lab.className = "flabel";
  lab.textContent = name;
  wrap.appendChild(lab);

  let inp, kind = spec.type;
  if (spec.type === "enum") {
    if (spec.free_str) {
      // free-form string with known values as suggestions (e.g. Gladia model)
      inp = document.createElement("input");
      inp.className = "finput";
      inp.type = "text";
      inp.setAttribute("list", "dl-" + path.split(".").join("_"));
      inp.value = cur ?? spec.enum[0] ?? "";
      const dl = document.createElement("datalist");
      dl.id = inp.getAttribute("list");
      for (const v of spec.enum) {
        const o = document.createElement("option");
        o.value = v;
        dl.appendChild(o);
      }
      wrap.appendChild(dl);
    } else {
      inp = document.createElement("select");
      inp.className = "finput";
      for (const v of spec.enum) {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        inp.appendChild(o);
      }
      inp.value = cur ?? spec.enum[0];
    }
    kind = "enum";
  } else if (typeof cur === "boolean" || spec.type === "bool") {
    kind = "bool";
    const sw = document.createElement("label");
    sw.className = "switch";
    inp = document.createElement("input");
    inp.type = "checkbox";
    inp.checked = !!cur;
    const knob = document.createElement("span");
    knob.className = "knob";
    sw.append(inp, knob);
    wrap.appendChild(sw);
  } else if (spec.type === "dict" || (cur && typeof cur === "object" && !Array.isArray(cur))) {
    kind = "kvobj"; // plain object -> key:value rows producing an object (never a string!)
    wrap.dataset.kv = "obj";
    const rows = document.createElement("div");
    rows.className = "kv-rows";
    const addRow = (k, v) => {
      const r = document.createElement("div");
      r.className = "kv-row";
      const ki = document.createElement("input");
      ki.placeholder = "key"; ki.value = k ?? "";
      const vi = document.createElement("input");
      vi.placeholder = "value"; vi.value = v ?? "";
      const rm = document.createElement("button");
      rm.className = "rm"; rm.textContent = "✕"; rm.type = "button";
      rm.addEventListener("click", () => r.remove());
      r.append(ki, vi, rm);
      rows.appendChild(r);
    };
    if (cur && typeof cur === "object") {
      for (const [k, v] of Object.entries(cur)) addRow(k, typeof v === "object" ? JSON.stringify(v) : String(v));
    }
    const add = document.createElement("button");
    add.className = "btn kv-add"; add.type = "button"; add.textContent = "+ add";
    add.addEventListener("click", () => addRow("", ""));
    wrap.appendChild(rows);
    wrap.appendChild(add);
  } else if (Array.isArray(cur) && spec.type !== "list") {
    kind = "kv"; // list of objects -> key:value rows producing a list
    wrap.dataset.kv = "list";
    const rows = document.createElement("div");
    rows.className = "kv-rows";
    const addRow = (k, v) => {
      const r = document.createElement("div");
      r.className = "kv-row";
      const ki = document.createElement("input");
      ki.placeholder = "key"; ki.value = k ?? "";
      const vi = document.createElement("input");
      vi.placeholder = "value"; vi.value = v ?? "";
      const rm = document.createElement("button");
      rm.className = "rm"; rm.textContent = "✕"; rm.type = "button";
      rm.addEventListener("click", () => r.remove());
      r.append(ki, vi, rm);
      rows.appendChild(r);
    };
    for (const item of cur) {
      if (item && typeof item === "object") for (const [k, v] of Object.entries(item)) addRow(k, typeof v === "object" ? JSON.stringify(v) : String(v));
      else addRow("", String(item));
    }
    const add = document.createElement("button");
    add.className = "btn kv-add"; add.type = "button"; add.textContent = "+ add";
    add.addEventListener("click", () => addRow("", ""));
    wrap.appendChild(rows);
    wrap.appendChild(add);
  } else if (spec.type === "list" || Array.isArray(cur)) {
    kind = "langlist"; // list of scalars -> comma-separated + chips
    inp = document.createElement("input");
    inp.className = "finput";
    inp.type = "text";
    inp.value = Array.isArray(cur) ? cur.join(", ") : (cur ?? "");
    const chips = document.createElement("div");
    chips.className = "chips";
    const renderChips = () => {
      chips.innerHTML = "";
      for (const v of inp.value.split(",").map((s) => s.trim()).filter(Boolean)) {
        const c = document.createElement("span");
        c.textContent = v;
        chips.appendChild(c);
      }
    };
    inp.addEventListener("input", renderChips);
    renderChips();
    wrap.appendChild(inp);
    wrap.appendChild(chips);
  } else {
    inp = document.createElement("input");
    inp.className = "finput";
    inp.type = spec.type === "int" || spec.type === "float" ? "number" : "text";
    if (inp.type === "number") inp.step = spec.type === "int" ? "1" : "any";
    inp.value = cur ?? spec.default ?? "";
    kind = inp.type === "number" ? "number" : "string";
  }
  if (inp && inp.classList) {
    inp.dataset.path = path;
    inp.dataset.kind = kind;
    if (spec.vtype) inp.dataset.vtype = spec.vtype; // numeric enums must read back as numbers
    if (kind !== "bool" && kind !== "kv" && kind !== "langlist") wrap.appendChild(inp);
  }
  const help = document.createElement("div");
  help.className = "fhelp";
  help.textContent = helperText(spec) || spec.type;
  wrap.appendChild(help);
  container.appendChild(wrap);
  return wrap;
}

function buildFields(container, fields, config, prefix, mode, core) {
  for (const [name, spec] of Object.entries(fields)) {
    const path = prefix ? `${prefix}.${name}` : name;
    // tier split applies at the top level only; nested objects stay whole
    if (!prefix && mode === "core" && !core.has(name)) continue;
    if (!prefix && mode === "all" && core.has(name)) continue;
    if (spec.type === "object" && spec.fields) {
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.textContent = name;
      det.appendChild(sum);
      buildFields(det, spec.fields, config, path, mode, core);
      container.appendChild(det);
    } else {
      buildField(container, name, spec, config, path);
    }
  }
}

function readForm(form, config) {
  const out = JSON.parse(JSON.stringify(config));
  for (const wrap of form.querySelectorAll(".field[data-path]")) {
    const path = wrap.dataset.path;
    if (wrap.dataset.kv) {
      const parseV = (s) => { try { return JSON.parse(s); } catch { return s; } };
      const rows = [...wrap.querySelectorAll(".kv-row")]
        .map((r) => r.querySelectorAll("input"))
        .filter(([ki]) => ki.value !== "")
        .map(([ki, vi]) => [ki.value, parseV(vi.value)]);
      if (wrap.dataset.kv === "obj") {
        const o = {};
        for (const [k, v] of rows) o[k] = v;
        setPath(out, path, o);
      } else {
        setPath(out, path, rows.map(([k, v]) => ({ [k]: v })));
      }
      continue;
    }
    const inp = wrap.querySelector("[data-kind]");
    if (!inp) continue;
    const kind = inp.dataset.kind;
    let v;
    if (kind === "bool") v = inp.checked;
    else if (kind === "enum") v = inp.dataset.vtype === "number" ? Number(inp.value) : inp.value;
    else if (kind === "number") v = inp.value === "" ? null : Number(inp.value);
    else if (kind === "langlist") v = inp.value.split(",").map((s) => s.trim()).filter(Boolean);
    else v = inp.value === "" ? null : inp.value;
    setPath(out, path, v);
  }
  return out;
}

// View-only normalization: re-segment the raw final lines into sentences with one
// deterministic rule (split after . ! ? … followed by whitespace), merging fragments
// across lines. Raw lines are never modified or persisted differently.
// Timestamps: a sentence spanning lines takes t0 of its first line and t1 of its
// last; a sentence split within one line is interpolated by character fraction.
function normalizeLines(lines) {
  const finals = lines.filter((l) => !l.partial).sort((a, b) => a.t0 - b.t0);
  const out = [];
  let carry = null; // {t0, text} fragment waiting for its sentence to end
  for (const line of finals) {
    const parts = line.text.split(/(?<=[.!?…])\s+/).filter((s) => s.trim());
    if (!parts.length) continue;
    const total = parts.reduce((s, p) => s + p.length, 0) || 1;
    let off = 0;
    parts.forEach((p, i) => {
      const isLast = i === parts.length - 1;
      const f0 = off / total;
      const f1 = (off + p.length) / total;
      let text = p;
      let t0 = line.t0 + (line.t1 - line.t0) * f0;
      if (i === 0 && carry) {
        text = carry.text + " " + p;
        t0 = carry.t0;
      }
      off += p.length;
      const t1 = line.t0 + (line.t1 - line.t0) * f1;
      if (isLast && !/[.!?…]\s*$/.test(p)) {
        carry = { t0, text: text.trim() }; // no sentence end yet: merge with next line
      } else {
        out.push({ t0, t1, text: text.trim(), partial: false });
        carry = null;
      }
    });
  }
  if (carry) {
    const last = finals[finals.length - 1];
    out.push({ t0: carry.t0, t1: last ? last.t1 : carry.t0, text: carry.text, partial: false });
  }
  return out;
}

export function createPanel(root, { model, label, schema, currentConfig, onJump, onListen, onClear }) {
  root.innerHTML = `
    <header class="panel-head">
      <span class="model-name">${label}</span>
      <span class="spacer"></span>
      <button class="btn listen" aria-pressed="false">${I.play}<span>Listen</span></button>
      <button class="btn norm" aria-pressed="false" title="Re-segment this transcript into sentences (view only; raw output unchanged)">${I.norm}<span>Normalize</span></button>
      <button class="btn scroll on" aria-pressed="true" title="Auto-scroll transcript">${I.scroll}<span>Auto-scroll</span></button>
      <button class="btn gear" title="Settings for ${label}">${I.gear}<span>Settings</span></button>
      <button class="btn trash" title="Clear transcript ${model}">${I.trash}<span>Clear</span></button>
      <div class="popover" hidden>
        <div class="pop-title">${label} settings</div>
        <div class="pop-body"><div class="form"></div></div>
        <div class="pop-foot"><button class="apply">Apply</button><button class="reset">Reset</button><span class="cfg-status" role="status"></span></div>
      </div>
    </header>
    <div class="viewport" aria-live="polite" role="log"></div>`;

  const vp = root.querySelector(".viewport");
  const lines = []; // raw lines in memory; normalize renders a derived copy of these
  let normalize = false;
  // keys of lines already rendered - only NEW lines get the reveal animation
  const seenKeys = new Set();
  const keyOf = (l) => `${l.t0}|${l.t1}|${l.text}|${!!l.partial}`;
  const listenBtn = root.querySelector(".listen");
  const listenTxt = listenBtn.querySelector("span");
  const normBtn = root.querySelector(".norm");
  const scrollBtn = root.querySelector(".scroll");
  const gear = root.querySelector(".gear");
  const trash = root.querySelector(".trash");
  const pop = root.querySelector(".popover");
  const form = root.querySelector(".form");
  const cfgStatus = root.querySelector(".cfg-status");
  let autoScroll = true;
  let listening = false;
  let dropped = false;
  let pending = false;
  let pendingDir = null; // "start" | "stop" while a listen/stop request is in flight

  function renderForm() {
    form.replaceChildren();
    const main = document.createElement("section");
    main.className = "sec";
    const t = document.createElement("div");
    t.className = "sec-title";
    t.textContent = "Core";
    main.appendChild(t);
    form.appendChild(main);
    buildFields(main, schema.fields, currentConfig, "", "core", CORE[model]);
    const adv = document.createElement("details");
    adv.className = "sec-adv";
    const sum = document.createElement("summary");
    sum.textContent = "All provider options";
    adv.appendChild(sum);
    const advSec = document.createElement("section");
    advSec.className = "sec";
    adv.appendChild(advSec);
    buildFields(advSec, schema.fields, currentConfig, "", "all", CORE[model]);
    form.appendChild(adv);
    syncEnhancedDisabled();
  }

  // disable Enhanced-only knobs when melia-1 is selected (mirrors server validation)
  function syncEnhancedDisabled() {
    if (model !== "A") return;
    const modelVal = getPath(currentConfig, "model");
    const isMelia = modelVal === "melia-1";
    for (const wrap of form.querySelectorAll(".field[data-path]")) {
      if (MELIA1_FORBIDDEN.includes(wrap.dataset.path)) {
        wrap.classList.toggle("disabled", isMelia);
        for (const el of wrap.querySelectorAll("input, select")) {
          el.disabled = isMelia;
          el.title = isMelia ? "Enhanced models only" : "";
        }
      }
    }
  }
  form.addEventListener("change", (e) => {
    if (e.target.dataset && e.target.dataset.path === "model") {
      setPath(currentConfig, "model", e.target.value);
      syncEnhancedDisabled();
    }
  });

  function setListenState() {
    listenBtn.classList.remove("on", "danger");
    listenBtn.disabled = pending;
    if (pending) {
      if (pendingDir === "stop") listenBtn.innerHTML = `${I.stop}<span>Stopping…</span>`;
      else listenBtn.innerHTML = `${I.play}<span>Starting…</span>`;
      listenBtn.setAttribute("aria-pressed", "false");
    } else if (listening) {
      listenBtn.classList.add("on");
      listenBtn.innerHTML = `${I.stop}<span>Listening</span>`;
      listenBtn.setAttribute("aria-pressed", "true");
    } else if (dropped) {
      listenBtn.classList.add("danger");
      listenBtn.innerHTML = `${I.alert}<span>Dropped — re-listen</span>`;
      listenBtn.setAttribute("aria-pressed", "false");
    } else {
      listenBtn.innerHTML = `${I.play}<span>Listen</span>`;
      listenBtn.setAttribute("aria-pressed", "false");
    }
  }

  listenBtn.addEventListener("click", () => {
    if (pending) return;
    dropped = false;
    pending = true; // immediate feedback; cleared by the next state event or timeout
    pendingDir = listening ? "stop" : "start";
    setListenState();
    setTimeout(() => { if (pending) { pending = false; pendingDir = null; setListenState(); } }, 12000);
    onListen(listening);
  });
  trash.addEventListener("click", () => onClear());
  normBtn.addEventListener("click", () => {
    setNormalize(!normalize);
    normBtn.classList.toggle("on", normalize);
    normBtn.setAttribute("aria-pressed", String(normalize));
  });
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
  root.querySelector(".apply").addEventListener("click", async () => {
    cfgStatus.className = "cfg-status";
    cfgStatus.textContent = "applying…";
    try {
      const cfg = readForm(form, currentConfig);
      await postJSON("/config", { model, config: cfg });
      Object.keys(currentConfig).forEach((k) => delete currentConfig[k]);
      Object.assign(currentConfig, cfg);
      cfgStatus.textContent = "applied ✓";
    } catch (e) {
      cfgStatus.className = "cfg-status err";
      cfgStatus.textContent = "error: " + (e.data && e.data.error || e.message);
    }
  });
  root.querySelector(".reset").addEventListener("click", () => {
    renderForm();
    cfgStatus.textContent = "";
  });

  function setState(st) {
    const open = !!(st.sessions && st.sessions[model]);
    listening = !!(st.listening && st.listening[model]);
    if (open) { pending = false; pendingDir = null; dropped = false; } // session confirmed open
    else if (pendingDir === "stop" && !listening) { pending = false; pendingDir = null; } // stop confirmed
    else if (pendingDir === "start" && !listening) { pending = false; pendingDir = null; } // start failed
    else if (listening && !open && !pending) dropped = true;
    setListenState();
  }

  function setDrop(text) {
    pending = false;
    pendingDir = null;
    dropped = true;
    listening = false;
    setListenState();
    listenBtn.title = text;
  }

  function makeLineEl(line, animate) {
    const div = document.createElement("div");
    div.className = "line jump" + (line.partial ? " partial" : "");
    div.title = "Jump source player to this timestamp";
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = fmtTime(line.t0);
    const txt = document.createElement("span");
    txt.className = "txt";
    if (animate && !line.partial && !seenKeys.has(keyOf(line))) {
      // word-by-word staggered reveal for newly arrived finals (cosmetic only)
      const words = line.text.split(/(\s+)/);
      const nWords = Math.max(1, words.filter((w) => w.trim()).length);
      const step = Math.min(45, 900 / nWords); // cap total reveal at ~0.9 s
      let delay = 0;
      for (const w of words) {
        if (!w.trim()) {
          txt.appendChild(document.createTextNode(w));
          continue;
        }
        const s = document.createElement("span");
        s.className = "w";
        s.textContent = w;
        s.style.animationDelay = `${Math.round(delay)}ms`;
        delay += step;
        txt.appendChild(s);
      }
    } else {
      txt.textContent = line.text;
    }
    div.append(ts, txt);
    div.addEventListener("click", () => onJump(line.t0));
    return div;
  }

  function renderLines(animateNew) {
    let view;
    if (normalize) {
      view = normalizeLines(lines);
      const lastPartial = [...lines].reverse().find((l) => l.partial);
      if (lastPartial) view = [...view, lastPartial];
    } else {
      view = lines.filter((l) => !l.partial);
      const lastPartial = [...lines].reverse().find((l) => l.partial);
      if (lastPartial) view = [...view, lastPartial];
    }
    if (!animateNew) for (const l of view) seenKeys.add(keyOf(l)); // boot/toggle: no replay
    vp.innerHTML = "";
    for (const line of view) vp.appendChild(makeLineEl(line, animateNew));
    if (animateNew) for (const l of view) seenKeys.add(keyOf(l));
    if (autoScroll) vp.scrollTop = vp.scrollHeight;
  }

  function addLine(line) {
    lines.push(line);
    renderLines(true); // animate only the newly arrived line
  }

  function setLines(arr) {
    lines.length = 0;
    lines.push(...arr);
    renderLines(false);
  }

  function setNormalize(on) {
    normalize = !!on;
    renderLines(false); // re-render without replaying the reveal animation
  }

  function clear() {
    lines.length = 0;
    vp.innerHTML = "";
  }

  renderForm(); // last on purpose: a form-rendering error must never kill the listeners above
  setListenState();
  return { setState, setDrop, addLine, clear, setLines, setNormalize };
}
