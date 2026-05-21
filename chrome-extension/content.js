(() => {
  const BASE = "http://127.0.0.1:8765";
  const CHECK_MS = 1200;
  const STABLE_TICKS = 2;
  let token = null;
  let busy = false;
  let lastText = "";
  let stable = 0;
  let lastSuccessAt = 0;

  async function ensureToken() {
    if (token) return token;
    const res = await fetch(`${BASE}/token`);
    const data = await res.json();
    token = data.token;
    return token;
  }

  function assistantText() {
    const selectors = [
      '[data-message-author-role="assistant"]',
      '[data-testid*="assistant"]',
      ".markdown",
      "article"
    ];
    const nodes = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
    const last = nodes.filter((node) => (node.innerText || "").trim().length > 20).at(-1);
    return ((last && last.innerText) || document.body.innerText || "").slice(-5000);
  }

  function hasStopSignal() {
    const text = document.body.innerText || "";
    return /stop generating|stop responding|停止生成|停止回答|停止/i.test(text);
  }

  function inputLooksReady() {
    const editable = document.querySelector("textarea, [contenteditable='true']");
    if (!editable) return false;
    if (editable.disabled) return false;
    if (editable.getAttribute("aria-disabled") === "true") return false;
    return true;
  }

  async function notify(type, message) {
    const t = await ensureToken();
    await fetch(`${BASE}/notify`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Pet-Token": t
      },
      body: JSON.stringify({
        type,
        source: location.hostname,
        message
      })
    }).catch(() => {});
  }

  function tick() {
    const text = assistantText();
    const stop = hasStopSignal();
    const ready = inputLooksReady();

    if ((stop || !ready) && !busy) {
      busy = true;
      stable = 0;
      notify("thinking", "AI is generating");
    }

    if (busy) {
      stable = text === lastText ? stable + 1 : 0;
      const enoughText = text.trim().length > 20;
      const cool = Date.now() - lastSuccessAt > 5000;
      if (!stop && ready && stable >= STABLE_TICKS && enoughText && cool) {
        busy = false;
        lastSuccessAt = Date.now();
        notify("success", "AI answer finished");
      }
    }

    lastText = text;
  }

  ensureToken()
    .then(() => notify("idle", "bridge connected"))
    .catch(() => {});
  setInterval(tick, CHECK_MS);
})();
