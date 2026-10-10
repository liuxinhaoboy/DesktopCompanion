# -*- coding: utf-8 -*-
"""
配置服务单元测试
================
运行：python tests/test_config.py

测试范围：
  1. ConfigStore：默认值合并、类型校验、原子保存、_说明保留、脱敏视图、错误处理
  2. SecretsManager：加密往返、迁移引用、明文回退、resolve 逻辑
  3. AppController：密钥迁移后 runtime_cfg 是明文、热更新替换 LLM 客户端
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.store import ConfigStore, ConfigError, DEFAULT_CONFIG  # noqa: E402
from config.secrets import SecretsManager, SECRET_PREFIX            # noqa: E402
from PyQt6.QtWidgets import QApplication                             # noqa: E402


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_cfg_"))


def write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ======================================================================
# 1. ConfigStore
# ======================================================================

def test_defaults_merged_for_missing_fields():
    """用户 config 缺字段时自动补默认值。"""
    d = temp_dir()
    write_config(d / "config.json", {"llm": {"api_key": "sk-test", "model": "gpt-5.5"}})
    store = ConfigStore(d / "config.json")
    cfg = store.load()
    # 用户配了的保留
    assert cfg["llm"]["api_key"] == "sk-test"
    # 没配的补默认
    assert cfg["llm"]["base_url"] == DEFAULT_CONFIG["llm"]["base_url"]
    assert cfg["llm"]["timeout_seconds"] == 90
    assert cfg["chaos"]["global_cooldown_seconds"] == 180
    assert cfg["emotion"]["mood_decay_per_minute"] == 0.5


def test_config_template_matches_runtime_defaults():
    """无密钥模板必须完整同步默认配置，避免老模板漏掉新配置项。"""
    template = json.loads((ROOT / "config_template.json").read_text(encoding="utf-8"))
    template = {k: v for k, v in template.items() if not k.startswith("_")}
    assert template == DEFAULT_CONFIG, "config_template.json 与 DEFAULT_CONFIG 已漂移"
    assert template["llm"]["api_key"] == "", "模板不应带示例/占位 API key"


def test_first_run_bootstraps_config_from_template():
    """缺少 config.json 时从旁边的模板自动生成可用配置。"""
    d = temp_dir()
    (d / "config_template.json").write_text(
        (ROOT / "config_template.json").read_text(encoding="utf-8"), encoding="utf-8")
    store = ConfigStore(d / "config.json")
    cfg = store.load()
    assert cfg == DEFAULT_CONFIG
    assert cfg["llm"]["api_key"] == ""
    assert (d / "config.json").is_file(), "首次启动应落盘生成 config.json"
    saved = json.loads((d / "config.json").read_text(encoding="utf-8"))
    assert saved["_说明"].startswith("无密钥模板。首次运行")


def test_type_mismatch_falls_back_to_default():
    """字段类型写错时用默认值并记警告，不崩。"""
    d = temp_dir()
    write_config(d / "config.json", {
        "llm": {"api_key": "sk-test", "timeout_seconds": "九十"},  # 应该是数字
        "chaos": {"enabled": "yes"},                                # 应该是 bool
        "emotion": {"mood_decay_per_minute": [0.5]},                # 应该是数字
    })
    store = ConfigStore(d / "config.json")
    cfg = store.load()
    assert cfg["llm"]["timeout_seconds"] == 90, "字符串应回退默认值"
    assert cfg["chaos"]["enabled"] is True, "字符串应回退默认值"
    assert cfg["emotion"]["mood_decay_per_minute"] == 0.5
    assert len(store.warnings) >= 3, f"应记录3条警告，实际 {len(store.warnings)}: {store.warnings}"


def test_save_and_reload_roundtrip():
    """保存后重新读取内容一致（原子保存不丢数据）。"""
    d = temp_dir()
    p = d / "config.json"
    write_config(p, {"llm": {"api_key": "sk-abc", "model": "gpt-5.5"}})
    store = ConfigStore(p)
    cfg = store.load()
    cfg["llm"]["model"] = "gpt-5.6-terra"
    cfg["ui"]["bubble_duration_ms"] = 8000
    store.save()

    store2 = ConfigStore(p)
    cfg2 = store2.load()
    assert cfg2["llm"]["model"] == "gpt-5.6-terra"
    assert cfg2["ui"]["bubble_duration_ms"] == 8000
    # 默认值也还在
    assert cfg2["llm"]["base_url"] == DEFAULT_CONFIG["llm"]["base_url"]


def test_comments_preserved_on_save():
    """_说明 字段在保存后保留（用户手改配置时看的注释不能丢）。"""
    d = temp_dir()
    p = d / "config.json"
    write_config(p, {"_说明": "这是配置文件", "llm": {"api_key": "sk-x"}})
    store = ConfigStore(p)
    store.load()
    store.save()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw.get("_说明") == "这是配置文件"


def test_masked_view_hides_api_key():
    """脱敏视图只显 key 末4位。"""
    d = temp_dir()
    write_config(d / "config.json", {"llm": {"api_key": "sk-1234567890abcdef"}})
    store = ConfigStore(d / "config.json")
    store.load()
    view = store.masked_view()
    masked = view["llm"]["api_key"]
    assert masked.endswith("cdef"), "应显示末4位"
    assert "123456" not in masked, "前面的字符应被遮蔽"
    assert masked != "sk-1234567890abcdef", "不能是完整 key"


def test_masked_view_keeps_secret_ref():
    """已经是 secret: 引用的 key 不做遮蔽（引用名不是密钥）。"""
    d = temp_dir()
    write_config(d / "config.json", {"llm": {"api_key": "secret:llm_api_key"}})
    store = ConfigStore(d / "config.json")
    store.load()
    view = store.masked_view()
    assert view["llm"]["api_key"] == "secret:llm_api_key"


def test_masked_view_masks_every_api_key():
    """密钥不止 llm 一处：vision / voice 的密钥也必须一起脱敏。

    真实踩过的坑：masked_view 原来只处理 llm.api_key，而它的 docstring 写着
    "可安全打印/显示"——结果这个"安全"的方法本身成了泄露 vision/voice 密钥的通道。
    """
    d = temp_dir()
    write_config(d / "config.json", {
        "llm": {"api_key": "sk-llm-1234567890"},
        "vision": {"api_key": "sk-vision-abcdefgh"},
        "voice": {"stt_api_key": "sk-voice-zyxwvuts"},
    })
    store = ConfigStore(d / "config.json")
    store.load()
    view = store.masked_view()
    for section, field in (("llm", "api_key"), ("vision", "api_key"),
                           ("voice", "stt_api_key")):
        shown = view[section][field]
        assert shown.endswith(store.cfg[section][field][-4:]), \
            f"{section}.{field} 应保留末 4 位"
        assert shown.startswith("*"), f"{section}.{field} 应被遮蔽，实际 {shown!r}"
        assert store.cfg[section][field] not in shown, \
            f"{section}.{field} 不能出现完整密钥"
    # 原配置不能被改动
    assert store.cfg["vision"]["api_key"] == "sk-vision-abcdefgh"


def test_masked_view_keeps_all_secret_refs():
    """所有段的 secret: 引用都原样保留（引用名不是密钥）。"""
    d = temp_dir()
    write_config(d / "config.json", {
        "llm": {"api_key": "secret:llm_api_key"},
        "vision": {"api_key": "secret:vision_api_key"},
        "voice": {"stt_api_key": "secret:stt_api_key"},
    })
    store = ConfigStore(d / "config.json")
    store.load()
    view = store.masked_view()
    assert view["llm"]["api_key"] == "secret:llm_api_key"
    assert view["vision"]["api_key"] == "secret:vision_api_key"
    assert view["voice"]["stt_api_key"] == "secret:stt_api_key"


def test_missing_file_raises_config_error():
    """config.json 不存在时抛 ConfigError（main.py 捕获后说人话退出）。"""
    d = temp_dir()
    store = ConfigStore(d / "nonexistent.json")
    try:
        store.load()
        assert False, "应该抛 ConfigError"
    except ConfigError as e:
        assert "不存在" in str(e)


def test_bad_json_raises_config_error():
    """JSON 格式错误时抛 ConfigError。"""
    d = temp_dir()
    p = d / "config.json"
    p.write_text("{这不是合法json", encoding="utf-8")
    store = ConfigStore(p)
    try:
        store.load()
        assert False, "应该抛 ConfigError"
    except ConfigError as e:
        assert "格式写错" in str(e)


def test_update_section():
    """update_section 正确合并新值。"""
    d = temp_dir()
    write_config(d / "config.json", {"llm": {"api_key": "sk-x", "model": "gpt-5.5"}})
    store = ConfigStore(d / "config.json")
    store.load()
    store.update_section("ui", {"bubble_duration_ms": 9000})
    assert store.cfg["ui"]["bubble_duration_ms"] == 9000
    assert store.cfg["ui"]["typing_speed_ms"] == 28  # 默认值还在


# ======================================================================
# 2. SecretsManager
# ======================================================================

def test_secret_encrypt_decrypt_roundtrip():
    """加密→解密往返一致（DPAPI 绑定当前用户）。"""
    sm = SecretsManager(temp_dir() / "secrets.bin")
    if not sm.available:
        return  # 没 pywin32 的环境跳过（不报错）
    test_key = "sk-test-abcdef-1234567890"
    ref = sm.store("llm_api_key", test_key)
    assert ref.startswith(SECRET_PREFIX), f"应返回引用名，实际 {ref}"
    resolved = sm.resolve(ref)
    assert resolved == test_key, f"往返不一致: {resolved}"


def test_secret_migration_updates_config():
    """迁移后 config 里的 key 变成 secret: 引用，secrets.bin 存在。"""
    d = temp_dir()
    (d / "data").mkdir()
    write_config(d / "config.json", {"llm": {"api_key": "sk-plaintext-key-1234"}})
    store = ConfigStore(d / "config.json")
    store.load()
    sm = SecretsManager(d / "data" / "secrets.bin")
    if not sm.available:
        return
    migrated = sm.migrate_config_key(store)
    assert migrated is True
    assert store.cfg["llm"]["api_key"].startswith(SECRET_PREFIX)
    # secrets.bin 文件已写入
    assert (d / "data" / "secrets.bin").exists()
    # 重新加载 config 文件，确认引用已持久化
    store2 = ConfigStore(d / "config.json")
    cfg2 = store2.load()
    assert cfg2["llm"]["api_key"].startswith(SECRET_PREFIX)
    # resolve 能拿回明文
    assert sm.resolve(cfg2["llm"]["api_key"]) == "sk-plaintext-key-1234"


def test_secret_already_migrated_no_rewrite():
    """已经是 secret: 引用的配置不重复迁移。"""
    d = temp_dir()
    write_config(d / "config.json", {"llm": {"api_key": "secret:llm_api_key"}})
    store = ConfigStore(d / "config.json")
    store.load()
    sm = SecretsManager(d / "secrets.bin")
    if not sm.available:
        return
    result = sm.migrate_config_key(store)
    assert result is True  # 已是引用，返回 True 表示"已加密状态"
    assert store.cfg["llm"]["api_key"] == "secret:llm_api_key"


def test_secret_empty_key_not_migrated():
    """空 key 不迁移。"""
    d = temp_dir()
    write_config(d / "config.json", {"llm": {"api_key": ""}})
    store = ConfigStore(d / "config.json")
    store.load()
    sm = SecretsManager(d / "secrets.bin")
    result = sm.migrate_config_key(store)
    assert result is False


def test_secret_resolve_plaintext_passthrough():
    """resolve 普通字符串原样返回（兼容未迁移的明文配置）。"""
    sm = SecretsManager(temp_dir() / "secrets.bin")
    assert sm.resolve("sk-plaintext") == "sk-plaintext"
    assert sm.resolve("") == ""


def test_secret_resolve_missing_ref_returns_empty():
    """引用了不存在的密钥名返回空串，不崩。"""
    sm = SecretsManager(temp_dir() / "secrets.bin")
    if not sm.available:
        return
    result = sm.resolve("secret:nonexistent_key")
    assert result == ""


def test_secret_fallback_when_dpapi_unavailable():
    """模拟 DPAPI 不可用时，store 返回明文、resolve 透传。"""
    sm = SecretsManager(temp_dir() / "secrets.bin")
    sm._dpapi_available = False   # 强制模拟不可用
    ref = sm.store("llm_api_key", "sk-test")
    assert ref == "sk-test", "DPAPI 不可用时应返回明文"
    assert sm.resolve("sk-test") == "sk-test"


# ======================================================================
# 3. AppController（需要 Qt 环境）
# ======================================================================

_qapp = None


def _make_qapp():
    """获取或创建 QApplication（测试用，不 run_forever）。
    必须把实例存到全局变量，否则 Python 会回收它导致 QWidget 崩溃。"""
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def test_controller_migrates_key_and_runtime_cfg_has_plaintext():
    """AppController 初始化时自动迁移密钥，runtime_cfg 里的 key 是明文。"""
    _make_qapp()
    d = temp_dir()
    (d / "data").mkdir()
    # 写一个最小 config（缺的字段 ConfigStore 会补默认值）
    write_config(d / "config.json", {
        "llm": {"api_key": "sk-controller-test-key", "model": "gpt-5.5"},
        "memory": {"use_chroma": False},
    })
    # character_core.json 和 master_profile.json 是 SoulEngine 启动需要的
    (d / "data").mkdir(exist_ok=True)
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "测试灵", "system_prompt": "测试。",
        "greeting_lines": ["测试~"],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "data" / "master_profile.json").write_text("{}", encoding="utf-8")

    from app.controller import AppController
    controller = AppController(d)

    # runtime_cfg 里的 key 应该是明文（给 LLMClient 用的）
    runtime_key = controller._runtime_cfg["llm"]["api_key"]
    assert runtime_key == "sk-controller-test-key", \
        f"runtime_cfg 应含明文 key，实际: {runtime_key[:10]}..."

    # config_store 里的 key 应该已经迁移成引用（如果 DPAPI 可用）
    stored_key = controller.config_store.cfg["llm"]["api_key"]
    if controller.secrets.available:
        assert stored_key.startswith(SECRET_PREFIX), \
            f"config_store 里应是引用，实际: {stored_key[:10]}..."
    else:
        assert stored_key == "sk-controller-test-key"


def test_apply_config_replaces_llm_client():
    """热更新后旧 LLM 客户端被替换，新客户端用新配置。"""
    _make_qapp()
    d = temp_dir()
    (d / "data").mkdir(parents=True, exist_ok=True)
    write_config(d / "config.json", {
        "llm": {"api_key": "sk-old-key", "model": "gpt-5.5"},
        "memory": {"use_chroma": False},
    })
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "测试灵", "system_prompt": "测试。",
        "greeting_lines": ["测试~"],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "data" / "master_profile.json").write_text("{}", encoding="utf-8")

    from app.controller import AppController
    controller = AppController(d)
    old_llm = controller.engine.llm
    old_model = old_llm.model

    # 模拟设置面板改了模型
    ok, msg = controller.apply_config({"llm": {"model": "gpt-5.6-sol"}})
    assert ok, f"热更新应成功: {msg}"
    assert controller.engine.llm is not old_llm, "旧 LLM 客户端应被替换"
    assert controller.engine.llm.model == "gpt-5.6-sol"
    assert old_model == "gpt-5.5"


def test_apply_config_blocked_while_streaming():
    """流式回复中禁止热更新。"""
    _make_qapp()
    d = temp_dir()
    (d / "data").mkdir(parents=True, exist_ok=True)
    write_config(d / "config.json", {
        "llm": {"api_key": "sk-x", "model": "gpt-5.5"},
        "memory": {"use_chroma": False},
    })
    (d / "data" / "character_core.json").write_text(json.dumps({
        "name": "测试灵", "system_prompt": "测试。",
        "greeting_lines": ["测试~"],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "data" / "master_profile.json").write_text("{}", encoding="utf-8")

    from app.controller import AppController
    controller = AppController(d)

    # 模拟正在流式回复
    controller._panel_holder.append(type("StubPanel", (), {"_streaming": True, "isVisible": lambda self: True})())
    ok, msg = controller.apply_config({"llm": {"model": "gpt-5.6-sol"}})
    assert not ok, "流式中应拒绝热更新"
    assert "说话" in msg or "回合" in msg


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
