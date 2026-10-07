# -*- coding: utf-8 -*-
"""
ConfigStore —— 配置文件的统一读写入口
=====================================
为什么要有这一层：
  以前 main.py 直接 json.loads(config.json)，字段缺了/类型错了
  要等到深层模块（LLMClient/CESM）用到时才炸，报错离根因很远。
  ConfigStore 负责：
    1. 读取 config.json
    2. 和 DEFAULT_CONFIG 递归合并（缺字段自动补默认值）
    3. 类型校验（类型错了用默认值 + 记警告，不崩）
    4. 原子保存（临时文件 + os.replace，写一半断电不毁文件）

设计约束：
  - _说明 字段是给用户看的中文注释，加载时跳过、保存时保留
  - API key 永远不进日志/报错；需要展示时用 masked_view() 只显末4位
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any


# ----------------------------------------------------------------------
# 默认配置：和 config_template.json 保持同步（不含 _说明 字段）。
# 新加配置项时在这里加默认值，老用户的 config.json 缺了也能自动补。
# ----------------------------------------------------------------------
DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "base_url": "https://docode.cc/v1",
        "api_key": "",
        "model": "gpt-5.5",
        "backup_models": ["gpt-5.6-terra", "gpt-5.6-sol"],
        "timeout_seconds": 90,
        "max_tokens": 800,
    },
    # P3 新增：视觉模型（图片多模态用）。默认继承文本 API，
    # 用户可以在设置面板里单独指定地址/密钥。P4 才真正接线，这里先存配置。
    "vision": {
        "enabled": False,
        "inherit_from_llm": True,       # True=用文本API的地址和密钥
        "base_url": "",
        "api_key": "",
        "model": "",
        "max_image_kb": 500,            # 发送前压缩到这个大小以内
    },
    # P3 新增：语音配置。P4 接线，现在只存设置项 + 设置面板可编辑。
    "voice": {
        "enabled": False,
        "record_device": "default",
        "max_record_seconds": 60,
        "stt_provider": "openai_compatible",
        "stt_base_url": "",
        "stt_api_key": "",
        "stt_model": "",
        "tts_provider": "edge-tts",      # edge-tts 免费无需key
        "tts_voice": "zh-CN-XiaoxiaoNeural",
        "auto_speak": False,             # 回复是否自动朗读
    },
    # P3 新增：浏览器插件配置。P6 接线，现在只存配对状态。
    "browser": {
        "enabled": False,
        "paired": False,
        "pair_token": "",                # 一次性配对码（展示用，配对成功后清空）
        "port": 8731,                    # 本机桥监听端口（只绑 127.0.0.1）
        "allow_read": True,              # 读网页正文（进模型前会确认）
        "allow_navigate": True,          # 打开网址
        "allow_click": True,             # 点击元素
        "allow_type": True,              # 在输入框填字（密码/验证码永禁）
        "allow_scroll": True,            # 滚动页面
        "allow_close_tab": False,        # 关标签页（较敏感，默认关）
    },
    # P3 新增：隐私设置。
    "privacy": {
        "save_audio": False,             # 是否保留录音文件（默认转写完即删）
        "log_retention_days": 30,        # 日志保留天数
    },
    "character": {
        "name": "小灵",
        "current_id": "xiaoling",   # P2：上次使用的角色id，启动时自动加载
        "size": 160,
        "opacity": 1.0,
    },
    "window": {
        "always_on_top": True,
        "start_position": "bottom_right",
        "click_through_when_idle": False,
    },
    "emotion": {
        "mood_decay_per_minute": 0.5,
        "time_decay_floor": 20,
        "affinity_diminish_power": 0.5,
        "low_mood_threshold": 30,
        "angry_mood_threshold": 20,
        "trust_gate_for_danger_ops": 30,
    },
    "memory": {
        "use_chroma": True,
        "recall_top_k": 3,
    },
    "chaos": {
        "enabled": True,
        "enable_L1": True,
        "enable_L2": True,
        "enable_L3": True,
        "L3_require_confirm": True,
        "tick_seconds": 3,
        "global_cooldown_seconds": 180,
        "ignore_cooldown_when_angry": True,
    },
    "sandbox": {
        "workspace_root": "",
        "enable_rollback_snapshot": True,
        # P3 新增：电脑控制能力开关。这里只是"允许她做什么"的声明，
        # 真正的执行逻辑在 P5 的 hands/ 模块里，到时会读这些开关。
        "allow_files": True,
        "allow_processes": False,
        "allow_clipboard": True,
        "allow_windows": True,
        "confirm_policy": "always",      # always=每次都问 / smart=高危才问
        "process_whitelist": [],          # 允许她启动/关闭的程序名
    },
    "sleep": {
        "sleep_start_hour": 23,
        "sleep_end_hour": 7,
        "enabled": True,
    },
    "ui": {
        "bubble_duration_ms": 6000,
        "typing_speed_ms": 28,
        "show_debug_hud": False,
    },
}


class ConfigError(Exception):
    """配置文件无法读取/解析时抛出（main.py 捕获后说人话）。"""


class ConfigStore:
    """配置读写器。一个进程只建一个，AppController 持有它。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._cfg: dict[str, Any] = {}
        self._comments: dict[str, Any] = {}   # _说明 字段，保存时原样写回
        self.warnings: list[str] = []          # 类型校验警告（设置面板可以显示）

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读取 config.json 并合并默认值。文件不存在/格式错抛 ConfigError。"""
        if not self.path.exists():
            raise ConfigError(
                "config.json 不存在！请把 config_template.json 复制一份并填入你的 API key。")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(
                f"config.json 格式写错了（通常是少了逗号或引号）：{e}") from e
        if not isinstance(raw, dict):
            raise ConfigError("config.json 顶层必须是 JSON 对象（花括号包裹）。")

        # 摘出 _说明 字段单独保存，不参与合并和校验
        self._comments = {k: v for k, v in raw.items() if k.startswith("_")}
        user_cfg = {k: v for k, v in raw.items() if not k.startswith("_")}

        self.warnings.clear()
        self._cfg = self._merge_with_defaults(copy.deepcopy(DEFAULT_CONFIG), user_cfg)
        return self._cfg

    def _merge_with_defaults(self, defaults: dict, user: dict,
                             path: str = "") -> dict:
        """递归合并：用户值覆盖默认值；缺的补默认；类型错的用默认+记警告。"""
        for key, default_val in defaults.items():
            full_key = f"{path}.{key}" if path else key
            if key not in user:
                # 用户没配这个字段，保留默认值
                continue
            user_val = user[key]
            if isinstance(default_val, dict):
                if not isinstance(user_val, dict):
                    self.warnings.append(
                        f"配置项 [{full_key}] 应该是对象，实际是 {type(user_val).__name__}，已用默认值")
                    user[key] = default_val
                else:
                    user[key] = self._merge_with_defaults(
                        default_val, user_val, full_key)
            elif isinstance(default_val, list):
                if not isinstance(user_val, list):
                    self.warnings.append(
                        f"配置项 [{full_key}] 应该是数组，实际是 {type(user_val).__name__}，已用默认值")
                    user[key] = default_val
                # 列表不做元素级校验：长度/类型由使用方自己兜底
            else:
                # 标量：检查类型是否和默认值一致（bool 是 int 的子类，要先判 bool）
                if default_val is not None and user_val is not None:
                    if type(default_val) is not type(user_val):
                        self.warnings.append(
                            f"配置项 [{full_key}] 应该是 {type(default_val).__name__}，"
                            f"实际是 {type(user_val).__name__}，已用默认值")
                        user[key] = default_val
        # 把默认值里有、用户没配的字段补进去
        for key, default_val in defaults.items():
            if key not in user:
                user[key] = default_val
        return user

    # ------------------------------------------------------------------
    # 访问
    # ------------------------------------------------------------------
    @property
    def cfg(self) -> dict[str, Any]:
        """当前配置（已合并默认值）。首次访问时自动 load。"""
        if not self._cfg:
            self.load()
        return self._cfg

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        """便捷取值：get('llm','model') 或 get('chaos')。"""
        sect = self.cfg.get(section, {})
        if key is None:
            return sect if isinstance(sect, dict) else default
        return sect.get(key, default) if isinstance(sect, dict) else default

    def update_section(self, section: str, values: dict) -> None:
        """更新某个配置段（设置面板保存时用）。"""
        if section not in self._cfg or not isinstance(self._cfg[section], dict):
            self._cfg[section] = {}
        self._cfg[section].update(values)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def save(self) -> None:
        """原子保存：先写临时文件再 os.replace，写一半断电也不毁原文件。"""
        to_save = {}
        # _说明 字段写在最前面（用户打开文件第一眼看到注释）
        to_save.update(self._comments)
        to_save.update(self._cfg)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)   # Windows 上是原子替换
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 脱敏视图（给设置面板/日志用）
    # ------------------------------------------------------------------
    def masked_view(self) -> dict[str, Any]:
        """返回配置的深拷贝，所有 API key 只显末 4 位。可安全打印/显示。

        为什么要把所有段都扫一遍：密钥不止 llm 一处——vision.api_key 和
        voice.stt_api_key 同样是明文密钥。只脱敏 llm 的话，这个"可以安全打印"
        的方法本身就成了泄露通道（日志、报错、调试输出都爱用它）。
        """
        view = copy.deepcopy(self.cfg)
        for section in view.values():
            if not isinstance(section, dict):
                continue
            for field, value in list(section.items()):
                if not field.endswith("api_key") or not value:
                    continue
                text = str(value)
                if text.startswith("secret:"):
                    continue          # 引用名不算密钥，原样显示
                section[field] = ("*" * max(0, len(text) - 4) + text[-4:]
                                  if len(text) > 4 else "*" * len(text))
        return view


# ----------------------------------------------------------------------
# 使用示例：python -m config.store
# ----------------------------------------------------------------------
if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    store = ConfigStore(root / "config.json")
    cfg = store.load()
    print("配置加载成功。")
    for w in store.warnings:
        print(f"  [警告] {w}")
    print(f"模型: {cfg['llm']['model']}")
    print(f"Key (脱敏): {store.masked_view()['llm']['api_key']}")
