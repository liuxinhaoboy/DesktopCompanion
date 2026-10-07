# -*- coding: utf-8 -*-
"""
3D 桌宠单元测试
================
运行：python tests/test_model3d.py

测试范围：
  1. face/model3d.py：GLB 生成/解析、损坏文件容错、归一化、软件渲染产出像素
  2. characters/character_loader.py：model3d 字段解析、display 字段容错
  3. face/pet_renderer.py：3D 分支优先级（模型>立绘>矢量）、加载失败回退、
     display=2d 强制 2D、情绪叠加不崩溃
  4. 渲染缩略图（设置面板卡片）支持 3D 角色

测试原则：不联网、全部 tempfile、QApplication 存全局防 GC 崩溃。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication                     # noqa: E402
from PyQt6.QtCore import Qt                                  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap    # noqa: E402

from characters.character_loader import CharacterLoader      # noqa: E402
from face.model3d import Model3D, make_test_glb              # noqa: E402
from face.pet_renderer import PetRenderer                    # noqa: E402
from face.settings_panel import render_thumbnail             # noqa: E402

_qapp = None


def _make_qapp():
    """QApplication 必须存全局变量，否则被 GC 回收引发 0xC0000409 原生崩溃。"""
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_m3d_"))


def make_character_pkg(d: Path, char_id: str, name: str,
                       extra_manifest: dict | None = None) -> Path:
    """造一个最小合法角色包，可附加 manifest 扩展字段。"""
    cdir = d / char_id
    cdir.mkdir(parents=True, exist_ok=True)
    manifest = {"id": char_id, "name": name, "shape": "cat",
                "mood_styles": {"平静": {"body": [168, 200, 236],
                                         "eye": [44, 58, 74]}}}
    if extra_manifest:
        manifest.update(extra_manifest)
    (cdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (cdir / "system_prompt.md").write_text("你是测试角色。", encoding="utf-8")
    (cdir / "lines.json").write_text(json.dumps(
        {"greeting_lines": ["你好~"]}, ensure_ascii=False), encoding="utf-8")
    return cdir


def _render_side_by_side(renderer: PetRenderer, yaw: float) -> int:
    """用 renderer 离屏画一帧，返回不透明采样点数。"""
    pix = QPixmap(220, 240)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    snap = {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}
    try:
        renderer.paint(p, pix.width(), pix.height(), 160,
                       yaw / 0.16, False, False, snap)   # yaw 反推 phase
    finally:
        p.end()
    img = pix.toImage()
    return sum(1 for y in range(0, 240, 3) for x in range(0, 220, 3)
               if img.pixelColor(x, y).alpha() > 0)


# ======================================================================
# 1. GLB 解析
# ======================================================================

def test_glb_roundtrip_parse():
    """程序化生成的 GLB 能被解析出预期面数。"""
    _make_qapp()
    p = make_test_glb(temp_dir() / "octa.glb")
    m = Model3D.load(p)
    assert m is not None, "合法 GLB 应解析成功"
    assert m.tri_count == 8, f"测试模型应有 8 个三角面，实际 {m.tri_count}"
    assert len(m.tris) == 8 and m.tris.shape[1:] == (3, 3)
    assert m.face_colors.shape == (8, 3)
    assert m.double_sided.shape == (8,)


def test_corrupted_glb_returns_none():
    """损坏/伪造的模型文件返回 None 而不是抛异常（上层回退 2D 的依据）。"""
    d = temp_dir()
    bad = d / "bad.glb"
    bad.write_bytes(b"this is definitely not a glb file" * 10)
    assert Model3D.load(bad) is None

    empty = d / "empty.glb"
    empty.write_bytes(b"")
    assert Model3D.load(empty) is None

    truncated = d / "trunc.glb"
    full = make_test_glb(d / "full.glb").read_bytes()
    truncated.write_bytes(full[: len(full) // 2])
    assert Model3D.load(truncated) is None, "截断的 GLB 应安全失败"


def test_unsupported_extension_and_missing_file():
    """后缀不对或文件不存在 → None。"""
    d = temp_dir()
    fake = d / "model.txt"
    fake.write_text("hello", encoding="utf-8")
    assert Model3D.load(fake) is None
    assert Model3D.load(d / "ghost.glb") is None


def test_normalized_bounds():
    """解析后端点归一化：包围盒中心≈原点、最大边长≈2。"""
    _make_qapp()
    p = make_test_glb(temp_dir() / "n.glb")
    m = Model3D.load(p)
    flat = m.tris.reshape(-1, 3)
    lo, hi = flat.min(0), flat.max(0)
    center = (lo + hi) / 2
    span = (hi - lo).max()
    assert abs(center).max() < 1e-3, f"中心应归零：{center}"
    assert abs(span - 2.0) < 1e-3, f"最大边长应归一为2：{span}"


# ======================================================================
# 2. 软件渲染
# ======================================================================

def test_render_produces_pixels():
    """渲染一帧后画布中心区域有足够不透明像素（模型真的被画出来了）。"""
    _make_qapp()
    p = make_test_glb(temp_dir() / "r.glb")
    m = Model3D.load(p)
    img = QImage(200, 200, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    qp = QPainter(img)
    m.render(qp, 100, 100, 80, yaw=0.5)
    qp.end()
    n = sum(1 for y in range(0, 200, 2) for x in range(0, 200, 2)
            if img.pixelColor(x, y).alpha() > 0)
    assert n > 200, f"画面应有大量像素，实际采样不透明数 {n}"


def test_render_different_angles_differ():
    """自转两个角度后画面不一样（yaw 真的驱动了旋转，而不是贴死图）。"""
    _make_qapp()
    p = make_test_glb(temp_dir() / "a.glb")
    m = Model3D.load(p)

    def sig(yaw):
        img = QImage(160, 160, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        qp = QPainter(img)
        m.render(qp, 80, 80, 64, yaw=yaw)
        qp.end()
        # 抽样像素做校验和（constBits 是 voidptr，不能直接 tobytes）
        return tuple(img.pixelColor(x, y).rgba()
                     for y in range(0, 160, 7) for x in range(0, 160, 7))

    assert sig(0.0) != sig(1.2), "不同自转角度画面不应完全相同"


def test_render_tint_and_dim_do_not_crash():
    """情绪叠加（色调用）与压暗在 3D 模式下正常执行。"""
    _make_qapp()
    p = make_test_glb(temp_dir() / "t.glb")
    m = Model3D.load(p)
    for tint, dim in ((None, 1.0), (QColor(232, 120, 120, 60), 1.0),
                      (None, 0.6), (QColor(120, 120, 126, 150), 0.6)):
        img = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        qp = QPainter(img)
        m.render(qp, 60, 60, 48, yaw=0.3, tint=tint, dim=dim)
        qp.end()


def test_double_sided_back_not_erased():
    """双面材质：从背面看仍有内容（单面模型会被背面剔除打空）。"""
    _make_qapp()
    p = make_test_glb(temp_dir() / "ds.glb")   # 测试模型 doubleSided=true
    m = Model3D.load(p)
    assert m.double_sided.all(), "测试模型应全部为双面材质"
    img = QImage(160, 160, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    qp = QPainter(img)
    m.render(qp, 80, 80, 64, yaw=3.5)          # 转到背面视角
    qp.end()
    n = sum(1 for y in range(0, 160, 2) for x in range(0, 160, 2)
            if img.pixelColor(x, y).alpha() > 0)
    assert n > 100, f"背面视角也应画出内容，实际 {n}"


# ======================================================================
# 3. 角色包：model3d / display 字段
# ======================================================================

def test_model3d_resolved_from_manifest():
    """manifest 写 model3d → Profile 带可加载的模型路径。"""
    _make_qapp()
    d = temp_dir()
    cdir = make_character_pkg(d, "cat3d", "立方猫", {"model3d": "model.glb"})
    make_test_glb(cdir / "model.glb")
    prof = CharacterLoader(d).load_character("cat3d")
    assert prof.model3d is not None and prof.model3d.is_file()
    assert Model3D.load(prof.model3d) is not None, "角色包内的模型必须可加载"


def test_model3d_auto_detected():
    """manifest 没写，但目录有 model.glb → 按约定识别。"""
    _make_qapp()
    d = temp_dir()
    cdir = make_character_pkg(d, "auto3d", "自动3D猫")
    make_test_glb(cdir / "model.glb")
    prof = CharacterLoader(d).load_character("auto3d")
    assert prof.model3d is not None


def test_model3d_missing_is_none():
    """没有模型文件 → None（角色照常以 2D 加载，不报错）。"""
    d = temp_dir()
    make_character_pkg(d, "no3d", "纯2D猫")
    prof = CharacterLoader(d).load_character("no3d")
    assert prof.model3d is None


def test_display_field_normalized():
    """display 字段容错：2d/3d/auto 原样，乱写一律回 auto 而不是加载失败。"""
    d = temp_dir()
    for raw, expect in (("2d", "2d"), ("3D", "3d"), ("auto", "auto"),
                        ("three-dim", "auto"), (123, "auto"), (None, "auto")):
        make_character_pkg(d, f"disp_{abs(hash(str(raw))) % 10**6}",
                           "测试", {"display": raw})
        prof = CharacterLoader(d).load_character(
            f"disp_{abs(hash(str(raw))) % 10**6}")
        assert prof.display == expect, f"display={raw!r} 应归一为 {expect}"


# ======================================================================
# 4. 渲染器：3D 分支优先级与回退
# ======================================================================

def test_renderer_prefers_model_over_avatar():
    """模型和立绘同时存在时 3D 优先（两者只生效一个）。"""
    _make_qapp()
    d = temp_dir()
    png = d / "avatar.png"
    pix = QPixmap(64, 64)
    pix.fill(QColor(240, 60, 60))             # 纯红立绘
    pix.save(str(png), "PNG")
    glb = make_test_glb(d / "model.glb")

    r = PetRenderer(shape="cat", avatar=png, model3d=glb, display="auto")
    n = _render_side_by_side(r, 0.6)
    assert n > 100, "3D 模式下应画出模型"
    # 立方猫是淡蓝色材质；若画的是红立绘说明优先级反了
    r2 = PetRenderer(shape="cat", avatar=png, model3d=glb, display="auto")
    img_pix = QPixmap(220, 240)
    img_pix.fill(Qt.GlobalColor.transparent)
    qp = QPainter(img_pix)
    snap = {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}
    r2.paint(qp, 220, 240, 160, 0.0, False, False, snap)
    qp.end()
    im = img_pix.toImage()
    reddish = 0
    for y in range(60, 200, 4):
        for x in range(60, 160, 4):
            c = im.pixelColor(x, y)
            if c.alpha() > 0 and c.red() > 180 and c.green() < 110:
                reddish += 1
    assert reddish == 0, f"画面不应出现红色立绘（应优先3D），检测到 {reddish} 点"


def test_display_2d_forces_vector():
    """display=2d：有模型也画矢量（用户想换回 2D 的出口）。"""
    _make_qapp()
    glb = make_test_glb(temp_dir() / "f.glb")
    r = PetRenderer(shape="cat", model3d=glb, display="2d")
    assert r._get_model() is None, "display=2d 时模型不应被加载"
    assert _render_side_by_side(r, 0.3) > 100, "2D 矢量照常能画"


def test_broken_model_falls_back_to_2d():
    """模型文件损坏：渲染回退矢量猫，绝不白屏/崩溃。"""
    _make_qapp()
    d = temp_dir()
    bad = d / "broken.glb"
    bad.write_bytes(b"not a glb at all" * 5)
    r = PetRenderer(shape="cat", model3d=bad, display="3d")
    assert r._get_model() is None
    assert _render_side_by_side(r, 0.4) > 100, "回退后 2D 内容应可见"


def test_model3d_frozen_and_angry_states():
    """假死/生气/各种情绪的叠加路径在 3D 下不崩。"""
    _make_qapp()
    glb = make_test_glb(temp_dir() / "moods.glb")
    for word, angry, hunger in (("生气", True, 90), ("难过", False, 20),
                                ("兴奋", False, 100), ("平静", False, 50)):
        snap = {"mood_word": word, "mood": 15 if angry else 70,
                "is_angry": angry, "hunger": hunger}
        r = PetRenderer(shape="cat", model3d=glb, display="auto")
        pix = QPixmap(200, 220)
        pix.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pix)
        r.paint(qp, 200, 220, 150, 1.2, False, word == "难过" and False, snap)
        qp.end()
        assert not pix.isNull()


def test_thumbnail_supports_3d_character():
    """设置面板卡片：3D 角色的缩略图也画模型。"""
    _make_qapp()
    d = temp_dir()
    cdir = make_character_pkg(d, "thumb3d", "3D猫", {"model3d": "model.glb"})
    make_test_glb(cdir / "model.glb")
    prof = CharacterLoader(d).load_character("thumb3d")
    pix = render_thumbnail(prof)
    assert not pix.isNull()
    img = pix.toImage()
    n = sum(1 for y in range(0, img.height(), 3)
            for x in range(0, img.width(), 3)
            if img.pixelColor(x, y).alpha() > 0)
    assert n > 30, "3D 角色卡片应有内容"


# ======================================================================
# 极简运行器
# ======================================================================

def _run_all() -> int:
    _make_qapp()
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
    print("[运行 3D 桌宠 测试]")
    sys.exit(_run_all())
