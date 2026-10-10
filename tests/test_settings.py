# -*- coding: utf-8 -*-
"""
设置控制中心单元测试
====================
运行：python tests/test_settings.py

测试范围：
  1. SettingsPanel：六个标签页、控件读值、_load_current 刷新、视觉模型联动
  2. 保存链路：on_save 收集分段、密钥留空不覆盖、填了的密钥（llm/vision/voice）加密
  3. 导入角色：validate_character_folder 各种非法情况拦截、合法通过
  4. apply_config：多段密钥统一加密、热更新不丢配置
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_qapp = None


def _get_qapp():
    """QApplication 必须存全局变量，否则被 GC 回收会导致原生崩溃。"""
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_set_"))


def make_project(d: Path) -> None:
    """搭一个最小可启动 AppController 的临时项目。"""
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({
        "llm": {"api_key": "sk-test-secret", "model": "gpt-5.5",
                "backup_models": ["gpt-5.6-terra"]},
        "memory": {"use_chroma": False},
        "character": {"size": 160, "opacity": 1.0, "current_id": "xiaoling"},
        "sandbox": {"workspace_root": "", "allow_processes": False},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "测试灵", "system_prompt": "测试。",
        "greeting_lines": ["测试~"],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "data" / "master_profile.json").write_text("{}", encoding="utf-8")
    # 商店页要有真商品才跑得到（否则每次都只是"暂无商品"的分支）
    (d / "shop").mkdir(parents=True, exist_ok=True)
    (d / "shop" / "catalog.json").write_text(json.dumps({"items": [
        {"id": "t_snack", "name": "测试零食", "kind": "snack",
         "price": 3, "boost": 1.5},
        {"id": "t_hat", "name": "测试帽子", "kind": "accessory",
         "price": 5, "slot": "head", "shape": "hat", "color": [200, 100, 100]},
        {"id": "t_lines", "name": "测试台词包", "kind": "lines",
         "price": 5, "lines_scene": "pat_lines", "lines": ["测试台词"]},
    ]}, ensure_ascii=False), encoding="utf-8")


def _controller(d: Path):
    from app.controller import AppController
    return AppController(d)


def _panel(controller):
    from face.settings_panel import SettingsPanel
    return SettingsPanel(controller)


# ======================================================================
# 1. 面板构建
# ======================================================================

def test_panel_has_seven_tabs():
    """七页：B 商城经济加了「商店」页（单独拆在 face/shop_tab.py）。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    assert panel.tabs.count() == 7
    names = [panel.tabs.tabText(i) for i in range(7)]
    assert names == ["宠物", "AI 与多模态", "语音", "电脑控制",
                     "浏览器插件", "隐私", "商店"]


def test_panel_has_personalized_brand_header():
    """设置页统一的暖色抬头应显示当前伙伴名称。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    from PyQt6.QtWidgets import QLabel
    title = panel.brand_header.findChild(QLabel, "brandHeaderTitle")
    assert title is not None and title.text() == "测试灵 的小窝"
    assert panel.brand_header.objectName() == "brandHeader"


def test_display_mode_combo_has_two_options():
    """3D 显示模式只给"自动/只用 2D"两项。

    原来的"优先 3D（没有模型时回退 2D）"和"自动"在渲染器里完全等价
    （pet_renderer 只判断 display 是否为 "2d"），留着只会让人纠结选哪个。
    """
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    data = [panel.display_combo.itemData(i)
            for i in range(panel.display_combo.count())]
    assert data == ["auto", "2d"], f"显示模式选项应为两项，实际：{data}"


def test_panel_loads_values_from_config():
    """控件初始值来自 config。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    panel = _panel(ctrl)
    assert panel.model_edit.text() == "gpt-5.5"
    assert panel.size_spin.value() == 160
    assert panel.opacity_spin.value() == 1.0


def test_load_current_refreshes_values():
    """外部改了配置后，_load_current 能把控件刷成新值。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    panel = _panel(ctrl)
    # 模拟外部把模型改了
    ctrl.config_store.update_section("llm", {"model": "gpt-5.6-sol"})
    panel.model_edit.setText("旧值")
    panel._load_current()
    assert panel.model_edit.text() == "gpt-5.6-sol"


def test_load_current_repeatable_no_crash():
    """_load_current 连续调用不崩（重复打开设置窗的场景）。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    panel._load_current()
    panel._load_current()


