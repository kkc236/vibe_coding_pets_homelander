const statusEl = document.querySelector("#status");

document.querySelector("#test").addEventListener("click", async () => {
  chrome.runtime.sendMessage({
    target: "ai-finish-pet",
    action: "test"
  }, (res) => {
    if (chrome.runtime.lastError) {
      statusEl.textContent = "扩展后台没有响应，请重新加载扩展。";
      return;
    }
    statusEl.textContent = res && res.ok ? "已发送测试提醒。" : "请先启动桌宠。";
  });
});
