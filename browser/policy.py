# -*- coding: utf-8 -*-
"""
policy —— 浏览器动作安全策略 + 非阻塞确认卡（P6）
==============================================
两道闸：
  1. BrowserPolicy.check：能力开关 + URL 黑名单 + 敏感元素，硬拒绝的直接不往下走；
  2. request_browser_confirmation：能做的动作也要用户在确认卡点"确认"才下发。

默认禁止（交接包 01/05 红线）：
  chrome:// edge:// about: 扩展页 文件页、扩展商店、密码/验证码/2FA 字段、
  支付/银行页、最终提交表单、自动上传、下载后执行。
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox, QPushButton

from .protocol import (ACTION_LABEL, BrowserAction, BrowserRisk,
                       RISK_OF_BROWSER_ACTION, ProtocolError)

# ----------------------------------------------------------------------
# URL 黑名单
# ----------------------------------------------------------------------
# 这些 scheme 是浏览器内部页，扩展本来就注入不进去，明确拒绝并给人话提示
_FORBIDDEN_SCHEMES = ("chrome", "edge", "about", "chrome-extension",
                      "devtools", "view-source", "file", "moz-extension",
                      "javascript", "data", "vbscript", "blob")

# 扩展商店：不允许桌宠在商店页乱点（防止诱导卸载/装恶意扩展）
_FORBIDDEN_HOST_KEYWORDS = (
    "chromewebstore.google.com",
    "chrome.google.com/webstore",
    "microsoftedge.microsoft.com",
    "addons.mozilla.org",
)

# 支付/银行/资金关键词：命中域名或路径就拒绝任何写操作，读正文也不允许
_PAYMENT_KEYWORDS = (
    "alipay", "wechatpay", "wxpay", "tenpay", "unionpay", "pay.", "/pay",
    "bank", "ebank", "网银", "支付", "付款", "转账", "收银台", "wallet",
    "trade/confirm", "order/confirm", "checkout",
)


def is_forbidden_url(url: str) -> tuple[bool, str]:
    """判断网址是否在禁止清单。返回 (是否禁止, 人话原因)。"""
    if not url:
        return False, ""
    raw = url.strip()
    low = raw.lower()
    try:
        parsed = urlparse(raw)
        # 裸域名（无 scheme）再按 http 解析，好取 host；伪协议保留原 scheme
        if not parsed.scheme:
            parsed = urlparse("http://" + raw)
    except ValueError:
        return True, "网址格式无法识别"
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    if scheme in _FORBIDDEN_SCHEMES:
        return True, f"浏览器内部页（{scheme}://）不允许操作"
    # 注意用 host+path 匹配而不是只用 host：清单里 "chrome.google.com/webstore"
    # 是带路径的，而 hostname 永远不含 "/"，只比 host 的话这条规则永不命中，
    # 等于扩展商店页实际没被拦住。
    haystack = host + path
    for bad in _FORBIDDEN_HOST_KEYWORDS:
        if bad in haystack:
            return True, "浏览器扩展商店页不允许操作"
    for kw in _PAYMENT_KEYWORDS:
        if kw in haystack:
            return True, "这是支付/银行相关页面，为了资金安全一律不自动操作"
    return False, ""


# ----------------------------------------------------------------------
# 敏感元素：密码 / 验证码 / 2FA / 文件上传 / 最终提交
# ----------------------------------------------------------------------
_SENSITIVE_FIELD_KEYWORDS = (
    "password", "passwd", "pwd", "验证码", "captcha", "verifycode",
    "verify_code", "otp", "2fa", "totp", "sms_code", "安全码", "cvv",
)
# 最终提交类按钮的文案/语义：点击=钱或订单落定，绝不自动点
_SUBMIT_KEYWORDS = (
    "提交订单", "确认付款", "立即支付", "确认支付", "付款", "支付", "转账",
    "确认下单", "立即购买", "submit", "place order", "pay now", "confirm pay",
)
_FILE_UPLOAD_HINTS = ("type=file", "file upload", "上传文件", "选择文件")


def is_sensitive_element(action: str, target: str,
                         args: dict | None = None) -> tuple[bool, str]:
    """检查要操作的元素是不是密码/验证码/最终提交/上传这类禁区。"""
    args = args or {}
    blob = f"{target or ''} {args.get('selector', '')} {args.get('label', '')}".lower()

    if action == BrowserAction.TYPE_TEXT:
        for kw in _SENSITIVE_FIELD_KEYWORDS:
            if kw in blob:
                return True, "密码/验证码/二次验证字段绝不自动填写"
        if args.get("is_password"):
            return True, "密码字段绝不自动填写"

    if action == BrowserAction.CLICK:
        for kw in _SUBMIT_KEYWORDS:
            if kw in blob:
                return True, "这是最终提交/付款按钮，绝不自动点击，请你自己手动确认"
        for kw in _FILE_UPLOAD_HINTS:
            if kw in blob:
                return True, "文件上传不允许自动操作"
    return False, ""


class BrowserPolicy:
    """集中判断一个浏览器动作"允不允许"。能力开关来自 config.browser。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        # 能力开关：默认全关，用户在设置里逐项打开（最小权限）
        self.allow_read = bool(cfg.get("allow_read", False))
        self.allow_navigate = bool(cfg.get("allow_navigate", False))
        self.allow_click = bool(cfg.get("allow_click", False))
        self.allow_type = bool(cfg.get("allow_type", False))
        self.allow_scroll = bool(cfg.get("allow_scroll", False))
        self.allow_close_tab = bool(cfg.get("allow_close_tab", False))

    def update(self, cfg: dict | None) -> None:
        self.__init__(cfg or {})

    def _capability_on(self, action: str) -> bool:
        return {
            BrowserAction.READ_TEXT: self.allow_read,
            BrowserAction.OPEN_URL: self.allow_navigate,
            BrowserAction.CLICK: self.allow_click,
            BrowserAction.TYPE_TEXT: self.allow_type,
            BrowserAction.SCROLL: self.allow_scroll,
            BrowserAction.CLOSE_TAB: self.allow_close_tab,
        }.get(action, False)

    def check(self, action: str, target: str = "", args: dict | None = None,
              page_url: str = "") -> tuple[bool, str]:
        """综合校验。返回 (允许, 拒绝原因)。允许时原因为空。"""
        if action not in BrowserAction.ALL:
            return False, "不支持的浏览器动作"
        if not self._capability_on(action):
            return False, "这个浏览器能力还没在设置里打开"
        # 注意：is_forbidden_url 返回的是非空 tuple（恒 truthy），
        # 不能用 `a or b` 短路，必须分别查当前页和目标网址。
        bad, why = is_forbidden_url(page_url)
        if not bad:
            bad, why = is_forbidden_url(target)
        if bad:
            return False, why
        bad, why = is_sensitive_element(action, target, args)
        if bad:
            return False, why
        # open_url 只允许 http/https；裸域名放行（扩展侧补协议），
        # javascript:/data: 等伪协议在上面已被 is_forbidden_url 拦下
        if action == BrowserAction.OPEN_URL:
            scheme = urlparse(target).scheme.lower()
            if scheme and scheme not in ("http", "https"):
                return False, "只允许打开普通网页（http/https）"
        return True, ""

    @staticmethod
    def risk_of(action: str) -> str:
        return RISK_OF_BROWSER_ACTION.get(action, BrowserRisk.HIGH)


