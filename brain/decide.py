# -*- coding: utf-8 -*-
"""Decide —— 决策层（ACL 的小脑）"""

from __future__ import annotations

import csv
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .perceive import Perception
from .circadian import Circadian, hours_from_range


@dataclass
class ActionCandidate:
    action_id: str
    level: str
    require_confirm: bool
    context: dict


def load_matrix(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[ACL] 触发矩阵读取失败({e})，用空矩阵")
        return {"actions": [], "情境加成": {}, "作息": {"quiet_hours": []}}


class Decider:
    def __init__(self, matrix_path: Path, surprise_path: Path,
                 sleep_cfg: dict | None = None):
        self.matrix_path = Path(matrix_path)
        self.matrix = load_matrix(self.matrix_path)
        self.surprises = self._load_surprises(surprise_path)
        self.sleep_cfg = sleep_cfg or {}
        self.circadian = Circadian(self.sleep_cfg, self.matrix.get("作息", {}))
        self._last_hour = None
        self.reload()

    def reload(self) -> None:
        self.matrix = load_matrix(self.matrix_path)
        self.circadian.update(self.sleep_cfg, self.matrix.get("作息", {}))
        boost = self.matrix.get("情境加成", {})
        self.prob_cap = float(boost.get("prob_cap", 0.9))
        self.long_idle_boost = float(boost.get("long_idle_boost", 1.5))
        self.high_cpu_boost = float(boost.get("high_cpu_boost", 1.3))
        self.quiet_hours = self._resolve_quiet_hours()
        self.chime = self.matrix.get("整点报时", {})

    def _resolve_quiet_hours(self) -> set[int]:
        return set(self.circadian.sleep_hours)

    def is_sleeping(self, hour: int) -> bool:
        return self.circadian.is_sleeping(hour)

    def activity_at(self, hour: int) -> float:
        return self.circadian.activity(hour)

    @staticmethod
    def _load_surprises(path: Path) -> list[tuple[str, str, str]]:
        try:
            with open(path, encoding="utf-8-sig") as f:
                return [(r[0], r[1], r[2]) for r in csv.reader(f) if r and r[0] != "小灵"]
        except (OSError, IndexError):
            return []

    def _match_conditions(self, cond: dict, per: Perception) -> bool:
        if "min_idle_seconds" in cond and per.mouse_idle_seconds < cond["min_idle_seconds"]:
            return False
        if "max_idle_seconds" in cond and per.mouse_idle_seconds > cond["max_idle_seconds"]:
            return False
        if "min_cpu_percent" in cond and per.cpu_percent < cond["min_cpu_percent"]:
            return False
        if cond.get("has_active_window") and not per.active_window_title:
            return False
        return True

    def tick(self, per: Perception, snap: dict, chaos_cfg: dict) -> ActionCandidate | None:
        if not chaos_cfg.get("enabled", True):
            return None
        if per.is_fullscreen_video_or_game:
            return None
        if per.hour in self.quiet_hours:
            return None

        mood = float(snap["mood"])
        multiplier = float(snap.get("chaos_multiplier", 1.0))

        if self.chime.get("enabled", True) and self._last_hour is not None \
                and per.hour != self._last_hour and random.random() < float(self.chime.get("prob", 0.5)):
            self._last_hour = per.hour
            lines = [s for s in self.surprises if s[2] == "hour_chime"]
            if lines:
                text = random.choice(lines)[1].replace("{hour}", str(per.hour))
                return ActionCandidate("hour_chime", "L1", False, {"text": text})
        self._last_hour = per.hour

        hits: list[tuple[float, dict]] = []
        for act in self.matrix.get("actions", []):
            if not act.get("enabled", True):
                continue
            level = act.get("level", "L1")
            level_key = {"L1": "enable_L1", "L2": "enable_L2", "L3": "enable_L3"}.get(level)
            if level_key and not chaos_cfg.get(level_key, True):
                continue
            cond = act.get("conditions", {})
            if mood < float(cond.get("min_mood", -1)):
                continue
            if mood > float(cond.get("max_mood", 101)):
                continue
            if not self._match_conditions(cond, per):
                continue
            prob = float(act.get("base_prob", 0)) * multiplier
            prob *= self.circadian.activity(per.hour)
            if per.mouse_idle_seconds > 1800:
                prob *= self.long_idle_boost
            if per.cpu_percent >= 80:
                prob *= self.high_cpu_boost
            prob = min(prob, self.prob_cap)
            if random.random() < prob:
                hits.append((prob, act))

        if not hits:
            return None
        hits.sort(key=lambda x: x[0], reverse=True)
        chosen = hits[0][1]
        return ActionCandidate(
            action_id=chosen["id"],
            level=chosen.get("level", "L1"),
            require_confirm=bool(chosen.get("require_confirm", False)),
            context={"perception": per},
        )
