# -*- coding: utf-8 -*-
"""
recorder.py —— 麦克风录音
=========================
用 PyQt6 自带的 QAudioSource 把声音录成 16kHz / 单声道 / 16bit 的 PCM，
停止时用标准库 wave 打包成 WAV 字节流返回。

为什么不用 pyaudio/sounddevice：
  它们在 Python 3.13 上大概率没有预编译 wheel，要用户装编译工具链；
  PyQt6.QtMultimedia 是现成依赖，底层用系统音频 API，零额外安装。

录音只落在内存（QByteArray），不落盘——转写一结束就随对象释放，
符合"默认不保存音频"的隐私约定（要保存由设置项 privacy.save_audio 决定）。
"""

from __future__ import annotations

import io
import wave
from typing import Optional

from PyQt6.QtCore import QBuffer, QByteArray, QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSource, QMediaDevices

SAMPLE_RATE = 16000       # 主流语音识别都按 16k 训练，重采样反而降精度
CHANNELS = 1
SAMPLE_WIDTH = 2         # Int16 = 2 字节


class RecorderError(Exception):
    """录音器自身的可预期问题（没麦克风/打不开设备），消息可直接给用户看。"""


class AudioRecorder(QObject):
    # 状态变化：idle / recording
    state_changed = pyqtSignal(str)
    # 录到设置的最大时长，自动停（由上层调 stop() 取走数据）
    max_duration_reached = pyqtSignal()

    def __init__(self,
                 max_seconds: int = 60,
                 device_name: str = "default",
                 parent: QObject | None = None):
        super().__init__(parent)
        self.max_seconds = max(1, int(max_seconds))
        self.device_name = device_name or "default"

        self._source: Optional[QAudioSource] = None
        self._buffer: Optional[QBuffer] = None
        self._pcm = QByteArray()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_max_time)
        self._state = "idle"

    # ------------------------------------------------------------------
    # 设备枚举（设置面板下拉用；静态方法不依赖实例）
    # ------------------------------------------------------------------
    @staticmethod
    def list_input_devices() -> list[QAudioDevice]:
        """所有可用麦克风，第一项通常是系统默认。"""
        return list(QMediaDevices.audioInputs())

    def _pick_device(self) -> QAudioDevice:
        default = QMediaDevices.defaultAudioInput()
        if self.device_name in ("", "default", None):
            device = default
        else:
            device = None
            for d in QMediaDevices.audioInputs():
                # 描述里包含配置名就算匹配（用户在设置里看到的就是描述文本）
                if self.device_name and self.device_name in d.description():
                    device = d
                    break
            device = device or default     # 配的设备拔了就回退默认，不能直接罢工
        if device is None or device.isNull():
            raise RecorderError("没有找到麦克风。请接一个麦克风，或在设置里选对录音设备。")
        return device

    @staticmethod
    def _make_format() -> QAudioFormat:
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        return fmt

    # ------------------------------------------------------------------
    # 开始 / 停止
    # ------------------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        """开始录音。返回 (是否成功, 人话说明)。重复 start 会先停掉上一段。"""
        if self._state == "recording":
            self.stop()

        device = self._pick_device()
        fmt = self._make_format()
        # 硬件不支持精确格式时，Qt 会用 nearestFormat；先问一句，差太多就提示
        if not device.isFormatSupported(fmt):
            nearest = device.preferredFormat()
            if nearest is None or nearest.isNull():
                return False, "麦克风不支持 16k 单声道录音格式，换个设备试试。"
            # Qt 能内部重采样的情况下仍可继续，这里不拦死

        self._pcm = QByteArray()
        self._buffer = QBuffer(self._pcm, self)
        if not self._buffer.open(QBuffer.OpenModeFlag.WriteOnly):
            return False, "录音缓冲区打不开，重试一次看看。"

        self._source = QAudioSource(device, fmt, self)
        self._source.start(self._buffer)
        # 极少数情况下设备立刻进入停止态（被占用/无权限），要能发现
        if self._source.error() != QAudioSource.Error.NoError and \
                self._source.state() == QAudioSource.State.StoppedState:
            err = self._source.errorString()
            self._cleanup()
            return False, f"麦克风打不开：{err or '被其他程序占用或没有权限'}"

        self._set_state("recording")
        self._timer.start(self.max_seconds * 1000)
        return True, "ok"

    def stop(self) -> bytes:
        """停止并返回 WAV 字节；没录到有效声音返回空 bytes。"""
        pcm = b""
        if self._source is not None:
            self._source.stop()
        if self._buffer is not None:
            self._buffer.close()
            pcm = bytes(self._pcm)
        self._cleanup()
        self._set_state("idle")
        if len(pcm) < SAMPLE_RATE * SAMPLE_WIDTH * 0.1:   # 短于 0.1 秒视为空
            return b""
        return _pcm_to_wav(pcm)

    def cancel(self) -> None:
        """放弃这段录音（用户点了取消），数据直接丢弃。"""
        self._timer.stop()
        if self._source is not None:
            self._source.stop()
        self._cleanup()
        self._set_state("idle")

    def _cleanup(self) -> None:
        self._timer.stop()
        if self._buffer is not None:
            if self._buffer.isOpen():
                self._buffer.close()
            self._buffer.setParent(None)
            self._buffer = None
        if self._source is not None:
            self._source.setParent(None)
            self._source = None

    def _on_max_time(self) -> None:
        # 不在这里直接拼 WAV（避免在 Qt 回调里做重活），通知上层来取
        if self._state == "recording":
            self.max_duration_reached.emit()

    def _set_state(self, st: str) -> None:
        if st != self._state:
            self._state = st
            self.state_changed.emit(st)

    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def is_recording(self) -> bool:
        return self._state == "recording"


def _pcm_to_wav(pcm: bytes) -> bytes:
    """裸 Int16 单声道 16k PCM → 带 44 字节头的 WAV（内存中完成，不写盘）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()
