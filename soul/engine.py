# -*- coding: utf-8 -*-
"""
SoulEngine —— 灵魂总管
======================
其他模块（界面、捣乱循环、文件助手）都不直接碰 CESM/LLM/记忆，
统一通过 SoulEngine 的几个方法跟角色打交道：

    engine.stream_chat(text)   聊天（流式）
    engine.snapshot()          查她现在的状态
    engine.react("pat_head")   触发一个事件并回一句台词
    engine.reload()            改了人设/档案后热重载

为什么要有这一层"总管"：
  把"读哪个文件、存哪份历史、拼什么 Prompt"这类杂事集中在一处，
  界面代码就能专注画图，后面模块也能随取随用。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import AsyncIterator

from .state import CESM
from .llm import LLMClient, sanitize
from .memory import MemoryStore
from .persona_check import PersonaChecker
from .profile import ProfileStore

# 工作记忆上限：太长会费钱+变慢，还会稀释人设。
# 到达上限就丢掉最旧的对话，只保留最近的部分（滑动窗口）。
# 4500 字符 ≈ 两千多 token，足够保持连贯，又比旧值省一截每轮固定输入。
WORKING_MEMORY_MAX_CHARS = 4500


class SoulEngine:
    def __init__(self, root: Path, config: dict):
        self.root = Path(root)
        self.cfg = config
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # ---- 内部成员：CESM（数值）、LLM（大脑）、三重记忆、角色文本 ----
        self.cesm = CESM(self.data_dir / "state.json", config.get("emotion", {}))
        self.llm = LLMClient(config.get("llm", {}), self.cesm)
        self.memory = MemoryStore(
            self.data_dir / "memory_db",
            self.data_dir / "memory_fallback.json",
            use_chroma=config.get("memory", {}).get("use_chroma", True),
        )

        self._character = {}      # 当前角色的人设/台词（角色包或旧版 character_core.json）
        self._profile = {}        # 保留 dict 兼容旧代码；ProfileStore 是语义记忆入口
        self.profile = ProfileStore(self.data_dir / "master_profile.json")
        self._history: list[dict] = []   # working memory [{role, content}]
        self.persona_checker = PersonaChecker()
        # P2：当前角色包信息 + 可用角色列表（由 CharacterManager 填充）
        self.character_profile = None
        self.available_characters: list = []
        # P4：视觉模型配置与懒加载客户端（None=没启用，带图时退回主文本模型）
        self._vision_cfg: dict = config.get("vision", {}) or {}
        self._vision_llm = None
        # 上一轮 OOC 时留下的纠正提示，会在下一轮 Prompt 里追加一次后清空。
        # 为什么不当场重试：流式内容已经逐字显示给用户了，撤回反而更出戏。
        self._pending_correction: str | None = None
        # A 昼夜作息：睡着时回答会带困倦前缀（由 AppController 按作息时间设置）
        self.drowsy = False
        # B 商城经济：由 AppController 注入（engine 不 import shop，保持分层）
        self.economy = None
        self._load_files()

    # ------------------------------------------------------------------
    # B 商城经济：给界面层/渲染器读的两个口子
    # ------------------------------------------------------------------
    def accessories(self) -> dict:
        """当前穿戴的配饰 {槽位: 物品}，给渲染器叠加用。没有经济系统就返回空。"""
        eco = getattr(self, "economy", None)
        return eco.equipped_items() if eco is not None else {}

    def extra_lines(self) -> dict:
        """商城台词包带来的额外台词 {台词池: [句子]}。"""
        eco = getattr(self, "economy", None)
        return eco.extra_lines() if eco is not None else {}

    # ------------------------------------------------------------------
    # 文件加载 / 热重载
    # ------------------------------------------------------------------
    def _load_files(self) -> None:
        ch_path = self.data_dir / "character_core.json"
        pf_path = self.data_dir / "master_profile.json"
        hs_path = self.data_dir / "chat_history.json"

        try:
            self._character = json.loads(ch_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[Soul] 角色配置读取失败({e})，用默认空人设")
            self._character = {"name": "小灵", "system_prompt": "你是桌面上的小精灵。"}
        # 人设里的禁语/特征词随人设一起热重载：改完人设，校验标准同步更新
        self.persona_checker = PersonaChecker(self._character)

        self._profile = self.profile.load()
        self._history = self._read_history(hs_path)

    def reload(self) -> None:
        """设置面板改完人设/档案后调用：不重启程序立即生效。"""
        self._load_files()

    def load_character(self, profile, data_dir: Path) -> None:
        """P2：加载/切换到一个角色包。
        会重建 CESM（情绪数值）、MemoryStore（情节记忆）、ProfileStore（御主档案），
        并把人设/台词换成角色包里的内容。LLM 客户端不重建（和角色无关）。

        为什么 CESM 和记忆也要换：
          每个角色是独立的"灵魂"——小灵的好感度不该算在豆豆头上，
          她们各自记得和主人的故事。这就是数据隔离。
        """
        # 旧角色先落盘，防止丢数据
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001
            print(f"[Soul] 切换前旧数据落盘失败（继续）：{exc}")

        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 重建"灵魂三件套"，全部指向新角色的数据目录
        self.cesm = CESM(self.data_dir / "state.json", self.cfg.get("emotion", {}))
        self.memory = MemoryStore(
            self.data_dir / "memory_db",
            self.data_dir / "memory_fallback.json",
            use_chroma=self.cfg.get("memory", {}).get("use_chroma", True),
        )
        self.profile = ProfileStore(self.data_dir / "master_profile.json")

        # 人设/台词来自角色包
        self.character_profile = profile
        self._character = profile.to_character_dict()
        self.persona_checker = PersonaChecker(self._character)
        self._profile = self.profile.load()

        # 加载新角色自己的聊天记录（没有就是空的）
        self._history = self._read_history(self.data_dir / "chat_history.json")
        self._pending_correction = None

    @staticmethod
    def _read_history(path: Path) -> list[dict]:
        """从指定路径读取聊天记录（滑窗截断 + 格式过滤）。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))[-40:]
            if isinstance(raw, list):
                return [m for m in raw
                        if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    @property
    def name(self) -> str:
        return self._character.get("name", "小灵")

    # ------------------------------------------------------------------
    # 档案 → 文本（给 Prompt 用）。跳过带下划线的说明字段和空值。
    # ------------------------------------------------------------------
    def profile_text(self) -> str:
        return self.profile.system_text()

    def recall_memories(self, query: str, top_k: int | None = None) -> list[str]:
        """按"当前情绪"检索情节记忆。
        为什么强制用当前情绪标签过滤（架构文档的要求）：
        人在难过的时候，浮现的是过去难过的片段。这样回忆自带情绪色彩，
        而不是每次都翻出同一批"最相关"的中性事实。"""
        tag = self.cesm.snapshot()["mood_word"]
        top_k = int(self.cfg.get("memory", {}).get("recall_top_k", 3))
        try:
            return self.memory.recall(query, tag, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 记忆检索永不能阻断对话
            print(f"[Soul] 记忆检索失败，本轮跳过：{exc}")
            return []

    def system_prompt(self, query: str = "") -> str:
        persona = self._character.get("system_prompt", "")
        # 语义记忆（人设+档案）永远置顶；情节记忆按当前情绪检索 Top-3。
        memory_lines = self.recall_memories(query) if query else []
        # 注意：不再把最近历史回退塞进 system prompt——这些对话在 messages 里
        # 已完整发给模型，重复只增输入 token。记忆检索为空就留空。
        prompt = self.llm.build_system_prompt(persona, self.profile_text(), memory_lines)
        if self._pending_correction:
            # 只追加一次：她道过歉之后就不翻旧账了，否则会永远别别扭扭
            prompt += "\n\n" + self._pending_correction
            self._pending_correction = None
        return prompt

    # ------------------------------------------------------------------
    # 核心接口 1：流式聊天
    # ------------------------------------------------------------------
    def setup_vision(self, vision_cfg: dict | None) -> None:
        """设置面板保存后更新视觉配置；换配置要丢弃旧的视觉客户端。"""
        self._vision_cfg = vision_cfg or {}
        self._vision_llm = None

    def _client_for_images(self):
        """带图请求该用哪个 LLM 客户端：启用了独立视觉模型就用它，否则用主模型
        （很多主力模型本身能看图，没单独配视觉模型时不该让图片功能失效）。"""
        if not self._vision_cfg.get("enabled", False):
            return self.llm
        from .vision import build_vision_llm_cfg
        merged = build_vision_llm_cfg(self._vision_cfg, self.cfg.get("llm", {}))
        if not merged:
            return self.llm
        # 配置没变就复用客户端（避免每条图片消息都新建连接）
        if self._vision_llm is None:
            from .llm import LLMClient
            self._vision_llm = LLMClient(merged, self.cesm)
        return self._vision_llm

    async def stream_chat(self, user_text: str,
                          image_data_urls: list[str] | None = None,
                          image_name: str | None = None) -> AsyncIterator[str]:
        """界面层 await 迭代它拿一小段一小段的回复，实现打字机效果。
        finally 块保证：哪怕回复被中途打断（关窗口/退出程序），
        已生成的部分也会作为一条不完整回忆存进历史——
        就像真人对话被打断，说过的话依然"发生过"。

        image_data_urls：data:image/...;base64 列表，只在本次请求里发给模型；
        历史和长期记忆里只存文字占位"[图片：文件名]"，绝不存 Base64（又大又费钱）。"""
        reply_parts: list[str] = []
        system_prompt = self.system_prompt(user_text)
        # 带图时按视觉配置选客户端，纯文本永远走主模型
        chat_client = self._client_for_images() if image_data_urls else self.llm
        # 落进历史/记忆的"文字版"用户消息：带图就加占位，纯文本就是原文
        history_text = user_text
        if image_data_urls:
            tag = f"[图片：{image_name or '未命名'}] "
            history_text = tag + (user_text or "看看这张图")
        # 睡梦中被人叫醒：先嘟囔一句，再说正事。
        # 为什么不让模型自己写"我好困"：模型不知道当前几点，也没法保证语气一致，
        # 由代码按作息状态拼前缀最可靠，而且不额外消耗 token。
        if self.drowsy:
            drowsy_prefix = "唔……好困……"
            reply_parts.append(drowsy_prefix)
            yield drowsy_prefix

        try:
            async for piece in chat_client.chat_stream(
                    system_prompt, self._history, user_text,
                    image_data_urls=image_data_urls):
                reply_parts.append(piece)
                yield piece
        finally:
            full_reply = sanitize("".join(reply_parts))
            if not full_reply:                  # 理论上兜底保证非空，防御一下
                full_reply = "……（她好像走神了）"

            self._push_history("user", history_text)
            self._push_history("assistant", full_reply)
            self._save_history()

            # 人格校验放在流式之后：流已经播给用户了，不能回收，
            # 所以这里不重写显示内容，而是记一笔并在下一轮追加纠正提示。
            verdict = self.persona_checker.check(full_reply)
            if not verdict.ok:
                self._pending_correction = self.persona_checker.correction_prompt(verdict.reasons)
                print(f"[Soul] 检测到 OOC：{verdict.reasons}")

            # 异步存入情节记忆：不 await，避免磁盘写入拖慢界面收尾
            try:
                tag = self.cesm.snapshot()["mood_word"]
                asyncio.ensure_future(
                    self.memory.remember_conversation(history_text, full_reply, tag))
            except RuntimeError:
                # 没有事件循环时（比如单元测试里同步调用）就直接同步写
                self.memory.add(self.memory.make_summary(history_text, full_reply),
                                self.cesm.snapshot()["mood_word"], "chat")

    async def try_computer_action(self, user_text: str, parent=None):
        """
        P5：先让 ToolPlanner 判断这句话是不是"要操作电脑"。
        是 → 弹非阻塞确认卡、经 HandsExecutor 执行，落历史，返回 ActionResult；
        否（普通聊天/没开能力/规划失败）→ 返回 None，调用方走正常流式聊天。
        图片消息不走这里（那是视觉聊天）。
        """
        planner = getattr(self, "planner", None)
        hands = getattr(self, "hands", None)
        if planner is None or hands is None or not user_text:
            return None
        try:
            req = await planner.plan(self.llm.plain_complete, user_text)
        except Exception:  # noqa: BLE001 —— 规划回合任何异常都退回普通聊天
            return None
        if req is None:
            return None
        result = await hands.request_and_execute(req, parent)
        # 动作也算一次互动：落历史、轻微抬情绪，但不进长期向量记忆（避免污染）
        self._push_history("user", user_text)
        self._push_history("assistant", result.message)
        self._save_history()
        try:
            self.cesm.apply_event("chat")
        except Exception:  # noqa: BLE001
            pass
        return result


    async def try_browser_action(self, user_text: str, parent=None):
        """
        P6 补：自然语言 → 浏览器动作闭环。
        规划 → policy 校验 → 非阻塞确认卡 → bridge 下发等结果，落历史。
        普通聊天/没连桥/规划失败返回 None，调用方走正常流式聊天。
        返回带 .message 的对象（与 hands 的 ActionResult 同形，聊天窗直接取 message）。
        """
        from types import SimpleNamespace
        bridge = getattr(self, "browser_bridge", None)
        bplanner = getattr(self, "browser_planner", None)
        bpolicy = getattr(self, "browser_policy", None)
        if bridge is None or bplanner is None or bpolicy is None or not user_text:
            return None
        try:
            plan = await bplanner.plan(self.llm.plain_complete, user_text)
        except Exception:  # noqa: BLE001
            return None
        if plan is None:
            return None

        def _done(msg: str):
            self._push_history("user", user_text)
            self._push_history("assistant", msg)
            self._save_history()
            try:
                self.cesm.apply_event("chat")
            except Exception:  # noqa: BLE001
                pass
            return SimpleNamespace(message=msg)

        # 1) policy 硬校验（当前页 URL 运行时未知传空，content.js 会再拦一道）
        ok, why = bpolicy.check(plan.action, plan.target, plan.args)
        if not ok:
            return _done(f"这个我不能做哦：{why}")

        # 2) 逐次确认
        from browser.policy import request_browser_confirmation
        try:
            agreed = await request_browser_confirmation(
                parent, plan.action, plan.target, plan.reason)
        except Exception:  # noqa: BLE001
            agreed = False
        if not agreed:
            return _done("好的，没操作（你取消了或超时）。")

        # 3) 下发并等扩展结果
        res = await bridge.send_command(plan.action, plan.target, plan.args)
        if not res.get("ok"):
            return _done("浏览器那边没成功：" + res.get("error", "未知原因"))
        msg = self._describe_browser_result(plan.action, res.get("data", {}))
        return _done(msg)

    @staticmethod
    def _describe_browser_result(action: str, data: dict) -> str:
        data = data or {}
        if action == "read_text":
            text = str(data.get("text", "")).strip()
            if not text:
                return "我没读到什么正文内容。"
            return "我读到网页正文了：\n" + text[:600] + ("…" if len(text) > 600 else "")
        if action == "scroll":
            return f"好，已经帮你滚动页面了（{data.get('scrolled', '')}）。"
        if action == "click":
            return "已经帮你点了那个元素。"
        if action == "type_text":
            return "已经帮你把文字填进输入框了。"
        if action == "open_url":
            return f"已经帮你打开网址了（{data.get('opened', '')}）。"
        if action == "close_tab":
            return "当前标签页已经关掉了。"
        return "操作完成。"

    def _push_history(self, role: str, content: str) -> None:
        self._history.append({"role": role,
                              "content": content,
                              "ts": int(time.time())})
        # 工作记忆裁剪：从最旧的开始丢，直到总长度回到限制内。
        # 为什么按字符数而不是精确 token：省掉 tokenizer 依赖，误差足够小。
        total = sum(len(m["content"]) for m in self._history)
        while total > WORKING_MEMORY_MAX_CHARS and len(self._history) > 2:
            total -= len(self._history[0]["content"])
            self._history.pop(0)

    def _save_history(self) -> None:
        path = self.data_dir / "chat_history.json"
        try:
            path.write_text(json.dumps(self._history, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError as e:
            print(f"[Soul] 聊天记录保存失败(不影响使用): {e}")

    # ------------------------------------------------------------------
    # 核心接口 2/3：状态查询 & 事件反应（带随机台词）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return self.cesm.snapshot()

    def flush(self) -> None:
        """退出前落盘：情绪数值结账 + 聊天记录保存。"""
        self.cesm.flush()
        self._save_history()

    def react(self, event: str, strength: float = 1.0) -> tuple[str, dict]:
        """
        触发事件（摸头/喂食等），返回 (她说的台词, 数值变化明细)。
        台词选择也受情绪影响：生气时优先用 angry_lines。
        为什么放在 engine 而不是界面：将来捣乱循环也要"做一件事+说句话"，
        复用同一套逻辑和数值通道，保证情绪系统是唯一入口。
        """
        delta = self.cesm.apply_event(event, strength)

        pool_key = {"pat_head": "pat_lines",
                    "feed": "feed_lines",
                    "praise": "greeting_lines"}.get(event)
        if self.cesm.is_angry and event in ("pat_head", "feed"):
            pool = self._character.get("angry_lines") or ["……"]
        elif pool_key and self._character.get(pool_key):
            pool = list(self._character[pool_key])
        else:
            pool = ["嗯？"]
        # 商城买的台词包并进同一个池子（买之前池子就是角色自带的那些）
        if pool_key:
            pool = pool + self.extra_lines().get(pool_key, [])

        line = random.choice(pool)
        # 告诉模型"刚才发生了摸头"，下一轮聊天她会接得上这个话头。
        # 但**连着同一种互动只留一条**：摸头可以连点几十下，每下都塞一条
        # 一模一样的标记（每条约 14 字）会很快把 4500 字的工作记忆窗口灌满，
        # 把真实对话挤出去——这些重复标记对模型也没有任何额外信息量。
        marker = f"(主人刚刚{self._event_verb(event)})"
        if not (self._history and self._history[-1].get("content") == marker):
            self._push_history("user", marker)
        return line, delta

    @staticmethod
    def _event_verb(event: str) -> str:
        return {"pat_head": "摸了摸你的头",
                "feed": "投喂了你零食",
                "praise": "夸了你"}.get(event, event)

    def feed_line(self) -> str:
        """被投喂时说的一句话。

        原来界面上是写死的 `_character["feed_lines"][0]`——永远只说第一句，
        喂十次听十遍一模一样的话。顺便也把商城买的吃货台词包并进来。
        """
        pool = list(self._character.get("feed_lines") or ["好吃好吃~"])
        pool += self.extra_lines().get("feed_lines", [])
        return random.choice(pool)

    def greet_line(self) -> str:
        pool = self._character.get("greeting_lines") or ["我回来啦~"]
        if self.cesm.is_angry:
            pool = ["……又见面了。"] + pool[:0] + ["哼。"]
        return random.choice(pool)


# ----------------------------------------------------------------------
# 使用示例：python soul/engine.py —— 不开界面也能验灵魂是否工作
# ----------------------------------------------------------------------
if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    eng = SoulEngine(root, cfg)

    import asyncio

    async def demo():
        print(f"当前状态: {eng.snapshot()}")
        print(f"开场白: {eng.greet_line()}")
        text = "今天有点累，安慰一下我吧"
        print(f"\n你对她说: {text}")
        print(f"{eng.name}说:", end=" ")
        async for piece in eng.stream_chat(text):
            print(piece, end="", flush=True)
        print("\n\n聊完后状态:", eng.snapshot())

    asyncio.run(demo())
