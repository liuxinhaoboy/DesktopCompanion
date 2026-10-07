# -*- coding: utf-8 -*-
"""
planner —— ToolPlanner 受限动作规划（P5）
=======================================
docode.cc 的原生 tool calling 没验证过，所以走"独立规划回合 + 只返回 JSON +
Pydantic 严格校验"的稳妥路线：把用户原话和"她到底能做哪些动作"发给模型，
要求它只回一个 JSON；解析不出来就当"她没想操作电脑"，绝不猜动作、绝不执行代码。

模型永远只能从 ACTION_SPEC 里挑动作；本模块不执行任何东西，只产出 ActionRequest。
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable

from .schema import ActionKind, ActionRequest, PolicyError

# 每种动作需要的参数（写进 prompt，让模型照着填）
ACTION_SPEC = {
    ActionKind.FILE_LIST:   ("列出工作目录里某文件夹的内容", "target=相对路径，根目录用 .", {}),
    ActionKind.FILE_PREVIEW:("预览某个文本文件", "target=相对路径", {}),
    ActionKind.FILE_MKDIR:  ("新建文件夹", "target=要创建的相对路径", {}),
    ActionKind.FILE_COPY:   ("复制文件/文件夹", "target=源相对路径", {"dest": "目标相对路径"}),
    ActionKind.FILE_MOVE:   ("移动文件/文件夹", "target=源相对路径", {"dest": "目标相对路径"}),
    ActionKind.FILE_RENAME: ("重命名", "target=相对路径", {"new_name": "只含新文件名"}),
    ActionKind.FILE_DELETE: ("删除（进回收站）", "target=相对路径", {}),
    ActionKind.PROC_START:  ("启动白名单程序", "target=程序名", {}),
    ActionKind.PROC_KILL:   ("关闭她启动的程序", "target=进程PID数字", {}),
    ActionKind.CLIP_READ:   ("读取剪贴板文本", "target 留空", {}),
    ActionKind.CLIP_WRITE:  ("写入剪贴板", "target 留空", {"content": "要写入的文字"}),
    ActionKind.WIN_FOCUS:   ("把某窗口切到前台", "target=窗口标题关键词", {}),
    ActionKind.WIN_MINIMIZE:("最小化某窗口", "target=窗口标题关键词", {}),
    ActionKind.WIN_RESTORE: ("还原某窗口", "target=窗口标题关键词", {}),
    ActionKind.WIN_MOVE:    ("移动窗口到坐标", "target=窗口标题关键词", {"x": "整数", "y": "整数"}),
}

# 能力开关 → 对应动作类别
_CAP_MAP = [
    ("allow_files", ActionKind.FILE_KINDS),
    ("allow_processes", ActionKind.PROC_KINDS),
    ("allow_clipboard", ActionKind.CLIP_KINDS),
    ("allow_windows", ActionKind.WIN_KINDS),
]


def available_actions(sandbox_cfg: dict) -> set[str]:
    """根据设置里的能力开关，算出当前允许模型选择的动作集合。"""
    cfg = sandbox_cfg or {}
    allowed: set[str] = set()
    for key, kinds in _CAP_MAP:
        if bool(cfg.get(key, key in ("allow_files", "allow_clipboard", "allow_windows"))):
            allowed |= set(kinds)
    return allowed


def build_plan_system(sandbox_cfg: dict, workspace_root: str = "") -> str:
    allowed = sorted(available_actions(sandbox_cfg))
    lines = [
        "你是桌面助手的「动作规划器」。判断用户这句话是否要你操作电脑。",
        "只能从下面允许的动作里选，且只能输出一个 JSON 对象，不要输出任何解释、markdown 或代码块外文字：",
        '{"action": 动作名, "target": "目标", "args": {}, "reason": "一句话理由"}',
        "如果用户只是聊天、提问，或想做的事不在允许动作内，输出：{\"action\": \"none\"}",
        "禁止输出运行命令、脚本、shell；文件路径一律用相对工作目录的相对路径。",
        f"工作目录：{workspace_root or '（未设置，不能做文件操作）'}",
        "允许的动作：",
    ]
    for a in allowed:
        desc, target, args = ACTION_SPEC.get(a, (a, "", {}))
        lines.append(f"- {a}：{desc}；{target}；args={json.dumps(args, ensure_ascii=False)}")
    return "\n".join(lines)


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹和前后废话）。"""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.IGNORECASE).strip()
    # 直接解析
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 退一步：截取第一个 { 到最后一个 }
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def parse_action(text: str, sandbox_cfg: dict) -> ActionRequest | None:
    """把模型文本解析成 ActionRequest；none/解析失败/不在能力范围都返回 None。"""
    obj = extract_json(text)
    if not obj:
        return None
    if str(obj.get("action", "")).lower() == "none":
        return None
    try:
        req = ActionRequest.from_model_json(obj)
    except PolicyError:
        return None
    if req.action not in available_actions(sandbox_cfg):
        return None
    return req



