import { fmtTime } from "./transport.js";

const ICON_GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/></svg>';

const STATUS_LABEL = { idle: "idle", active: "session running", error: "error, see lines below" };

// Form field specs per model. "enh" fields only apply to Speechmatics enhanced.
const FIELDS = {
  A: [
    { k: "model", label: "Model", type: "select", options: ["melia-1", "enhanced"] },
    { k: "language", label: "Language", type: "text", def: "multi" },
    { k: "partials", label: "Enable partials", type: "check", def: false },
    { k: "max_delay", label: "Max delay (s)", type: "number", step: "0.1", def: "4.0", enh: true },
    { k: "max_delay_mode", label: "Delay mode", type: "select", options: ["default", "flexible"], def: "flexible", enh: true },
    { k: "punct", label: "Punct. sensitivity", type: "number", step: "0.05", def: "0.3", enh: true },
    { k: "disfluencies", label: "Remove disfluencies", type: "check", def: true, enh: true },
  ],
  B: [
    { k: "model", label: "Model", type: "select", options: ["solaria-1", "solaria-3"] },
    { k: "languages", label: "Languages", type: "text", def: "en,de", ph: "en,de (empty = auto)" },
    { k: "code_switching", label: "Code switching", type: "check", def: true },
    { k: "endpointing", label: "Endpointing (s)", type: "number", step: "0.1", def: "0.5" },
    { k: "maxdur", label: "Max utterance (s)", type: "number", step: "1", def: "5" },
    { k: "partials", label: "Receive partials", type: "check", def: false },
    { k: "enhancer", label: "Audio enhancer", type: "check", def: false },
  ],
};

function buildConfig(model, f) {
  if (model === "A") {
    const tc = { model: f.model, language: f.language || "multi", enable_partials: !!f.partials };
    if (f.model === "enhanced") {
      tc.max_delay = parseFloat(f.max_delay) || 4.0;
      tc.max_delay_mode = f.max_delay_mode;
      tc.punctuation_overrides = { sensitivity: parseFloat(f.punct) || 0.3 };
      tc.transcript_filtering_config = { remove_disfluencies: !!f.disfluencies };
    }
    return {
      message: "StartRecognition",
      audio_format: { type: "raw", encoding: "pcm_s16le", sample_rate: 16000 },
      transcription_config: tc,
    };
  }
  const langs = (f.languages || "").split(",").map((s) => s.trim()).filter(Boolean);
  return {
    encoding: "wav/pcm",
    bit_depth: 16,
    sample_rate: 16000,
    channels: 1,
    model: f.model,
    language_config: { languages: langs, code_switching: !!f.code_switching },
    endpointing: parseFloat(f.endpointing) || 0.5,
    maximum_duration_without_endpointing: parseFloat(f.maxdur) || 5,
    messages_config: { receive_partial_transcripts: !!f.partials, receive_final_transcripts: true },
    ...(f.enhancer ? { pre_processing: { audio_enhancer: true } } : {}),
  };
}

function fillForm(model, cfg) {
  if (model === "A") {
    const tc = cfg.transcription_config || {};
    return {
      model: tc.model || "melia-1",
      language: tc.language || "multi",
      partials: !!tc.enable_partials,
      max_delay: tc.max_delay ?? 4.0,
      max_delay_mode: tc.max_delay_mode || "flexible",
      punct: tc.punctuation_overrides?.sensitivity ?? 0.3,
      disfluencies: tc.transcript_filtering_config?.remove_disfluencies ?? true,
    };
  }
  const lc = cfg.language_config || {};
  const mc = cfg.messages_config || {};
  return {
    model: cfg.model || "solaria-1",
    languages: (lc.languages || []).join(","),
    code_switching: lc.code_switching ?? true,
    endpointing: cfg.endpointing ?? 0.5,
    maxdur: cfg.maximum_duration_without_endpointing ?? 5,
    partials: !!mc.receive_partial_transcripts,
    enhancer: !!cfg.pre_processing?.audio_enhancer,
  };
}

