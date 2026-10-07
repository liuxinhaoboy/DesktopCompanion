# -*- coding: utf-8 -*-
"""
角色包与宠物切换单元测试
========================
运行：python tests/test_characters.py

测试范围：
  1. CharacterLoader：内置角色加载、manifest 校验、损坏包跳过、旧版迁移、颜色格式
  2. CharacterManager：初始加载、切换、数据隔离（state/history 各自独立）、目录规则
  3. PetRenderer：三种形状创建、非法形状回退、离屏绘制不崩
"""

from __future__ import annotations

import json
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from characters.character_loader import CharacterLoader, CharacterProfile  # noqa: E402


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_char_"))


def make_character(base: Path, cid: str, manifest: dict | None = None,
                   prompt: str = "你是测试角色。",
                   lines: dict | None = None) -> Path:
    """在 base 下造一个最小合法角色包，返回角色目录。"""
    cdir = base / cid
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "sprites").mkdir(exist_ok=True)
    manifest = manifest if manifest is not None else {
        "id": cid, "name": cid, "shape": "cat",
        "mood_styles": {"平静": {"body": [100, 100, 100], "eye": [10, 10, 10]}}}
    (cdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (cdir / "system_prompt.md").write_text(prompt, encoding="utf-8")
    (cdir / "lines.json").write_text(
        json.dumps(lines or {"greeting_lines": ["你好"]}, ensure_ascii=False),
        encoding="utf-8")
    return cdir


# ======================================================================
# 1. CharacterLoader
# ======================================================================

def test_load_all_builtin_characters():
    """三个内置角色（小灵/豆豆/布丁）都能加载，shape 分别是 cat/dog/slime。"""
    loader = CharacterLoader(ROOT / "characters")
    chars = loader.list_characters()
    ids = {c.id for c in chars}
    assert {"xiaoling", "dog", "slime"} <= ids, f"缺少内置角色：{ids}"
    by_id = {c.id: c for c in chars}
    assert by_id["xiaoling"].shape == "cat"
    assert by_id["dog"].shape == "dog"
    assert by_id["slime"].shape == "slime"
    # 每个角色都有 9 组台词和非空人设
    for c in chars:
        assert c.system_prompt, f"{c.id} 人设为空"
        assert len(c.lines) >= 9, f"{c.id} 台词组不足9组：{len(c.lines)}"
        assert "greeting_lines" in c.lines


def test_manifest_missing_id_raises():
    """manifest 缺 id 必须报错。"""
    d = temp_dir()
    make_character(d, "bad", manifest={"name": "没id"})
    loader = CharacterLoader(d)
    try:
        loader.load_character("bad")
        assert False, "缺 id 应该抛 ValueError"
    except ValueError as e:
        assert "id" in str(e)


def test_manifest_missing_name_raises():
    """manifest 缺 name 必须报错。"""
    d = temp_dir()
    make_character(d, "bad", manifest={"id": "bad"})
    loader = CharacterLoader(d)
    try:
        loader.load_character("bad")
        assert False, "缺 name 应该抛 ValueError"
    except ValueError as e:
        assert "name" in str(e)


def test_missing_system_prompt_raises():
    """缺 system_prompt.md 必须报错。"""
    d = temp_dir()
    cdir = make_character(d, "bad")
    (cdir / "system_prompt.md").unlink()
    loader = CharacterLoader(d)
    try:
        loader.load_character("bad")
        assert False, "缺人设文件应该抛错"
    except FileNotFoundError:
        pass


def test_broken_character_skipped():
    """扫描时损坏的角色包被跳过，不影响其他正常角色。"""
    d = temp_dir()
    make_character(d, "good1")
    bad = make_character(d, "bad")
    (bad / "manifest.json").write_text("{不是合法json", encoding="utf-8")
    make_character(d, "good2")
    loader = CharacterLoader(d)
    chars = loader.list_characters()
    ids = {c.id for c in chars}
    assert ids == {"good1", "good2"}, f"损坏包应被跳过，实际：{ids}"


def test_mood_styles_are_rgb_arrays():
    """manifest 的情绪颜色必须是长度3的 [r,g,b] 数组（JSON 不能存 QColor）。"""
    loader = CharacterLoader(ROOT / "characters")
    for c in loader.list_characters():
        for mood, colors in c.mood_styles.items():
            for part in ("body", "eye"):
                rgb = colors[part]
                assert isinstance(rgb, list) and len(rgb) == 3, \
                    f"{c.id}/{mood}/{part} 应为[r,g,b]，实际{rgb}"
                assert all(isinstance(v, int) and 0 <= v <= 255 for v in rgb), \
                    f"{c.id}/{mood}/{part} 颜色值越界：{rgb}"


def test_to_character_dict_format():
    """to_character_dict 输出 SoulEngine._character 需要的扁平格式。"""
    loader = CharacterLoader(ROOT / "characters")
    prof = loader.load_character("dog")
    d = prof.to_character_dict()
    assert d["name"] == "豆豆"
    assert d["system_prompt"] == prof.system_prompt
    assert d["greeting_lines"] == prof.lines["greeting_lines"]
    # 不应残留 shape/renderer 等元数据
    assert "shape" not in d and "mood_styles" not in d


def test_legacy_character_migration():
    """旧版 data/character_core.json 能迁移成 CharacterProfile。"""
    d = temp_dir()
    legacy = {
        "name": "老版小灵",
        "system_prompt": "旧人设",
        "greeting_lines": ["旧问候"],
        "pat_lines": ["旧摸头"],
    }
    (d / "character_core.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    loader = CharacterLoader(d / "characters")
    prof = loader.load_legacy_character(d / "character_core.json")
    assert prof is not None
    assert prof.id == "xiaoling" and prof.name == "老版小灵"
    assert prof.shape == "cat"
    assert prof.lines["greeting_lines"] == ["旧问候"]
    assert prof.system_prompt == "旧人设"


def test_legacy_missing_returns_none():
    """旧版文件不存在时返回 None（不崩）。"""
    loader = CharacterLoader(temp_dir())
    assert loader.load_legacy_character(temp_dir() / "nope.json") is None


# ======================================================================
# 2. CharacterManager + 数据隔离
# ======================================================================

def _make_project(tmp: Path, ids=("xiaoling", "dog")) -> None:
    """搭一个最小项目：characters/ 角色包 + data/ 目录。"""
    chars_dir = tmp / "characters"
    for cid in ids:
        src = ROOT / "characters" / cid
        if src.exists():
            shutil.copytree(src, chars_dir / cid)
        else:
            make_character(chars_dir, cid,
                           manifest={"id": cid, "name": cid, "shape": "cat"})
    (tmp / "data").mkdir(exist_ok=True)


def _engine(tmp: Path):
    from soul.engine import SoulEngine
    cfg = {"memory": {"use_chroma": False}, "llm": {}, "emotion": {}}
    return SoulEngine(tmp, cfg)


def test_initial_load_sets_profile():
    """初始加载后 engine 的人设和角色 profile 一致。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    _make_project(d)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    mgr.discover()
    prof = mgr.load_initial("dog")
    assert prof.id == "dog"
    assert eng.character_profile is prof
    assert eng.name == "豆豆"
    assert "豆豆" in eng._character["system_prompt"]


def test_switch_updates_character_data():
    """切换角色后人设/台词/数据目录全部更新。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    _make_project(d)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    mgr.discover()
    mgr.load_initial("xiaoling")

    ok, _ = mgr.switch_to("dog")
    assert ok and mgr.current.id == "dog"
    assert eng.name == "豆豆"
    assert eng.data_dir == d / "data" / "characters" / "dog"
    assert any("汪" in g for g in eng._character["greeting_lines"])


def test_data_isolation_between_characters():
    """每个角色的聊天记录和情绪状态各自独立，切换不串数据。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    _make_project(d)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    mgr.discover()
    mgr.load_initial("xiaoling")

    # 小灵留下专属对话和状态
    eng._push_history("user", "只属于小灵的秘密")
    eng.cesm.apply_event("pat_head")
    eng.flush()

    # 切到豆豆：历史必须是空的
    mgr.switch_to("dog")
    assert eng._history == [], "新角色不应看到小灵的历史"
    eng._push_history("user", "只属于豆豆的秘密")
    eng.flush()

    # 切回小灵：只有小灵的对话，且文件物理隔离
    mgr.switch_to("xiaoling")
    contents = [m["content"] for m in eng._history]
    assert any("小灵" in c for c in contents)
    assert not any("豆豆" in c for c in contents)
    assert (d / "data" / "state.json").exists()
    assert (d / "data" / "characters" / "dog" / "state.json").exists()


def test_default_character_uses_data_root():
    """默认角色 xiaoling 的数据放在 data/ 根目录（向后兼容老数据）。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    _make_project(d)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    assert mgr.data_dir_for("xiaoling") == d / "data"
    assert mgr.data_dir_for("dog") == d / "data" / "characters" / "dog"


def test_switch_unknown_character_rejected():
    """切换到不存在的角色返回失败，不改变当前角色。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    _make_project(d)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    mgr.discover()
    mgr.load_initial("xiaoling")
    ok, msg = mgr.switch_to("ghost")
    assert not ok
    assert "ghost" in msg
    assert mgr.current.id == "xiaoling"


def test_switch_to_same_is_noop():
    """切到当前角色直接短路成功。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    _make_project(d)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    mgr.discover()
    mgr.load_initial("dog")
    ok, msg = mgr.switch_to("dog")
    assert ok and "已经" in msg


def test_fallback_when_characters_empty():
    """characters/ 为空时降级用旧版 character_core.json。"""
    from soul.character_manager import CharacterManager
    d = temp_dir()
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "遗产角色", "system_prompt": "古老的人设",
        "greeting_lines": ["嗨"]}, ensure_ascii=False), encoding="utf-8")
    (d / "characters").mkdir(exist_ok=True)
    eng = _engine(d)
    mgr = CharacterManager(d, eng)
    found = mgr.discover()
    assert len(found) == 1 and found[0].name == "遗产角色"


