// Thin REST + SSE client for the workbench backend.

export async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

export async function postJSON(path, payload) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.error || `${path}: ${r.status}`);
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

// Server-Sent Events stream: {type:"state"|"line"|"provider"|"cleared", ...}
export function connectEvents(onMessage, onStatus) {
  const es = new EventSource("/events");
  es.onopen = () => onStatus(true);
  es.onerror = () => onStatus(false);
  es.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      /* ignore malformed frame */
    }
  };
  return es;
}

export function fmtTime(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
