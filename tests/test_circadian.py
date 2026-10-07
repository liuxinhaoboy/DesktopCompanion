# -*- coding: utf-8 -*-
"""
昼夜作息单元测试（A 功能）
==========================
运行：python tests/test_circadian.py

测试范围：
  1. brain/circadian.py：睡觉窗口展开、config 优先/矩阵兜底、活动系数、
     时段名、衰减系数
  2. brain/decide.py：活动系数真的参与概率计算
  3. soul/state.py：衰减系数生效（睡着时减半）
  4. face/pet_renderer.py：睡态三条渲染路径（矢量/立绘/睡帧）都不崩
  5. face/pet_window.py：set_sleeping 切换睡/醒、呼吸变慢、Zzz 定时器

测试原则（沿用项目惯例）：不联网、全部 tempfile、QApplication 存全局防 GC 崩溃。
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication                       # noqa: E402
from PyQt6.QtCore import Qt                                    # noqa: E402
from PyQt6.QtGui import QPainter, QPixmap                      # noqa: E402

from brain.circadian import (Circadian, hours_from_range,      # noqa: E402
                             SLEEP_DECAY_FACTOR)
from brain.decide import Decider                               # noqa: E402
from soul.state import CESM                                    # noqa: E402
from face.pet_renderer import PetRenderer                      # noqa: E402

_qapp = None


def _make_qapp():
    """QApplication 必须存全局变量，否则被 GC 回收引发 0xC0000409 原生崩溃。"""
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_circ_"))


def _make_matrix(d: Path, schedule: dict | None = None) -> tuple[Path, Path]:
    """造一个最小触发矩阵：一个必中的动作 + 可指定的作息段。"""
    matrix = {
        "actions": [{"id": "spam", "level": "L1", "enabled": True,
                     "base_prob": 0.5, "require_confirm": False,
                     "conditions": {}}],
        "情境加成": {"prob_cap": 0.9, "long_idle_boost": 1.5,
                 "high_cpu_boost": 1.3},
        "作息": schedule if schedule is not None else
        {"quiet_hours": [23, 0, 1, 2, 3, 4, 5, 6]},
        "整点报时": {"enabled": False, "prob": 0},
    }
    d.mkdir(parents=True, exist_ok=True)
    mp = d / "matrix.json"
    mp.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    sp = d / "surprise.csv"
    sp.write_text("小灵,场景,台词\n", encoding="utf-8")
    return mp, sp


_SNAP = {"mood": 80, "chaos_multiplier": 1.0}


# ======================================================================
# 1. 睡眠窗口展开
# ======================================================================

def test_hours_from_range_crosses_midnight():
    assert hours_from_range(23, 7) == {23, 0, 1, 2, 3, 4, 5, 6}
    assert hours_from_range(2, 5) == {2, 3, 4}
    assert hours_from_range(7, 23) == set(range(7, 23))


def test_hours_from_range_edge_cases():
    assert hours_from_range(3, 3) == set(), "起止相同按'不睡'处理，别变成整天睡"
    assert hours_from_range(None, 7) is None
    assert hours_from_range(23, "abc") is None
    assert hours_from_range(True, 7) is None, "布尔值也是 int，必须先挡掉"
    assert hours_from_range(25, 3) == {1, 2}, "超过 24 的小时取模"


# ======================================================================
# 2. 睡觉窗口：config 优先，矩阵兜底
# ======================================================================

def test_sleep_window_comes_from_config():
    c = Circadian({"enabled": True, "sleep_start_hour": 23, "sleep_end_hour": 7},
                  {"quiet_hours": [1, 2]})
    assert c.sleep_hours == {23, 0, 1, 2, 3, 4, 5, 6}, "config 说了算，矩阵的 1-2 点被覆盖"
    assert c.is_sleeping(23) and c.is_sleeping(3) and not c.is_sleeping(12)


def test_sleep_disabled_never_sleeps():
    c = Circadian({"enabled": False, "sleep_start_hour": 23, "sleep_end_hour": 7},
                  {"quiet_hours": [23, 0]})
    assert c.sleep_hours == set()
    assert not any(c.is_sleeping(h) for h in range(24)), "关掉作息后任何小时都不该睡"


def test_matrix_fallback_when_config_missing():
    c = Circadian(None, {"quiet_hours": [23, 0, 1]})
    assert c.sleep_hours == {23, 0, 1}
    assert c.is_sleeping(0) and not c.is_sleeping(5)


def test_bad_config_falls_back_to_matrix():
    """config 里写了但字段非法 → 不能让她从此不睡觉，退回矩阵。"""
    c = Circadian({"enabled": True, "sleep_start_hour": "晚", "sleep_end_hour": None},
                  {"quiet_hours": [22, 23]})
    assert c.sleep_hours == {22, 23}


# ======================================================================
# 3. 活动系数 / 时段 / 衰减
# ======================================================================

def test_activity_factor():
    sched = {"quiet_hours": [23], "活动系数": {"8": 0.5, "13": 0.7, "16": 1.5}}
    c = Circadian({"enabled": True, "sleep_start_hour": 23, "sleep_end_hour": 7}, sched)
    assert c.activity(8) == 0.5, "清晨她还没睡醒"
    assert c.activity(13) == 0.7, "午后犯困"
    assert c.activity(16) == 1.5
    assert c.activity(10) == 1.0, "没写的小时按 1.0"
    assert c.activity(23) == 0.0, "睡觉时恒为 0"


def test_activity_clamped_and_bad_entries_skipped():
    sched = {"活动系数": {"5": 99.0, "6": -3.0, "7": "x", "_说明": "忽略我"}}
    c = Circadian(None, sched)
    assert c.activity(5) == 2.0, "过大夹到 2.0"
    assert c.activity(6) == 0.0, "负数夹到 0"
    assert c.activity(7) == 1.0, "坏条目跳过，回默认"


def test_period_names():
    sched = {"quiet_hours": [23, 0], "早安时段": [7, 8], "晚安时段": [21, 22]}
    c = Circadian(None, sched)
    assert c.period(0) == "sleep"
    assert c.period(7) == "morning"
    assert c.period(21) == "night"
    assert c.period(13) == "day"


def test_decay_factor_halves_when_sleeping():
    c = Circadian(None, {"quiet_hours": [23, 0]})
    assert c.decay_factor(23) == SLEEP_DECAY_FACTOR
    assert c.decay_factor(12) == 1.0


# ======================================================================
# 4. 决策层：活动系数真的参与概率
# ======================================================================

def _acts_at(decider, hour: int, rounds: int = 40) -> bool:
    from brain.perceive import Perception
    random.seed(99)
    per = Perception(ts=1.0, hour=hour, active_window_title="测试",
                     active_process="test.exe",
                     is_fullscreen_video_or_game=False,
                     cpu_percent=10.0, mem_percent=50.0,
                     mouse_idle_seconds=5.0)
    for _ in range(rounds):
        if decider.tick(per, _SNAP, {"enabled": True}) is not None:
            return True
    return False


def test_activity_zero_means_silent():
    """活动系数 0 → 她完全不闹（但不等于在睡觉，那由 sleep 段管）。"""
    d = temp_dir()
    mp, sp = _make_matrix(d, {"quiet_hours": [], "活动系数": {"12": 0.0}})
    decider = Decider(mp, sp, {"enabled": False})
    assert not decider.is_sleeping(12), "系数 0 不等于睡着"
    assert not _acts_at(decider, hour=12), "活动系数 0 时不该有任何捣乱"


def test_normal_activity_still_acts():
    d = temp_dir()
    mp, sp = _make_matrix(d, {"quiet_hours": [], "活动系数": {"12": 1.0}})
    decider = Decider(mp, sp, {"enabled": False})
    assert _acts_at(decider, hour=12), "常态活动系数下她该照常闹腾"


def test_decider_exposes_circadian_queries():
    d = temp_dir()
    mp, sp = _make_matrix(d)
    decider = Decider(mp, sp, {"enabled": True,
                               "sleep_start_hour": 23, "sleep_end_hour": 7})
    assert decider.is_sleeping(2) is True, "凌晨 2 点该在睡"
    assert decider.is_sleeping(14) is False, "下午 2 点不该睡"
    assert decider.activity_at(2) == 0.0, "睡着时活动度为 0"


# ======================================================================
# 5. 灵魂层：衰减系数生效
# ======================================================================

def _mood_after(minutes: int, factor: float) -> float:
    d = temp_dir()
    c = CESM(d / "state.json", {"mood_decay_per_minute": 1.0, "time_decay_floor": 0})
    c.set_decay_factor(factor)
    c.state.mood = 80.0
    c.state.last_mood_decay_ts = time.time() - minutes * 60
    c._tick_decay()
    return c.state.mood


def test_cesm_decay_factor_halves_decay():
    awake = _mood_after(10, 1.0)
    asleep = _mood_after(10, 0.5)
    assert abs(awake - 70.0) < 0.01, f"常态衰减：期望 70，实际 {awake}"
    assert abs(asleep - 75.0) < 0.01, f"睡着衰减减半：期望 75，实际 {asleep}"


def test_cesm_decay_factor_invalid_value_ignored():
    d = temp_dir()
    c = CESM(d / "state.json", {})
    c.set_decay_factor("半夜")
    assert c.decay_factor == 1.0, "非法值按 1.0 处理，不能让她莫名其妙不衰减"


# ======================================================================
# 6. 界面层：睡态渲染
# ======================================================================

def _paint(renderer: PetRenderer, sleeping: bool) -> QPixmap:
    pix = QPixmap(220, 240)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    snap = {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}
    try:
        renderer.paint(p, pix.width(), pix.height(), 160, 1.0, False, False,
                       snap, sleeping=sleeping)
    finally:
        p.end()
    return pix


def _opaque_pixels(pix: QPixmap) -> int:
    img = pix.toImage()
    return sum(1 for y in range(0, 240, 3) for x in range(0, 220, 3)
               if img.pixelColor(x, y).alpha() > 0)


def test_renderer_sleeping_vector_shapes_do_not_crash():
    _make_qapp()
    for shape in ("cat", "dog", "slime"):
        r = PetRenderer(shape=shape)
        assert _opaque_pixels(_paint(r, False)) > 0, f"{shape} 醒着应该有画面"
        assert _opaque_pixels(_paint(r, True)) > 0, f"{shape} 睡着也不该白屏"


def test_renderer_sleeping_prefers_sleepy_frame():
    """有多帧立绘的角色，睡着时优先用 sleepy.png（小蓝那 15 张里就有）。"""
    _make_qapp()
    d = temp_dir()
    from PIL import Image
    for name, color in (("avatar.png", (255, 0, 0)),
                        ("sleepy.png", (0, 0, 255)),
                        ("happy.png", (0, 255, 0))):
        Image.new("RGBA", (32, 32), color + (255,)).save(d / name)
    r = PetRenderer(shape="cat", sprites_dir=d, avatar=d / "avatar.png")
    assert r._pick_sprite({"_sleeping": True}).toImage().pixelColor(16, 16).blue() > 200
    assert r._pick_sprite({"mood_word": "开心"}).toImage().pixelColor(16, 16).green() > 200


def test_renderer_sleeping_ignores_anger():
    """睡着了就不该还叉腰生气——睡态优先级最高（假死除外）。"""
    _make_qapp()
    d = temp_dir()
    from PIL import Image
    Image.new("RGBA", (32, 32), (0, 0, 255, 255)).save(d / "sleepy.png")
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(d / "angry.png")
    r = PetRenderer(shape="cat", sprites_dir=d)
    pix = r._pick_sprite({"_sleeping": True, "is_angry": True})
    assert pix.toImage().pixelColor(16, 16).blue() > 200


def test_renderer_frozen_beats_sleeping():
    """假死是捣乱行为，优先级高于睡觉：假死时不能画成睡着的样子。"""
    _make_qapp()
    pix = QPixmap(220, 240)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    snap = {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}
    r = PetRenderer(shape="cat")
    try:
        r.paint(p, 220, 240, 160, 1.0, False, True, snap, sleeping=True)
    finally:
        p.end()
    assert _opaque_pixels(pix) > 0


# ======================================================================
# 7. 窗口层：睡/醒切换
# ======================================================================

class _StubEngine:
    """PetWindow 构造与开场白只用到这几样，够用就行（不碰真实数据目录）。"""
    character_profile = None
    _character = {}          # 空人设：所有台词池都取不到，走通用兜底

    def snapshot(self):
        return {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}

    def greet_line(self):
        return "我回来啦~"


def test_pet_window_sleeping_toggles():
    _make_qapp()
    from face.pet_window import PetWindow
    pet = PetWindow({"character": {"size": 120}}, _StubEngine())
    assert not pet.is_sleeping()

    pet.set_sleeping(True)
    assert pet.is_sleeping()
    assert pet._zzz_timer.isActive(), "睡着后 Zzz 定时器要跑起来"

    pet.set_sleeping(False)
    assert not pet.is_sleeping()
    assert not pet._zzz_timer.isActive(), "醒来后不该再冒 Zzz"

    pet.set_sleeping(False)   # 重复设置不该出错
    assert not pet.is_sleeping()


def test_pet_window_breathing_slows_when_sleeping():
    _make_qapp()
    from face.pet_window import PetWindow
    pet = PetWindow({"character": {"size": 120}}, _StubEngine())

    pet._phase = 0.0
    pet._breathe()
    awake_step = pet._phase

    pet.set_sleeping(True)
    pet._phase = 0.0
    pet._breathe()
    asleep_step = pet._phase

    assert abs(awake_step - 0.05) < 1e-6
    assert abs(asleep_step - 0.025) < 1e-6, "睡着时呼吸应该慢一半"


def test_pet_window_greeting_by_period():
    _make_qapp()
    from face.pet_window import PetWindow
    pet = PetWindow({"character": {"size": 120}}, _StubEngine())
    assert "Zzz" in pet._greeting_for("sleep")
    assert "早上好" in pet._greeting_for("morning")
    assert "晚" in pet._greeting_for("night")
    assert pet._greeting_for("day")      # 走 engine 的通用开场白，不报错即可


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
    print("[运行 昼夜作息 测试]")
    sys.exit(_run_all())
