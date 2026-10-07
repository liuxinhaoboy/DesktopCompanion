# -*- coding: utf-8 -*-
"""
protocol —— 本机桥消息协议
桌宠（桥，server）和浏览器扩展（client）之间只传这里定义过的消息。
白名单制：陌生 type / 陌生 action 一律拒，extra=forbid。
"""

from __future__ import annotations

import secrets
import string
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PROTOCOL_VERSION = "1.0"
MIN_EXT_VERSION = "1.0"


class BrowserAction:
    READ_TEXT = "read_text"
    OPEN_URL = "open_url"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SCROLL = "scroll"
    CLOSE_TAB = "close_tab"
    ALL = frozenset({READ_TEXT, OPEN_URL, CLICK, TYPE_TEXT, SCROLL, CLOSE_TAB})


class BrowserRisk:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


RISK_OF_BROWSER_ACTION: dict[str, str] = {
    BrowserAction.SCROLL: BrowserRisk.LOW,
    BrowserAction.READ_TEXT: BrowserRisk.MEDIUM,
    BrowserAction.OPEN_URL: BrowserRisk.MEDIUM,
    BrowserAction.CLICK: BrowserRisk.MEDIUM,
    BrowserAction.TYPE_TEXT: BrowserRisk.MEDIUM,
    BrowserAction.CLOSE_TAB: BrowserRisk.HIGH,
}

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


_PAIR_ALPHABET = "".join(set(string.ascii_uppercase + string.digits) - set("0O1IL"))


def generate_pair_code(length: int = 8) -> str:
    raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + 4] for i in range(0, length, 4))


def normalize_pair_code(code: str) -> str:
    return "".join((code or "").upper().split()).replace("-", "")


def generate_session_token() -> str:
    return secrets.token_hex(24)


def generate_cmd_id() -> str:
    return secrets.token_hex(8)


class HelloMessage(BaseModel):
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
        if not self.pair_code and not self.session:
            raise ValueError("握手必须带配对码或会话 token")
        return self

    def is_reconnect(self) -> bool:
        return bool(self.session) and not self.pair_code


class ResultMessage(BaseModel):
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
    import json
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as e:
        raise ProtocolError("消息不是合法 JSON") from e
    if not isinstance(data, dict) or "type" not in data:
        raise ProtocolError("消息缺少 type 字段")
    model_cls = _INBOUND.get(data["type"])
    if model_cls is None:
        raise ProtocolError("不认识的消息类型")
    try:
        return model_cls.model_validate(data)
    except Exception as e:
        raise ProtocolError(f"消息字段不符合协议：{type(e).__name__}") from e


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
