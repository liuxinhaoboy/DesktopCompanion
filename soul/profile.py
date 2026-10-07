# -*- coding: utf-8 -*-
"""语义记忆：御主档案的独立存储。

它故意不继承 MemoryStore，也不进入 ChromaDB：
御主是谁是不可变的 Prompt 前缀，不应该因为一次相似度检索就被遗漏。
"""

from __future__ import annotations

import json
from pathlib import Path


class ProfileStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        self.load()

    def load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.data = {}
        return self.data

    def save(self, data: dict | None = None) -> None:
        if data is not None:
            self.data = dict(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def system_text(self) -> str:
        lines = []
        for key, value in self.data.items():
            if key.startswith("_") or not isinstance(value, str) or not value.strip():
                continue
            lines.append(f"{key}：{value.strip()}")
        return "\n".join(lines)
