# -*- coding: utf-8 -*-
"""
schema —— 动作请求/结果的严格数据模型（P5）
==========================================
模型只能产出 ActionRequest 这一种受限结构，经 Pydantic 校验后才能往下走。
模型永远不能生成/运行代码：这里不接受任何"执行命令/脚本"类动作。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ----------------------------------------------------------------------
# 动作清单：白名单制，不在此列的动作一律拒绝
# ----------------------------------------------------------------------
class ActionKind:
    # 文件（全部锁在 workspace_root 内）
    FILE_LIST = "file_list"          # 列举目录
    FILE_PREVIEW = "file_preview"    # 预览文本文件前若干行
    FILE_MKDIR = "file_mkdir"        # 新建文件夹
    FILE_COPY = "file_copy"         # 复制（args.dest）
    FILE_MOVE = "file_move"         # 移动（args.dest）
    FILE_RENAME = "file_rename"     # 重命名（args.new_name）
    FILE_DELETE = "file_delete"     # 删除（只进回收站）
    # 进程
    PROC_START = "proc_start"       # 启动白名单程序
    PROC_KILL = "proc_kill"         # 关闭（只能关本程序启动的子进程）
    # 剪贴板
    CLIP_READ = "clip_read"         # 读取（会进模型上下文，需确认）
    CLIP_WRITE = "clip_write"       # 写入（覆盖前自动留快照）
    # 窗口（仅普通用户级窗口）
    WIN_FOCUS = "win_focus"
    WIN_MINIMIZE = "win_minimize"
    WIN_RESTORE = "win_restore"
    WIN_MOVE = "win_move"           # args.x / args.y

    ALL = frozenset({
        FILE_LIST, FILE_PREVIEW, FILE_MKDIR, FILE_COPY, FILE_MOVE,
        FILE_RENAME, FILE_DELETE, PROC_START, PROC_KILL, CLIP_READ,
        CLIP_WRITE, WIN_FOCUS, WIN_MINIMIZE, WIN_RESTORE, WIN_MOVE,
    })

    FILE_KINDS = frozenset({FILE_LIST, FILE_PREVIEW, FILE_MKDIR, FILE_COPY,
                            FILE_MOVE, FILE_RENAME, FILE_DELETE})
    PROC_KINDS = frozenset({PROC_START, PROC_KILL})
    CLIP_KINDS = frozenset({CLIP_READ, CLIP_WRITE})
    WIN_KINDS = frozenset({WIN_FOCUS, WIN_MINIMIZE, WIN_RESTORE, WIN_MOVE})


class RiskLevel:
    LOW = "low"        # 只读，基本无副作用
    MEDIUM = "medium"  # 可逆的改动
    HIGH = "high"      # 需要用户重点确认（删除/关进程）


# 动作 → 风险等级（集中在此，policy 与确认卡共用同一份事实）
RISK_OF_ACTION: dict[str, str] = {
    ActionKind.FILE_LIST: RiskLevel.LOW,
    ActionKind.FILE_PREVIEW: RiskLevel.LOW,
    # 剪贴板读取必须是 MEDIUM：读出来的内容会进模型上下文，
    # 而剪贴板里很可能是密码、验证码、私密文本。登记成 LOW 的话，
    # confirm_policy="smart" 时会被当成只读操作静默放行，
    # 与上面 ActionKind.CLIP_READ 的注释和"进模型上下文前要确认"的红线都矛盾。
    ActionKind.CLIP_READ: RiskLevel.MEDIUM,
    ActionKind.WIN_FOCUS: RiskLevel.LOW,
    ActionKind.WIN_MINIMIZE: RiskLevel.MEDIUM,
    ActionKind.WIN_RESTORE: RiskLevel.MEDIUM,
    ActionKind.WIN_MOVE: RiskLevel.MEDIUM,
    ActionKind.FILE_MKDIR: RiskLevel.MEDIUM,
    ActionKind.FILE_COPY: RiskLevel.MEDIUM,
    ActionKind.FILE_MOVE: RiskLevel.MEDIUM,
    ActionKind.FILE_RENAME: RiskLevel.MEDIUM,
    ActionKind.CLIP_WRITE: RiskLevel.MEDIUM,
    ActionKind.PROC_START: RiskLevel.MEDIUM,
    ActionKind.FILE_DELETE: RiskLevel.HIGH,
    ActionKind.PROC_KILL: RiskLevel.HIGH,
}


class PolicyError(ValueError):
    """策略校验失败：动作不被允许。message 是能直接给小白看的人话。"""


class ActionRequest(BaseModel):
    """模型规划回合产出的单个动作请求（严格白名单）。"""

    action: str = Field(..., description="动作类型，必须在 ActionKind.ALL 内")
    target: str = Field("", description="目标：路径 / 程序名 / 窗口标题；clip 类可空")
    args: dict[str, Any] = Field(default_factory=dict, description="附加参数")
    reason: str = Field("", description="小灵为什么想做这件事（展示给用户）")

    model_config = {"extra": "forbid", "frozen": True}

    @field_validator("action")
    @classmethod
    def _action_in_whitelist(cls, v: str) -> str:
        if v not in ActionKind.ALL:
            # 不回显陌生动作的完整内容，避免模型吐出的危险字符串直接进 UI
            raise ValueError("不支持的动作类型")
        return v

    @field_validator("target", "reason")
    @classmethod
    def _strip_str(cls, v: str) -> str:
        return (v or "").strip()

    @property
    def risk(self) -> str:
        return RISK_OF_ACTION.get(self.action, RiskLevel.HIGH)

    @classmethod
    def from_model_json(cls, raw: str | dict) -> "ActionRequest":
        """把模型"只返回 JSON"的规划结果解析成 ActionRequest，失败统一抛 PolicyError。"""
        import json
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, dict):
                raise ValueError("顶层必须是对象")
            return cls.model_validate(data)
        except PolicyError:
            raise
        except Exception as e:  # noqa: BLE001 —— 模型 JSON 千奇百怪，统一成一句人话
            raise PolicyError(f"模型给出的动作格式不对，已拦下：{type(e).__name__}")


class ActionResult(BaseModel):
    """执行结果，回写给小灵、同时落审计。"""

    ok: bool
    action: str
    message: str = ""                                   # 给用户/模型的人话
    data: dict[str, Any] = Field(default_factory=dict)  # 结构化结果
    reversible: bool = False                            # 是否可撤销
    rollback: dict[str, Any] = Field(default_factory=dict)  # 回滚所需信息

    model_config = {"extra": "forbid"}

    @classmethod
    def success(cls, action: str, message: str, *, data: dict | None = None,
                reversible: bool = False, rollback: dict | None = None) -> "ActionResult":
        return cls(ok=True, action=action, message=message, data=data or {},
                   reversible=reversible, rollback=rollback or {})

    @classmethod
    def fail(cls, action: str, message: str, *, data: dict | None = None) -> "ActionResult":
        return cls(ok=False, action=action, message=message, data=data or {})
