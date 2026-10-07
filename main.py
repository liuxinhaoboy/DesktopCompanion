# -*- coding: utf-8 -*-
"""
DesktopCompanion —— 程序入口
============================
启动顺序（为什么是这个顺序，出了问题好排查）：
  1. 装崩溃日志"黑匣子"       —— 闪退时也有 traceback
  2. 建 QApplication          —— Qt 主程序
  3. 建 qasync 事件循环       —— 让 asyncio 和 Qt 共用一根线程
  4. 建 AppController         —— 配置/灵魂/界面/ACL 统一由它持有
  5. controller.start()       —— 显示宠物、启动 ACL、开场白
  6. 进入主循环

冒烟测试支持：
  设置环境变量 DC_SMOKE_TEST=1 启动，窗口显示 6 秒后自动退出并打印 SMOKE_OK。
  这是给自动化验证用的；平时双击 run.bat 不受影响。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


def _install_crash_logger() -> None:
    """把未捕获异常写入 data/crash.log。

    为什么要有这个：曾经出现过"启动失败 exit code 1 但日志为空"的事故，
    双击 run.bat 闪退时黑框一闪而过，用户什么都看不到。
    有了崩溃日志，任何启动/运行崩溃都会留下完整 traceback，截图它就够。
    日志滚动保留最近约 100KB，避免无限膨胀。
    """
    import traceback
    from datetime import datetime

    log_dir = ROOT / "data"
    log_dir.mkdir(parents=True, exist_ok=True)

    def _dump(kind: str, exc: BaseException) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (f"\n{'=' * 60}\n[{stamp}] {kind}\n"
                 + "".join(traceback.format_exception(exc)))
        try:
            path = log_dir / "crash.log"
            old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            path.write_text((old + entry)[-100_000:], encoding="utf-8")
        except OSError:
            pass
        print(entry)

    def _excepthook(args) -> None:
        _dump("未捕获异常 threading", args.exc_value or Exception("unknown"))

    def _sys_excepthook(tp, val, tb) -> None:
        _dump("未捕获异常 main", val)

    threading.excepthook = _excepthook
    sys.excepthook = _sys_excepthook


def main() -> int:
    _install_crash_logger()          # 第一件事：先装好"黑匣子"再干别的

    app = QApplication(sys.argv)
    app.setApplicationName("DesktopCompanion")
    # ★ 必须 False：宠物窗口是 Tool 类型，不纳入"应用窗口"计数。
    # 若为 True，关闭设置/聊天面板时 Qt 误判"所有窗口已关"→ 整个程序退出，
    # 桌宠跟着消失。真正的退出入口是右键菜单"退出"（显式 QApplication.quit()）。
    app.setQuitOnLastWindowClosed(False)

    loop = qasync_loop(app)
    asyncio.set_event_loop(loop)

    # AppController 统一持有配置/灵魂/界面/ACL（配置坏了在这里就报错）
    from app.controller import AppController
    from config.store import ConfigError
    try:
        controller = AppController(ROOT)
    except ConfigError as e:
        print(str(e))
        return 2

    controller.start(app)

    # ---- 冒烟测试 ----
    smoke = os.environ.get("DC_SMOKE_TEST")
    if smoke:
        # 值写错（例如 DC_SMOKE_TEST=yes）不该让程序崩在启动阶段：
        # 这个变量本来就是给自动化用的，容错比报错重要。
        try:
            seconds = int(smoke) or 6
        except ValueError:
            seconds = 6
        def finish_smoke():
            snap = controller.engine.snapshot()
            print(f"SMOKE_OK {json.dumps(snap, ensure_ascii=False)}")
            app.quit()
        QTimer.singleShot(seconds * 1000, finish_smoke)

    with loop:
        try:
            loop.run_forever()
        finally:
            pass
    return 0


def qasync_loop(app: QApplication):
    """单独拆出 qasync 的导入：万一这包有问题，错误信息更聚焦。"""
    from qasync import QEventLoop
    return QEventLoop(app)


if __name__ == "__main__":
    sys.exit(main())
