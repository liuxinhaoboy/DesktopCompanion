/*
 * background.js —— MV3 Service Worker
 * 负责：和桌宠的本机桥(127.0.0.1)建立 WebSocket、握手/重连、
 *       收到指令后路由给当前标签页的 content.js 执行，再把结果回传。
 * 安全：只连本机回环；每条指令带会话 token（由桥下发，这里原样回带由桥校验）。
 */

const STATE = {
  ws: null,
  session: "",
  host: "127.0.0.1",
  port: 8731,
  pairCode: "",
  connected: false,
  retry: 0,
  reconnectTimer: null,
};

// ---------- 存储读写 ----------
function loadConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(
      { host: "127.0.0.1", port: 8731, pairCode: "", session: "" },
      (cfg) => {
        STATE.host = cfg.host || "127.0.0.1";
        STATE.port = Number(cfg.port) || 8731;
        STATE.pairCode = cfg.pairCode || "";
        STATE.session = cfg.session || "";
        resolve();
      }
    );
  });
}

function saveSession(session) {
  STATE.session = session || "";
  chrome.storage.local.set({ session: STATE.session });
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setBadgeText({ text });
  } catch (e) { /* 旧版浏览器忽略 */ }
}

function setConnected(ok) {
  STATE.connected = ok;
  setBadge(ok ? "连" : "断", ok ? "#2e7d32" : "#9e9e9e");
  chrome.storage.local.set({ connected: ok });
}

// ---------- 连接 ----------
async function connect() {
  await loadConfig();
  // 既没配对码也没历史会话，就不连（用户还没配对）
  if (!STATE.pairCode && !STATE.session) {
    setConnected(false);
    return;
  }
  if (STATE.ws && (STATE.ws.readyState === WebSocket.OPEN ||
                   STATE.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const url = `ws://${STATE.host}:${STATE.port}`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  STATE.ws = ws;

  ws.onopen = () => {
    STATE.retry = 0;
    // 有历史会话优先重连，否则用一次性配对码
    const hello = STATE.session
      ? { type: "hello", session: STATE.session, ext_version: "1.0" }
      : { type: "hello", pair_code: STATE.pairCode, ext_version: "1.0" };
    ws.send(JSON.stringify(hello));
  };

  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }

    if (msg.type === "hello_ack") {
      if (msg.ok) {
        saveSession(msg.session);
        STATE.pairCode = "";
        chrome.storage.local.set({ pairCode: "" });
        setConnected(true);
      } else {
        // 配对失败（码错/过期/会话失效）：清掉，提示用户去选项页重新配对
        saveSession("");
        setConnected(false);
        try { ws.close(); } catch (e) {}
      }
      return;
    }

    if (msg.type === "pong") return;

    if (msg.type === "command") {
      const result = await dispatch(msg);
      ws.send(JSON.stringify({
        type: "result",
        session: STATE.session,
        cmd_id: msg.cmd_id,
        ok: result.ok,
        data: result.data || {},
        error: result.error || "",
      }));
    }
  };

  ws.onclose = () => { setConnected(false); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

function scheduleReconnect() {
  if (STATE.reconnectTimer) return;
  // 指数退避：1s,2s,4s... 最长 15s，避免空转狂连
  const delay = Math.min(15000, 1000 * Math.pow(2, STATE.retry++));
  STATE.reconnectTimer = setTimeout(() => {
    STATE.reconnectTimer = null;
    connect();
  }, delay);
}

// ---------- 指令路由 ----------
async function dispatch(cmd) {
  const action = cmd.action;
  const target = cmd.target || "";
  const args = cmd.args || {};
  try {
    // 这两个动作由 background 直接用 tabs API 做，不进页面 DOM
    if (action === "open_url") {
      if (!/^https?:\/\//i.test(target)) {
        return { ok: false, error: "只允许打开 http/https 网页" };
      }
      const tab = await chrome.tabs.create({ url: target, active: true });
      return { ok: true, data: { tab_id: tab.id } };
    }
    if (action === "close_tab") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab) await chrome.tabs.remove(tab.id);
      return { ok: true, data: {} };
    }
    // 页面内动作：发给当前活动标签页的 content.js
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https?:/i.test(tab.url || "")) {
      return { ok: false, error: "当前标签不是普通网页，无法操作" };
    }
    const resp = await chrome.tabs.sendMessage(tab.id, {
      type: "do_action", action, target, args,
    });
    return resp || { ok: false, error: "页面没有回应（刷新一下页面再试）" };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}

// ---------- 心跳 + service worker 保活 ----------
chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(() => {
  if (STATE.connected && STATE.ws && STATE.ws.readyState === WebSocket.OPEN) {
    try { STATE.ws.send(JSON.stringify({ type: "ping", session: STATE.session })); }
    catch (e) {}
  } else {
    connect();
  }
});

// 选项页保存了新配置 -> 立刻重连
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "reconnect") {
    saveSession("");
    if (STATE.ws) { try { STATE.ws.close(); } catch (e) {} }
    connect();
    sendResponse({ ok: true });
  }
  return true;
});

// 启动 / 安装时连一次
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
