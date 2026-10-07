# -*- coding: utf-8 -*-
"""
catalog —— 商品表加载与校验（B 商城经济）
==========================================
`catalog.json` 是**纯数据**：价格、效果数值、配饰外观全在里面，
调平衡只改这个文件，不用动代码（这是文档明确要求的）。

校验原则与 character_loader 一致：**坏条目跳过并打日志，绝不让整张表失效**。
理由同上——文件是给用户手改的，写错一个逗号不该导致商店整个消失。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

KIND_SNACK = "snack"          # 零食：消耗品，按份数存
KIND_ACCESSORY = "accessory"  # 配饰：买断，可穿可脱
KIND_LINES = "lines"          # 台词包：买断，给她加台词

VALID_KINDS = (KIND_SNACK, KIND_ACCESSORY, KIND_LINES)
VALID_SLOTS = ("head", "neck")
VALID_SHAPES = ("hat", "bow", "scarf", "glasses")


@dataclass
class ShopItem:
    """一件商品。字段按类型取值，用不到的留默认。"""
    id: str
    name: str
    kind: str
    price: int
    desc: str = ""
    # 配饰
    slot: str = ""                    # head / neck
    shape: str = ""                   # hat / bow / scarf / glasses
    color: tuple = (200, 200, 200)    # RGB
    # 零食：加成倍数（1.0 = 和一顿正餐持平，拖文件投喂就是 1.0）
    # 为什么用"倍数"而不是直接写 hunger/mood 数值：
    #   那样等于把 CESM 的加成公式抄一份到商品表里，公式一改两边就对不上。
    boost: float = 1.0
    # 台词包
    lines_scene: str = ""             # 加到哪个台词池（如 pat_lines）
    lines: list = field(default_factory=list)
    # 彩蛋商品：不在商店里显示（D 隐藏彩蛋会用到）
    hidden: bool = False

    @property
    def is_consumable(self) -> bool:
        return self.kind == KIND_SNACK


class Catalog:
    """商品表。加载时逐条校验，坏条目进 warnings 并被丢弃。"""

    def __init__(self, items: list[ShopItem] | None = None,
                 warnings: list[str] | None = None):
        self.items: list[ShopItem] = list(items or [])
        self.warnings: list[str] = list(warnings or [])
        self._by_id = {it.id: it for it in self.items}

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path) -> "Catalog":
        """从 JSON 加载。文件不存在/整份坏掉 → 返回空表（商店显示"暂无商品"）。"""
        # 只返回诊断、不在这里 print：由调用方（AppController）统一输出一次，
        # 否则同一个问题会被打印两遍，日志很吵。
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return cls([], [f"商品表读取失败（商店将显示为空）：{e}"])
        entries = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return cls([], ["商品表格式不对（应为 items 数组），商店将显示为空"])
        items: list[ShopItem] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for idx, entry in enumerate(entries):
            item, why = cls._parse(entry, seen)
            if item is None:
                warnings.append(f"第 {idx + 1} 条商品已跳过：{why}")
                continue
            seen.add(item.id)
            items.append(item)
        return cls(items, warnings)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(entry, seen: set[str]) -> tuple[ShopItem | None, str]:
        if not isinstance(entry, dict):
            return None, "不是对象"
        item_id = str(entry.get("id", "")).strip()
        if not item_id:
            return None, "缺 id"
        if item_id in seen:
            return None, f"id 重复（{item_id}）"
        name = str(entry.get("name", "")).strip()
        if not name:
            return None, f"{item_id} 缺 name"
        kind = str(entry.get("kind", "")).strip()
        if kind not in VALID_KINDS:
            return None, f"{item_id} 的 kind「{kind}」不认识"
        try:
            price = int(entry.get("price", 0))
        except (TypeError, ValueError):
            return None, f"{item_id} 的 price 不是数字"
        if price < 0:
            return None, f"{item_id} 的 price 是负数"

        item = ShopItem(id=item_id, name=name, kind=kind, price=price,
                        desc=str(entry.get("desc", "")),
                        hidden=bool(entry.get("hidden", False)))

        if kind == KIND_ACCESSORY:
            item.slot = str(entry.get("slot", "")).strip()
            item.shape = str(entry.get("shape", "")).strip()
            if item.slot not in VALID_SLOTS:
                return None, f"{item_id} 的 slot「{item.slot}」不认识"
            if item.shape not in VALID_SHAPES:
                return None, f"{item_id} 的 shape「{item.shape}」不认识"
            rgb = entry.get("color", (200, 200, 200))
            try:
                item.color = tuple(max(0, min(255, int(c))) for c in rgb)[:3]
                if len(item.color) != 3:
                    raise ValueError
            except (TypeError, ValueError):
                item.color = (200, 200, 200)
        elif kind == KIND_SNACK:
            try:
                item.boost = max(0.1, float(entry.get("boost", 1.0)))
            except (TypeError, ValueError):
                return None, f"{item_id} 的 boost 不是数字"
        elif kind == KIND_LINES:
            item.lines_scene = str(entry.get("lines_scene", "")).strip()
            lines = entry.get("lines")
            if not item.lines_scene or not isinstance(lines, list):
                return None, f"{item_id} 缺 lines_scene 或 lines"
            item.lines = [str(x) for x in lines if str(x).strip()]
            if not item.lines:
                return None, f"{item_id} 的 lines 是空的"
        return item, ""

    # ------------------------------------------------------------------
    def get(self, item_id: str) -> ShopItem | None:
        return self._by_id.get(str(item_id))

    def all(self, kind: str | None = None,
            include_hidden: bool = False) -> list[ShopItem]:
        out = []
        for it in self.items:
            if kind is not None and it.kind != kind:
                continue
            if it.hidden and not include_hidden:
                continue
            out.append(it)
        return out

    def by_slot(self, slot: str) -> list[ShopItem]:
        return [it for it in self.items
                if it.kind == KIND_ACCESSORY and it.slot == slot]

    def __len__(self) -> int:
        return len(self.items)
