# -*- coding: utf-8 -*-
"""
clipboard —— 剪贴板预览/快照/替换/恢复（P5）
==========================================
红线：写入覆盖前**必须**先把当前内容压入快照栈，restore 可逐级恢复；
审计层不记录剪贴板正文，只记长度。测试用替身，绝不真改用户剪贴板。
"""

from __future__ import annotations

from .schema import ActionResult, ActionKind

CLIP_MAX_CHARS = 20000        # 剪贴板只处理文本，超长截断保护


class ClipboardHands:
    def __init__(self):
        self._snapshots: list[str] = []     # 覆盖前的旧内容栈

    # --- 实际读写单独包一层，便于测试替身注入 --------------------------
    @staticmethod
    def _read() -> str:
        import pyperclip
        return pyperclip.paste() or ""

    @staticmethod
    def _write(text: str) -> None:
        import pyperclip
        pyperclip.copy(text)

    # ------------------------------------------------------------------
    def read(self) -> ActionResult:
        try:
            text = self._read()
        except Exception as e:  # noqa: BLE001
            return ActionResult.fail(ActionKind.CLIP_READ, f"读不到剪贴板：{type(e).__name__}")
        if not text:
            return ActionResult.success(ActionKind.CLIP_READ, "剪贴板现在是空的。",
                                        data={"text": ""})
        shown = text[:CLIP_MAX_CHARS]
        return ActionResult.success(
            ActionKind.CLIP_READ, f"当前剪贴板内容（{len(text)} 字）：\n{shown}",
            data={"text": shown, "length": len(text)})

    def write(self, content: str) -> ActionResult:
        content = "" if content is None else str(content)
        try:
            old = self._read()
            self._snapshots.append(old)              # 覆盖前留快照
            self._write(content[:CLIP_MAX_CHARS])
            return ActionResult.success(
                ActionKind.CLIP_WRITE, f"已写入剪贴板（{len(content)} 字），原内容已留快照可恢复。",
                reversible=True, rollback={"op": "clip"})
        except Exception as e:  # noqa: BLE001
            return ActionResult.fail(ActionKind.CLIP_WRITE, f"写剪贴板失败：{type(e).__name__}")

    def restore(self) -> ActionResult:
        if not self._snapshots:
            return ActionResult.fail("undo", "没有可恢复的剪贴板快照。")
        old = self._snapshots.pop()
        try:
            self._write(old)
            return ActionResult.success("undo", "已恢复剪贴板上一个内容。")
        except Exception as e:  # noqa: BLE001
            self._snapshots.append(old)
            return ActionResult.fail("undo", f"恢复失败：{type(e).__name__}")
