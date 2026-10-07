# -*- coding: utf-8 -*-
"""
circadian —— 昼夜节律
把"现在几点、她该是什么状态"集中在一处回答。
配置分两层：config.json 的 sleep 段（用户级）+ chaos_trigger_matrix.json 的 作息 段（进阶调参）。
"""

from __future__ import annotations

SLEEP_DECAY_FACTOR = 0.5

PERIOD_SLEEP = "sleep"
PERIOD_MORNING = "morning"
PERIOD_DAY = "day"
PERIOD_NIGHT = "night"


def hours_from_range(start, end) -> set[int] | None:
    """把 (23, 7) 这种跨午夜时段展开成小时集合。返回 None 表示值没法用。"""
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    try:
        s = int(start) % 24
        e = int(end) % 24
    except (TypeError, ValueError):
        return None
    if s == e:
        return set()
    hours: set[int] = set()
    h = s
    while h != e:
        hours.add(h)
        h = (h + 1) % 24
    return hours


class Circadian:
    """昼夜状态机：给一个小时，回答她此刻该是什么状态。"""

    def __init__(self, sleep_cfg: dict | None = None, schedule: dict | None = None):
        self.update(sleep_cfg, schedule)

    def update(self, sleep_cfg: dict | None, schedule: dict | None) -> None:
        self._sleep_cfg = sleep_cfg or {}
        self._schedule = schedule or {}
        self.sleep_hours: set[int] = set()
        if self._sleep_cfg and self._sleep_cfg.get("enabled", True):
            hours = hours_from_range(self._sleep_cfg.get("sleep_start_hour"),
                                     self._sleep_cfg.get("sleep_end_hour"))
            if hours is not None:
                self.sleep_hours = hours
        if not self.sleep_hours and not (self._sleep_cfg and
                                         not self._sleep_cfg.get("enabled", True)):
            fallback = self._schedule.get("quiet_hours")
            if isinstance(fallback, (list, tuple, set)):
                try:
                    self.sleep_hours = {int(h) % 24 for h in fallback}
                except (TypeError, ValueError):
                    self.sleep_hours = set()
        self.activity_table = self._parse_activity(self._schedule.get("活动系数"))
        self.morning_hours = self._parse_hours(self._schedule.get("早安时段"))
        self.night_hours = self._parse_hours(self._schedule.get("晚安时段"))

    @staticmethod
    def _parse_activity(raw) -> dict[int, float]:
        out: dict[int, float] = {}
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            if str(k).startswith("_"):
                continue
            try:
                hour = int(k) % 24
                factor = float(v)
            except (TypeError, ValueError):
                continue
            out[hour] = max(0.0, min(2.0, factor))
        return out

    @staticmethod
    def _parse_hours(raw) -> set[int]:
        if not isinstance(raw, (list, tuple, set)):
            return set()
        out: set[int] = set()
        for h in raw:
            try:
                out.add(int(h) % 24)
            except (TypeError, ValueError):
                continue
        return out

    def is_sleeping(self, hour: int) -> bool:
        try:
            h = int(hour) % 24
        except (TypeError, ValueError):
            return False
        return h in self.sleep_hours

    def activity(self, hour: int) -> float:
        if self.is_sleeping(hour):
            return 0.0
        try:
            h = int(hour) % 24
        except (TypeError, ValueError):
            return 1.0
        return self.activity_table.get(h, 1.0)

    def period(self, hour: int) -> str:
        if self.is_sleeping(hour):
            return PERIOD_SLEEP
        try:
            h = int(hour) % 24
        except (TypeError, ValueError):
            return PERIOD_DAY
        if h in self.morning_hours:
            return PERIOD_MORNING
        if h in self.night_hours:
            return PERIOD_NIGHT
        return PERIOD_DAY

    def decay_factor(self, hour: int) -> float:
        return SLEEP_DECAY_FACTOR if self.is_sleeping(hour) else 1.0
