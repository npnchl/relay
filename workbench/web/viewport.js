import { fmtTime } from "./transport.js";

const ICON_GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/></svg>';

export function createViewport(root, model, label, { onConfig, onClear, onReset }) {
  root.innerHTML = `
    <header class="panel-head">
      <span class="badge">${model}</span>
      <span class="model-name" title="${label}">${label}</span>
      <span class="spacer"></span>
      <label class="switch" title="Auto scroll">
        <input type="checkbox" class="autoscroll" checked aria-label="Auto scroll ${model}">
        <span class="knob"></span>
      </label>
      <button class="icon-btn gear" aria-label="Settings for model ${model}" title="Settings">${ICON_GEAR}</button>
      <button class="icon-btn trash" aria-label="Clear transcript ${model}" title="Clear">${ICON_TRASH}</button>
      <div class="popover" hidden>
        <div class="pop-title">Config ${model} (JSON; applies immediately if playing, else on next play)</div>
        <textarea class="config" spellcheck="false" aria-label="Config JSON for model ${model}"></textarea>
        <div class="pop-row"><button class="apply">Apply</button><button class="reset">Reset</button><span class="cfg-status"></span></div>
      </div>
    </header>
    <div class="viewport" aria-live="polite" role="log"></div>`;

  const vp = root.querySelector(".viewport");
  const gear = root.querySelector(".gear");
  const pop = root.querySelector(".popover");
  const ta = root.querySelector(".config");
  const status = root.querySelector(".cfg-status");
  const autoscroll = root.querySelector(".autoscroll");

  gear.addEventListener("click", (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !gear.contains(e.target)) pop.hidden = true;
  });
  root.querySelector(".apply").addEventListener("click", () => {
    try {
      onConfig(JSON.parse(ta.value));
      status.textContent = "";
    } catch {
      status.textContent = "invalid JSON";
    }
  });
  root.querySelector(".trash").addEventListener("click", onClear);
  root.querySelector(".reset").addEventListener("click", () => {
    status.textContent = "";
    onReset();
  });

  return {
    model,
    setConfig(cfg) {
      ta.value = JSON.stringify(cfg, null, 2);
    },
    setStatus(t) {
      status.textContent = t;
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
        if (last && last.classList.contains("partial")) {
          // a final line replaces the partial it completes; a new partial replaces the previous one
          last.replaceWith(div);
        } else {
          vp.appendChild(div);
        }
      }
      if (autoscroll.checked) vp.scrollTop = vp.scrollHeight;
    },
  };
}
