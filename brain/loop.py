# -*- coding: utf-8 -*-
"""AgentLoop —— ACL 的心跳"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .perceive import snapshot
from .decide import Decider, ActionCandidate
from .act import ChaosActor


class AgentLoop:
    def __init__(self, engine, pet, root, chaos_cfg: dict,
                 matrix_path, surprise_path, tick_seconds: int = 3,
                 sleep_cfg: dict | None = None):
        self.root = Path(root)
        self.decider = Decider(Path(matrix_path), Path(surprise_path), sleep_cfg)
        self.actor = ChaosActor(engine, pet, self.root, chaos_cfg)
        self.chaos_cfg = chaos_cfg
        self.tick_seconds = tick_seconds
        self.engine = engine
        self.pet = pet
        self._last_action_ts = 0.0
        self._running = False
        self.stats = {"ticks": 0, "fused": 0, "acted": 0, "cooldown_skips": 0}

    def _cooldown_ok(self, snap: dict) -> tuple[bool, str]:
        remaining = self.cooldown_seconds() - (time.time() - self._last_action_ts)
        if remaining <= 0:
            return True, ""
        if snap.get("is_angry") and self.chaos_cfg.get("ignore_cooldown_when_angry", True):
            return True, "angry_override"
        return False, f"冷却中，还剩{remaining:.0f}秒"

    def cooldown_seconds(self) -> int:
        return int(self.chaos_cfg.get("global_cooldown_seconds", 180))

    async def run(self) -> None:
        self._running = True
        print(f"[ACL] 自主循环启动，tick={self.tick_seconds}s，冷却={self.cooldown_seconds()}s")
        while self._running:
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ACL] tick 异常(已忽略，继续): {e}")
            await asyncio.sleep(self.tick_seconds)
        print("[ACL] 自主循环停止")

    def stop(self) -> None:
        self._running = False

    async def _tick_once(self) -> None:
        self.stats["ticks"] += 1
        per = await snapshot()
        snap = self.engine.snapshot()
        if per.is_fullscreen_video_or_game:
            self.stats["fused"] += 1
            return
        ok, _reason = self._cooldown_ok(snap)
        if not ok:
            self.stats["cooldown_skips"] += 1
            return
        cand: ActionCandidate | None = self.decider.tick(per, snap, self.chaos_cfg)
        if cand is None:
            return
        await self.actor.perform(cand)
        self._last_action_ts = time.time()
        self.stats["acted"] += 1
