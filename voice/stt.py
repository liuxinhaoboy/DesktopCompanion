# -*- coding: utf-8 -*-
"""
stt.py —— 语音转文字（Speech-To-Text）
=====================================
走 OpenAI 兼容的 /audio/transcriptions 接口。docode.cc 只验证过文字模型，
**没有**验证它支持语音端点，所以这里一切按"可能没配 / 可能不支持"来写：
  - 没填地址/模型/密钥 → 抛 VoiceConfigError，界面引导去设置，而不是崩
  - 网络或接口报错     → 抛 STTError，消息翻译成用户看得懂的话
"""

from __future__ import annotations

import io

from openai import AsyncOpenAI


class VoiceConfigError(Exception):
    """语音功能缺少必要配置（地址/模型/密钥）。"""


class STTError(Exception):
    """转写请求失败（网络/接口/空结果）。"""


class STTClient:
    def __init__(self, stt_cfg: dict | None = None, timeout: float = 30.0):
        cfg = stt_cfg or {}
        self.base_url = (cfg.get("stt_base_url") or cfg.get("base_url") or "").strip()
        self.api_key = cfg.get("stt_api_key") or cfg.get("api_key") or ""
        self.model = (cfg.get("stt_model") or cfg.get("model") or "").strip()
        self.timeout = float(cfg.get("timeout_seconds", timeout))
        self._client: AsyncOpenAI | None = None

    def is_configured(self) -> bool:
        """三项齐全才算能用。模型供应商一般要求三者都有。"""
        return bool(self.base_url and self.model and self.api_key)

    def _ensure_client(self) -> AsyncOpenAI:
        if not self.is_configured():
            raise VoiceConfigError(
                "还没配置语音识别：请在「设置 → 语音」里填好语音转写的地址、模型和密钥。")
        if self._client is None:
            self._client = AsyncOpenAI(base_url=self.base_url,
                                       api_key=self.api_key,
                                       timeout=self.timeout)
        return self._client

    async def transcribe(self, wav_bytes: bytes,
                         filename: str = "speech.wav") -> str:
        """把一段 WAV 转成文字。空音频/空结果都抛 STTError。"""
        if not wav_bytes:
            raise STTError("没有录到声音，靠近麦克风再按住说一次试试。")
        client = self._ensure_client()
        try:
            # OpenAI SDK 接受 (文件名, 字节流, MIME) 三元组，不用落盘
            resp = await client.audio.transcriptions.create(
                model=self.model,
                file=(filename, io.BytesIO(wav_bytes), "audio/wav"),
                response_format="text",
            )
        except VoiceConfigError:
            raise
        except Exception as e:  # noqa: BLE001 —— 各种网络/鉴权错误统一翻译
            raise STTError(self._humanize_error(e)) from e

        text = (getattr(resp, "text", None) or str(resp or "")).strip()
        if not text:
            raise STTError("没听清内容，再说一遍试试？")
        return text

    @staticmethod
    def _humanize_error(e: Exception) -> str:
        msg = str(e).lower()
        if "connection" in msg or "timeout" in msg or "timed out" in msg:
            return "连不上语音识别服务，检查一下网络或语音地址对不对。"
        if "auth" in msg or "api key" in msg or "401" in msg:
            return "语音密钥不对（401），去「设置 → 语音」核对密钥。"
        if "not found" in msg or "404" in msg:
            return "这个地址没有语音识别接口（404），可能供应商不支持 /audio/transcriptions。"
        if "model" in msg and ("not" in msg or "exist" in msg):
            return "填的语音模型不存在，换一个转写模型名字试试。"
        return f"语音识别失败：{type(e).__name__}"