# ----------------------------------------------------------------------
# 本地意图预筛（省额度的关键一道闸）
# ----------------------------------------------------------------------
# 问题背景：如果每条消息都调一次规划 LLM，那么普通聊天会先烧一次规划、
# 再烧一次聊天 = 双倍调用，而绝大多数消息其实只是聊天。
# 所以先用零成本的关键词规则判断"像不像操作电脑"，只有命中明显意图
# 才调规划模型。宁可偶尔漏掉（漏掉会当聊天，用户可重试），也不浪费额度。

_ACTION_VERBS = (
    "新建", "创建", "复制", "拷贝", "移动", "重命名", "改名", "删除", "删掉",
    "清空", "打开", "启动", "关掉", "关闭", "结束", "杀掉", "最小化", "最大化",
    "还原", "切到", "切换到", "列出", "预览", "写入", "读取", "放到前台",
    "置顶", "挪到", "放到", "整理", "解压", "压缩",
)
_ACTION_OBJECTS = (
    "文件", "文件夹", "目录", "程序", "软件", "进程", "窗口", "剪贴板", "粘贴板",
    "记事本", "浏览器", "计算器", "任务管理器", "vscode", "chrome", "edge",
    "代码", "文档", "盘", "快捷方式", "app", "应用",
)
# 不含动词也几乎一定是操作的强短语
_ACTION_PHRASES = (
    "读一下剪贴板", "看看剪贴板", "复制到剪贴板", "写入剪贴板", "剪贴板里",
    "c盘", "d盘", "e盘", "列出目录", "目录里有什么", "里面有什么文件",
)


def looks_like_action(user_text: str) -> bool:
    """纯本地、零 LLM 成本的粗判：这句话像不像要操作电脑。"""
    if not user_text:
        return False
    t = user_text.lower()
    if any(p in t for p in _ACTION_PHRASES):
        return True
    has_verb = any(v in t for v in _ACTION_VERBS)
    has_object = any(o in t for o in _ACTION_OBJECTS)
    return has_verb and has_object


# llm_call 签名：async (system_prompt: str, user_text: str) -> str（返回模型纯文本）
LlmCall = Callable[[str, str], Awaitable[str]]


class ToolPlanner:
    def __init__(self, sandbox_cfg_getter: Callable[[], dict],
                 workspace_getter: Callable[[], str] = lambda: ""):
        # 用 getter 而不是快照：设置热更新后规划能力立即跟着变
        self._cfg_getter = sandbox_cfg_getter
        self._ws_getter = workspace_getter

    async def plan(self, llm_call: LlmCall, user_text: str) -> ActionRequest | None:
        cfg = self._cfg_getter()
        if not available_actions(cfg):
            return None
        # 本地粗判：不像操作就直接当聊天，绝不为此调用模型（省额度）
        if not looks_like_action(user_text):
            return None
        system = build_plan_system(cfg, self._ws_getter())
        try:
            raw = await llm_call(system, user_text)
        except Exception:  # noqa: BLE001 —— 规划回合失败不影响正常聊天
            return None
        return parse_action(raw, cfg)
