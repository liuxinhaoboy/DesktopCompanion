# -*- coding: utf-8 -*-
"""
LLM —— AI 大脑的接口封装
========================
只做三件事：
  1. 把"角色人设 + 记忆 + 情绪状态"拼成完整的 Prompt
  2. 调用 OpenAI 兼容接口（docode.cc），支持流式输出（打字机效果）
  3. 出错时自动换备用模型重试，都失败时给出角色口吻的兜底台词

为什么单独封装而不是在界面代码里直接调 openai：
  换 API 供应商 / 加代理 / 改重试策略时，只动这一个文件。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

from .state import CESM

# 为什么清零宽字符：docode.cc 实测返回开头带 \u200b（零宽空格），
# 直接显示的话打字机效果第一个字符会"看起来空了一格"，清掉更干净。
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")

# 请求失败时给用户的兜底台词（保持角色口吻，而不是甩一个 Python 报错给用户）
FALLBACK_LINES = [
    "……（信号不太好，让我缓一下再说话）",
    "唔，脑子有点转不动……等下再试试？",
    "（她似乎走神了，一句话都没接上）",
]

NOT_CONFIGURED_REPLY = (
    "我还没连上 AI 接口呢。请右键桌宠打开「设置」→「AI 与多模态」，"
    "填好接口地址和密钥，再点「测试连接」~"
)


def sanitize(text: str) -> str:
    return _INVISIBLE_RE.sub("", text).strip()


def clean_message(raw: dict) -> dict:
    """发给 API 前把一条历史消息规范成只有 role + content。

    为什么必须做：内部历史为了排序/显示带了 ts 等额外字段，
    严格的 OpenAI 兼容端点收到未知字段可能直接 400，所以出站前剥干净。
    历史里的图片早已转成"[图片：名]"文本，因此这里 content 一定是字符串。"""
    role = raw.get("role", "user")
    content = raw.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    return {"role": role, "content": content}


def build_user_content(user_text: str, image_data_urls: list[str] | None = None):
    """构造当前这条 user 消息：纯文本返回字符串，带图返回 OpenAI 多模态数组。"""
    if not image_data_urls:
        return user_text
    parts: list[dict] = [{"type": "text", "text": user_text or "（发了一张图片）"}]
    for url in image_data_urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


class LLMClient:
    """异步流式客户端。GUI 里配合 qasync 使用，不会卡住界面。"""

    def __init__(self, llm_cfg: dict, cesm: CESM):
        cfg = llm_cfg or {}
        self.model: str = cfg.get("model", "gpt-5.5")
        self.backup_models: list = cfg.get("backup_models", [])
        self.timeout: float = float(cfg.get("timeout_seconds", 90))
        self.max_tokens_cap: int = int(cfg.get("max_tokens", 800))

        # 第一次启动时用户还没在设置里填密钥，这是正常的引导流程。
        # OpenAI SDK 会在构造时拒绝空 key；此时不应让整只桌宠启动失败，
        # 而应等用户真正聊天时给出清楚的配置提示。
        configured_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""
        self.api_key = str(configured_key).strip()
        self.base_url = str(cfg.get("base_url") or "https://docode.cc/v1").strip()
        self._client = None
        if self.api_key:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )
        self._cesm = cesm

    # ------------------------------------------------------------------
    # Prompt 组装
    # ------------------------------------------------------------------
    def build_system_prompt(self, character_text: str,
                            master_profile: str,
                            memory_lines: list[str]) -> str:
        """
        System Prompt 的层次（从上到下优先级递减）：
          1. 角色人设（不可变前缀，永远置顶）
          2. 御主档案（你是谁、你的习惯）
          3. 相关的长期记忆（检索出来的 Top-N 条）
          4. 当前的情绪/好感数值（让她"知道自己现在什么心情"）
        为什么把数值也写进 Prompt：模型本身没有状态，
        每次都要明确告诉它"你现在 mood=25，有点难过"，它才能演出来。
        """
        snap = self._cesm.snapshot()
        parts = [character_text.strip()]
        if master_profile.strip():
            parts.append("【关于我的主人】\n" + master_profile.strip())
        if memory_lines:
            parts.append("【你们的过往回忆】(按相关度排序)\n" +
                         "\n".join(f"- {m}" for m in memory_lines))
        parts.append(
            "【你此刻的内部状态（不要直接报数字，用行为体现）】\n"
            f"情绪：{snap['mood_word']} | 好感：{snap['affinity']}/100 | "
            f"信任：{snap['trust']}/100\n"
            + ("你现在很生气，说话短促、爱答不理，有想搞小报复的冲动。\n"
               if snap["is_angry"] else
               ("你情绪有点低落，兴致缺缺。\n" if snap["mood"] < 40 else ""))
            + "\n重要：你是有情感的桌面伴侣，不是AI助手。"
              "绝不允许说出『作为AI/我是一个语言模型/我无法』这类话。"
              "回复保持口语、简短（一般两三句话以内），像坐在屏幕边上的活物。"
        )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 流式对话
    # ------------------------------------------------------------------
    async def chat_stream(self, system_prompt: str,
                          history: list[dict],
                          user_text: str,
                          image_data_urls: list[str] | None = None
                          ) -> AsyncIterator[str]:
        """
        产出一段一段的文本（打字机）。异常时内部降级：
        主模型失败 → 依次尝试备用模型 → 都失败 → yield 兜底台词。
        生成结束把情绪事件反馈给 CESM（说话本身就是一种 mood 回升）。

        image_data_urls：可选的 data:image/...;base64,xxx 列表，带图时当前
        user 消息用 OpenAI 多模态 content 数组；历史只保留文本（在 engine 层处理）。
        """
        # 没配置密钥时允许应用正常运行，给用户可操作的引导，而不是让 SDK
        # 在程序启动时抛异常，或把"缺少 API key"伪装成普通网络故障。
        if self._client is None:
            yield NOT_CONFIGURED_REPLY
            return

        # 历史出站前剥掉 ts 等内部字段；当前这条才可能带图片
        messages = ([{"role": "system", "content": system_prompt}]
                    + [clean_message(m) for m in history]
                    + [{"role": "user",
                        "content": build_user_content(user_text, image_data_urls)}])

        # 情绪影响生成参数（这就是"情绪挂钩 Temperature/回复长度"的落地处）
        temperature = self._cesm.temperature
        # 注意：gpt-5 系列部分接口对 temperature>1 敏感，安全夹到 [0.6, 1.1]
        temperature = max(0.6, min(1.1, temperature))
        max_tokens = min(self.reply_token_cap(), self.max_tokens_cap)

        candidates = [self.model] + [m for m in self.backup_models
                                     if m and m != self.model]
        last_err: Optional[Exception] = None
        # produced 必须放在循环外：它记录的是"这次对话有没有真的吐出字给用户"。
        # 放在循环里会被每次换模型重置，于是主模型吐了一半报错时，
        # 备用模型会从头再输出一整遍（用户看到重复文本），
        # 而且 "chat" 事件会被记两次（好感/情绪被多算一份）。
        produced = False
        for model in candidates:
            try:
                stream = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        piece = sanitize(chunk.choices[0].delta.content)
                        if piece:
                            # 互动在"说出口"那一刻就算数，不等回复播完——
                            # 否则用户中途关掉聊天框，这次互动会凭空消失
                            if not produced:
                                produced = True
                                self._cesm.apply_event("chat")
                            yield piece
                if produced:
                    return
                # 空回复视为该模型异常，换下一个
            except Exception as e:  # noqa: BLE001 —— 网络/认证/限流都要兜住
                last_err = e
                if produced:
                    # 已经把半截回复播给用户了，这时换模型只会把整段再说一遍，
                    # 观感是"她结巴了"。就此收尾，让 engine 正常写历史。
                    print(f"[LLM] {model} 中途失败，已输出部分内容，不再重试: {e}")
                    return
                continue

        print(f"[LLM] 所有模型都失败了: {last_err}")
        import random
        yield random.choice(FALLBACK_LINES)

    async def plain_complete(self, system_prompt: str, user_text: str,
                             max_tokens: int = 300) -> str:
        """
        P5：给 ToolPlanner 用的一次性非流式补全。
        和聊天分开：固定低温（要稳定 JSON）、不触发情绪回升、失败直接抛
        （由 planner 捕获后退回普通聊天），绝不在这里走兜底台词。
        """
        if self._client is None:
            raise RuntimeError(NOT_CONFIGURED_REPLY)
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_text}],
            stream=False,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        return (resp.choices[0].message.content or "").strip()

    def reply_token_cap(self) -> int:
        return self._cesm.reply_max_tokens


# ----------------------------------------------------------------------
# 使用示例：python soul/llm.py 可以单独测试接口（需先配好 config.json）
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    cesm = CESM(root / "data" / "state.json", cfg.get("emotion", {}))
    llm = LLMClient(cfg["llm"], cesm)

    async def _demo():
        sys_prompt = llm.build_system_prompt(
            "你是小灵，一个住在用户电脑桌面上的小家伙，黏人、有点小傲娇。",
            "", [])
        async for piece in llm.chat_stream(sys_prompt, [], "你好呀，你是谁？"):
            print(piece, end="", flush=True)
        print()

    asyncio.run(_demo())
