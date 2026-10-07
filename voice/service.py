# -*- coding: utf-8 -*-
"""
service.py —— 语音状态机（界面只跟它打交道）
===========================================
把录音、转写、合成、播放四件事串成一条可取消的流水线：

    idle → recording → transcribing →（交回界面走文字流式）→ speaking → idle

界面（ChatPanel）不直接 import recorder/stt/tts/player，只调这里的高层方法，
这样换实现 / 加新供应商时界面不用动。

设计原则：
  - 任何一步没配好或失败，都返回人话提示、回到 idle，绝不让异常冒到界面层崩掉
  - 每个阶段都能 cancel：录音丢弃、转写任务取消、播放立即停
  - 关窗口必须调 shutdown() 释放麦克风和播放器
"""

from __future__ import annotations

import asyncio

from PyQt6.QtCore import QObject, pyqtSignal

from .player import AudioPlayer
from .recorder import AudioRecorder, RecorderError
from .stt import STTClient, STTError, VoiceConfigError
from .tts import TTSClient, TTSError

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_SPEAKING = "speaking"


class VoiceService(QObject):
    state_changed = pyqtSignal(str)
    hint = pyqtSignal(str)              # 短暂状态提示（如"正在听…"）
    failed = pyqtSignal(str)            # 失败提示（人话）
    transcript_ready = pyqtSignal(str)  # 转写文本出来了
    speaking_started = pyqtSignal()
    speaking_finished = pyqtSignal()

    def __init__(self, voice_cfg: dict | None = None,
                 llm_cfg: dict | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._voice_cfg = dict(voice_cfg or {})
        self._llm_cfg = dict(llm_cfg or {})
        self._state = STATE_IDLE

        self.recorder = AudioRecorder(
            max_seconds=self._voice_cfg.get("max_record_seconds", 60),
            device_name=self._voice_cfg.get("record_device", "default"),
            parent=self)
        self.recorder.max_duration_reached.connect(self._on_max_duration)

        self.stt = STTClient(self._voice_cfg)
        self.tts = TTSClient(self._voice_cfg, self._llm_cfg)
        self.player = AudioPlayer(parent=self)
        self.player.error_occurred.connect(lambda m: self.failed.emit(m))
        # 自然播完也要把状态机收回 idle（play_bytes 是异步播放，await 合成时还没播完）
        self.player.finished.connect(self._finish_speak_state)

        self._transcribe_task: asyncio.Task | None = None
        self._speak_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 配置热更新（设置面板保存后由 AppController 调用）
    # ------------------------------------------------------------------
    def update_config(self, voice_cfg: dict | None = None,
                      llm_cfg: dict | None = None) -> None:
        if voice_cfg is not None:
            self._voice_cfg = dict(voice_cfg)
            self.recorder.max_seconds = max(1, int(voice_cfg.get("max_record_seconds", 60)))
            self.recorder.device_name = voice_cfg.get("record_device", "default")
            self.stt = STTClient(self._voice_cfg)
            self.tts = TTSClient(self._voice_cfg, self._llm_cfg)
        if llm_cfg is not None:
            self._llm_cfg = dict(llm_cfg)
            self.tts = TTSClient(self._voice_cfg, self._llm_cfg)

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, st: str) -> None:
        if st != self._state:
            self._state = st
            self.state_changed.emit(st)

    def voice_enabled(self) -> bool:
        return bool(self._voice_cfg.get("enabled", False))

    def auto_speak(self) -> bool:
        return bool(self._voice_cfg.get("auto_speak", False))

    def can_transcribe(self) -> bool:
        """转写要三项配置齐全；朗读（edge-tts）不需要。"""
        return self.stt.is_configured()

    # ------------------------------------------------------------------
    # 录音 → 转写
    # ------------------------------------------------------------------
    def start_recording(self) -> tuple[bool, str]:
        """按下说话按钮。返回 (是否开始成功, 人话说明)。"""
        if not self.voice_enabled():
            return False, "语音功能没开，去「设置 → 语音」打开总开关。"
        if self._state == STATE_SPEAKING:
            self.stop_speaking()
        if self._state not in (STATE_IDLE,):
            return False, "现在还在处理上一段，稍等一下。"
        try:
            ok, msg = self.recorder.start()
        except RecorderError as e:
            return False, str(e)
        if not ok:
            return False, msg
        self._set_state(STATE_RECORDING)
        self.hint.emit("正在听…松开发送，最多录 "
                       f"{self.recorder.max_seconds} 秒")
        return True, "ok"

    async def finish_recording(self) -> str:
        """松开按钮：停录 → 转写，返回识别到的文本（失败返回空串并 emit failed）。"""
        if self._state != STATE_RECORDING:
            return ""
        wav = self.recorder.stop()
        if not wav:
            self._set_state(STATE_IDLE)
            self.failed.emit("录得太短了，按住按钮多说一会儿。")
            return ""

        self._set_state(STATE_TRANSCRIBING)
        self.hint.emit("正在识别…")
        try:
            self._transcribe_task = asyncio.ensure_future(self.stt.transcribe(wav))
            text = await self._transcribe_task
        except asyncio.CancelledError:
            self._set_state(STATE_IDLE)
            raise
        except (VoiceConfigError, STTError) as e:
            self._set_state(STATE_IDLE)
            self.failed.emit(str(e))
            return ""
        except Exception as e:  # noqa: BLE001 —— 兜底，绝不让界面崩
            self._set_state(STATE_IDLE)
            self.failed.emit(f"语音识别出了点问题：{type(e).__name__}")
            return ""
        finally:
            wav = None  # 尽早释放录音字节
            self._transcribe_task = None

        self._set_state(STATE_IDLE)
        text = text.strip()
        if text:
            self.transcript_ready.emit(text)
        return text

    def _on_max_duration(self) -> None:
        # 录满自动停：界面层（按钮）连接这个信号去调 finish_recording，
        # 这里只负责把"到点了"翻译成提示
        self.hint.emit("录到最大时长了，正在识别…")

    # ------------------------------------------------------------------
    # 朗读
    # ------------------------------------------------------------------
    async def speak(self, text: str) -> None:
        """合成并朗读一段文字。正在朗读会先停旧的。"""
        text = (text or "").strip()
        if not text:
            return
        self.stop_speaking()
        self._set_state(STATE_SPEAKING)
        self.speaking_started.emit()
        try:
            self._speak_task = asyncio.ensure_future(self.tts.synthesize(text))
            audio, suffix = await self._speak_task
            # await 期间可能已被 cancel/stop
            if self._state != STATE_SPEAKING:
                return
            if not self.player.play_bytes(audio, suffix):
                self.failed.emit("朗读没播出来，换个音色试试。")
        except asyncio.CancelledError:
            raise
        except TTSError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"朗读出了点问题：{type(e).__name__}")
        finally:
            self._speak_task = None
            # play_bytes 是异步播放，状态由 _after_play 收尾；失败时直接回 idle
            if not self.player.is_playing():
                self._finish_speak_state()

    def _finish_speak_state(self) -> None:
        if self._state == STATE_SPEAKING:
            self._set_state(STATE_IDLE)
            self.speaking_finished.emit()

    def stop_speaking(self) -> None:
        """立即停止朗读。"""
        if self._speak_task is not None and not self._speak_task.done():
            self._speak_task.cancel()
        self._speak_task = None
        self.player.stop()
        self._finish_speak_state()

    # ------------------------------------------------------------------
    # 取消 / 释放
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """取消当前任何阶段，回到 idle。"""
        if self._state == STATE_RECORDING:
            self.recorder.cancel()
        if self._transcribe_task is not None and not self._transcribe_task.done():
            self._transcribe_task.cancel()
        self.stop_speaking()
        self._set_state(STATE_IDLE)

    def shutdown(self) -> None:
        """关窗口时调用：释放麦克风和音频资源。"""
        try:
            if self.recorder.is_recording():
                self.recorder.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass
        self._set_state(STATE_IDLE)
