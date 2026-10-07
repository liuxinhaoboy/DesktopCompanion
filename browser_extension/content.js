/*
 * content.js —— 注入到普通网页里执行受限动作（只读/点击/输入/滚动）。
 * 这是最后一道防线：即便桌宠侧策略漏判，这里也会再次拒绝密码、验证码、
 * 最终提交/付款、文件上传。绝不执行页面传来的任意脚本。
 */
(() => {
  if (window.__desktopCompanionInjected) return;
  window.__desktopCompanionInjected = true;

  const MAX_TEXT = 4000; // 读正文最多回传字符数，避免把整页塞进模型上下文

  const SENSITIVE_FIELD = /(password|passwd|pwd|captcha|verify|otp|2fa|totp|sms|验证码|安全码|cvv)/i;
  const SUBMIT_TEXT = /(提交订单|确认付款|立即支付|确认支付|付款|支付|转账|确认下单|立即购买|submit|place order|pay now)/i;

  function isSensitiveInput(el) {
    if (!el) return false;
    if (el.type === "password" || el.type === "file") return true;
    const blob = [el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute("aria-label")]
      .filter(Boolean).join(" ");
    return SENSITIVE_FIELD.test(blob);
  }

  // 找元素：先当 CSS 选择器，找不到再按可见文本匹配按钮/链接
  function findElement(target) {
    if (!target) return null;
    try {
      const direct = document.querySelector(target);
      if (direct) return direct;
    } catch (e) { /* 不是合法选择器，走文本匹配 */ }
    const nodes = document.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]");
    const want = target.trim().toLowerCase();
    for (const n of nodes) {
      const txt = (n.innerText || n.value || n.getAttribute("aria-label") || "").trim().toLowerCase();
      if (txt && (txt === want || txt.includes(want))) return n;
    }
    return null;
  }

  // 兼容 React/Vue 受控输入：直接赋 value 不会触发框架更新，要用原生 setter
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const actions = {
    read_text() {
      const text = (document.body.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
      return {
        ok: true,
        data: {
          title: document.title,
          url: location.href,
          text: text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) + "\n…（已截断）" : text,
          truncated: text.length > MAX_TEXT,
        },
      };
    },

    scroll(target, args) {
      const dx = Number(args.dx || (args.dir === "left" ? -300 : args.dir === "right" ? 300 : 0));
      const dy = Number(args.dy || (args.dir === "up" ? -300 : args.dir === "down" ? 300 : 300));
      window.scrollBy({ left: dx, top: dy, behavior: "smooth" });
      return { ok: true, data: { scroll_y: Math.round(window.scrollY) } };
    },

    click(target) {
      const el = findElement(target);
      if (!el) return { ok: false, error: `没找到元素：${target}` };
      if (el.type === "file") return { ok: false, error: "文件上传不允许自动操作" };
      const label = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
      if (SUBMIT_TEXT.test(label) || el.type === "submit") {
        return { ok: false, error: "这是最终提交/付款按钮，需要你自己手动点" };
      }
      el.scrollIntoView({ block: "center", behavior: "instant" });
      el.click();
      return { ok: true, data: { clicked: label || target } };
    },

    type_text(target, args) {
      const value = String(args.text || "");
      const el = findElement(target) || document.activeElement;
      if (!el || !(/^(INPUT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable)) {
        return { ok: false, error: "没找到可输入的输入框" };
      }
      if (isSensitiveInput(el)) {
        return { ok: false, error: "密码/验证码/二次验证字段绝不自动填写" };
      }
      if (el.isContentEditable) el.innerText = value;
      else setNativeValue(el, value);
      return { ok: true, data: { typed_length: value.length } };
    },
  };

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "do_action") return;
    const fn = actions[msg.action];
    if (!fn) {
      sendResponse({ ok: false, error: `扩展不支持的动作：${msg.action}` });
      return;
    }
    try {
      sendResponse(fn(msg.target, msg.args || {}));
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message || e) });
    }
    // 同步返回即可
  });
})();