# ----------------------------------------------------------------------
# 非阻塞确认卡（和 hands/confirmation 同一套 show + Future 模式，不冻 qasync）
# ----------------------------------------------------------------------
CONFIRM_TTL_SECONDS = 30


async def request_browser_confirmation(parent, action: str, target: str,
                                       reason: str = "", *,
                                       page_url: str = "",
                                       ttl: float = CONFIRM_TTL_SECONDS):
    """弹浏览器动作确认卡。返回 True=用户同意，False/超时=拒绝。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    risk = RISK_OF_BROWSER_ACTION.get(action, BrowserRisk.HIGH)
    label = ACTION_LABEL.get(action, action)
    risk_word = {BrowserRisk.LOW: "低风险（只看不改）",
                 BrowserRisk.MEDIUM: "中风险",
                 BrowserRisk.HIGH: "⚠ 高风险，请仔细确认"}.get(risk, "")
    lines = [f"她想在浏览器里：{label}",
             f"目标：{target or '（当前页面）'}"]
    if page_url:
        host = urlparse(page_url if "://" in page_url else "http://" + page_url).hostname or page_url
        lines.append(f"网站：{host}")
    lines.append(f"风险：{risk_word}")
    if reason:
        lines.append(f"她的理由：{reason}")
    lines.append("")
    lines.append("点「确认执行」才会操作浏览器；30 秒不操作自动取消。")

    box = QMessageBox(parent)
    box.setWindowTitle("小灵想操作你的浏览器")
    box.setIcon(QMessageBox.Icon.Warning if risk == BrowserRisk.HIGH
                else QMessageBox.Icon.Question)
    box.setText("\n".join(lines))
    yes_btn: QPushButton = box.addButton("确认执行", QMessageBox.ButtonRole.AcceptRole)
    no_btn: QPushButton = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no_btn)
    box.setEscapeButton(no_btn)

    remain = {"sec": int(ttl) + 1}

    def _refresh():
        remain["sec"] -= 1
        if remain["sec"] > 0:
            yes_btn.setText(f"确认执行（{remain['sec']}s）")
        else:
            tick.stop()
            if not fut.done():
                box.reject()

    tick = QTimer(box)
    tick.setInterval(1000)
    tick.timeout.connect(_refresh)
    _refresh()
    tick.start()

    def _on_clicked(btn):
        tick.stop()
        if not fut.done():
            fut.set_result(btn is yes_btn)

    box.buttonClicked.connect(_on_clicked)

    def _on_finished(_code):
        tick.stop()
        if not fut.done():
            fut.set_result(False)

    box.finished.connect(_on_finished)
    box.show()

    agreed = await fut
    box.deleteLater()
    return bool(agreed)