# ======================================================================
# 2. 视觉模型联动
# ======================================================================

def test_vision_disabled_locks_inputs():
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    panel.vision_enable.setChecked(False)
    panel._sync_vision_enabled()
    assert not panel.vision_url.isEnabled()
    assert not panel.vision_model.isEnabled()


def test_vision_inherit_locks_independent_fields():
    """启用视觉但勾选"继承文本API"时，独立地址/密钥仍锁定。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    panel.vision_enable.setChecked(True)
    panel.vision_inherit.setChecked(True)
    panel._sync_vision_enabled()
    assert not panel.vision_url.isEnabled()
    # 取消继承后解锁
    panel.vision_inherit.setChecked(False)
    panel._sync_vision_enabled()
    assert panel.vision_url.isEnabled()
    assert panel.vision_model.isEnabled()


# ======================================================================
# 3. 保存链路
# ======================================================================

def test_save_writes_sections():
    """on_save 收集的值经 apply_config 落盘。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    panel = _panel(ctrl)
    panel.timeout_spin.setValue(120)
    panel.maxtoken_spin.setValue(1200)
    panel.opacity_spin.setValue(0.7)
    panel.allow_clipboard.setChecked(False)
    panel.confirm_policy.setCurrentIndex(1)   # smart
    panel.whitelist_edit.setText("notepad, mspaint")
    panel.on_save()
    cfg = ctrl.config_store.cfg
    assert cfg["llm"]["timeout_seconds"] == 120
    assert cfg["llm"]["max_tokens"] == 1200
    assert cfg["character"]["opacity"] == 0.7
    assert cfg["sandbox"]["allow_clipboard"] is False
    assert cfg["sandbox"]["confirm_policy"] == "smart"
    assert cfg["sandbox"]["process_whitelist"] == ["notepad", "mspaint"]


def test_blank_key_does_not_overwrite_saved():
    """密钥框留空保存，不会把已保存的密钥覆盖成空。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    panel = _panel(ctrl)
    assert panel.key_edit.text() == ""       # 没填
    panel.on_save()
    saved = ctrl.config_store.cfg["llm"]["api_key"]
    # DPAPI 可用时是 secret 引用，不可用时是原明文，总之不是空
    assert saved, "留空不应清空已保存密钥"


def test_new_llm_key_becomes_secret_reference():
    """填了新文本密钥，保存后 config 里只留 secret: 引用（不写明文）。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    panel = _panel(ctrl)
    if not ctrl.secrets.available:
        return  # DPAPI 不可用时回退明文，这条断言不适用
    panel.key_edit.setText("sk-brand-new-key-1234")
    panel.on_save()
    saved = ctrl.config_store.cfg["llm"]["api_key"]
    assert saved.startswith("secret:"), f"新密钥应加密为引用，实际: {saved[:8]}"
    assert "brand-new" not in json.dumps(ctrl.config_store.cfg)


def test_vision_and_voice_keys_also_encrypted():
    """视觉/语音的独立密钥填了也要加密，不能明文落盘。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    if not ctrl.secrets.available:
        return
    ok, msg = ctrl.apply_config({
        "vision": {"enabled": True, "inherit_from_llm": False,
                   "api_key": "sk-vision-xyz", "base_url": "x", "model": "v"},
        "voice": {"stt_api_key": "sk-stt-abc"},
    })
    assert ok, msg
    cfg = ctrl.config_store.cfg
    assert cfg["vision"]["api_key"].startswith("secret:")
    assert cfg["voice"]["stt_api_key"].startswith("secret:")
    raw = json.dumps(cfg)
    assert "sk-vision-xyz" not in raw and "sk-stt-abc" not in raw


def test_backup_models_parsed_from_comma_text():
    """备用模型逗号文本正确解析成列表。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    ctrl = _controller(d)
    panel = _panel(ctrl)
    panel.backup_edit.setText(" m1, m2 ,, m3 ")
    panel.on_save()
    assert ctrl.config_store.cfg["llm"]["backup_models"] == ["m1", "m2", "m3"]


