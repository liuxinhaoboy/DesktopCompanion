# -*- coding: utf-8 -*-
"""
policy —— 执行前的策略闸门（P5）
==============================
负责：
  1. 能力开关（设置页四个 allow_*）
  2. 文件操作锁死在 workspace_root 内，挡住路径穿越(..\\)和符号链接逃逸
  3. 进程只能启动/关闭白名单内、且由本程序启动的子进程
  4. 确认策略（每次都问 / 仅高危问）
  5. 确认令牌：30 秒过期、一次性、且与具体动作绑定（确认 A 不能拿去执行 B）

注意：这里**不真正执行**任何动作，只做"允不允许"的判断；executor 在真正
动手前还会再调一次本闸门做二次校验，防止"确认后、执行前"状态被掉包。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .schema import ActionKind, ActionRequest, PolicyError, RiskLevel

CONFIRM_TTL_SECONDS = 30.0


class PolicyGate:
    def __init__(self, sandbox_cfg: dict | None = None):
        cfg = sandbox_cfg or {}
        self.allow_files = bool(cfg.get("allow_files", True))
        self.allow_processes = bool(cfg.get("allow_processes", False))
        self.allow_clipboard = bool(cfg.get("allow_clipboard", True))
        self.allow_windows = bool(cfg.get("allow_windows", True))
        self.confirm_policy = str(cfg.get("confirm_policy", "always"))
        # 白名单统一成小写、去空格、去 .exe 后缀，匹配时同样归一化
        self.process_whitelist = {
            self._norm_proc(n) for n in cfg.get("process_whitelist", []) if str(n).strip()
        }
        ws = str(cfg.get("workspace_root", "") or "").strip()
        self.workspace_root: Path | None = Path(ws).resolve() if ws else None

    def update(self, sandbox_cfg: dict | None) -> None:
        """设置热更新后重建闸门状态。"""
        self.__init__(sandbox_cfg)

    # ------------------------------------------------------------------
    # 进程名归一化：notepad / notepad.exe / " Notepad " 视为同一个
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_proc(name: str) -> str:
        n = str(name).strip().lower()
        if n.endswith(".exe"):
            n = n[:-4]
        return n

    # ------------------------------------------------------------------
    # 沙箱路径：把任意输入路径变成"保证在工作目录内"的绝对真实路径
    # ------------------------------------------------------------------
    def resolve_inside(self, raw: str, *, must_exist: bool = False) -> Path:
        """
        返回解析后的绝对路径；任何越界（..\\ 穿越、符号链接指向外部、
        绝对路径指向别处、盘符跳转）都抛 PolicyError。
        """
        if self.workspace_root is None:
            raise PolicyError("还没在「设置→电脑控制」里指定工作目录，文件操作已被拦下。")
        raw = (raw or "").strip()
        if not raw:
            raise PolicyError("没有给出要操作的路径。")
        # Windows 下正斜杠/反斜杠都认；相对路径相对工作目录
        p = Path(raw)
        if not p.is_absolute():
            p = self.workspace_root / p
        # resolve() 会把 ..、符号链接全部解析成真实绝对路径——这是防逃逸的关键
        real = p.resolve()
        root = self.workspace_root
        if not self._is_within(real, root):
            raise PolicyError(
                f"路径「{raw}」跑到工作目录外面去了，为安全起见已拒绝。"
                f"（我只能动「{root}」里面的东西）")
        if must_exist and not real.exists():
            raise PolicyError(f"「{self.rel(real)}」不存在。")
        return real

    @staticmethod
    def _is_within(child: Path, root: Path) -> bool:
        try:
            return child.is_relative_to(root)
        except AttributeError:  # pragma: no cover —— 老 Python 兜底
            try:
                child.relative_to(root)
                return True
            except ValueError:
                return False

    def rel(self, real: Path) -> str:
        """给用户看的短路径（相对工作目录），失败就退回文件名。"""
        try:
            return str(real.relative_to(self.workspace_root)) if self.workspace_root \
                else real.name
        except ValueError:
            return real.name

    # ------------------------------------------------------------------
    # 能力开关
    # ------------------------------------------------------------------
    def check_capability(self, req: ActionRequest) -> None:
        a = req.action
        if a in ActionKind.FILE_KINDS and not self.allow_files:
            raise PolicyError("文件操作在设置里是关闭的，去「设置→电脑控制」打开后再试。")
        if a in ActionKind.PROC_KINDS and not self.allow_processes:
            raise PolicyError("进程操作在设置里是关闭的（这是最敏感的能力，默认关闭）。")
        if a in ActionKind.CLIP_KINDS and not self.allow_clipboard:
            raise PolicyError("剪贴板操作在设置里是关闭的。")
        if a in ActionKind.WIN_KINDS and not self.allow_windows:
            raise PolicyError("窗口操作在设置里是关闭的。")

    def check_process_whitelist(self, name: str) -> str:
        """返回归一化后的程序名；不在白名单则拒绝。"""
        norm = self._norm_proc(name)
        if not norm:
            raise PolicyError("没说要启动哪个程序。")
        if norm not in self.process_whitelist:
            raise PolicyError(
                f"程序「{name}」不在你设定的白名单里，已拒绝。"
                f"需要的话先去设置里把它加进白名单。")
        return norm

    # ------------------------------------------------------------------
    # 综合校验：确认前 / 执行前都会调
    # ------------------------------------------------------------------
    def validate(self, req: ActionRequest) -> dict:
        """
        校验一个动作请求，返回执行时要用的"已验证上下文"：
          - file 动作：{"real": Path, "dest_real": Path|None}
          - proc_start：{"proc": 归一化名}
          - 其它：{}
        """
        self.check_capability(req)
        ctx: dict = {}
        a = req.action

        if a in ActionKind.FILE_KINDS:
            # 删除/移动/复制/重命名/预览/列举要求源存在；新建不要求
            must_exist = a != ActionKind.FILE_MKDIR
            ctx["real"] = self.resolve_inside(req.target, must_exist=must_exist)
            if a in (ActionKind.FILE_COPY, ActionKind.FILE_MOVE):
                dest = str(req.args.get("dest", ""))
                if not dest.strip():
                    raise PolicyError("复制/移动需要给出目标位置 dest。")
                ctx["dest_real"] = self.resolve_inside(dest, must_exist=False)
            if a == ActionKind.FILE_RENAME:
                new_name = str(req.args.get("new_name", "")).strip()
                if not new_name:
                    raise PolicyError("重命名需要给出新名字 new_name。")
                # 新名字只能是单个文件名，不许带路径分隔符往外跳
                if Path(new_name).name != new_name or new_name in (".", "..") \
                        or any(ch in new_name for ch in '\\/'):
                    raise PolicyError("新名字只能是文件名，不能带路径。")
                ctx["new_name"] = new_name
        elif a == ActionKind.PROC_START:
            ctx["proc"] = self.check_process_whitelist(req.target)
        return ctx

    # ------------------------------------------------------------------
    # 确认策略
    # ------------------------------------------------------------------
    def needs_confirm(self, req: ActionRequest) -> bool:
        """是否需要弹确认卡。smart 策略下放行纯只读 LOW，其余都问。"""
        if self.confirm_policy == "smart":
            return req.risk != RiskLevel.LOW
        return True  # always：逐次确认（默认，最稳）


# ----------------------------------------------------------------------
# 确认令牌：把"用户对某张卡片点了同意"变成一个可校验、会过期、一次性的凭证
# ----------------------------------------------------------------------
@dataclass
class ConfirmationToken:
    token: str
    action: str
    target: str
    args_fingerprint: str
    created_at: float = field(default_factory=time.time)
    ttl: float = CONFIRM_TTL_SECONDS
    consumed: bool = False

    @classmethod
    def issue(cls, req: ActionRequest, ttl: float = CONFIRM_TTL_SECONDS) -> "ConfirmationToken":
        import json
        return cls(
            token=uuid.uuid4().hex,
            action=req.action,
            target=req.target,
            args_fingerprint=json.dumps(req.args, ensure_ascii=False,
                                        sort_keys=True, default=str),
            ttl=ttl,
        )

    def matches(self, req: ActionRequest) -> bool:
        import json
        fp = json.dumps(req.args, ensure_ascii=False, sort_keys=True, default=str)
        return (not self.consumed and self.action == req.action
                and self.target == req.target and self.args_fingerprint == fp)

    def is_fresh(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at <= self.ttl

    def consume(self) -> None:
        self.consumed = True
