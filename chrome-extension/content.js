(() => {
  const CHECK_MS = 700;
  const STABLE_TICKS = 3;
  const host = location.hostname;
  const isGemini = host === "gemini.google.com";
  let busy = false;
  let initialized = false;
  let lastText = "";
  let stable = 0;
  let lastSuccessAt = 0;
  let lastThinkingAt = 0;
  let lastChangeAt = 0;
  let tickQueued = false;
  let sawComposer = false;

  const genericResponseSelectors = [
      '[data-message-author-role="assistant"]',
      '[data-testid*="assistant"]',
      '[data-testid*="bot"]',
      '[data-testid*="message"]',
      '[data-testid*="conversation"]',
      '[data-test-id*="assistant"]',
      ".chat-message",
      ".model-response-text",
      ".response-container",
      ".prose",
      ".markdown",
      "article"
  ];

  const geminiResponseSelectors = [
    ".model-response-text",
    "[id^='model-response-message-content']",
    "model-response .markdown",
    "model-response message-content",
    "model-response",
    "message-content",
    "message-bubble",
    "[data-response-id]",
    ".response-container",
    ".conversation-container .markdown",
    ".conversation-container",
    "[role='main'] .markdown",
    "main .markdown"
  ];

  function query(selectors) {
    try {
      return Array.from(document.querySelectorAll(selectors.join(",")));
    } catch {
      return [];
    }
  }

  function nodeText(node) {
    return ((node && (node.innerText || node.textContent)) || "").trim();
  }

  function bestText(selectors) {
    const nodes = query(selectors);
    const last = nodes
      .filter((node) => isVisible(node) && nodeText(node).length > 20)
      .at(-1);
    return nodeText(last).slice(-5000);
  }

  function scopedPageText() {
    const roots = query(["main", "[role='main']", ".conversation-container", ".chat-history"]);
    const root = roots.find((node) => isVisible(node) && nodeText(node).length > 20);
    return nodeText(root).slice(-5000);
  }

  function assistantText(generating = false) {
    const text = bestText(isGemini ? geminiResponseSelectors : genericResponseSelectors);
    if (text) return text;
    if (isGemini) {
      return busy || generating ? scopedPageText() : "";
    }
    return ((document.body && document.body.innerText) || "").slice(-5000);
  }

  function isVisible(node) {
    return !!(node && (node.offsetParent || node.getClientRects().length));
  }

  function buttonText(node) {
    return [
      node.innerText,
      node.textContent,
      node.getAttribute("aria-label"),
      node.getAttribute("title"),
      node.getAttribute("data-tooltip"),
      node.getAttribute("mattooltip")
    ].filter(Boolean).join(" ");
  }

  function hasProgressSignal() {
    if (!isGemini) return false;
    return query([
      '[aria-busy="true"]',
      '[role="progressbar"]',
      "mat-progress-bar",
      "mat-spinner",
      ".mat-mdc-progress-spinner",
      ".generating",
      ".loading"
    ]).some(isVisible);
  }

  function hasGeneratingSignal() {
    const stopPattern = isGemini
      ? /(^|\b)(stop response|stop generating|cancel response|cancel generation|interrupt)(\b|$)|^\s*stop\s*$|停止生成|停止回答|^\s*停止\s*$/i
      : /stop generating|stop responding|stop response|停止生成|停止回答|停止|cancel response|cancel generation|interrupt/i;
    const controls = Array.from(document.querySelectorAll("button, [role='button'], [aria-label], [title]"));
    if (controls.some((node) => isVisible(node) && stopPattern.test(buttonText(node)))) {
      return true;
    }
    if (hasProgressSignal()) return true;

    if (isGemini) return false;
    const text = document.body.innerText || "";
    return /stop generating|stop responding|stop response|停止生成|停止回答/i.test(text);
  }

  function composerElements() {
    return Array.from(document.querySelectorAll("textarea, rich-textarea, .ql-editor, [contenteditable='true'], [role='textbox']"))
      .filter(isVisible);
  }

  function inputLooksReady(composers = composerElements()) {
    const editable = composers.at(-1);
    if (!editable) return false;
    if (editable.disabled) return false;
    if (editable.getAttribute("aria-disabled") === "true") return false;
    if (editable.getAttribute("contenteditable") === "false") return false;
    return true;
  }

  function normalizeText(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
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
    const generating = hasGeneratingSignal();
    const text = normalizeText(assistantText(generating));
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

    if (textChanged) {
      lastChangeAt = Date.now();
    }

    const startSignal = generating || (isGemini ? textChanged : composerBusy || textChanged);
    if (startSignal && !busy) {
      busy = true;
      stable = 0;
      noteThinking();
    }

    if (busy) {
      stable = text === lastText ? stable + 1 : 0;
      const enoughText = text.trim().length > 20;
      const cool = Date.now() - lastSuccessAt > 5000;
      const quietLongEnough = Date.now() - lastChangeAt > 1200;
      const canFinish = isGemini
        ? !generating
        : !generating && (ready || composers.length === 0);
      if (canFinish && stable >= STABLE_TICKS && quietLongEnough && enoughText && cool) {
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
