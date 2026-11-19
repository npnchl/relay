export function connect(url, onMessage, onOpen, onClose) {
  const ws = new WebSocket(url);
  ws.addEventListener("open", () => onOpen && onOpen());
  ws.addEventListener("close", () => onClose && onClose());
  ws.addEventListener("message", (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    onMessage(msg);
  });
  return {
    send(obj) {
      if (ws.readyState === 1) ws.send(JSON.stringify(obj));
    },
  };
}
