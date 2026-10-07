# -*- coding: utf-8 -*-
"""
ACL（自主混乱循环）单元测试
==========================
运行：python tests/test_acl.py   （或 pytest tests/test_acl.py -q）

测试策略：
  - 感知数据全部用合成的 Perception 对象，不依赖你屏幕上真开着什么
  - 决策用固定随机种子：概率型逻辑也能确定性断言
  - 行动层用"替身桌宠"记录调用，绝不真动你的剪贴板/窗口
  - 确认框整体替换成异步桩函数，单元测试不弹任何 Qt 对话框
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.perceive import Perception            # noqa: E402
from brain.decide import Decider, ActionCandidate  # noqa: E402
from brain.act import ChaosActor                 # noqa: E402
from brain import loop as loop_mod               # noqa: E402
from brain.loop import AgentLoop                 # noqa: E402
from soul.engine import SoulEngine               # noqa: E402


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_acl_"))


MATRIX = {
    "actions": [
        {"id": "spam", "level": "L1", "enabled": True, "base_prob": 0.5,
         "require_confirm": False, "conditions": {}},
        {"id": "idle_care", "level": "L1", "enabled": True, "base_prob": 0.5,
         "require_confirm": False, "conditions": {"min_idle_seconds": 1200}},
        {"id": "heavy", "level": "L3", "enabled": True, "base_prob": 0.5,
         "require_confirm": True, "conditions": {"max_mood": 45}},
    ],
    "情境加成": {"prob_cap": 0.9, "long_idle_boost": 1.5, "high_cpu_boost": 1.3},
    "作息": {"quiet_hours": [23, 0, 1, 2, 3, 4, 5, 6]},
    "整点报时": {"enabled": False, "prob": 0},
}


def make_matrix_file(d: Path) -> tuple[Path, Path]:
    d.mkdir(parents=True, exist_ok=True)
    mp = d / "matrix.json"
    mp.write_text(json.dumps(MATRIX, ensure_ascii=False), encoding="utf-8")
    sp = d / "surprise.csv"
    sp.write_text("小灵,场景,台词\n", encoding="utf-8")
    return mp, sp


def make_perception(**kw) -> Perception:
    base = dict(ts=1.0, hour=12, active_window_title="测试.txt - 记事本",
                active_process="notepad.exe", is_fullscreen_video_or_game=False,
                cpu_percent=10.0, mem_percent=50.0, mouse_idle_seconds=5.0)
    base.update(kw)
    return Perception(**base)


def make_engine(d: Path) -> SoulEngine:
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "data" / "character_core.json").write_text(
        json.dumps({"name": "测试灵", "system_prompt": "测试。"}, ensure_ascii=False),
        encoding="utf-8")
    (d / "data" / "master_profile.json").write_text("{}", encoding="utf-8")
    return SoulEngine(d, {"memory": {"use_chroma": False}, "emotion": {}})


class StubPet:
    """替身桌宠：只记录被谁摸过什么，绝不碰真窗口。"""
    def __init__(self):
        self.calls: list[tuple] = []

    def say(self, text, duration_ms=None):
        self.calls.append(("say", text))

    def start_fake_freeze(self, seconds, complain):
        self.calls.append(("freeze", seconds))

    def set_temp_opacity(self, value, seconds):
        self.calls.append(("opacity", value))

    def move_over_active_window(self):
        self.calls.append(("cover",))
        return True


# ======================================================================
# 1. 决策层
# ======================================================================

def test_fuse_returns_silent():
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp)
    per = make_perception(is_fullscreen_video_or_game=True)
    for _ in range(30):
        assert decider.tick(per, {"mood": 15, "chaos_multiplier": 7.5},
                            {"enabled": True}) is None, "全屏熔断必须完全静默"


def test_quiet_hours_silent():
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp)
    per = make_perception(hour=23)
    for _ in range(30):
        assert decider.tick(per, {"mood": 80, "chaos_multiplier": 1.0},
                            {"enabled": True}) is None, "深夜时段她应该在睡觉"


def _acts_at(decider, hour: int, rounds: int = 40) -> bool:
    """在指定小时摇 rounds 次骰子，只要命中过一次就算"她没在睡觉"。"""
    random.seed(2026)
    per = make_perception(hour=hour)
    for _ in range(rounds):
        if decider.tick(per, {"mood": 80, "chaos_multiplier": 1.0},
                        {"enabled": True}) is not None:
            return True
    return False


def test_sleep_config_overrides_matrix():
    """config 的 sleep 段是唯一入口：它能把矩阵里写死的静音时段改掉。

    矩阵 MATRIX 里 23 点是静音的，这里把 sleep 配成 1~5 点，
    于是 23 点该恢复正常（证明 config 真的在说话，不是摆设）。
    """
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp, {"enabled": True,
                               "sleep_start_hour": 1, "sleep_end_hour": 5})
    assert decider.quiet_hours == {1, 2, 3, 4}, f"实际静音时段：{decider.quiet_hours}"
    assert not _acts_at(decider, hour=3), "3 点在 sleep 段内，应该完全静默"
    assert _acts_at(decider, hour=23), "23 点不在 sleep 段内，不该再沿用矩阵的静音"


def test_sleep_disabled_means_never_silent():
    """关掉作息开关 → 任何小时都不静默（哪怕覆盖了矩阵里的 23 点）。"""
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp, {"enabled": False,
                               "sleep_start_hour": 23, "sleep_end_hour": 7})
    assert decider.quiet_hours == set()
    assert _acts_at(decider, hour=23), "作息关掉后深夜也该正常活动"


def test_sleep_range_wraps_midnight():
    """23→7 跨午夜要正确展开：0 点静默、12 点正常。"""
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp, {"enabled": True,
                               "sleep_start_hour": 23, "sleep_end_hour": 7})
    assert 0 in decider.quiet_hours and 12 not in decider.quiet_hours
    assert not _acts_at(decider, hour=0), "跨午夜的 0 点应静默"
    assert _acts_at(decider, hour=12), "白天不该被误判成睡觉"


def test_min_idle_condition():
    """idle_care 只在闲置超过 20 分钟时可能出现。"""
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp)

    random.seed(42)
    busy = make_perception(mouse_idle_seconds=5.0)
    hits_busy = set()
    for _ in range(60):
        c = decider.tick(busy, {"mood": 80, "chaos_multiplier": 1.0}, {"enabled": True})
        if c:
            hits_busy.add(c.action_id)
    assert "idle_care" not in hits_busy, "刚动过鼠标不该触发关心"

    random.seed(42)
    away = make_perception(mouse_idle_seconds=2000.0)
    got_care = False
    for _ in range(60):
        c = decider.tick(away, {"mood": 80, "chaos_multiplier": 1.0}, {"enabled": True})
        if c and c.action_id == "idle_care":
            got_care = True
            break
    assert got_care, "长时间闲置应该能触发关心（概率0.5×60次，不应全空）"


def test_mood_multiplies_probability():
    """同样的矩阵，mood=15（暴怒倍率7.5，封顶0.9）命中率必须远高于 mood=80。"""
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp)
    per = make_perception()

    random.seed(7)
    calm = sum(1 for _ in range(200)
               if decider.tick(per, {"mood": 80, "chaos_multiplier": 1.0}, {"enabled": True}))
    random.seed(7)
    angry = sum(1 for _ in range(200)
                if decider.tick(per, {"mood": 15, "chaos_multiplier": 7.5}, {"enabled": True}))
    assert angry > calm * 1.3, f"暴怒命中率({angry})应显著高于平静({calm})"


def test_level_gating():
    d = temp_dir()
    mp, sp = make_matrix_file(d)
    decider = Decider(mp, sp)
    per = make_perception()
    cfg = {"enabled": True, "enable_L1": True, "enable_L2": True, "enable_L3": False}
    random.seed(11)
    for _ in range(100):
        c = decider.tick(per, {"mood": 30, "chaos_multiplier": 5.0}, cfg)
        assert c is None or c.level != "L3", "L3 被关掉后绝不允许出现 L3 动作"


# ======================================================================
# 2. 行动层
# ======================================================================

def make_actor(d: Path, engine, pet) -> ChaosActor:
    actor = ChaosActor(engine, pet, d, {"global_cooldown_seconds": 180})
    return actor


def test_fake_freeze_calls_pet_and_logs():
    d = temp_dir()
    mp, sp = make_matrix_file(d / "cfg")
    engine = make_engine(d)
    pet = StubPet()
    actor = make_actor(d, engine, pet)

    asyncio.run(actor.perform(ActionCandidate("fake_freeze", "L2", False, {})))
    assert ("freeze", 6) in pet.calls, "假死应调用桌宠的 freeze 方法"
    log = json.loads((d / "data" / "action_log.json").read_text(encoding="utf-8"))
    assert log[-1]["action"] == "fake_freeze", "每次行动都要留日志"


def test_clipboard_rejected_path():
    """用户点拒绝：不碰剪贴板 + 记一次 chaos_rejected（她会不高兴）。"""
    d = temp_dir()
    make_matrix_file(d / "cfg")
    engine = make_engine(d)
    pet = StubPet()
    actor = make_actor(d, engine, pet)

    async def fake_confirm(title, text):
        return False
    actor._confirm = fake_confirm
    touched = []
    read = []
    actor._clipboard_set = touched.append   # 替身剪贴板
    actor._clipboard_get = lambda: read.append(1) or "旧内容"

    asyncio.run(actor.perform(ActionCandidate("clipboard_kaomoji", "L2", True, {})))
    assert touched == [], "拒绝后绝不能碰剪贴板"
    assert read == [], "拒绝后连读都不该读（读了就等于把内容带进内存）"
    assert not actor.has_clipboard_backup()
    assert engine.cesm.state.counters["chaos_rejected"] == 1


def test_clipboard_accepted_path():
    d = temp_dir()
    make_matrix_file(d / "cfg")
    engine = make_engine(d)
    pet = StubPet()
    actor = make_actor(d, engine, pet)

    async def fake_confirm(title, text):
        return True
    actor._confirm = fake_confirm
    touched = []
    actor._clipboard_set = touched.append
    actor._clipboard_get = lambda: "用户原来的重要内容"

    asyncio.run(actor.perform(ActionCandidate("clipboard_kaomoji", "L2", True, {})))
    assert len(touched) == 1 and "颜" not in touched[0], "接受后剪贴板应被替换成颜文字"
    assert engine.cesm.state.counters["chaos_success"] == 1, "捣乱成功要加情绪"

    # 红线：覆盖前必须留快照，且要能恢复回来
    assert actor.has_clipboard_backup(), "覆盖剪贴板前必须留快照"
    assert actor.restore_clipboard() is True, "有快照时恢复应成功"
    assert touched[-1] == "用户原来的重要内容", "恢复后剪贴板应是原内容"
    assert not actor.has_clipboard_backup(), "恢复后快照要清掉，避免重复恢复"
    assert actor.restore_clipboard() is False, "没有快照时恢复应失败而不是乱写"


def test_unknown_action_is_ignored():
    d = temp_dir()
    make_matrix_file(d / "cfg")
    actor = make_actor(d, make_engine(d), StubPet())
    asyncio.run(actor.perform(ActionCandidate("不存在的动作", "L1", False, {})))
    assert not (d / "data" / "action_log.json").exists()


# ======================================================================
# 3. 主循环：冷却与熔断
# ======================================================================

def make_loop(d: Path, engine, pet, per: Perception, chaos_cfg: dict) -> AgentLoop:
    mp, sp = make_matrix_file(d / "cfg")
    agent = AgentLoop(engine, pet, d, chaos_cfg, mp, sp, tick_seconds=3)
    agent.cooldown_seconds = lambda: int(chaos_cfg.get("global_cooldown_seconds", 180))

    async def fake_snapshot():
        return per
    loop_mod.snapshot = fake_snapshot
    return agent


def _run(coro):
    return asyncio.run(coro)


def test_loop_cooldown_blocks_second_action():
    d = temp_dir()
    engine = make_engine(d)
    engine.cesm.state.mood = 80.0            # 平静：冷却正常生效
    pet = StubPet()
    agent = make_loop(d, engine, pet, make_perception(),
                      {"enabled": True, "global_cooldown_seconds": 180})

    # 摇到第一次行动为止（概率0.9封顶，30次内几乎必中）
    for _ in range(30):
        _run(agent._tick_once())
        if agent.stats["acted"] >= 1:
            break
    assert agent.stats["acted"] == 1

    before = agent.stats["cooldown_skips"]
    _run(agent._tick_once())
    assert agent.stats["acted"] == 1, "冷却期内不允许第二次行动"
    assert agent.stats["cooldown_skips"] == before + 1


def test_loop_angry_ignores_cooldown():
    d = temp_dir()
    engine = make_engine(d)
    engine.cesm.state.mood = 10.0            # 暴怒：无视冷却
    pet = StubPet()
    agent = make_loop(d, engine, pet, make_perception(),
                      {"enabled": True, "global_cooldown_seconds": 180,
                       "ignore_cooldown_when_angry": True})

    for _ in range(40):
        _run(agent._tick_once())
        if agent.stats["acted"] >= 2:
            break
    assert agent.stats["acted"] >= 2, "生气状态下连续行动（气头上不讲道理）"


def test_loop_fuse_counts():
    d = temp_dir()
    engine = make_engine(d)
    pet = StubPet()
    agent = make_loop(d, engine, pet,
                      make_perception(is_fullscreen_video_or_game=True),
                      {"enabled": True})
    _run(agent._tick_once())
    _run(agent._tick_once())
    assert agent.stats["fused"] == 2 and agent.stats["acted"] == 0, \
        "熔断期间只记录次数，绝不行动"


# ======================================================================
# 运行器
# ======================================================================

def _run_all() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    if failed == 0:
        print("ALL TESTS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
