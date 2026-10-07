# -*- coding: utf-8 -*-
"""
executor —— 电脑控制的唯一执行入口（P5）
=======================================
模型、ACL、浏览器、UI 要动电脑，**只能**走这里。execute 内部分四步：
  1. PolicyGate.validate 二次校验（确认后、执行前再验一次，防掉包）；
  2. 校验确认令牌（需要确认时：必须匹配动作、未过期、一次性）；
  3. 分发到 files/processes/clipboard/windows；
  4. 脱敏审计落盘。
request_and_execute 是给界面用的一站式封装：校验→弹非阻塞确认卡→执行。
"""

from __future__ import annotations

import time
from pathlib import Path

from .audit import AuditLogger
from .clipboard import ClipboardHands
from .confirmation import request_confirmation
from .files import FileHands
from .policy import PolicyGate
from .processes import ProcessHands
from .schema import ActionKind, ActionRequest, ActionResult
from .windows import WindowHands


class HandsExecutor:
    def __init__(self, root: Path | str, sandbox_cfg: dict | None = None, parent=None):
        self.root = Path(root)
        self.parent = parent
        self.gate = PolicyGate(sandbox_cfg)
        self.files = FileHands(self.gate)
        self.processes = ProcessHands(self.gate)
        self.clipboard = ClipboardHands()
        self.windows = WindowHands()
        self.audit = AuditLogger(self.root)

    def update_config(self, sandbox_cfg: dict | None) -> None:
        self.gate.update(sandbox_cfg)

    # ------------------------------------------------------------------
    def _short_target(self, req: ActionRequest, ctx: dict) -> str | None:
        real = ctx.get("real")
        if real is not None:
            return self.gate.rel(real)
        if req.action == ActionKind.PROC_START:
            return ctx.get("proc", req.target)
        return None

    # ------------------------------------------------------------------
    def execute(self, req: ActionRequest, token=None) -> ActionResult:
        """真正执行。需要确认的动作必须带有效 token。"""
        # 1) 执行前二次校验（不信任"确认那一刻"的状态）
        try:
            ctx = self.gate.validate(req)
        except Exception as e:  # noqa: BLE001 —— PolicyError 等统一拦下
            res = ActionResult.fail(req.action, str(e))
            self.audit.record(req, False, res.message, stage="blocked")
            return res

        # 2) 确认令牌
        if self.gate.needs_confirm(req):
            invalid = self._check_token(req, token)
            if invalid:
                self.audit.record(req, False, invalid, stage="blocked",
                                  short_target=self._short_target(req, ctx))
                return ActionResult.fail(req.action, invalid)
            token.consume()

        short = self._short_target(req, ctx)
        # 3) 分发
        try:
            res = self._dispatch(req, ctx)
        except Exception as e:  # noqa: BLE001 —— 任何意外都不能炸到聊天主流程
            res = ActionResult.fail(req.action, f"执行时出错：{type(e).__name__}")

        # 4) 审计（脱敏在 AuditLogger 内部）
        self.audit.record(req, res.ok, res.message, short_target=short,
                          stage="executed" if res.ok else "failed")
        return res

    @staticmethod
    def _check_token(req: ActionRequest, token) -> str:
        """返回非空字符串表示拒绝原因；空串表示通过。"""
        if token is None:
            return "这个操作需要你点确认卡片同意后才能做。"
        if getattr(token, "consumed", False):
            return "这次确认已经用过了，安全起见请重新确认一次。"
        if not token.matches(req):
            return "确认信息和要执行的动作对不上，已拦下（请重新确认）。"
        if not token.is_fresh():
            return "确认已经超过 30 秒失效了，出于安全请重新确认一次。"
        return ""

    # ------------------------------------------------------------------
    def _dispatch(self, req: ActionRequest, ctx: dict) -> ActionResult:
        a = req.action
        if a == ActionKind.FILE_LIST:
            return self.files.list_dir(ctx["real"])
        if a == ActionKind.FILE_PREVIEW:
            return self.files.preview(ctx["real"])
        if a == ActionKind.FILE_MKDIR:
            return self.files.mkdir(ctx["real"])
        if a == ActionKind.FILE_COPY:
            return self.files.copy(ctx["real"], ctx["dest_real"])
        if a == ActionKind.FILE_MOVE:
            return self.files.move(ctx["real"], ctx["dest_real"])
        if a == ActionKind.FILE_RENAME:
            return self.files.rename(ctx["real"], ctx["new_name"])
        if a == ActionKind.FILE_DELETE:
            return self.files.delete(ctx["real"])
        if a == ActionKind.PROC_START:
            return self.processes.start(ctx["proc"])
        if a == ActionKind.PROC_KILL:
            return self.processes.kill(req.target)
        if a == ActionKind.CLIP_READ:
            return self.clipboard.read()
        if a == ActionKind.CLIP_WRITE:
            return self.clipboard.write(str(req.args.get("content", "")))
        if a in (ActionKind.WIN_FOCUS, ActionKind.WIN_MINIMIZE,
                 ActionKind.WIN_RESTORE, ActionKind.WIN_MOVE):
            fn = {ActionKind.WIN_FOCUS: self.windows.focus,
                  ActionKind.WIN_MINIMIZE: self.windows.minimize,
                  ActionKind.WIN_RESTORE: self.windows.restore,
                  ActionKind.WIN_MOVE: self.windows.move}[a]
            return fn(req.target, req.args)
        return ActionResult.fail(a, "该动作还不支持。")

    # ------------------------------------------------------------------
    async def request_and_execute(self, req: ActionRequest, parent=None) -> ActionResult:
        """界面一站式入口：先过策略，再弹非阻塞确认卡，确认后执行。"""
        try:
            ctx = self.gate.validate(req)
        except Exception as e:  # noqa: BLE001
            res = ActionResult.fail(req.action, str(e))
            self.audit.record(req, False, res.message, stage="blocked")
            return res

        token = None
        if self.gate.needs_confirm(req):
            short = self._short_target(req, ctx)
            token = await request_confirmation(parent or self.parent, req,
                                               short_target=short)
            if token is None:
                self.audit.record(req, False, "用户取消/超时", stage="declined",
                                  short_target=self._short_target(req, ctx))
                return ActionResult.fail(req.action, "好的，没操作（你取消了或超时）。")
        return self.execute(req, token)

    def undo_last_clipboard(self) -> ActionResult:
        return self.clipboard.restore()
