# -*- coding: utf-8 -*-
"""
player.py —— 音频播放
=====================
QMediaPlayer + QAudioOutput 播放 TTS 合成出来的音频，可随时 stop。

为什么先落临时文件再播：QMediaPlayer 的 setSource 只认 URL（本地路径/网络），
不能直接吃内存字节。临时文件放在系统 temp，**播完立刻删**，不在用户目录留痕
（对应"音频默认不保存"的隐私约定）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer


class AudioPlayer(QObject):
    started = pyqtSignal()
    finished = pyqtSignal()        # 自然播完
    error_occurred = pyqtSignal(str)

    def __init__(self, volume: float = 1.0, parent: QObject | None = None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        # QAudioOutput 必须挂在 self 上保持引用，否则会被 GC 导致没声音
        self._audio_out = QAudioOutput(self)
        self._audio_out.setVolume(max(0.0, min(1.0, float(volume))))
        self.player.setAudioOutput(self._audio_out)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self._tmp_path: Path | None = None

    # ------------------------------------------------------------------
    def play_bytes(self, audio_bytes: bytes, suffix: str = "mp3") -> bool:
        """播放一段内存音频。正在播就先停掉旧的。返回是否成功开始。"""
        if not audio_bytes:
            return False
        self.stop()
        try:
            # delete=False：Windows 下文件被播放器占用时不能边播边删，所以手动管理
            fd, name = tempfile.mkstemp(prefix="dc_tts_", suffix=f".{suffix}")
            import os
            with os.fdopen(fd, "wb") as f:
                f.write(audio_bytes)
            self._tmp_path = Path(name)
            self.player.setSource(QUrl.fromLocalFile(str(self._tmp_path)))
            self.player.play()
            self.started.emit()
            return True
        except Exception as e:  # noqa: BLE001
            self._cleanup_tmp()
            self.error_occurred.emit(f"播放失败：{type(e).__name__}")
            return False

    def stop(self) -> None:
        """停止当前朗读并清理临时文件。"""
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.player.stop()
        self.player.setSource(QUrl())
        self._cleanup_tmp()

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def set_volume(self, v: float) -> None:
        self._audio_out.setVolume(max(0.0, min(1.0, float(v))))

    # ------------------------------------------------------------------
    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.finished.emit()
            self.player.setSource(QUrl())
            self._cleanup_tmp()

    def _on_error(self, _err, message: str = "") -> None:
        if message:
            self.error_occurred.emit(f"播放出错：{message}")
        self._cleanup_tmp()

    def _cleanup_tmp(self) -> None:
        if self._tmp_path is None:
            return
        try:
            self._tmp_path.unlink(missing_ok=True)
        except OSError:
            # Windows 上文件常被播放器占着删不掉。这时**不能**把路径清空——
            # 清空了就再也没人记得它，这个临时音频会永久留在磁盘上。
            # 留着路径，下一次清理时再试。
            return
        self._tmp_path = None