# ======================================================================
# 3. PetRenderer（需要 QApplication，必须存全局变量防 GC 崩溃）
# ======================================================================

_qapp = None


def _get_qapp():
    global _qapp
    if _qapp is None:
        from PyQt6.QtWidgets import QApplication
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def test_renderer_creates_three_shapes():
    """cat/dog/slime 三种形状都能创建渲染器。"""
    _get_qapp()
    from face.pet_renderer import PetRenderer
    for shape in ("cat", "dog", "slime"):
        r = PetRenderer(shape=shape, mood_styles={
            "平静": {"body": [168, 200, 236], "eye": [44, 58, 74]}})
        assert r.shape == shape


def test_renderer_invalid_shape_falls_back_to_cat():
    """未知形状安全回退到 cat，不崩。"""
    _get_qapp()
    from face.pet_renderer import PetRenderer
    r = PetRenderer(shape="dragon")
    assert r.shape == "cat"


def test_renderer_paint_all_shapes_no_crash():
    """三种形状离屏绘制一遍，验证路径/坐标计算不报错。"""
    _get_qapp()
    from PyQt6.QtGui import QPixmap, QPainter
    from face.pet_renderer import PetRenderer
    styles = {mood: {"body": [168, 200, 236], "eye": [44, 58, 74]}
              for mood in ("兴奋", "开心", "平静", "低落", "难过", "生气")}
    snap = {"mood_word": "开心", "mood": 80, "is_angry": False, "hunger": 80}
    for shape in ("cat", "dog", "slime"):
        pm = QPixmap(200, 200)
        p = QPainter(pm)
        r = PetRenderer(shape=shape, mood_styles=styles)
        r.paint(p, 200, 200, 160, 0.5, False, False, snap)
        # 再画一遍眨眼 + 生气 + 假死 + 饥饿的组合，覆盖分支
        snap2 = {"mood_word": "生气", "mood": 10, "is_angry": True, "hunger": 10}
        r.paint(p, 200, 200, 160, 1.2, True, False, snap2)
        r.paint(p, 200, 200, 160, 0.0, False, True, snap)
        p.end()


def test_renderer_bad_color_does_not_crash():
    """颜色表缺字段/格式错时用灰色兜底，不崩。"""
    _get_qapp()
    from PyQt6.QtGui import QPixmap, QPainter
    from face.pet_renderer import PetRenderer
    r = PetRenderer(shape="cat", mood_styles={"平静": {"body": "坏数据"}})
    pm = QPixmap(200, 200)
    p = QPainter(pm)
    r.paint(p, 200, 200, 160, 0.0, False, False,
            {"mood_word": "平静", "mood": 50, "is_angry": False, "hunger": 80})
    p.end()


# ======================================================================
# 极简运行器
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
