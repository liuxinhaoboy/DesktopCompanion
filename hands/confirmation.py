# -*- coding: utf-8 -*-
"""
confirmation —— Qt 非阻塞确认卡片（P5）
=====================================
为什么不用 QMessageBox.exec()：它会开局部事件循环把外层 qasync 冻住，
桌宠会"真卡死"。这里和 brain/act.py 一样用 show() + 信号 + asyncio.Future。

卡片必须让小白一眼看清四件事：要做什么、动哪个目标、有什么影响、能不能撤销。
30 秒不操作自动按"取消"处理（默认拒绝更安全），并产出一个一次性、与动作
绑定的 ConfirmationToken 交给 executor。
"""

from __future__ import annotations

import asyncio

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox, QPushButton

from .policy import CONFIRM_TTL_SECONDS, ConfirmationToken
from .schema import ActionKind, ActionRequest, RiskLevel

# 动作 → 中文人话（标题用）
ACTION_LABEL = {
    ActionKind.FILE_LIST: "查看文件夹里有什么",
    ActionKind.FILE_PREVIEW: "预览文件内容",
    ActionKind.FILE_MKDIR: "新建文件夹",
    ActionKind.FILE_COPY: "复制文件/文件夹",
    ActionKind.FILE_MOVE: "移动文件/文件夹",
    ActionKind.FILE_RENAME: "重命名",
    ActionKind.FILE_DELETE: "删除（放进回收站，可还原）",
    ActionKind.PROC_START: "启动程序",
    ActionKind.PROC_KILL: "关闭程序",
    ActionKind.CLIP_READ: "读取你的剪贴板",
    ActionKind.CLIP_WRITE: "写入剪贴板（会先留快照）",
    ActionKind.WIN_FOCUS: "把窗口切到前台",
    ActionKind.WIN_MINIMIZE: "最小化窗口",
    ActionKind.WIN_RESTORE: "还原窗口",
    ActionKind.WIN_MOVE: "移动窗口位置",
}

RISK_LABEL = {
    RiskLevel.LOW: "低风险（只看不改）",
    RiskLevel.MEDIUM: "中风险（可撤销）",
    RiskLevel.HIGH: "⚠ 高风险，请仔细确认",
}

# 每个动作"能不能撤销"的说明
REVERSIBLE_HINT = {
    ActionKind.FILE_DELETE: "删除只进回收站，之后可以从回收站还原。",
    ActionKind.FILE_MOVE: "执行前会生成回滚快照，出问题可移回原位。",
    ActionKind.FILE_RENAME: "执行前会生成回滚快照，可改回原名。",
    ActionKind.FILE_COPY: "只是复制一份，不动原件。",
    ActionKind.CLIP_WRITE: "会先保存当前剪贴板内容，可一键恢复。",
    ActionKind.FILE_MKDIR: "只是新建一个空文件夹。",
    ActionKind.WIN_MINIMIZE: "只是收起窗口，不会关闭。",
    ActionKind.WIN_RESTORE: "只是把窗口重新展开。",
    ActionKind.WIN_MOVE: "只是挪一下窗口位置。",
    ActionKind.WIN_FOCUS: "只是切到前台，不改任何东西。",
    ActionKind.FILE_LIST: "只读，不改动任何文件。",
    ActionKind.FILE_PREVIEW: "只读，不改动任何文件。",
    ActionKind.CLIP_READ: "只是读取剪贴板文字用于理解，不外传。",
    ActionKind.PROC_START: "会启动一个白名单内的程序。",
    ActionKind.PROC_KILL: "将关闭指定程序，未保存的内容可能丢失。",
}


def describe_action(req: ActionRequest, short_target: str | None = None) -> str:
    """拼出卡片正文：动作 / 目标 / 风险 / 可撤销性 / 小灵的理由。"""
    label = ACTION_LABEL.get(req.action, req.action)
    target = short_target if short_target is not None else req.target
    lines = [
        f"她想做的事：{label}",
        f"目标：{target or '（无）'}",
        f"风险：{RISK_LABEL.get(req.risk, '')}",
        REVERSIBLE_HINT.get(req.action, ""),
    ]
    if req.reason:
        lines.append(f"她的理由：{req.reason}")
    lines.append("")
    lines.append("点「确认执行」才会动手；30 秒不操作会自动取消。")
    return "\n".join(x for x in lines if x is not None)


async def request_confirmation(parent, req: ActionRequest, *,
                               short_target: str | None = None,
                               ttl: float = CONFIRM_TTL_SECONDS
                               ) -> ConfirmationToken | None:
    """
    弹出非阻塞确认卡。
    返回 ConfirmationToken 表示用户同意；None 表示拒绝或超时。
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    box = QMessageBox(parent)
    box.setWindowTitle("小灵想操作你的电脑")
    box.setIcon(QMessageBox.Icon.Warning if req.risk == RiskLevel.HIGH
                else QMessageBox.Icon.Question)
    box.setText(describe_action(req, short_target))

    yes_btn: QPushButton = box.addButton("确认执行", QMessageBox.ButtonRole.AcceptRole)
    no_btn: QPushButton = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no_btn)                 # 默认焦点在取消，防误触
    box.setEscapeButton(no_btn)

    remain = {"sec": int(ttl) + 1}

    def _refresh():
        remain["sec"] -= 1
        if remain["sec"] > 0:
            yes_btn.setText(f"确认执行（{remain['sec']}s）")
        else:
            tick.stop()
            if not fut.done():
                box.defaultButton()
                box.reject()                     # 超时按取消

    tick = QTimer(box)
    tick.setInterval(1000)
    tick.timeout.connect(_refresh)
    _refresh()
    tick.start()

    def _on_clicked(btn):
        tick.stop()
        if fut.done():
            return
        agreed = (btn is yes_btn)
        fut.set_result(agreed)

    box.buttonClicked.connect(_on_clicked)

    def _on_finished(_code):
        tick.stop()
        if not fut.done():                       # 超时 reject / 关窗都算拒绝
            fut.set_result(False)

    box.finished.connect(_on_finished)
    box.show()

    agreed = await fut
    box.deleteLater()
    if agreed:
        return ConfirmationToken.issue(req, ttl=ttl)
    return None
