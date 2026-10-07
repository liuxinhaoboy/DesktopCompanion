# -*- coding: utf-8 -*-
"""
tts.py —— 文字转语音（Text-To-Speech）
=====================================
默认走 edge-tts（微软在线合成，免费、不要密钥、中文音色多）；
也预留 OpenAI 兼容的 /audio/speech（供应商支持时可在设置里切换）。

合成结果是音频字节（edge 是 mp3），交给 player.py 播放，本模块不碰扬声器。
"""

from __future__ import annotations

import io


class TTSError(Exception):
    """语音合成失败。"""


class TTSClient:
    def __init__(self, voice_cfg: dict | None = None,
                 llm_cfg: dict | None = None):
        cfg = voice_cfg or {}
        self.provider = (cfg.get("tts_provider") or "edge-tts").strip()
        self.voice = cfg.get("tts_voice") or "zh-CN-XiaoxiaoNeural"
        # OpenAI 兼容 TTS 复用文本模型的地址/密钥（config 没有为 TTS 单开字段）
        llm = llm_cfg or {}
        self._base_url = llm.get("base_url", "")
        self._api_key = llm.get("api_key", "")
        self._openai_model = cfg.get("tts_model", "tts-1")
        self._client = None

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """文本 → (音频字节, 格式后缀如 'mp3')。空文本直接返回空。"""
        text = (text or "").strip()
        if not text:
            return b"", "mp3"
        if self.provider == "openai_compatible":
            return await self._synthesize_openai(text)
        return await self._synthesize_edge(text)

    # ------------------------------------------------------------------
    async def _synthesize_edge(self, text: str) -> tuple[bytes, str]:
        """edge-tts：把流式音频块拼成完整 mp3。"""
        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("缺少 edge-tts 库，无法朗读（pip install edge-tts）。") from e

        chunks: list[bytes] = []
        try:
            communicate = edge_tts.Communicate(text, self.voice)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    chunks.append(chunk["data"])
        except Exception as e:  # noqa: BLE001 —— 网络/音色名都要兜住
            raise TTSError(self._humanize_edge_error(e)) from e

        audio = b"".join(chunks)
        if not audio:
            raise TTSError("合成出来是空的，换个音色或检查网络再试。")
        return audio, "mp3"

    async def _synthesize_openai(self, text: str) -> tuple[bytes, str]:
        """OpenAI 兼容 /audio/speech（第一版复用文本模型的地址和密钥）。"""
        if not (self._base_url and self._api_key):
            raise TTSError("没配 OpenAI 兼容的地址和密钥，无法用这种合成方式。")
        try:
            if self._client is None:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(base_url=self._base_url,
                                           api_key=self._api_key, timeout=60)
            resp = await self._client.audio.speech.create(
                model=self._openai_model, voice=self.voice, input=text)
            data = resp.content if hasattr(resp, "content") else \
                b"".join([c async for c in resp.aiter_bytes()])
            if not data:
                raise TTSError("接口返回了空音频。")
            return data, "mp3"
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"OpenAI 兼容合成失败：{type(e).__name__}") from e

    @staticmethod
    def _humanize_edge_error(e: Exception) -> str:
        msg = str(e).lower()
        if "connection" in msg or "timeout" in msg or "network" in msg:
            return "朗读服务连不上，检查网络后再试。"
        if "voice" in msg or "no such" in msg:
            return "这个音色名字不对，去「设置 → 语音」换一个。"
        return f"朗读合成失败：{type(e).__name__}"

    @staticmethod
    async def list_edge_voices(language_prefix: str = "zh") -> list[tuple[str, str]]:
        """列出 edge-tts 可用音色 [(短名, 性别/说明), ...]，给设置下拉用。"""
        try:
            import edge_tts
            voices = await edge_tts.list_voices()
        except Exception:  # noqa: BLE001 —— 离线时返回空列表，不影响主流程
            return []
        out = []
        for v in voices:
            if language_prefix and not v.get("Locale", "").lower().startswith(
                    language_prefix.lower()):
                continue
            gender = v.get("Gender", "")
            out.append((v.get("ShortName", ""),
                        f"{v.get('Locale', '')} {gender}"))
        return sorted(out)
