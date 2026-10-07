# -*- coding: utf-8 -*-
"""Perceive —— 感知层（ACL 的眼睛和耳朵）"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field

import psutil


@dataclass
class Perception:
    ts: float = 0.0
    hour: int = 12
    active_window_title: str = ""
    active_process: str = ""
    is_fullscreen_video_or_game: bool = False
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    mouse_idle_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


ENTERTAINMENT_PROCESSES = {
    "cloudmusic.exe": "听歌", "qqmusic.exe": "听歌", "kugou.exe": "听歌",
    "bili.exe": "看视频", "bilibili.exe": "看视频",
    "steam.exe": "游戏平台", "steamwebhelper.exe": "游戏平台",
    "wegame.exe": "游戏平台", "mihoyo.exe": "游戏", "yuanshen.exe": "游戏",
    "starrail.exe": "游戏", "zenlesszonezero.exe": "游戏",
    "dwm.exe": "",
}

MEETING_TITLE_KEYWORDS = ("会议", "meeting", "teams", "zoom", "腾讯会议",
                          "钉钉", "演示", "投屏")


def _mouse_idle_seconds_win() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now_tick = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, (now_tick - lii.dwTime) / 1000.0)


def _active_window_info() -> tuple[str, str]:
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win is None:
            return "", ""
        title = str(win.title or "")
        process = ""
        try:
            import win32process
            hwnd = win._hWnd
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid).name().lower()
        except Exception:
            process = ""
        return title, process
    except Exception:
        return "", ""


def _is_fullscreen() -> bool:
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win is None:
            return False
        import pyautogui
        screen = pyautogui.size()
        return (win.width >= screen.width and win.height >= screen.height)
    except Exception:
        return False


def snapshot_sync() -> Perception:
    p = Perception(ts=time.time(), hour=time.localtime().tm_hour)
    p.active_window_title, p.active_process = _active_window_info()
    lower_title = p.active_window_title.lower()
    lower_proc = p.active_process
    if lower_proc in ENTERTAINMENT_PROCESSES and ENTERTAINMENT_PROCESSES[lower_proc]:
        p.notes.append(f"娱乐进程：{lower_proc}")
    if any(kw in lower_title for kw in MEETING_TITLE_KEYWORDS):
        p.notes.append("疑似会议/演示窗口")
        p.is_fullscreen_video_or_game = True
    if _is_fullscreen() and ENTERTAINMENT_PROCESSES.get(lower_proc):
        p.is_fullscreen_video_or_game = True
    try:
        p.cpu_percent = psutil.cpu_percent(interval=None)
        p.mem_percent = psutil.virtual_memory().percent
    except Exception:
        p.notes.append("CPU/内存读取失败")
    try:
        p.mouse_idle_seconds = _mouse_idle_seconds_win()
    except Exception:
        p.mouse_idle_seconds = 0.0
    return p


async def snapshot() -> Perception:
    import asyncio
    return await asyncio.to_thread(snapshot_sync)
