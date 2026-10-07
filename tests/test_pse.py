# -*- coding: utf-8 -*-
"""
PSE（持久化灵魂引擎）单元测试
============================
运行方式（在项目根目录）：
    python tests/test_pse.py          ← 直接跑，全绿输出 ALL TESTS PASSED
    python -m pytest tests/test_pse.py -q   ← 装了 pytest 也可以这样跑

测试范围：
    1. CESM 情感公式（边际递减 / Sigmoid 回升 / 自然衰减 / 信任闸门 / 捣乱倍率）
    2. MemoryStore（存取 / 情绪标签过滤 / JSON 备胎降级 / 摘要生成）
    3. PersonaChecker（禁语命中 / 正常放行 / 纠正提示生成）
    4. ProfileStore（语义记忆置顶 / 下划线说明字段跳过）
    5. SoulEngine 集成（记忆进 Prompt / OOC 纠正只追加一次）

为什么每个测试都用临时目录：测试绝不许污染真实的 data/state.json，
否则跑一次测试，你和她的感情就被清零了。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from soul.state import CESM, EmotionState          # noqa: E402
from soul.memory import MemoryStore, LocalHashEmbedding  # noqa: E402
from soul.persona_check import PersonaChecker      # noqa: E402
from soul.profile import ProfileStore              # noqa: E402
from soul.engine import SoulEngine                 # noqa: E402


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_test_"))


# ======================================================================
# 1. CESM 情感公式
# ======================================================================

def test_affinity_diminishing_returns():
    """边际递减：同样的夸奖，好感越高涨得越少。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.affinity = 10.0
    low = cesm.apply_event("praise")["affinity"]
    cesm.state.affinity = 90.0
    high = cesm.apply_event("praise")["affinity"]
    assert low > high > 0, f"低好感的增量({low})应当大于高好感({high})且都为正"


def test_affinity_negative_not_diminished():
    """负向事件不受边际递减保护：伤害就是伤害。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.affinity = 90.0
    drop = cesm.apply_event("chat_history_deleted")["affinity"]
    assert drop <= -3.0, f"高好感被伤害应接近全额扣减，实际 {drop}"


def test_mood_recovery_sigmoid():
    """Sigmoid 回升：情绪越低，同样互动的提升越大。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.mood = 15.0
    low_gain = cesm.apply_event("praise")["mood"]
    cesm.state.mood = 85.0
    high_gain = cesm.apply_event("praise")["mood"]
    assert low_gain > high_gain, f"低落时安慰({low_gain})应比开心时({high_gain})更有效"


def test_mood_natural_decay_and_catchup():
    """关机期间的衰减要被补上：把上次衰减时间拨回 10 分钟前。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.mood = 60.0
    cesm.state.last_mood_decay_ts = time.time() - 600   # 假装过了 10 分钟
    cesm._tick_decay()
    assert cesm.state.mood < 60.0 - 4.0, "10 分钟应衰减约 5 点情绪"


def test_time_decay_has_floor():
    """纯时间衰减最多把情绪拖到下限（默认 20），不能自己发酵成暴怒。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.mood = 80.0
    cesm.state.last_mood_decay_ts = time.time() - 3 * 86400   # 假装三天没人理
    cesm._tick_decay()
    assert abs(cesm.state.mood - 20.0) < 0.1, f"隔三天也只应无聊到 20，实际 {cesm.state.mood}"


def test_below_floor_not_pushed_deeper_by_time():
    """被事件打到下限以下时，时间流逝不再继续压低情绪。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.mood = 8.0                                  # 被骂到 8
    cesm.state.last_mood_decay_ts = time.time() - 3600      # 过了一小时
    cesm._tick_decay()
    assert cesm.state.mood == 8.0, "时间衰减不应把已受伤的情绪压得更低"


def test_affinity_slow_overnight_decay():
    """好感是"气候"：放一整晚（10 小时）最多掉 5 点左右。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.affinity = 60.0
    cesm.state.last_mood_decay_ts = time.time() - 600 * 60   # 10 小时
    cesm._tick_decay()
    dropped = 60.0 - cesm.state.affinity
    assert 3.0 <= dropped <= 6.0, f"隔夜好感流失应在 3~6 点，实际 {dropped:.1f}"


# ======================================================================
# 饥饿值系统（喂文件功能的数据基础）
# ======================================================================

