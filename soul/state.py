# -*- coding: utf-8 -*-
"""
CESM —— 中央情感状态机（Central Emotion State Machine）
======================================================
这是整个项目的心脏。设计原则（来自架构文档）：

  「禁止任何功能独立于情绪系统运行」

所以这个类的职责不是"存三个数字"，而是：
  1. 维护三个核心数值（好感/情绪/信任），并保证它们的变化规则有"人味"
  2. 对外输出"派生值"——其他模块不许自己看数值算概率，
     必须来问 CESM："我现在说话该多热情？捣乱该多频繁？"

为什么数值规则要这么讲究：
  - 好感用边际递减公式：越接近满分越难涨，防止用户刷互动把好感刷满，
    模拟真实关系中"从 60 分到 80 分需要远多于从 0 到 20 分的付出"。
  - 情绪会自然衰减：她不是永远开心的，你不理她她就会慢慢无聊。
  - 信任只能靠长期正确行为积累，一次失信扣很多：这是"承诺"的数学化。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ----------------------------------------------------------------------
# 数值事件表：定义"发生了什么事"对应"数值怎么变"
# 为什么用表而不是散落在代码里的 if-else：
#   新手想调整"摸头加多少好感"时，只需要改这张表，不用读逻辑代码。
# ----------------------------------------------------------------------

# 事件表里 base_delta 是"基础变化量"，实际变化还要经过公式调制（见下文）,
# 其中 hunger 是单独维护的饱和指数，不走一般事件流。
AFFINITY_EVENTS = {
    "chat":            1.5,   # 正常聊一次天
    "praise":          4.0,   # 被夸奖
    "feed":            3.0,   # 被喂文件/零食（默认值，实际按 recipe 细分）
    "gift":            6.0,   # 收到礼物（商城道具）
    "pat_head":        1.0,   # 摸头（双击）
    "chat_history_deleted": -3.0,  # 用户删聊天记录（她记仇）
    "neglect_long":    -1.0,  # 长时间被冷落
    "broken_promise":  -5.0,  # 她的承诺没兑现（比如说了帮忙却失败）
}

MOOD_EVENTS = {
    "chat":            6.0,   # 有人说话就是开心
    "praise":          10.0,
    "feed":            10.0,  # 吃饱了好开心
    "gift":            12.0,
    "pat_head":        5.0,
    "chaos_success":   7.0,   # 捣乱成功，坏心情一扫而空
    "chaos_rejected":  -6.0,  # 捣乱被用户拒绝/批评
    "scolded":         -12.0, # 被骂
    "high_cpu":        -0.3,  # 系统过热她也不舒服
    "neglect_long":    -4.0,
    "chat_history_deleted": -8.0,
}

HUNGER_EVENTS = {
    "meal":            35.0,  # 正餐（文档/压缩包等"有内容"的文件）
    "snack":           12.0,  # 零食（txt/md/log 等轻量文本）
    "config":          6.0,   # 配置文件（小口小口啃）
    "unknown":         4.0,   # 不认识的文件
}

TRUST_EVENTS = {
    # 信任的规则（架构文档原文）：仅通过"完成承诺""长时间未打扰""正确执行文件操作"缓慢积累
    "promise_kept":        2.0,    # 兑现了承诺
    "quiet_companion":     0.5,    # 一个安静周期（半小时）没打扰用户
    "file_op_success":     1.5,    # 正确完成了一次文件操作
    "file_op_rollback":    -2.0,   # 文件操作出错需要回滚
    "broken_promise":      -8.0,   # 失信一次，信任大跌
    "chat_history_deleted": -4.0,  # 反向驯养：频繁删记录 → 疏离
    "wrong_file_op":       -6.0,   # 动错文件
}


@dataclass
class EmotionState:
    """实际存进 state.json 的内容。加字段时注意向后兼容（旧文件缺字段用默认值补）。"""
    affinity: float = 50.0          # 好感度 0-100，初始 50（陌生但不敌对）
    mood: float = 70.0              # 情绪值 0-100，初始 70（刚见面有点新鲜）
    trust: float = 40.0             # 信任度 0-100，初始 40（还不敢让你放权）
    hunger: float = 100.0           # 饱腹度 100=饱了 / 0=饿疯了
    last_interaction: str = ""      # 最后一次交互时间 ISO 格式
    last_mood_decay_ts: float = 0.0 # 上次情绪衰减的时间戳（用于断电后补衰减）
    birth_ts: float = field(default_factory=time.time)  # 诞生时间（30天月报用）
    counters: dict = field(default_factory=lambda: {
        "total_chats": 0,
        "promise_kept": 0,
        "promise_broken": 0,
        "file_ops_ok": 0,
        "file_ops_bad": 0,
        "chaos_done": 0,
        "chaos_rejected": 0,
        "history_deleted": 0,
    })

    # ---- 商城经济（本地单机，绝不涉及真实货币/内购/联网支付）----
    # 为什么放在这里而不是另开一个文件：coins 是"她"的状态的一部分，
    # 跟好感/情绪一样要每角色独立、要跟着 state.json 一起损坏自愈，
    # 另开一个文件就会多一套持久化与隔离逻辑要维护。
    coins: int = 0                    # 她的零花钱
    earned_today: int = 0             # 今天已经赚了多少（防刷封顶用）
    last_earn_date: str = ""          # earned_today 是哪一天的（跨天要清零）
    last_first_chat_date: str = ""    # 哪天领过"每日首聊"奖励了
    streak_days: int = 0              # 连续互动天数（连续陪她有递增奖励）
    last_active_date: str = ""        # 最后一次互动是哪天
    owned: list = field(default_factory=list)     # 已拥有的非消耗品 id（配饰/台词包）
    snacks: dict = field(default_factory=dict)    # 零食库存 {物品id: 剩余份数}
    equipped: dict = field(default_factory=dict)  # 已穿戴 {槽位: 物品id}


class CESM:
    """
    情感状态机。所有方法线程安全（内部有锁），
    因为 Agent 循环、聊天、UI 定时器都会在不同线程里碰它。
    """

    def __init__(self, state_path: Path, emotion_cfg: dict):
        self._path = Path(state_path)
        self._lock = threading.RLock()
        self._cfg = emotion_cfg or {}

        # 从 config 读调节旋钮（都有默认值，config 缺字段也不崩）
        self.mood_decay_per_min = float(self._cfg.get("mood_decay_per_minute", 0.5))
        self.affinity_power = float(self._cfg.get("affinity_diminish_power", 0.5))
        self.low_mood = float(self._cfg.get("low_mood_threshold", 30))
        self.angry_mood = float(self._cfg.get("angry_mood_threshold", 20))
        self.trust_gate = float(self._cfg.get("trust_gate_for_danger_ops", 30))
        # 纯时间衰减能触及的最低情绪。低于这条线只能由"事件"（被骂等）造成。
        # 为什么要有下限：没有它，放一个晚上 mood 就归零，她每天早上都从
        # 暴怒开始、捣乱倍率常驻 7.5——"生气"就贬值了，用户也会被烦死。
        self.time_decay_floor = float(self._cfg.get("time_decay_floor", 20.0))
        # 衰减速度的外部系数（昼夜作息用：睡着时减半）。默认 1.0 = 不变。
        # 为什么放在这里而不是让调用方算：衰减是本模块的职责，
        # 让外部直接改数值会绕过 _tick_decay 的补算逻辑。
        self.decay_factor = 1.0
        # 事件监听器（商城经济靠它统计"陪伴行为"赚币）。
        # 为什么用监听而不是让各模块自己去记：CESM 是唯一事件入口，
        # 挂在别处迟早漏掉某条路径（比如以后新增的互动方式）。
        self._listeners: list = []

        self.state = self._load_or_create()

    def add_event_listener(self, fn) -> None:
        """注册事件监听。回调签名为 fn(event: str, delta: dict)。"""
        with self._lock:
            self._listeners.append(fn)

    def _notify(self, event: str, delta: dict) -> None:
        """通知监听器。**必须在锁外调用**，且监听器抛异常绝不能影响情绪系统。"""
        for fn in list(self._listeners):
            try:
                fn(event, delta)
            except Exception as e:  # noqa: BLE001
                print(f"[CESM] 事件监听器出错（已忽略）：{type(e).__name__}: {e}")

    def save(self) -> None:
        """公开的落盘入口：经济系统改完 coins/owned 之类后自己调。"""
        with self._lock:
            self._save()

    def set_decay_factor(self, factor: float) -> None:
        """设置衰减速度系数（1.0 = 常态，0.5 = 减半）。非法值一律按 1.0 处理。"""
        try:
            f = float(factor)
        except (TypeError, ValueError):
            f = 1.0
        self.decay_factor = max(0.0, min(1.0, f))

    # ------------------------------------------------------------------
    # 持久化。为什么用"写临时文件再替换"而不是直接写：
    #   直接写一半时断电/崩溃会留下半个 JSON，下次启动就再也读不回来了。
    #   先写临时文件再原子替换，最坏情况只是丢这一次变化，文件结构永远完整。
    # ------------------------------------------------------------------
    def _load_or_create(self) -> EmotionState:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                # asdict 反向构造：旧版本文件缺的字段自动用默认值补上
                base = EmotionState()
                data = asdict(base)
                for k in data:
                    if k in raw:
                        data[k] = raw[k]
                st = EmotionState(**data)
                self._catch_up_decay(st)
                return st
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # 状态文件坏了不致命：备份后重新开始，角色"失忆"但能活
                backup = self._path.with_suffix(".broken.bak")
                try:
                    self._path.replace(backup)
                except OSError:
                    pass
                print(f"[CESM] state.json 损坏({e})，已备份到 {backup.name}，重新开始")
        fresh = EmotionState(last_mood_decay_ts=time.time(),
                             last_interaction=datetime.now().isoformat())
        if self._path.exists() is False and self._path.parent.exists():
            # 损坏恢复路径：立刻重写一份合法文件，不等下次事件——
            # 否则用户此刻关机，磁盘上留下的还是坏文件，下次还得再自愈一次
            try:
                import os as _os
                self.state = fresh
                self._save()
            except OSError:
                pass
        return fresh

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(self.state), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)  # Windows 上 os.replace 是原子替换
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 数值变化的核心公式
    # ------------------------------------------------------------------
    def _clamp(self, v: float) -> float:
        return max(0.0, min(100.0, v))

    def _affinity_delta(self, base_delta: float) -> float:
        """
        边际递减公式：delta = base * (1 - affinity/100) ^ power
        - 好感 0 时，加满全额（趁热打铁效果好）
        - 好感 90 时，只加 32% 左右（高好感来之不易）
        - 负向变化不衰减：伤害就是伤害，90 好感被骂照样疼
        """
        cur = self.state.affinity
        if base_delta > 0:
            factor = (1.0 - cur / 100.0) ** self.affinity_power
            return base_delta * factor
        return base_delta

    def _mood_recovery(self, base_delta: float) -> float:
        """
        Sigmoid 回升曲线：把 base_delta 映射到实际情绪增量。
        效果：情绪很低落时，一次互动的提振效果最明显（安慰效应）；
              情绪已经很高时，同样的互动只带来少量提升（防止轻易满分）。
        公式：delta = base * sigmoid_scale(mood)，其中
              sigmoid_scale(m) = 1 / (1 + exp((m-50)/25)) 在低段趋近 1，高段趋近 0.27
        """
        m = self.state.mood
        scale = 1.0 / (1.0 + math.exp((m - 50.0) / 25.0))
        # 归一化：让 scale 在 0-100 区间的均值约为 0.5，乘 2 让整体力度合适
        return base_delta * (0.5 + scale)

    def apply_event(self, event: str, strength: float = 1.0,
                    extra_affinity: float = 0.0) -> dict:
        """
        对外的统一入口：发生了事件 → 数值变化 → 存盘 → 返回变化明细。
        strength 是强度系数（比如同一句夸奖，第一次 1.0，连着夸第十次 0.4）。
        返回 {'affinity': 实际变化, 'mood': ..., 'trust': ...} 方便 UI 弹"+2 好感"。
        """
        with self._lock:
            self._tick_decay()

            aff_base = AFFINITY_EVENTS.get(event, 0.0) * strength + extra_affinity
            mood_base = MOOD_EVENTS.get(event, 0.0) * strength
            trust_base = TRUST_EVENTS.get(event, 0.0)

            d_aff = self._affinity_delta(aff_base)
            d_mood = self._mood_recovery(mood_base)
            d_trust = trust_base  # 信任线性：积累慢、破坏快，本来就不该被曲线放大

            before = (self.state.affinity, self.state.mood, self.state.trust)
            self.state.affinity = self._clamp(self.state.affinity + d_aff)
            self.state.mood = self._clamp(self.state.mood + d_mood)
            self.state.trust = self._clamp(self.state.trust + d_trust)
            self.state.last_interaction = datetime.now().isoformat()

            if event == "chat":
                self.state.counters["total_chats"] += 1
            for key in ("promise_kept", "broken_promise", "file_op_success",
                        "chaos_success", "chaos_rejected", "chat_history_deleted"):
                if event == key:
                    self.state.counters[key] = self.state.counters.get(key, 0) + 1

            self._save()
            result = {
                "affinity": self.state.affinity - before[0],
                "mood": self.state.mood - before[1],
                "trust": self.state.trust - before[2],
            }
        # 锁外通知：监听器（经济系统）可能回头读 CESM 的派生值，
        # 在锁内调用会把两个锁套在一起，迟早死锁。
        self._notify(event, result)
        return result

    # ------------------------------------------------------------------
    # 情绪自然衰减
    # ------------------------------------------------------------------
    def feed(self, kind: str, description: str, boost: float = 1.0) -> dict:
        """
        喂食接口：kind 是 recipe_map 里的键（meal/snack/config/unknown），
        description 是文件名，给日志和台词用。
        boost 是加成倍数（商城买的零食比随手拖个文件香），1.0 = 普通投喂。
        返回 {hunger: 变化, mood: 变化, affinity: 变化, full: 是否已吃撑}。
        为什么单独做成方法而不是直接用 apply_event：
        喂文件前要查当前饥饿度，吃撑了就不能再喂——这不是简单加减
        而是"够了/不够"/"吃饱了"三种状态的判定，集中在这里写清楚一次。
        """
        with self._lock:
            self._tick_decay()
            try:
                boost = max(0.1, float(boost))
            except (TypeError, ValueError):
                boost = 1.0
            base = HUNGER_EVENTS.get(kind, HUNGER_EVENTS["unknown"]) * boost

            if self.state.hunger >= 95:
                # 吃撑了：投喂被婉拒，好感小涨（心意领了），饥饿不再加
                self.state.mood = self._clamp(self.state.mood + 2.0)
                self.state.affinity = self._clamp(self.state.affinity + 0.4)
                self._save()
                result = {
                    "hunger": 0.0,
                    "mood": 2.0,
                    "affinity": 0.4,
                    "full": True,
                    "comment": f"已经吃得饱饱的，{description} 只好先放着了。",
                }
            else:
                # 正常吃
                new_hunger = self._clamp(self.state.hunger + base)
                hunger_gain = new_hunger - self.state.hunger
                self.state.hunger = new_hunger

                # 吃比自己小但更用心的输入，好感收益边际递减
                # 小零食重复喂降速（防止狂丢文件刷满好感）
                strength = max(0.3, 1.0 - self.state.counters.get("total_feeds", 0) * 0.05)
                aff_delta = self._affinity_delta(3.0 * boost * strength)
                mood_delta = self._mood_recovery(
                    8.0 * boost * (0.5 if kind == "unknown" else 1.0))

                self.state.affinity = self._clamp(self.state.affinity + aff_delta)
                self.state.mood = self._clamp(self.state.mood + mood_delta)
                self.state.counters["total_feeds"] = \
                    self.state.counters.get("total_feeds", 0) + 1
                self._save()

                kind_name = {"meal": "正餐", "snack": "小零食",
                             "config": "配置文件", "unknown": "（不明）"}.get(kind, "食物")
                result = {
                    "hunger": hunger_gain,
                    "mood": mood_delta,
                    "affinity": aff_delta,
                    "full": False,
                    "comment": f"{description} 被当作 {kind_name} 一口吃掉了~",
                }
        # 锁外通知（经济系统靠这个记"喂食 +2 币"）
        self._notify("feed", result)
        return result

    # ------------------------------------------------------------------
    # 情绪自然衰减
    # ------------------------------------------------------------------
    def _tick_decay(self) -> None:
        """按经过的真实时间补衰减（每分钟 mood_decay_per_min 点）。
        为什么按真实时间补而不是定时器触发：关机期间她也在"独自无聊"，
        重新启动时把这段日子的衰减一次性结清，情绪才是连续的。"""
        now = time.time()
        last = self.state.last_mood_decay_ts or now
        minutes = max(0.0, (now - last) / 60.0)
        if minutes < 0.5:  # 半分钟内的碎碎调用不值得写盘
            return
        # 昼夜作息：睡着时整体衰减减半（她也在休息，不该照样饿、照样无聊）
        decay = self.mood_decay_per_min * minutes * self.decay_factor
        # 情绪：无聊最多衰减到 time_decay_floor；若已低于下限（被事件伤到），
        # 时间不再继续往深渊推——只有新的伤害事件才能更低。
        floor = min(self.time_decay_floor, self.state.mood)
        self.state.mood = self._clamp(max(self.state.mood - decay, floor))
        # 好感：比情绪慢两个数量级的"气候"级变化。一晚上没人理 ≈ 掉 4~5 点，
        # 而不是之前的 19 点（那会让人觉得"睡一觉就被讨厌了"）。
        aff_decay = self.mood_decay_per_min * minutes * 0.015 * self.decay_factor
        self.state.affinity = self._clamp(self.state.affinity - aff_decay)
        # 饥饿值独立衰减：比情绪略快（模拟生理），但同样有下限——
        # 不会饿到负数，也不会因为放着不理就扣感情，只会影响"她现在很饿"的状态。
        hunger_decay = (minutes * self._cfg.get("hunger_decay_per_minute", 0.3)
                        * self.decay_factor)
        self.state.hunger = self._clamp(self.state.hunger - hunger_decay)
        self.state.last_mood_decay_ts = now

    def _catch_up_decay(self, st: EmotionState) -> None:
        """启动时对读入的历史状态补一次衰减。"""
        self.state = st
        self._tick_decay()

    # ------------------------------------------------------------------
    # 派生值：其他模块做决策只能问这里，不许自己拿数值算
    # ------------------------------------------------------------------
    @property
    def temperature(self) -> float:
        """情绪 → 说话的"活泼度"。低落时 0.6 冷淡克制，兴奋时 1.1 跳脱。"""
        m = self.state.mood
        return round(0.6 + (m / 100.0) * 0.5, 3)

    @property
    def reply_max_tokens(self) -> int:
        """情绪 → 回复长度。生气时惜字如金（模拟闹别扭），开心话多。"""
        m = self.state.mood
        if m < self.angry_mood:
            return 120   # 生气：短句
        if m < self.low_mood:
            return 250   # 低落：不太想说话
        return 500       # 正常/开心：正常交流

    @property
    def chaos_multiplier(self) -> float:
        """情绪 → 捣乱概率倍率（非线性）。
        mood>=50 时是 1.0（按触发矩阵原概率）；
        mood 在 20~50 之间平滑升到 5 倍；mood<20 直接 7.5 倍（生气无视常理）。
        这就是架构文档里"mood<20 时遮挡窗口从 2% 升到 15%"的通用实现。"""
        m = self.state.mood
        if m < self.angry_mood:
            return 7.5
        if m < 50:
            t = (50.0 - m) / 30.0  # 0~1
            return 1.0 + t * 4.0
        return 1.0

    @property
    def is_sleepy(self) -> bool:
        return self.state.mood < 25

    @property
    def is_angry(self) -> bool:
        return self.state.mood < self.angry_mood

    @property
    def allows_danger_ops(self) -> bool:
        """信任闸门：<30 时拒绝一切高危操作（删文件/关进程）。
        这是"信任度"存在的意义：不信任你，就不让你放权。"""
        return self.state.trust >= self.trust_gate

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """给 UI / Prompt 用的完整快照。"""
        with self._lock:
            self._tick_decay()
            s = self.state
            mood_word = self._mood_word(s.mood)
            return {
                "affinity": round(s.affinity, 1),
                "mood": round(s.mood, 1),
                "trust": round(s.trust, 1),
                "hunger": round(s.hunger, 1),
                "hunger_word": self._hunger_word(s.hunger),
                "mood_word": mood_word,
                "is_angry": self.is_angry,
                "is_hungry": self.is_hungry,
                "allows_danger_ops": self.allows_danger_ops,
                "chaos_multiplier": round(self.chaos_multiplier, 2),
                "last_interaction": s.last_interaction,
                "counters": dict(s.counters),
            }

    @property
    def is_hungry(self) -> bool:
        return self.state.hunger < 40

    @staticmethod
    def _hunger_word(h: float) -> str:
        if h >= 80: return "饱饱的"
        if h >= 50: return "还行"
        if h >= 40: return "有点饿"
        if h >= 20: return "饿了"
        return "饿扁了"

    def flush(self) -> None:
        """程序退出前调用：结清期间的衰减并落盘。
        没有它，退出前一段时间的情绪变化只存在于内存里，关机即失忆。"""
        with self._lock:
            self._tick_decay()
            self._save()

    @staticmethod
    def _mood_word(m: float) -> str:
        if m >= 80: return "兴奋"
        if m >= 60: return "开心"
        if m >= 40: return "平静"
        if m >= 30: return "低落"
        if m >= 20: return "难过"
        return "生气"


# ----------------------------------------------------------------------
# 使用示例（可直接 python soul/state.py 运行体验）
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile as _tf
    tmpdir = _tf.mkdtemp()
    cesm = CESM(Path(tmpdir) / "state.json",
                {"mood_decay_per_minute": 0.5})
    print("初始状态:", cesm.snapshot())
    print("被夸一次 →", cesm.apply_event("praise"))
    print("再夸九次 →", [cesm.apply_event("praise")["affinity"] for _ in range(9)])
    print("骂一次   →", cesm.apply_event("scolded"))
    print("最终状态:", cesm.snapshot())
    print("当前 temperature =", cesm.temperature,
          "| reply_max_tokens =", cesm.reply_max_tokens,
          "| chaos_multiplier =", cesm.chaos_multiplier)
