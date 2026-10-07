# -*- coding: utf-8 -*-
"""
planner —— 浏览器自然语言动作规划（P6 补）
========================================
和 hands/planner 同一套"独立规划回合 + 只回 JSON"思路，但产出的是浏览器动作。
先用零成本本地 gate 判断"像不像要操作浏览器"，不像就不调模型（省额度）。

模型只能从 BrowserAction 里挑；本模块不执行、不碰页面，只产出 BrowserPlan。
真正能不能做由 BrowserPolicy 判，用户在确认卡点确认后才经 bridge 下发。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .protocol import BrowserAction

# 每种动作给模型看的说明
_ACTION_SPEC = {
    BrowserAction.READ_TEXT: "读取当前网页正文，用于理解/总结；target 留空；args={}",
    BrowserAction.OPEN_URL:  "打开一个网址；target=完整 http(s) 网址；args={}",
    BrowserAction.CLICK:     "点击网页上的元素；target=元素文字；args={\"selector\":\"CSS选择器(可空)\"}",
    BrowserAction.TYPE_TEXT: "在网页输入框填字；target=输入框文字；args={\"content\":\"要输入的文字\"}",
    BrowserAction.SCROLL:    "滚动当前页面；target 留空；args={\"dir\":\"down或up\"}",
    BrowserAction.CLOSE_TAB: "关闭当前标签页；target 留空；args={}",
}


@dataclass
class BrowserPlan:
    action: str
    target: str = ""
    args: dict = field(default_factory=dict)
    reason: str = ""


# ----------------------------------------------------------------------
# 本地意图 gate（省额度）
# ----------------------------------------------------------------------
_VERBS = ("滚动", "滑", "翻页", "点击", "点一下", "输入", "打字", "填",
          "打开", "访问", "读取", "读一下", "看看", "看一下", "关闭", "关掉")
_OBJECTS = ("网页", "页面", "网址", "标签页", "链接", "网站", "当前页", "正文", "文章",
            "百度", "谷歌", "google", "淘宝", "京东", "知乎", "微博", "哔哩", "b站",
            "bilibili", "github", "youtube")
# 裸域名片段：baidu.com / b23.tv 等（打开/访问时）
_DOMAIN_RE = re.compile(r"[a-z0-9-]+\.(com|cn|net|org|io|gov|edu|tv|me|xyz)\b")
_PHRASES = ("往下滑", "往下滚", "向上滑", "滚到底", "这个网页", "这页",
            "网页讲了什么", "网页说了什么", "读一下正文", "关闭标签", "关掉标签",
            "打开 http", "访问 http")


def looks_like_browser_action(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    if any(p in t for p in _PHRASES):
        return True
    # 直接出现 http(s) 网址，且语气是打开/访问
    if re.search(r"https?://", t) and any(v in t for v in ("打开", "访问", "去", "开")):
        return True
    if _DOMAIN_RE.search(t) and any(v in t for v in ("打开", "访问", "去", "开")):
        return True
    has_verb = any(v in t for v in _VERBS)
    has_object = any(o in t for o in _OBJECTS)
    return has_verb and has_object


# ----------------------------------------------------------------------
def build_plan_system(browser_cfg: dict) -> str:
    lines = [
        "你是浏览器助手的「动作规划器」。判断用户这句话是否要你操作浏览器。",
        "只能输出一个 JSON 对象，不要输出解释/markdown：",
        '{"action": 动作名, "target": "目标", "args": {}, "reason": "一句话理由"}',
        "只是聊天/提问，或不在动作范围内，输出：{\"action\": \"none\"}",
        "允许的动作：",
    ]
    for a, spec in _ACTION_SPEC.items():
        enabled = bool(browser_cfg.get(
            {BrowserAction.READ_TEXT: "allow_read",
             BrowserAction.OPEN_URL: "allow_navigate",
             BrowserAction.CLICK: "allow_click",
             BrowserAction.TYPE_TEXT: "allow_type",
             BrowserAction.SCROLL: "allow_scroll",
             BrowserAction.CLOSE_TAB: "allow_close_tab"}[a], False))
        if enabled:
            lines.append(f"- {a}：{spec}")
    return "\n".join(lines)


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.IGNORECASE).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def parse_plan(text: str) -> BrowserPlan | None:
    obj = extract_json(text)
    if not obj or str(obj.get("action", "")).lower() == "none":
        return None
    action = str(obj.get("action", "")).lower()
    if action not in BrowserAction.ALL:
        return None
    target = str(obj.get("target", "") or "")
    args = obj.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    # 动作参数兜底/规范化
    if action == BrowserAction.SCROLL:
        d = str(args.get("dir", "down")).lower()
        args["dir"] = "up" if d in ("up", "上", "向上") else "down"
    if action == BrowserAction.TYPE_TEXT:
        content = str(args.get("content", "") or "")
        if not content and target:
            # 模型把文字误放 target 时，无法可靠区分元素与内容，宁可返回 none 重聊
            return None
        args["content"] = content
    if action == BrowserAction.OPEN_URL:
        # 裸域名补 https://
        if target and not target.startswith(("http://", "https://")):
            target = "https://" + target
    return BrowserPlan(action=action, target=target, args=args,
                       reason=str(obj.get("reason", "") or ""))


LlmCall = Callable[[str, str], Awaitable[str]]


class BrowserPlanner:
    def __init__(self, browser_cfg_getter: Callable[[], dict]):
        self._cfg_getter = browser_cfg_getter

    async def plan(self, llm_call: LlmCall, user_text: str) -> BrowserPlan | None:
        cfg = self._cfg_getter() or {}
        if not looks_like_browser_action(user_text):
            return None
        system = build_plan_system(cfg)
        try:
            raw = await llm_call(system, user_text)
        except Exception:  # noqa: BLE001
            return None
        plan = parse_plan(raw)
        # 模型选了当前能力开关没打开的动作 → 放弃（policy 也会再拦）
        if plan is not None:
            cap_key = {BrowserAction.READ_TEXT: "allow_read",
                       BrowserAction.OPEN_URL: "allow_navigate",
                       BrowserAction.CLICK: "allow_click",
                       BrowserAction.TYPE_TEXT: "allow_type",
                       BrowserAction.SCROLL: "allow_scroll",
                       BrowserAction.CLOSE_TAB: "allow_close_tab"}[plan.action]
            if not bool(cfg.get(cap_key, False)):
                return None
        return plan