# ======================================================================
# 4. 导入角色校验（静态方法，不需要完整面板）
# ======================================================================

def test_import_rejects_missing_manifest():
    d = Path(tempfile.mkdtemp())
    from face.settings_panel import SettingsPanel
    _, err = SettingsPanel.validate_character_folder(d)
    assert err and "manifest" in err


def test_import_rejects_broken_json():
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text("{不是json", encoding="utf-8")
    from face.settings_panel import SettingsPanel
    _, err = SettingsPanel.validate_character_folder(d)
    assert err and "JSON" in err


def test_import_rejects_missing_required_field():
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text(json.dumps({"name": "没id"}), encoding="utf-8")
    from face.settings_panel import SettingsPanel
    _, err = SettingsPanel.validate_character_folder(d)
    assert err and "id" in err


def test_import_rejects_illegal_id():
    from face.settings_panel import SettingsPanel
    for bad in ("a/b", "a:b", "a*b", ""):
        d = Path(tempfile.mkdtemp())
        (d / "manifest.json").write_text(
            json.dumps({"id": bad, "name": "x"}), encoding="utf-8")
        _, err = SettingsPanel.validate_character_folder(d)
        assert err, f"非法 id {bad!r} 应被拒绝"


def test_import_rejects_missing_prompt():
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text(
        json.dumps({"id": "ok", "name": "好"}), encoding="utf-8")
    from face.settings_panel import SettingsPanel
    _, err = SettingsPanel.validate_character_folder(d)
    assert err and "system_prompt" in err


def test_import_accepts_valid_folder():
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text(
        json.dumps({"id": "newbie", "name": "新人"}), encoding="utf-8")
    (d / "system_prompt.md").write_text("人设", encoding="utf-8")
    (d / "lines.json").write_text(json.dumps({"greeting_lines": ["hi"]}),
                                  encoding="utf-8")
    from face.settings_panel import SettingsPanel
    manifest, err = SettingsPanel.validate_character_folder(d)
    assert err is None and manifest["id"] == "newbie"


def test_import_rejects_missing_lines():
    """导入校验必须查 lines.json：运行时加载强制要求它，缺了会让角色
    导入"成功"却加载失败被静默跳过（用户看到角色凭空消失）。"""
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text(
        json.dumps({"id": "nolines", "name": "没台词"}), encoding="utf-8")
    (d / "system_prompt.md").write_text("人设", encoding="utf-8")
    # 故意不放 lines.json
    from face.settings_panel import SettingsPanel
    manifest, err = SettingsPanel.validate_character_folder(d)
    assert manifest is None, "缺 lines.json 应该被拒绝"
    assert err is not None and "lines.json" in err


def test_import_rejects_empty_lines():
    """lines.json 存在但一句有效台词都没有（只有注释），也应拒绝。"""
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text(
        json.dumps({"id": "emptylines", "name": "空台词"}), encoding="utf-8")
    (d / "system_prompt.md").write_text("人设", encoding="utf-8")
    (d / "lines.json").write_text(json.dumps({"_说明": "只有注释"}), encoding="utf-8")
    from face.settings_panel import SettingsPanel
    manifest, err = SettingsPanel.validate_character_folder(d)
    assert manifest is None, "没有有效台词应该被拒绝"
    assert err is not None


