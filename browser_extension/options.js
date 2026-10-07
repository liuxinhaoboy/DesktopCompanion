/* options.js —— 保存配对配置并让 background 立即重连，轮询显示连接状态 */
const $ = (id) => document.getElementById(id);

function fill() {
  chrome.storage.local.get(
    { host: "127.0.0.1", port: 8731, pairCode: "", session: "", connected: false },
    (cfg) => {
      $("host").value = cfg.host;
      $("port").value = cfg.port;
      $("pairCode").value = cfg.pairCode || "";
      render(cfg.connected, cfg.session);
    }
  );
}

function render(connected, session) {
  const el = $("status");
  if (connected) {
    el.className = "ok";
    el.textContent = "● 已和桌宠连上（会话已保存，之后会自动重连）";
  } else if (session) {
    el.className = "bad";
    el.textContent = "○ 之前配对过但现在没连上，确认桌宠正在运行后会自动重连";
  } else {
    el.className = "bad";
    el.textContent = "○ 还没配对：填好配对码点「保存并连接」";
  }
}

$("save").addEventListener("click", () => {
  const host = $("host").value.trim() || "127.0.0.1";
  const port = Number($("port").value.trim()) || 8731;
  const pairCode = $("pairCode").value.trim();
  // 安全：host 只允许本机回环，防止被改成连远程
  if (!/^(127\.0\.0\.1|localhost)$/i.test(host)) {
    $("status").className = "bad";
    $("status").textContent = "地址只能填 127.0.0.1 或 localhost（只连本机）";
    return;
  }
  chrome.storage.local.set({ host, port, pairCode, session: "" }, () => {
    chrome.runtime.sendMessage({ type: "reconnect" }, () => {
      $("status").className = "";
      $("status").textContent = "已保存，正在连接…请稍候看状态变化";
      setTimeout(fill, 1500);
    });
  });
});

// 每 2 秒刷新一次连接状态
setInterval(fill, 2000);
fill();
