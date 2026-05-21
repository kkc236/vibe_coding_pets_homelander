const BASE = "http://127.0.0.1:8765";

let token = null;
let tokenPromise = null;

async function readJson(res) {
  const text = await res.text();
  if (!text) return {};
  return JSON.parse(text);
}

async function getToken(force = false) {
  if (token && !force) return token;
  if (tokenPromise && !force) return tokenPromise;

  tokenPromise = fetch(`${BASE}/token`, { cache: "no-store" })
    .then(async (res) => {
      if (!res.ok) throw new Error(`Token request failed: ${res.status}`);
      const data = await readJson(res);
      if (!data.token) throw new Error("Pet token missing");
      token = data.token;
      return token;
    })
    .finally(() => {
      tokenPromise = null;
    });

  return tokenPromise;
}

async function post(path, payload, needsToken = true, retried = false) {
  const headers = { "Content-Type": "application/json" };
  if (needsToken) headers["X-Pet-Token"] = await getToken();

  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload || {})
  });

  if (needsToken && res.status === 401 && !retried) {
    token = null;
    return post(path, payload, true, true);
  }

  const data = await readJson(res).catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || `Pet request failed: ${res.status}`);
  }
  return data;
}

async function status() {
  const res = await fetch(`${BASE}/status`, { cache: "no-store" });
  const data = await readJson(res).catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `Pet status failed: ${res.status}`);
  return data;
}

async function notify(message) {
  return post("/notify", {
    type: message.type,
    source: message.source || "web",
    message: message.message || ""
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.target !== "ai-finish-pet") return false;

  (async () => {
    if (message.action === "status") {
      return status();
    }
    if (message.action === "test") {
      return post("/test", {
        type: "success",
        source: "extension-popup",
        message: "test"
      }, false);
    }
    if (message.action === "connect") {
      await getToken();
      return notify({
        type: "idle",
        source: message.source,
        message: "bridge connected"
      });
    }
    if (message.action === "notify") {
      return notify(message);
    }
    throw new Error(`Unknown action: ${message.action}`);
  })()
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({
      ok: false,
      message: error && error.message ? error.message : String(error)
    }));

  return true;
});
