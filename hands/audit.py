# -*- coding: utf-8 -*-
"""
audit —— 脱敏操作审计（P5）
==========================
每次经 executor 执行（或被拦下）的动作都落一条到 data/hands_audit.json，
用户之后能回看"小灵对我的电脑做过什么"。

隐私红线：
  - 剪贴板**绝不记内容**，只记字符长度；
  - 文件只记相对工作目录的短路径；
  - 任何字段超长都截断；不记录密钥、不记录文件正文。
落盘沿用项目铁律：临时文件 + os.replace 原子替换。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .schema import ActionKind, ActionRequest

MAX_FIELD = 200          # 单字段最长字符
MAX_ENTRIES = 200        # 只留最近 200 条，避免无限膨胀


def _clip(text: str) -> str:
    text = str(text or "")
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + "…"


class AuditLogger:
    def __init__(self, root: Path | str, max_entries: int = MAX_ENTRIES):
        self.log_path = Path(root) / "data" / "hands_audit.json"
        self.max_entries = max_entries

    # ------------------------------------------------------------------
    def _redact_target(self, req: ActionRequest, short_target: str | None) -> str:
        """目标脱敏：剪贴板不记内容，其余用短路径并截断。"""
        if req.action in (ActionKind.CLIP_READ, ActionKind.CLIP_WRITE):
            return "<剪贴板>"
        t = short_target if short_target is not None else req.target
        return _clip(t)

    def record(self, req: ActionRequest, ok: bool, message: str, *,
               short_target: str | None = None, extra: dict | None = None,
               stage: str = "executed") -> None:
        """
        stage: executed=已执行 / blocked=被策略拦下 / declined=用户拒绝 / failed=执行失败
        """
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": req.action,
            "target": self._redact_target(req, short_target),
            "risk": req.risk,
            "stage": stage,
            "ok": bool(ok),
            "message": _clip(message),
        }
        # 剪贴板只额外记长度，不记内容
        if req.action == ActionKind.CLIP_WRITE:
            entry["clip_chars"] = len(str(req.args.get("content", "") or ""))
        if extra:
            entry["extra"] = {k: _clip(str(v)) for k, v in dict(extra).items()}
        self._append(entry)

    # ------------------------------------------------------------------
    def _append(self, entry: dict) -> None:
        try:
            log = self.recent()
            log = log[-(self.max_entries - 1):] + [entry]
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.log_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(log, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.log_path)          # 原子替换
        except OSError as e:                          # 审计失败不影响主流程
            print(f"[hands] 审计日志写失败(不影响使用): {e}")

    def recent(self, limit: int | None = None) -> list[dict]:
        try:
            if not self.log_path.exists():
                return []
            data = json.loads(self.log_path.read_text(encoding="utf-8"))
            data = data if isinstance(data, list) else []
            return data[-limit:] if limit else data
        except (OSError, json.JSONDecodeError):
            return []