export function createViewport(root, model, label, { onConfig, onClear, onReset, getAutoScroll }) {
  root.innerHTML = `
    <header class="panel-head">
      <span class="dot" data-state="idle" role="img" aria-label="status: idle" title="idle"></span>
      <span class="model-name" title="${label}">${label}</span>
      <span class="spacer"></span>
      <button class="icon-btn gear" aria-label="Settings for ${label}" title="Settings">${ICON_GEAR}</button>
      <button class="icon-btn trash" aria-label="Clear transcript ${model}" title="Clear">${ICON_TRASH}</button>
      <div class="popover" hidden>
        <div class="pop-title">${label} settings</div>
        <div class="form"></div>
        <div class="pop-row"><button class="apply">Apply</button><button class="reset">Reset</button><span class="cfg-status"></span></div>
      </div>
    </header>
    <div class="viewport" aria-live="polite" role="log"></div>`;

  const vp = root.querySelector(".viewport");
  const gear = root.querySelector(".gear");
  const pop = root.querySelector(".popover");
  const form = root.querySelector(".form");
  const status = root.querySelector(".cfg-status");
  const dot = root.querySelector(".dot");

  // build the settings form
  const inputs = {};
  let enhWrap = null;
  for (const spec of FIELDS[model]) {
    const wrap = document.createElement("label");
    wrap.className = "field" + (spec.type === "check" ? " check" : "");
    if (spec.enh) {
      if (!enhWrap) {
        enhWrap = document.createElement("div");
        enhWrap.className = "enh";
        form.appendChild(enhWrap);
      }
      wrap.dataset.enh = "1";
      enhWrap.appendChild(wrap);
    } else {
      form.appendChild(wrap);
    }
    if (spec.type === "check") {
      const inp = document.createElement("input");
      inp.type = "checkbox";
      inp.className = "fcheck";
      inp.dataset.k = spec.k;
      inp.checked = !!spec.def;
      wrap.appendChild(inp);
      const t = document.createElement("span");
      t.className = "flabel";
      t.textContent = spec.label;
      wrap.appendChild(t);
      inputs[spec.k] = inp;
    } else {
      const t = document.createElement("span");
      t.className = "flabel";
      t.textContent = spec.label;
      wrap.appendChild(t);
      let inp;
      if (spec.type === "select") {
        inp = document.createElement("select");
        for (const o of spec.options) {
          const opt = document.createElement("option");
          opt.value = o;
          opt.textContent = o;
          inp.appendChild(opt);
        }
        inp.value = spec.options[0];
      } else {
        inp = document.createElement("input");
        inp.type = spec.type;
        if (spec.step) inp.step = spec.step;
        if (spec.ph) inp.placeholder = spec.ph;
        inp.value = spec.def ?? "";
      }
      inp.className = "finput";
      inp.dataset.k = spec.k;
      wrap.appendChild(inp);
      inputs[spec.k] = inp;
    }
  }
  const syncEnh = () => {
    if (enhWrap) enhWrap.style.display = inputs.model.value === "enhanced" ? "" : "none";
  };
  if (inputs.model) inputs.model.addEventListener("change", syncEnh);

  const readForm = () => {
    const f = {};
    for (const [k, inp] of Object.entries(inputs)) {
      f[k] = inp.type === "checkbox" ? inp.checked : inp.value;
    }
    return f;
  };

  gear.addEventListener("click", (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !gear.contains(e.target)) pop.hidden = true;
  });
  root.querySelector(".apply").addEventListener("click", () => {
    onConfig(buildConfig(model, readForm()));
    status.textContent = "";
  });
  root.querySelector(".reset").addEventListener("click", () => {
    status.textContent = "";
    onReset();
  });
  root.querySelector(".trash").addEventListener("click", onClear);

  return {
    model,
    setDefaultConfig(cfg) {
      const f = fillForm(model, cfg);
      for (const [k, v] of Object.entries(f)) {
        const inp = inputs[k];
        if (!inp) continue;
        if (inp.type === "checkbox") inp.checked = !!v;
        else inp.value = v;
      }
      syncEnh();
    },
    setStatus(t) {
      status.textContent = t;
    },
    setDot(s) {
      dot.dataset.state = s;
      dot.title = STATUS_LABEL[s] || s;
      dot.setAttribute("aria-label", "status: " + (STATUS_LABEL[s] || s));
    },
    clear() {
      vp.innerHTML = "";
    },
    addLine(msg) {
      if (msg.type === "error") {
        const div = document.createElement("div");
        div.className = "line err";
        const span = document.createElement("span");
        span.className = "txt";
        span.textContent = `[${msg.model}] error: ${msg.message}`;
        div.appendChild(span);
        vp.appendChild(div);
      } else {
        const div = document.createElement("div");
        div.className = "line" + (msg.partial ? " partial" : "");
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = fmtTime(msg.t0);
        const txt = document.createElement("span");
        txt.className = "txt";
        txt.textContent = msg.text;
        div.append(ts, txt);
        const last = vp.lastElementChild;
        if (last && last.classList.contains("partial")) last.replaceWith(div);
        else vp.appendChild(div);
      }
      if (getAutoScroll()) vp.scrollTop = vp.scrollHeight;
    },
  };
}
