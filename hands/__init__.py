# -*- coding: utf-8 -*-
"""
hands —— 中央"电脑控制"受限执行器（P5）
======================================
铁律：模型、ACL 捣乱循环、浏览器、UI 都**不得绕过本包**直接操作系统。

固定链路：
    聊天意图 → ToolPlanner（只产出受限 JSON）→ schema 严格校验
    → Policy 校验（工作目录/白名单/能力开关/风险分级）
    → 非阻塞确认卡（30 秒自动失效）
    → Executor 执行前**二次**重新校验路径/PID/窗口
    → 脱敏审计 → ActionResult 回写给小灵

模块：
    schema.py       ActionRequest / ActionResult（Pydantic 严格模型）
    policy.py       风险分级、沙箱路径、白名单、确认令牌过期
    confirmation.py Qt 非阻塞确认卡片（show+Future，绝不用 exec()）
    files.py        工作目录内的列举/预览/新建/复制/移动/重命名/回收站删除
    processes.py    白名单启动、只杀本程序启动的子进程
    clipboard.py    剪贴板预览/快照/替换/恢复
    windows.py      普通用户窗口的聚焦/最小化/恢复/移动
    audit.py        脱敏 action_log
    executor.py     唯一执行入口
"""

from .schema import ActionRequest, ActionResult, ActionKind, RiskLevel  # noqa: F401
