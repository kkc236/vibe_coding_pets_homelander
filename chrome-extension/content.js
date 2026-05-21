(() => {
  const CHECK_MS = 900;
  const STABLE_TICKS = 3;
  let busy = false;
  let initialized = false;
  let lastText = "";
  let stable = 0;
  let lastSuccessAt = 0;
  let lastThinkingAt = 0;
  let tickQueued = false;
  let sawComposer = false;

  function assistantText() {
    const selectors = [
      '[data-message-author-role="assistant"]',
      '[data-testid*="assistant"]',
      '[data-testid*="bot"]',
      '[data-testid*="message"]',
      '[data-testid*="conversation"]',
      '[data-test-id*="assistant"]',
      "model-response",
      "message-content",
      "message-bubble",
      ".chat-message",
      ".model-response-text",
      ".response-container",
      ".prose",
      ".markdown",
      "article"
    ];
    const nodes = Array.from(document.querySelectorAll(selectors.join(",")));
    const last = nodes
      .filter((node) => isVisible(node) && (node.innerText || "").trim().length > 20)
      .at(-1);
    return ((last && last.innerText) || document.body.innerText || "").slice(-5000);
  }

  function isVisible(node) {
    return !!(node && (node.offsetParent || node.getClientRects().length));
  }

  function buttonText(node) {
    return [
      node.innerText,
      node.getAttribute("aria-label"),
      node.getAttribute("title")
    ].filter(Boolean).join(" ");
  }

  function hasStopSignal() {
    const stopPattern = /stop generating|stop responding|stop response|stop|停止生成|停止回答|停止|cancel response|cancel generation|interrupt/i;
    const controls = Array.from(document.querySelectorAll("button, [role='button'], [aria-label], [title]"));
    if (controls.some((node) => isVisible(node) && stopPattern.test(buttonText(node)))) {
      return true;
    }

    const text = document.body.innerText || "";
    return /stop generating|stop responding|stop response|停止生成|停止回答/i.test(text);
  }

  function composerElements() {
    return Array.from(document.querySelectorAll("textarea, [contenteditable='true'], [role='textbox']"))
      .filter(isVisible);
  }

  function inputLooksReady(composers = composerElements()) {
    const editable = composers.at(-1);
    if (!editable) return false;
    if (editable.disabled) return false;
    if (editable.getAttribute("aria-disabled") === "true") return false;
    return true;
  }

  function send(action, payload = {}) {
    try {
      chrome.runtime.sendMessage({
        target: "ai-finish-pet",
        action,
        source: location.hostname,
        ...payload
      }, () => void chrome.runtime.lastError);
    } catch {
      // The extension may have been reloaded while this tab stayed open.
    }
  }

  function notify(type, message) {
    send("notify", { type, message });
  }

  function noteThinking() {
    const now = Date.now();
    if (now - lastThinkingAt < 2000) return;
    lastThinkingAt = now;
    notify("thinking", "AI is generating");
  }

  function tick() {
    const text = assistantText();
    const stop = hasStopSignal();
    const composers = composerElements();
    if (composers.length > 0) sawComposer = true;
    const ready = inputLooksReady(composers);
    const composerBusy = sawComposer && !ready;
    const textChanged = text !== lastText && text.trim().length > 20;

    if (!initialized) {
      initialized = true;
      lastText = text;
      return;
    }

    if ((stop || composerBusy || textChanged) && !busy) {
      busy = true;
      stable = 0;
      noteThinking();
    }

    if (busy) {
      stable = text === lastText ? stable + 1 : 0;
      const enoughText = text.trim().length > 20;
      const cool = Date.now() - lastSuccessAt > 5000;
      const canFinish = !stop && (ready || composers.length === 0);
      if (canFinish && stable >= STABLE_TICKS && enoughText && cool) {
        busy = false;
        lastSuccessAt = Date.now();
        notify("success", "AI answer finished");
      }
    }

    lastText = text;
  }

  function scheduleTick() {
    if (tickQueued) return;
    tickQueued = true;
    setTimeout(() => {
      tickQueued = false;
      tick();
    }, 150);
  }

  send("connect");
  new MutationObserver(scheduleTick).observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true
  });
  setInterval(tick, CHECK_MS);
})();
