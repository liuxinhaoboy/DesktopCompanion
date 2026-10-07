# -*- coding: utf-8 -*-
"""
processes —— 受控的进程启动/关闭（P5）
=====================================
红线：
  - 启动只认 PolicyGate 白名单（这里再查一次），用 shutil.which 找真实路径，
    绝不开 shell、不拼接命令字符串；
  - 关闭**只能关本程序亲手启动的子进程**（记录 PID），系统/管理员/受保护进程
    即使 PID 被误传也一律拒绝；
  - 测试时把 _popen / _psutil_process 替换成替身，绝不真起进程。
"""

from __future__ import annotations

import shutil
import subprocess

from .schema import ActionResult, ActionKind

# 系统关键进程：任何情况下都不允许关闭（双保险，哪怕误记进 spawned）
PROTECTED_PROCESSES = frozenset({
    "csrss", "wininit", "winlogon", "services", "lsass", "smss", "svchost",
    "explorer", "dwm", "system", "system idle process", "registry", "ntoskrnl",
    "taskmgr", "msmpeng", "securityhealthservice",
})


class ProcessHands:
    def __init__(self, gate):
        self.gate = gate
        # 记录本程序亲手启动的子进程 PID：kill 的唯一合法来源
        self._spawned: dict[int, str] = {}

    # ------------------------------------------------------------------
    def start(self, name: str) -> ActionResult:
        try:
            norm = self.gate.check_process_whitelist(name)   # 再过一遍白名单
        except Exception as e:  # noqa: BLE001
            return ActionResult.fail(ActionKind.PROC_START, str(e))
        if norm in PROTECTED_PROCESSES:
            return ActionResult.fail(ActionKind.PROC_START, "这是系统关键程序，不允许这样启动。")
        exe = shutil.which(norm) or shutil.which(norm + ".exe")
        if not exe:
            return ActionResult.fail(ActionKind.PROC_START,
                                     f"在系统里找不到程序「{name}」，确认名字对不对、是否已安装。")
        try:
            # shell=False：不经过 cmd，参数是列表，杜绝命令注入
            proc = self._popen([exe])
            self._spawned[proc.pid] = norm
            return ActionResult.success(
                ActionKind.PROC_START, f"已启动「{norm}」（PID {proc.pid}）。",
                data={"pid": proc.pid, "proc": norm})
        except OSError as e:
            return ActionResult.fail(ActionKind.PROC_START, f"启动失败：{e}")

    def _popen(self, argv: list[str]):
        # 单独包一层，方便测试替身；CREATE_NO_WINDOW 避免弹黑框
        creationflags = 0
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        except AttributeError:  # pragma: no cover
            pass
        return subprocess.Popen(argv, shell=False, creationflags=creationflags)

    # ------------------------------------------------------------------
    def kill(self, pid: int) -> ActionResult:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return ActionResult.fail(ActionKind.PROC_KILL, "进程号(PID)不对。")
        if pid not in self._spawned:
            return ActionResult.fail(
                ActionKind.PROC_KILL,
                "只能关闭由小灵亲手启动的程序；这个进程不是她启动的，已拒绝。")
        try:
            proc = self._psutil_process(pid)
            if proc is None:
                self._spawned.pop(pid, None)
                return ActionResult.fail(ActionKind.PROC_KILL, "程序已经不在运行了。")
            pname = self._norm(proc.name())
            if pname in PROTECTED_PROCESSES:
                return ActionResult.fail(ActionKind.PROC_KILL,
                                         "这是系统关键进程，绝对不能关，已拦下。")
            owner = self._owner(proc)
            if owner and self._current_user() and owner != self._current_user():
                # 归属别的用户（如 SYSTEM）的进程拒绝
                return ActionResult.fail(ActionKind.PROC_KILL,
                                         "该进程属于其他用户/系统，无权关闭，已拦下。")
            label = self._spawned.get(pid, pname)
            proc.terminate()                 # 温和终止，不用 kill
            self._spawned.pop(pid, None)
            return ActionResult.success(ActionKind.PROC_KILL,
                                        f"已关闭「{label}」（PID {pid}）。")
        except Exception as e:  # noqa: BLE001
            return ActionResult.fail(ActionKind.PROC_KILL, f"关闭失败：{type(e).__name__}")

    # --- 以下包一层是为了测试替身，不在测试里真动系统进程 ----------------
    @staticmethod
    def _norm(name: str) -> str:
        n = str(name or "").strip().lower()
        return n[:-4] if n.endswith(".exe") else n

    def _psutil_process(self, pid: int):
        import psutil
        try:
            return psutil.Process(pid)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _owner(proc) -> str:
        try:
            return str(proc.username() or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _current_user() -> str:
        try:
            import psutil
            return str(psutil.Process().username() or "").lower()
        except Exception:  # noqa: BLE001
            return ""
