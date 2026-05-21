const statusEl = document.querySelector("#status");

document.querySelector("#test").addEventListener("click", async () => {
  try {
    const res = await fetch("http://127.0.0.1:8765/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "success", source: "extension-popup", message: "test" })
    });
    statusEl.textContent = res.ok ? "已发送测试提醒。" : "桌宠没有响应。";
  } catch {
    statusEl.textContent = "请先启动桌宠。";
  }
});