def test_import_rejects_broken_lines_json():
    """lines.json 是坏 JSON 时应拒绝并提示，而不是让导入"成功"后角色消失。"""
    d = Path(tempfile.mkdtemp())
    (d / "manifest.json").write_text(
        json.dumps({"id": "badlines", "name": "坏台词"}), encoding="utf-8")
    (d / "system_prompt.md").write_text("人设", encoding="utf-8")
    (d / "lines.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
    from face.settings_panel import SettingsPanel
    manifest, err = SettingsPanel.validate_character_folder(d)
    assert manifest is None, "坏 lines.json 应该被拒绝"
    assert err is not None and "lines.json" in err


# ======================================================================
# 4.5 自定义立绘
# ======================================================================

def _make_manager_with_character(d: Path, src_character: str = "dog"):
    """搭一个只含单个角色副本的 CharacterManager（不需要 engine）。"""
    import shutil
    chars = d / "characters"
    shutil.copytree(ROOT / "characters" / src_character, chars / src_character)
    from soul.character_manager import CharacterManager
    cm = CharacterManager(d, engine=None, characters_dir=chars)
    cm.discover()
    cm.current = cm.available[0]
    return cm


def _make_png(path: Path) -> None:
    from PyQt6.QtGui import QPixmap, QColor
    pm = QPixmap(20, 20)
    pm.fill(QColor(200, 200, 255, 0))
    pm.save(str(path), "PNG")


def test_set_avatar_copies_png_and_updates_manifest():
    _get_qapp()
    d = Path(tempfile.mkdtemp())
    cm = _make_manager_with_character(d)
    png = d / "pet.png"
    _make_png(png)
    prof, msg = cm.set_avatar("dog", png)
    assert prof is not None, msg
    assert prof.avatar is not None and prof.avatar.name == "avatar.png"
    assert (d / "characters" / "dog" / "avatar.png").is_file()
    manifest = json.loads(
        (d / "characters" / "dog" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["avatar"] == "avatar.png"
    assert cm.current.avatar is not None  # 当前角色引用同步更新


def test_set_avatar_rejects_non_png():
    _get_qapp()
    d = Path(tempfile.mkdtemp())
    cm = _make_manager_with_character(d)
    bad = d / "pic.jpg"
    bad.write_text("not-an-image", encoding="utf-8")
    prof, msg = cm.set_avatar("dog", bad)
    assert prof is None and "PNG" in msg


def test_set_avatar_rejects_unknown_character_and_missing_file():
    _get_qapp()
    d = Path(tempfile.mkdtemp())
    cm = _make_manager_with_character(d)
    png = d / "pet.png"; _make_png(png)
    p1, _ = cm.set_avatar("ghost", png)
    assert p1 is None
    p2, _ = cm.set_avatar("dog", d / "nope.png")
    assert p2 is None


def test_pet_tab_has_avatar_button():
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    assert hasattr(panel, "avatar_btn")
    assert panel.avatar_btn.text().endswith("PNG 立绘…")


# ======================================================================
# 5. 未接线页面的占位按钮确实置灰
# ======================================================================

def test_future_features_disabled():
    """P4 语音试听、P6 浏览器配对均已开放；未开始监听时断开按钮置灰。"""
    _get_qapp()
    d = temp_dir(); make_project(d)
    panel = _panel(_controller(d))
    assert panel.voice_test_btn.isEnabled()       # P4：试听已开放
    assert panel.pair_btn.isEnabled()             # P6：生成配对码已开放
    assert not panel.unpair_btn.isEnabled()       # 还没开始监听，断开置灰


# ======================================================================
# 6. 关闭窗口不能把桌宠一起带走（回归守护）
# ======================================================================
# 背景：曾经出过一个真实 bug——关掉设置面板，桌宠本体跟着消失，用户以为
# 程序挂了只能重启。原因是 Qt 默认"最后一个窗口关闭就退出程序"，而宠物窗口
# 是 Tool 类型（无任务栏图标），Qt 不把它算进"应用窗口"计数，所以设置面板
# 一关就被判定为"窗口全关了"→ 整个应用退出。
# 修复在 AppController.configure_app_lifecycle；下面这条测试守着它——
# 上一次重写 test_settings.py 时，守护测试曾被覆盖丢失过。

def test_app_lifecycle_disables_quit_on_last_window():
    """应用级开关必须关掉，否则任何面板一关桌宠就跟着退。"""
    app = _get_qapp()
    from app.controller import AppController
    AppController.configure_app_lifecycle(app)
    assert app.quitOnLastWindowClosed() is False, \
        "quitOnLastWindowClosed 必须是 False，否则关设置面板会连带退出桌宠"


# 注：这里不再放"关面板后宠物还活着"的行为测试——变异验证证明它在离屏环境下
# 抓不到 bug：panel.close() 不会真正触发 Qt 的 lastWindowClosed → app.quit()
# （需要真实事件循环和真实窗口），断言会恒成立。上面那条开关断言才是真守护，
# 端到端的"关设置不退出"已由冒烟启动 + 手工验证覆盖。


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