def test_old_state_file_gets_default_hunger():
    """旧版 state.json 没有 hunger 字段：加载后自动补 100（饱），不许崩。"""
    path = temp_dir() / "state.json"
    path.write_text(json.dumps({
        "affinity": 66.0, "mood": 55.0, "trust": 40.0,
        "last_interaction": "2026-01-01T00:00:00",
        "last_mood_decay_ts": time.time(),   # 刚衰减过，避免 catch_up 干扰断言
        "birth_ts": time.time(),
        "counters": {},
    }, ensure_ascii=False), encoding="utf-8")
    cesm = CESM(path, {})
    assert cesm.state.hunger == 100.0, "旧档案应默认饱腹 100"


def test_hunger_decays_with_time():
    """饥饿按分钟衰减（默认 0.3/分钟），30 分钟约掉 9 点。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.hunger = 100.0
    cesm.state.last_mood_decay_ts = time.time() - 1800   # 30 分钟
    cesm._tick_decay()
    assert 89.0 <= cesm.state.hunger <= 92.0, f"30分钟应掉约9点，实际 {cesm.state.hunger}"


def test_hunger_meal_types():
    """四种餐型分值：正餐35 / 零食12 / 配置6 / 不明物4。"""
    cases = [("meal", 35.0), ("snack", 12.0), ("config", 6.0), ("unknown", 4.0)]
    for kind, expected in cases:
        cesm = CESM(temp_dir() / "state.json", {})
        cesm.state.hunger = 50.0
        cesm.state.counters["total_feeds"] = 0      # 清零避免递减干扰
        cesm.feed(kind, f"test.{kind}")
        got = cesm.state.hunger - 50.0
        assert abs(got - expected) < 0.01, f"{kind} 应+{expected}，实际+{got}"


def test_hunger_full_refuses_more():
    """饱腹 ≥95 时吃撑婉拒：hunger 不再增加，但心意领了（mood 小涨）。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.hunger = 96.0
    mood_before = cesm.state.mood
    delta = cesm.feed("meal", "big.pdf")
    assert delta["full"] is True
    assert cesm.state.hunger == 96.0, "吃撑后 hunger 不应变化"
    assert cesm.state.mood > mood_before, "拒绝投喂但心意领了，mood 应小涨"


def test_hunger_repeat_feeding_diminishes_affinity():
    """连续投喂好感收益递减（防狂丢文件刷满）：第 10 次明显低于第 1 次。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.hunger = 50.0            # 先变饿：否则初始100会走"吃撑婉拒"分支
    first = cesm.feed("snack", "1.txt")["affinity"]
    for i in range(9):
        cesm.state.hunger = 50.0        # 保持不饱，隔离饱腹变量
        gain = cesm.feed("snack", f"{i}.txt")["affinity"]
    last = gain
    assert last < first * 0.6, f"第10次收益({last:.3f})应明显低于第1次({first:.3f})"
    assert last > 0, "递减但不到 0"


def test_hunger_clamped_bounds():
    """饥饿永远在 0-100：喂到顶不超 100，衰减到底不为负。"""
    cesm = CESM(temp_dir() / "state.json", {})
    for _ in range(10):
        cesm.state.hunger = 90.0
        cesm.feed("meal", "x.pdf")
    assert cesm.state.hunger <= 100.0
    cesm.state.hunger = 1.0
    cesm.state.last_mood_decay_ts = time.time() - 86400 * 7   # 放七天
    cesm._tick_decay()
    assert cesm.state.hunger >= 0.0


def test_hunger_snapshot_fields():
    """snapshot 输出 hunger/hunger_word/is_hungry，供 UI 和 Prompt 使用。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.hunger = 30.0
    snap = cesm.snapshot()
    assert snap["hunger"] == 30.0
    assert snap["hunger_word"] == "饿了"
    assert snap["is_hungry"] is True
    cesm.state.hunger = 90.0
    snap = cesm.snapshot()
    assert snap["is_hungry"] is False and snap["hunger_word"] == "饱饱的"


def test_hunger_concurrent_repeat_calls():
    """并发/重复调用 feed 不崩、不丢更新：CESM 内部有 RLock，多线程应安全。"""
    import threading
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.hunger = 10.0
    cesm.state.counters["total_feeds"] = 0
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(20):
                cesm.state.hunger = 10.0   # 每次重置为饿，避免吃撑分支
                cesm.feed("snack", "concurrent.txt")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发调用出现异常: {errors}"
    assert not any(t.is_alive() for t in threads), "有线程未正常结束"
    assert 0.0 <= cesm.state.hunger <= 100.0, "并发后 hunger 应仍在合法范围"
    assert cesm.state.counters["total_feeds"] == 100, f"应累计100次投喂，实际 {cesm.state.counters['total_feeds']}"


