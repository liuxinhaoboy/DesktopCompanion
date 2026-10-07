# -*- coding: utf-8 -*-
"""
AppController —— 应用生命周期总管
================================
以前 main.py 里散着创建 SoulEngine、PetWindow、ChatPanel、ACL 的代码，
设置面板要热更新时不知道该找谁。AppController 把这些对象统一持有：

    controller = AppController(root)
    controller.start(app)        # 启动 ACL + 开场白
    controller.apply_config(...) # 设置面板保存后热更新
    controller.switch_character(id)  # 切换伙伴
    controller.stop()            # 退出时清理

热更新范围（apply_config）：
  - LLM 客户端：换模型/换 key/换地址后立即生效
  - 情绪旋钮：mood_decay 等参数热更新
  - ACL：重启循环（新概率/新冷却/开关）
  - 外观：透明度立即生效（尺寸变更需重启，会提示）
  - 正在流式回复时禁止热更新（避免中途换客户端导致乱序）
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from config.store import ConfigStore, ConfigError
from config.secrets import SecretsManager


class AppController:
    def __init__(self, root: Path):
        self.root = Path(root)

        # ---- 配置层 ----
        self.config_store = ConfigStore(self.root / "config.json")
        raw_cfg = self.config_store.load()
        for w in self.config_store.warnings:
            print(f"[Config] {w}")

        self.secrets = SecretsManager(self.root / "data" / "secrets.bin")
        # 自动迁移明文 key 到 DPAPI 加密（失败自动回退明文，不影响启动）
        self.secrets.migrate_config_key(self.config_store)

        # runtime_cfg 是传给各模块的配置：key 已解析为明文，
        # config_store 里存的可能是 "secret:xxx" 引用，两者分离
        self._runtime_cfg: dict[str, Any] = {}
        self._refresh_runtime_cfg()

        # ---- 灵魂层 ----
        from soul.engine import SoulEngine
        self.engine = SoulEngine(self.root, self._runtime_cfg)

        # ---- P2：角色管理器（必须在 PetWindow 之前，pet 要读当前角色外观）----
        from soul.character_manager import CharacterManager
        self.characters = CharacterManager(self.root, self.engine)
        self.characters.discover()
        last_id = self._runtime_cfg.get("character", {}).get("current_id", "xiaoling")
        initial = self.characters.load_initial(last_id)
        # 把可用角色列表挂到 engine 上，PetWindow 右键菜单从这里读
        self.engine.available_characters = self.characters.available
        print(f"[Character] 当前伙伴：{initial.name}（{initial.id}），"
              f"共 {len(self.characters.available)} 个角色")

        # ---- B 商城经济 ----
        # 必须在角色加载之后建：换角色会重建 CESM（每个角色的钱包独立），
        # 经济系统是挂在 CESM 的 state 上的。
        from shop import Catalog, Economy
        self.catalog = Catalog.load(self.root / "shop" / "catalog.json")
        self.economy = Economy(self.engine.cesm, self.catalog)
        self.engine.economy = self.economy
        if self.catalog.warnings:
            for w in self.catalog.warnings:
                print(f"[Shop] {w}")

        # ---- P4：语音服务（录音/转写/朗读）+ 视觉配置注入 ----
        from voice.service import VoiceService
        self.voice = VoiceService(self._runtime_cfg.get("voice", {}),
                                  self._runtime_cfg.get("llm", {}))
        self.engine.setup_vision(self._runtime_cfg.get("vision", {}))

        # ---- 界面层 ----
        from face.pet_window import PetWindow
        self.pet = PetWindow(self._runtime_cfg, self.engine)
        self._panel_holder: list = []   # ChatPanel 单例（打开时创建）
        self._settings_holder: list = []  # P3：SettingsPanel 单例

        # ---- P5：电脑控制（必须在 self.pet 创建后，确认卡以宠物窗为父）----
        from hands.executor import HandsExecutor
        from hands.planner import ToolPlanner
        self.hands = HandsExecutor(self.root,
                                   self._runtime_cfg.get("sandbox", {}),
                                   parent=self.pet)
        self.engine.hands = self.hands
        self.engine.planner = ToolPlanner(
            lambda: self._runtime_cfg.get("sandbox", {}),
            lambda: str(self._runtime_cfg.get("sandbox", {}).get("workspace_root", "")))

        # ---- P6：浏览器本机桥（只监听 127.0.0.1）+ 浏览器动作策略 ----
        from browser.bridge import BrowserBridge
        from browser.policy import BrowserPolicy
        self.browser_bridge = BrowserBridge(
            lambda: self._runtime_cfg.get("browser", {}),
            on_state=self._on_browser_state)
        self.browser_policy = BrowserPolicy(self._runtime_cfg.get("browser", {}))
        self.engine.browser_bridge = self.browser_bridge
        self._browser_state_listeners: list = []

        # ---- A 昼夜作息：睡/醒、活动度、衰减系数都由这里统一驱动 ----
        # 为什么要一个独立的钟：捣乱循环（ACL）可以在设置里整个关掉，
        # 但"她该睡觉了"这件事跟捣乱无关，关了捣乱她也得照常睡。
        from brain.circadian import Circadian
        self.circadian = Circadian(self._runtime_cfg.get("sleep", {}),
                                   self._load_schedule_cfg())
        self._circadian_timer = None

        # ---- ACL ----
        self._acl_holder: list = []
        self._acl_task = None

    # ------------------------------------------------------------------
    # A 昼夜作息
    # ------------------------------------------------------------------
    def _load_schedule_cfg(self) -> dict:
        """读触发矩阵的 作息 段（活动系数 / 早安晚安时段）。读不到就当没配。"""
        import json
        try:
            raw = json.loads((self.root / "brain" / "chaos_trigger_matrix.json")
                             .read_text(encoding="utf-8"))
            sched = raw.get("作息")
            return sched if isinstance(sched, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _start_circadian(self) -> None:
        """启动昼夜钟：立即校准一次，之后每分钟校准一次。"""
        from PyQt6.QtCore import QTimer
        self._circadian_timer = QTimer()
        self._circadian_timer.setInterval(60_000)
        self._circadian_timer.timeout.connect(self._tick_circadian)
        self._circadian_timer.start()
        self._tick_circadian()

    def _tick_circadian(self) -> None:
        """按当前小时校准：睡态、开场白时段、情绪衰减速度、困倦前缀。"""
        from datetime import datetime
        try:
            hour = datetime.now().hour
            sleeping = self.circadian.is_sleeping(hour)
            self.pet.set_sleeping(sleeping)
            self.pet.set_greet_period(self.circadian.period(hour))
            self.engine.cesm.set_decay_factor(self.circadian.decay_factor(hour))
            self.engine.drowsy = sleeping
        except Exception as e:  # noqa: BLE001 —— 作息出错绝不能带崩整个程序
            print(f"[Circadian] 作息校准失败（忽略）：{e}")

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def _refresh_runtime_cfg(self) -> None:
        """从 config_store 取配置，解析密钥引用为明文，生成运行时副本。"""
        cfg = copy.deepcopy(self.config_store.cfg)
        # 文本模型
        cfg["llm"]["api_key"] = self.secrets.resolve(
            cfg.get("llm", {}).get("api_key", ""))
        # 视觉模型独立密钥（P4 多模态用）
        vision = cfg.setdefault("vision", {})
        if vision.get("api_key"):
            vision["api_key"] = self.secrets.resolve(vision["api_key"])
        # 语音转写密钥（P4 用）
        voice = cfg.setdefault("voice", {})
        if voice.get("stt_api_key"):
            voice["stt_api_key"] = self.secrets.resolve(voice["stt_api_key"])
        self._runtime_cfg = cfg

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    @staticmethod
    def configure_app_lifecycle(app) -> None:
        """设置应用级生命周期开关（与 start 拆开，方便单测断言）。

        ★ 关掉"最后一个可见窗口关闭就退出程序"的默认行为。
        原因：宠物窗口是 Tool 类型（无任务栏、置顶），Qt 把它排除在
        "应用窗口"计数之外——设置面板/聊天面板一关，Qt 就误以为"窗口全关了"，
        自动退出把桌宠一起带走。桌宠是常驻应用，退出只有右键菜单"退出"一条路。
        """
        app.setQuitOnLastWindowClosed(False)

    def start(self, app) -> None:
        """在 QApplication 创建后调用：连信号、显示宠物、启动 ACL、开场白。"""
        self.configure_app_lifecycle(app)
        self.pet.chat_requested.connect(self.open_chat)

        self.pet.character_switch_requested.connect(self.switch_character)
        self.pet.settings_requested.connect(self.open_settings)
        self.pet.clipboard_restore_requested.connect(self.restore_clipboard)
        self.pet.show()
        # 先校准一次作息（决定开场白是早安/晚安/还是她在睡觉），再让她开口
        self._start_circadian()

        app.aboutToQuit.connect(self.stop)

        loop = asyncio.get_event_loop()
        self._acl_task = loop.create_task(self._start_acl())
        loop.call_soon(lambda: asyncio.ensure_future(self._greet()))

    async def _greet(self) -> None:
        try:
            await self.pet.greet()
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[开场白失败，不影响运行] {e}")
            traceback.print_exc()

    # ------------------------------------------------------------------
    # P6：浏览器配对 / 状态
    # ------------------------------------------------------------------
    def _on_browser_state(self, state: str) -> None:
        """桥连接状态变化：落 config 并刷新开着的设置页。"""
        try:
            paired = state == "connected"
            self.config_store.update_section("browser", {"paired": paired})
        except Exception:  # noqa: BLE001
            pass
        for fn in list(getattr(self, "_browser_state_listeners", [])):
            try:
                fn(state)
            except Exception:  # noqa: BLE001
                pass

    async def browser_start_pairing(self) -> str:
        """生成配对码并开始监听，返回好读的配对码。"""
        return await self.browser_bridge.start_pairing()

    async def browser_unpair(self) -> None:
        await self.browser_bridge.unpair()
        self.config_store.update_section("browser", {"paired": False, "pair_token": ""})

    def stop(self) -> None:
        """退出时清理：停 ACL、释放语音、落盘。"""
        try:
            if getattr(self, "browser_bridge", None) is not None:
                import asyncio as _aio
                try:
                    _aio.get_event_loop().create_task(self.browser_bridge.shutdown())
                except Exception:  # noqa: BLE001 —— 事件循环已关时忽略
                    pass
            if getattr(self, "_circadian_timer", None) is not None:
                self._circadian_timer.stop()
            if getattr(self, "voice", None) is not None:
                self.voice.shutdown()
            if self._acl_holder:
                self._acl_holder[0].stop()
            self.engine.flush()
        except Exception as e:  # noqa: BLE001
            print(f"[退出保存失败] {e}")

    # ------------------------------------------------------------------
    # P2：角色切换
    # ------------------------------------------------------------------
    def switch_character(self, character_id: str) -> tuple[bool, str]:
        """右键菜单"换伙伴"的入口。流式回复中禁止切换。"""
        if self.is_busy():
            self.pet.say("正在说话呢……等这轮聊完再换嘛~")
            return False, "busy"

        ok, msg = self.characters.switch_to(character_id)
        if not ok:
            self.pet.say(msg)
            return False, msg

        # 界面层换外观
        profile = self.characters.current
        self.pet.apply_character(profile)
        # 钱包也跟着角色走：CESM 刚被重建，经济系统要重新挂上去
        self._rebind_economy()
        # 聊天面板标题刷新（如果开着）
        if self._panel_holder and self._panel_holder[0].isVisible():
            self._panel_holder[0].setWindowTitle(f"和 {profile.name} 聊天")
        # 记住选择，下次启动自动加载这个角色
        self.config_store.update_section("character", {"current_id": profile.id})
        try:
            self.config_store.save()
        except OSError as e:
            print(f"[Character] 角色选择保存失败（不影响切换）：{e}")

        # 换角色会重建 CESM（每个角色的情绪数值独立），新实例的衰减系数是默认 1.0。
        # 不在这里重新校准的话，深夜换伙伴后她会有一段（最长 60 秒）按"没在睡觉"
        # 的速度掉情绪和饱腹，作息就白做了。
        self._tick_circadian()

        self.pet.say(f"我是{profile.name}，以后请多关照啦~")
        return True, msg

    # ------------------------------------------------------------------
    # B 商城经济（UI ↔ engine 的中转；界面层不直接碰 engine）
    # ------------------------------------------------------------------
    def _rebind_economy(self) -> None:
        """把经济系统重新挂到当前的 CESM 上。

        换角色会重建 CESM（每个角色独立），旧的监听器留在被丢弃的实例上，
        这里必须重新挂一次，否则新角色的陪伴行为不再产生币。
        """
        from shop import Economy
        self.economy = Economy(self.engine.cesm, self.catalog)
        self.engine.economy = self.economy

    def wallet(self) -> dict:
        return {"coins": self.economy.balance(),
                "earned_today": self.economy.earned_today(),
                "streak_days": int(getattr(self.engine.cesm.state, "streak_days", 0))}

    def buy_item(self, item_id: str) -> tuple[bool, str]:
        ok, msg = self.economy.purchase(item_id)
        if ok:
            self.pet.update()      # 买了配饰/台词包，画面与台词立即生效
        return ok, msg

    def equip_item(self, item_id: str) -> tuple[bool, str]:
        ok, msg = self.economy.equip(item_id)
        if ok:
            self.pet.update()
        return ok, msg

    def feed_snack(self, item_id: str) -> tuple[bool, str]:
        ok, msg = self.economy.feed_snack(item_id)
        if ok:
            self.pet.say(msg)
        return ok, msg

    def restore_clipboard(self) -> None:
        """右键「恢复剪贴板」：先找捣乱循环留的快照，再找电脑控制留的。

        为什么要问两个地方：捣乱循环（ACL）按红线不能调用 hands/，
        所以它自己留一份快照；hands/ 的剪贴板操作另有自己的一份。
        用户不关心是谁换的，只想要回原来的内容。
        """
        actor = None
        if self._acl_holder:
            actor = getattr(self._acl_holder[0], "actor", None)
        if actor is not None and actor.restore_clipboard():
            self.pet.say("好啦，剪贴板还给你了~")
            return
        if getattr(self, "hands", None) is not None:
            res = self.hands.undo_last_clipboard()
            if getattr(res, "ok", False):
                self.pet.say("好啦，剪贴板恢复成上一条了~")
                return
        self.pet.say("唔……我这边没有可以恢复的剪贴板内容。")

    def set_character_avatar(self, character_id: str, png_path) -> tuple[bool, str]:
        """设置面板"选 PNG 立绘"入口：复制图片、重载角色，当前角色立即换外观。"""
        new_profile, msg = self.characters.set_avatar(character_id, png_path)
        if new_profile is None:
            return False, msg
        # 改的正是当前伙伴：立刻让渲染器换图，不用等重启
        if self.characters.current and self.characters.current.id == character_id:
            self.pet.apply_character(new_profile)
        return True, msg

    def set_character_model3d(self, character_id: str, model_path) -> tuple[bool, str]:
        """设置面板"上传 3D 模型"入口：复制 .glb/.gltf 进角色目录、
        登记 manifest、重载角色；改的是当前伙伴就立刻换渲染器（可能直接 3D 化）。"""
        new_profile, msg = self.characters.set_model3d(character_id, model_path)
        if new_profile is None:
            return False, msg
        if self.characters.current and self.characters.current.id == character_id:
            self.pet.apply_character(new_profile)
        return True, msg

    def remove_character_model3d(self, character_id: str) -> tuple[bool, str]:
        """移除某伙伴的 3D 模型（文件改名保留，回到 2D 形象）。"""
        new_profile, msg = self.characters.remove_model3d(character_id)
        if new_profile is None:
            return False, msg
        if self.characters.current and self.characters.current.id == character_id:
            self.pet.apply_character(new_profile)
        return True, msg

    def set_character_display(self, character_id: str, mode: str) -> tuple[bool, str]:
        """切换某伙伴的 2D/3D 显示模式（auto/2d/3d）。"""
        new_profile, msg = self.characters.set_display(character_id, mode)
        if new_profile is None:
            return False, msg
        if self.characters.current and self.characters.current.id == character_id:
            self.pet.apply_character(new_profile)
        return True, msg

    # ------------------------------------------------------------------
    # 聊天面板（按需创建单例）
    # ------------------------------------------------------------------
    def open_chat(self) -> None:
        from face.chat_panel import ChatPanel
        if not self._panel_holder or not self._panel_holder[0].isVisible():
            self._panel_holder.clear()
            panel = ChatPanel(self.engine, voice_service=self.voice)
            panel.setWindowTitle(f"和 {self.engine.name} 聊天")
            self._panel_holder.append(panel)
            self._panel_holder[0].show()
        else:
            self._panel_holder[0].raise_()
            self._panel_holder[0].activateWindow()

    # ------------------------------------------------------------------
    # P3：设置面板（按需创建单例）
    # ------------------------------------------------------------------
    def open_settings(self) -> None:
        """右键菜单"设置"的入口。

        为什么每次打开都重新读一遍配置：面板可能一直开着，期间用户从右键菜单
        换了角色、或者上一轮保存改了密钥引用。不刷新的话界面上显示的是旧值，
        用户以为没保存成功会重复操作。
        """
        from face.settings_panel import SettingsPanel
        if self._settings_holder and self._settings_holder[0].isVisible():
            panel = self._settings_holder[0]
            panel._load_current()
            panel.raise_()
            panel.activateWindow()
            return

        self._settings_holder.clear()
        panel = SettingsPanel(self)
        self._settings_holder.append(panel)
        panel.show()

    # ------------------------------------------------------------------
    # ACL 生命周期
    # ------------------------------------------------------------------
    async def _start_acl(self) -> None:
        chaos_cfg = self._runtime_cfg.get("chaos", {})
        if not chaos_cfg.get("enabled", False):
            return
        from brain.loop import AgentLoop
        loop_obj = AgentLoop(
            self.engine, self.pet, self.root, chaos_cfg,
            self.root / "brain" / "chaos_trigger_matrix.json",
            self.root / "brain" / "surprise_table.csv",
            tick_seconds=int(chaos_cfg.get("tick_seconds", 3)),
            sleep_cfg=self._runtime_cfg.get("sleep", {}),
        )
        self._acl_holder.clear()
        self._acl_holder.append(loop_obj)
        await loop_obj.run()

    def _restart_acl(self) -> None:
        """停掉旧 ACL 循环，启动新的（配置变了的时候用）。"""
        if self._acl_holder:
            self._acl_holder[0].stop()
            self._acl_holder.clear()
        if self._acl_task and not self._acl_task.done():
            self._acl_task.cancel()
        loop = asyncio.get_event_loop()
        self._acl_task = loop.create_task(self._start_acl())

    # ------------------------------------------------------------------
    # 热更新
    # ------------------------------------------------------------------
    def is_busy(self) -> bool:
        """是否正在流式回复/录音等不可中断的回合。"""
        if self._panel_holder and self._panel_holder[0].isVisible():
            return getattr(self._panel_holder[0], "_streaming", False)
        return False

    def apply_config(self, new_cfg: dict) -> tuple[bool, str]:
        """设置面板保存后调用：热更新所有受影响的模块。
        返回 (成功与否, 提示消息)。"""
        if self.is_busy():
            return False, "小灵正在说话，等这一轮结束后再应用设置吧。"

        try:
            # 1. 更新配置存储
            #    各段的密钥可能是明文（用户刚填的）或已是 secret: 引用。
            #    明文密钥统一走 DPAPI 加密，config.json 只留引用，避免明文落盘。
            key_fields = (("llm", "api_key", "llm_api_key"),
                          ("vision", "api_key", "vision_api_key"),
                          ("voice", "stt_api_key", "stt_api_key"))
            for section, field, secret_name in key_fields:
                sec = new_cfg.get(section)
                if not isinstance(sec, dict):
                    continue
                val = sec.get(field, "")
                if val and not str(val).startswith("secret:"):
                    sec[field] = self.secrets.store(secret_name, val)

            for section, values in new_cfg.items():
                if isinstance(values, dict):
                    self.config_store.update_section(section, values)
            self.config_store.save()

            # 2. 刷新运行时配置
            self._refresh_runtime_cfg()
            cfg = self._runtime_cfg

            # 3. 重建 LLM 客户端（换模型/key/地址立即生效）
            from soul.llm import LLMClient
            old_llm = self.engine.llm
            self.engine.llm = LLMClient(cfg.get("llm", {}), self.engine.cesm)
            del old_llm  # 显式释放旧客户端

            # 3.5 P4：语音服务与视觉配置热更新（视觉客户端作废，下次带图重建）
            if getattr(self, "voice", None) is not None:
                self.voice.update_config(cfg.get("voice", {}), cfg.get("llm", {}))
            self.engine.setup_vision(cfg.get("vision", {}))
            # P5：电脑控制策略热更新（工作目录/能力开关/白名单立即生效）
            if getattr(self, "hands", None) is not None:
                self.hands.update_config(cfg.get("sandbox", {}))
            # P6：浏览器能力开关热更新立即生效
            if getattr(self, "browser_policy", None) is not None:
                self.browser_policy.update(cfg.get("browser", {}))
            # A：作息改了立刻生效（不用重启，她当场睡着/醒来）
            if getattr(self, "circadian", None) is not None:
                self.circadian.update(cfg.get("sleep", {}), self._load_schedule_cfg())
                self._tick_circadian()

            # 4. 更新情绪旋钮
            emo_cfg = cfg.get("emotion", {})
            cesm = self.engine.cesm
            cesm._cfg = emo_cfg
            cesm.mood_decay_per_min = float(emo_cfg.get("mood_decay_per_minute", 0.5))
            cesm.affinity_power = float(emo_cfg.get("affinity_diminish_power", 0.5))
            cesm.low_mood = float(emo_cfg.get("low_mood_threshold", 30))
            cesm.angry_mood = float(emo_cfg.get("angry_mood_threshold", 20))
            cesm.trust_gate = float(emo_cfg.get("trust_gate_for_danger_ops", 30))
            cesm.time_decay_floor = float(emo_cfg.get("time_decay_floor", 20.0))

            # 5. 重启 ACL（新概率/冷却/开关）
            self._restart_acl()

            # 6. 刷新外观
            char_cfg = cfg.get("character", {})
            opacity = float(char_cfg.get("opacity", 1.0))
            self.pet.setWindowOpacity(opacity)
            size_changed = char_cfg.get("size", 160) != self.pet.size
            if size_changed:
                # 尺寸变更涉及 resize + 重定位，第一版提示重启
                return True, "设置已保存。宠物大小变更需要重启程序后生效。"

            return True, "设置已保存并生效。"
        except Exception as e:  # noqa: BLE001
            return False, f"应用设置时出错：{e}"


# ----------------------------------------------------------------------
# 使用示例：python -m app.controller（启动完整桌宠）
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    root = Path(__file__).resolve().parent.parent
    app = QApplication(sys.argv)
    app.setApplicationName("DesktopCompanion")
    # 窗口关闭策略由 controller.start() 统一配置（见 configure_app_lifecycle）

    controller = AppController(root)
    controller.start(app)

    from qasync import QEventLoop
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_forever()
