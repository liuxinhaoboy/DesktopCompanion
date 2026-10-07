# -*- coding: utf-8 -*-
"""
protocol —— 本机桥消息协议（P6）
================================
桌宠（桥，server）和浏览器扩展（client）之间只传这里定义过的消息。
白名单制：陌生 type / 陌生 action 一律拒，extra=forbid，防止扩展被篡改后乱发。

握手流程：
  1. 设置页点"生成配对码" -> 桥生成一次性 pair_code 并开始监听 127.0.0.1
  2. 扩展 options 页填入 端口 + pair_code，background 建立 WebSocket 后发 hello
  3. 桥校验 pair_code：通过则生成长期 session_token 回 hello_ack，并清掉一次性码
  4. 之后每条消息都必须带正确 session，否则立刻断开
"""

from __future__ import annotations

import secrets
import string
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 协议版本：扩展和桥不一致时拒绝握手，避免新旧版本对不上乱执行
PROTOCOL_VERSION = "1.0"
MIN_EXT_VERSION = "1.0"   # 兼容的最低扩展版本

# ----------------------------------------------------------------------
# 浏览器动作白名单：扩展 content.js 只会执行这些，别的一律不认
# ----------------------------------------------------------------------
class BrowserAction:
    READ_TEXT = "read_text"   # 读当前页正文（会进模型上下文，需确认）
    OPEN_URL = "open_url"     # 在当前/新标签打开网址
    CLICK = "click"           # 点击元素
    TYPE_TEXT = "type_text"   # 往输入框填文字（禁密码/验证码字段）
    SCROLL = "scroll"         # 滚动页面
    CLOSE_TAB = "close_tab"   # 关闭当前标签页

    ALL = frozenset({READ_TEXT, OPEN_URL, CLICK, TYPE_TEXT,
                     SCROLL, CLOSE_TAB})


class BrowserRisk:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# 动作 -> 风险等级（policy 与确认卡共用同一份事实）
RISK_OF_BROWSER_ACTION: dict[str, str] = {
    BrowserAction.SCROLL: BrowserRisk.LOW,
    BrowserAction.READ_TEXT: BrowserRisk.MEDIUM,
    BrowserAction.OPEN_URL: BrowserRisk.MEDIUM,
    BrowserAction.CLICK: BrowserRisk.MEDIUM,
    BrowserAction.TYPE_TEXT: BrowserRisk.MEDIUM,
    BrowserAction.CLOSE_TAB: BrowserRisk.HIGH,
}

# 动作 -> 中文人话（确认卡标题用）
ACTION_LABEL = {
    BrowserAction.READ_TEXT: "读取这个网页的正文",
    BrowserAction.OPEN_URL: "打开一个网址",
    BrowserAction.CLICK: "点击网页上的元素",
    BrowserAction.TYPE_TEXT: "在网页输入框里填字",
    BrowserAction.SCROLL: "滚动网页",
    BrowserAction.CLOSE_TAB: "关闭当前标签页",
}


class ProtocolError(ValueError):
    """协议校验失败。message 是能直接给用户看的人话。"""


# ----------------------------------------------------------------------
# 配对码 / 会话 token
# ----------------------------------------------------------------------
# 去掉 0/O、1/I/L 这些容易看混的字符，配对码是给人手敲的
_PAIR_ALPHABET = "".join(set(string.ascii_uppercase + string.digits)
                         - set("0O1IL"))


def generate_pair_code(length: int = 8) -> str:
    """生成一次性配对码（大写字母+数字，每 4 位一横，方便人读）。"""
    raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + 4] for i in range(0, length, 4))


def normalize_pair_code(code: str) -> str:
    """用户粘贴时可能带空格/小写/横杠，统一成大写无横杠再比较。"""
    return "".join((code or "").upper().split()).replace("-", "")


def generate_session_token() -> str:
    """配对成功后发给扩展的长期会话令牌。"""
    return secrets.token_hex(24)


def generate_cmd_id() -> str:
    """每条下发指令的唯一 id，用于把扩展回的 result 对上号。"""
    return secrets.token_hex(8)


# ----------------------------------------------------------------------
# 扩展 -> 桥 的入站消息
# ----------------------------------------------------------------------
class HelloMessage(BaseModel):
    """握手：首次拿一次性配对码换会话 token；断线重连可直接带 session。"""
    type: Literal["hello"]
    pair_code: str = ""
    session: str = ""
    ext_version: str = "1.0"

    model_config = {"extra": "forbid"}

    @field_validator("pair_code")
    @classmethod
    def _clean(cls, v: str) -> str:
        return normalize_pair_code(v)

    @model_validator(mode="after")
    def _need_one_credential(self) -> "HelloMessage":
        # 首次配对要配对码，断线重连要会话，二者必须有一个
        if not self.pair_code and not self.session:
            raise ValueError("握手必须带配对码或会话 token")
        return self

    def is_reconnect(self) -> bool:
        return bool(self.session) and not self.pair_code


class ResultMessage(BaseModel):
    """扩展执行完一条指令后回的结果。"""
    type: Literal["result"]
    session: str
    cmd_id: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""

    model_config = {"extra": "forbid"}


class PingMessage(BaseModel):
    type: Literal["ping"]
    session: str

    model_config = {"extra": "forbid"}


_INBOUND = {"hello": HelloMessage, "result": ResultMessage, "ping": PingMessage}


def parse_inbound(raw: str | dict) -> BaseModel:
    """把扩展发来的一段 JSON 解析成对应消息模型；陌生结构抛 ProtocolError。"""
    import json
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as e:
        raise ProtocolError("消息不是合法 JSON") from e
    if not isinstance(data, dict) or "type" not in data:
        raise ProtocolError("消息缺少 type 字段")
    model_cls = _INBOUND.get(data["type"])
    if model_cls is None:
        # 不回显陌生 type 的内容
        raise ProtocolError("不认识的消息类型")
    try:
        return model_cls.model_validate(data)
    except Exception as e:  # noqa: BLE001 —— 扩展可能是旧版/被改，统一成人话
        raise ProtocolError(f"消息字段不符合协议：{type(e).__name__}") from e


# ----------------------------------------------------------------------
# 桥 -> 扩展 的出站消息（直接构造 dict，序列化成 JSON 发出）
# ----------------------------------------------------------------------
def make_hello_ack(ok: bool, session: str = "", reason: str = "") -> dict:
    return {"type": "hello_ack", "ok": ok, "protocol": PROTOCOL_VERSION,
            "session": session, "reason": reason}


def make_command(session: str, cmd_id: str, action: str,
                 target: str = "", args: dict | None = None) -> dict:
    if action not in BrowserAction.ALL:
        raise ProtocolError("不支持的浏览器动作")
    return {"type": "command", "session": session, "cmd_id": cmd_id,
            "action": action, "target": target or "", "args": args or {}}


def make_pong() -> dict:
    return {"type": "pong"}