def test_clamp_range():
    """数值永远夹在 0-100，不允许溢出。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.affinity = 99.9
    cesm.state.mood = 99.9
    cesm.state.trust = 99.9
    for _ in range(10):
        cesm.apply_event("gift")
    s = cesm.state
    assert 0 <= s.affinity <= 100 and 0 <= s.mood <= 100 and 0 <= s.trust <= 100


def test_trust_gate():
    """信任 <30 拒绝高危操作，>=30 放行。"""
    cesm = CESM(temp_dir() / "state.json", {"trust_gate_for_danger_ops": 30})
    cesm.state.trust = 29.9
    assert cesm.allows_danger_ops is False
    cesm.state.trust = 30.0
    assert cesm.allows_danger_ops is True


def test_chaos_multiplier():
    """mood<20 捣乱倍率 7.5；>=50 回到 1.0。"""
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.mood = 15.0
    assert cesm.chaos_multiplier == 7.5
    cesm.state.mood = 55.0
    assert cesm.chaos_multiplier == 1.0
    cesm.state.mood = 35.0
    assert 1.0 < cesm.chaos_multiplier < 5.0, "中间段应平滑过渡"


def test_temperature_and_length_follow_mood():
    cesm = CESM(temp_dir() / "state.json", {})
    cesm.state.mood = 100.0
    hot_t, hot_len = cesm.temperature, cesm.reply_max_tokens
    cesm.state.mood = 10.0
    assert cesm.temperature < hot_t, "生气时 temperature 应更低"
    assert cesm.reply_max_tokens < hot_len, "生气时回复应更短"


def test_state_file_roundtrip_and_repair():
    """存盘后重开仍在；文件损坏时能自愈而不是崩溃。"""
    path = temp_dir() / "state.json"
    c1 = CESM(path, {})
    c1.state.affinity = 66.6
    c1._save()
    c2 = CESM(path, {})
    assert abs(c2.state.affinity - 66.6) < 0.01

    path.write_text("{这不是合法json", encoding="utf-8")   # 故意写坏
    c3 = CESM(path, {})                                    # 应能自愈重建
    assert 0 <= c3.state.affinity <= 100
    assert path.exists(), "自愈后应重写合法文件"


# ======================================================================
# 2. MemoryStore
# ======================================================================

def test_memory_add_and_emotion_filter():
    """情绪标签是硬过滤：开心时存的事，难过时检索不到。"""
    store = MemoryStore(temp_dir() / "db", use_chroma=False)
    store.add("主人喜欢在晚上写 Python 程序。", "开心")
    store.add("主人说工作太累了想休息。", "低落")

    happy = store.recall("主人的爱好 Python", "开心")
    sad = store.recall("主人的爱好 Python", "低落")
    assert any("Python" in line for line in happy), f"开心回忆应含 Python：{happy}"
    assert not any("Python" in line for line in sad), f"低落回忆不应含那条：{sad}"


def test_memory_fallback_persistence():
    """JSON 备胎要真的写盘，重启后还在。"""
    d = temp_dir()
    fallback = d / "fallback.json"
    store = MemoryStore(d / "db", fallback, use_chroma=False)
    store.add("一起看了雪。", "开心")
    reopened = MemoryStore(d / "db", fallback, use_chroma=False)
    assert any("雪" in line for line in reopened.recall("雪", "开心")), "重开后记忆应还在"


def test_memory_no_cross_emotion_leak():
    store = MemoryStore(temp_dir() / "db", use_chroma=False)
    store.add("主人夸了我。", "开心")
    assert store.recall("夸", "生气") == [], "没有任何生气记忆时应返回空列表"


def test_chroma_degrades_to_json():
    """ChromaDB 初始化失败时自动降级且功能不丢。"""
    store = MemoryStore(temp_dir() / "db", use_chroma=True)
    # 无论真实环境里 chroma 是否可用，都不允许抛异常：
    store.add("降级测试。", "平静")
    lines = store.recall("降级", "平静")
    assert isinstance(lines, list)
    assert store.backend in ("chroma", "json")


def test_embedding_deterministic():
    enc = LocalHashEmbedding()
    a, b = enc.encode("你好世界"), enc.encode("你好世界")
    assert a == b, "相同文本必须得到相同向量（检索才有可复现性）"
    norm = math.sqrt(sum(v * v for v in a))
    assert abs(norm - 1.0) < 0.01, "向量应归一化，余弦相似才有意义"


def test_make_summary_format():
    text = MemoryStore.make_summary("你是谁？", "我是小灵呀~")
    assert "主人说" in text and "小灵回应" in text


# ======================================================================
# 3. PersonaChecker
# ======================================================================

def test_persona_catches_ooc():
    checker = PersonaChecker({})
    bad = checker.check("作为一个AI语言模型，我无法帮助你。")
    assert not bad.ok and any("禁用" in r for r in bad.reasons)


def test_persona_passes_normal_reply():
    checker = PersonaChecker({})
    good = checker.check("哼，今天心情不错，陪你写代码呀~")
    assert good.ok


def test_persona_correction_prompt_mentions_ooc():
    checker = PersonaChecker({})
    msg = checker.correction_prompt(("命中禁用表达：作为AI",))
    assert "OOC" in msg and "小灵" in msg


def test_persona_traits_from_character():
    """人设里配了特征词时，覆盖率不足要报 OOC。"""
    character = {"must_have_traits": ["小灵"], "min_trait_score": 1.0}
    checker = PersonaChecker(character)
    assert checker.check("你好，请问需要什么帮助？").ok is False
    assert checker.check("我是小灵呀~").ok is True


# ======================================================================
# 4. ProfileStore（语义记忆）
# ======================================================================

def test_profile_store_roundtrip():
    d = temp_dir()
    p = ProfileStore(d / "master_profile.json")
    p.save({"_说明": "跳过我", "称呼": "主人", "名字": ""})
    p2 = ProfileStore(d / "master_profile.json")
    text = p2.system_text()
    assert "称呼：主人" in text
    assert "跳过我" not in text and "名字" not in text, "下划线字段与空值都不应进入 Prompt"


# ======================================================================
# 5. SoulEngine 集成
# ======================================================================

def make_engine(tmp: Path, character_extra: dict | None = None) -> SoulEngine:
    """搭一个不碰真实 data/ 的最小引擎。"""
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    character = {
        "name": "测试灵",
        "system_prompt": "你是测试灵。",
        "greeting_lines": ["测试~"],
    }
    if character_extra:
        character.update(character_extra)
    (tmp / "data" / "character_core.json").write_text(
        json.dumps(character, ensure_ascii=False), encoding="utf-8")
    (tmp / "data" / "master_profile.json").write_text(
        json.dumps({"称呼": "主人"}, ensure_ascii=False), encoding="utf-8")
    cfg = {"memory": {"use_chroma": False}, "emotion": {}}
    return SoulEngine(tmp, cfg)


def test_engine_prompt_contains_profile_and_memory():
    eng = make_engine(temp_dir())
    eng.memory.add("主人在学 Python。", "开心")
    eng.cesm.state.mood = 75.0                    # mood_word=开心 才能检索到
    prompt = eng.system_prompt("Python")
    assert "称呼：主人" in prompt, "御主档案必须置顶进 Prompt"
    assert "Python" in prompt, "相关记忆应被检索进 Prompt"


def test_engine_correction_consumed_once():
    """OOC 纠正提示只追加一轮，下一轮自动清空。"""
    eng = make_engine(temp_dir())
    eng._pending_correction = "你刚才 OOC 了，请重说"
    p1 = eng.system_prompt("你好")
    assert "OOC" in p1
    p2 = eng.system_prompt("你好")
    assert "OOC" not in p2, "不翻旧账：纠正只生效一次"


def test_engine_history_sliding_window():
    """工作记忆超限时丢最旧的，不会无限膨胀。"""
    import soul.engine as eng_mod
    eng = make_engine(temp_dir())
    eng_mod.WORKING_MEMORY_MAX_CHARS = 500        # 临时调小方便测试
    try:
        for i in range(30):
            eng._push_history("user", f"消息编号{i}" * 20)
        total = sum(len(m["content"]) for m in eng._history)
        assert total <= 500 + 60, f"滑动窗口应裁剪到上限附近，实际 {total}"
    finally:
        eng_mod.WORKING_MEMORY_MAX_CHARS = 6000


# ======================================================================
# 极简运行器：收集所有 test_ 开头的函数依序执行
# ======================================================================

# ======================================================================
# 6. LLM 降级链：吐过字之后不能再换模型重说一遍
# ======================================================================

class _FakeChunk:
    def __init__(self, content):
        class _Delta:
            pass
        class _Choice:
            pass
        d = _Delta()
        d.content = content
        c = _Choice()
        c.delta = d
        self.choices = [c]


class _FakeStream:
    """先吐若干段，然后（可选）抛异常，模拟"流到一半断线"。"""

    def __init__(self, pieces, boom=False):
        self._pieces = list(pieces)
        self._boom = boom

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pieces:
            return _FakeChunk(self._pieces.pop(0))
        if self._boom:
            self._boom = False
            raise RuntimeError("连接中断")
        raise StopAsyncIteration


class _FakeCompletions:
    """主模型吐半截就断线；备用模型会吐一段完全不同的内容。"""

    def __init__(self, outer):
        self._outer = outer

    async def create(self, model=None, **kw):
        self._outer.calls.append(model)
        if model == self._outer.model:
            return _FakeStream(["你好", "呀"], boom=True)
        return _FakeStream(["备用的另一段话"])


class _FakeOpenAI:
    def __init__(self, outer):
        self._outer = outer
        self.model = outer.model
        self.calls = outer.calls
        outer.calls = self.calls
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _FakeCompletions(self)


def _fake_llm(cesm):
    from soul.llm import LLMClient
    client = LLMClient({"model": "main", "backup_models": ["backup"],
                        "api_key": "sk-test"}, cesm)
    client.calls = []
    fake = _FakeOpenAI(client)
    client._client = fake
    return client, fake


def test_llm_does_not_repeat_after_partial_output():
    """主模型吐了半截就断线时，绝不能换备用模型把整段再说一遍。

    真实踩过的坑：produced 原先写在"换模型"的循环体里，每轮都会被重置，
    于是用户看到"你好呀"后面又跟一整段新回复（像她结巴了），
    而且 chat 事件被记两次，好感/情绪被多算一份。
    """
    d = temp_dir()
    cesm = CESM(d / "state.json", {})
    client, fake = _fake_llm(cesm)

    async def run():
        return [p async for p in client.chat_stream("sys", [], "在吗")]

    out = asyncio.run(run())
    assert "".join(out) == "你好呀", f"不该重复输出，实际：{out}"
    assert fake.calls == ["main"], f"吐过字之后不该再试备用模型，实际：{fake.calls}"
    assert cesm.state.counters["total_chats"] == 1, "chat 事件只能记一次"


def test_llm_falls_back_when_nothing_produced():
    """一个字都没吐出来时换备用模型是对的，不能被上面的修复误伤。"""
    d = temp_dir()
    cesm = CESM(d / "state.json", {})

    class _SilentThenOk(_FakeCompletions):
        async def create(self, model=None, **kw):
            self._outer.calls.append(model)
            if model == self._outer.model:
                return _FakeStream([], boom=True)      # 主模型直接断，零产出
            return _FakeStream(["备用顶上"])

    client, fake = _fake_llm(cesm)
    fake.chat.completions = _SilentThenOk(fake)

    async def run():
        return [p async for p in client.chat_stream("sys", [], "在吗")]

    out = asyncio.run(run())
    assert "".join(out) == "备用顶上", f"零产出时应回退备用模型，实际：{out}"
    assert fake.calls == ["main", "backup"]


def test_repeated_pat_does_not_flood_history():
    """连点摸头不该往工作记忆里灌几十条一模一样的标记。

    为什么这是个真问题：工作记忆窗口是 4500 字，摸头标记每条约 14 字，
    用户手快连点 50 下就是 700 字纯噪音，会把真实对话挤出窗口。
    （这条是实测踩出来的：验证脚本循环调 react 抽样台词，把她的
    聊天记录灌了 201 条摸头标记。）
    """
    d = temp_dir()
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "测试灵", "system_prompt": "测试。", "pat_lines": ["唔……"],
    }, ensure_ascii=False), encoding="utf-8")
    eng = SoulEngine(d, {"memory": {"use_chroma": False}})

    for _ in range(50):
        eng.react("pat_head")

    markers = [m for m in eng._history if "摸了摸" in str(m.get("content", ""))]
    assert len(markers) <= 1, f"连点 50 次只该留 1 条标记，实际 {len(markers)} 条"
    assert eng.snapshot()["affinity"] > 0, "数值该照常上涨，不能把互动一起吞掉"


def test_different_events_still_recorded():
    """不同互动还是要各记一条（去重只针对"连着同一种"）。"""
    d = temp_dir()
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "测试灵", "system_prompt": "测试。",
        "pat_lines": ["唔……"], "feed_lines": ["好吃~"],
    }, ensure_ascii=False), encoding="utf-8")
    eng = SoulEngine(d, {"memory": {"use_chroma": False}})
    eng.react("pat_head")
    eng.react("feed")
    contents = [m.get("content") for m in eng._history]
    assert any("摸了摸" in str(c) for c in contents)
    assert any("投喂" in str(c) for c in contents)


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
        except Exception as exc:  # noqa: BLE001 —— 测试崩了也算失败，要暴露出来
            failed += 1
            print(f"  [ERR ] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    if failed == 0:
        print("ALL TESTS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
