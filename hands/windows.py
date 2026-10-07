# -*- coding: utf-8 -*-
"""
windows —— 普通用户级窗口的聚焦/最小化/还原/移动（P5）
====================================================
红线：
  - 只操作"可见、有标题、属于当前用户、非系统关键进程"的普通顶层窗口；
  - UAC 安全桌面/锁屏/管理员或其他用户窗口一律拒绝；
  - 不做关闭、不做全局键鼠注入；移动保持原宽高，只改左上角坐标。
枚举/操作 win32 API 单独包方法，方便测试替身。
"""

from __future__ import annotations

from .schema import ActionResult, ActionKind
from .processes import PROTECTED_PROCESSES


class WindowHands:
    def __init__(self):
        pass

    # ------------------------------------------------------------------
    def _enum_visible(self) -> list[tuple[int, str, int]]:
        """返回 [(hwnd, 标题, pid)]，只含可见且有标题的顶层窗口。"""
        import win32gui
        import win32process
        out: list[tuple[int, str, int]] = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title.strip():
                return True
            _t, pid = win32process.GetWindowThreadProcessId(hwnd)
            out.append((hwnd, title, pid))
            return True

        win32gui.EnumWindows(_cb, None)
        return out

    def _is_safe_window(self, hwnd: int, pid: int) -> tuple[bool, str]:
        """安全校验：非系统关键进程、归属当前用户。"""
        try:
            import psutil
            proc = psutil.Process(pid)
            name = (proc.name() or "").lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name in PROTECTED_PROCESSES:
                return False, "这是系统关键窗口，不能操作。"
            owner = str(proc.username() or "").lower()
            me = str(psutil.Process().username() or "").lower()
            if owner and me and owner != me:
                return False, "该窗口属于其他用户或系统，已拒绝。"
        except Exception:  # noqa: BLE001 —— 拿不到进程信息时保守拒绝
            return False, "无法确认窗口归属，为安全起见拒绝。"
        return True, ""

    def _find(self, title_keyword: str) -> tuple[int | None, str, list[str]]:
        """按标题模糊匹配，返回 (hwnd, 命中标题, 其它候选标题)。"""
        kw = title_keyword.strip().lower()
        if not kw:
            return None, "", []
        candidates = []
        for hwnd, title, pid in self._enum_visible():
            if kw in title.lower():
                ok, _why = self._is_safe_window(hwnd, pid)
                if ok:
                    candidates.append((hwnd, title))
        if not candidates:
            return None, "", []
        # 标题完全相等优先，否则取最短标题（通常是主窗口而非子对话框）
        candidates.sort(key=lambda ht: (ht[1].lower() != kw, len(ht[1])))
        hwnd, title = candidates[0]
        others = [t for _h, t in candidates[1:4]]
        return hwnd, title, others

    # ------------------------------------------------------------------
    def _act(self, kind: str, target: str, args: dict | None = None) -> ActionResult:
        args = args or {}
        hwnd, title, others = self._find(target)
        if hwnd is None:
            return ActionResult.fail(kind, f"没找到标题包含「{target}」的普通窗口。")
        try:
            import win32gui
            import win32con
            if kind == ActionKind.WIN_FOCUS:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return ActionResult.success(kind, f"已把「{title}」切到前台。")
            if kind == ActionKind.WIN_MINIMIZE:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                return ActionResult.success(kind, f"已最小化「{title}」（没关闭）。",
                                            reversible=True, rollback={"op": "win", "title": title})
            if kind == ActionKind.WIN_RESTORE:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                return ActionResult.success(kind, f"已还原「{title}」。")
            if kind == ActionKind.WIN_MOVE:
                x, y = int(args.get("x", 0)), int(args.get("y", 0))
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                win32gui.MoveWindow(hwnd, x, y, right - left, bottom - top, True)
                note = f"已把「{title}」移到屏幕坐标 ({x}, {y})。"
                if others:
                    note += f"（另有相似窗口：{'、'.join(others)}）"
                return ActionResult.success(kind, note,
                                            reversible=True, rollback={"op": "win", "title": title,
                                                                       "x": left, "y": top})
            return ActionResult.fail(kind, "未知窗口动作。")
        except Exception as e:  # noqa: BLE001
            return ActionResult.fail(kind, f"窗口操作失败：{type(e).__name__}（可能被系统拦截）")

    def focus(self, target, args=None):     return self._act(ActionKind.WIN_FOCUS, target, args)
    def minimize(self, target, args=None):  return self._act(ActionKind.WIN_MINIMIZE, target, args)
    def restore(self, target, args=None):   return self._act(ActionKind.WIN_RESTORE, target, args)
    def move(self, target, args=None):      return self._act(ActionKind.WIN_MOVE, target, args)
