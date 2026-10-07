# -*- coding: utf-8 -*-
"""voice 包：录音 / 语音转写 / 朗读合成 / 播放 / 状态机编排。

模块分工：
  recorder.py  用 Qt 自带的 QAudioSource 录音（不依赖需要编译的 pyaudio）
  stt.py       OpenAI 兼容 /audio/transcriptions 语音转文字
  tts.py       edge-tts 为主合成语音（可选 OpenAI 兼容 /audio/speech）
  player.py    QMediaPlayer 播放合成结果，可随时停止
  service.py   把上面四件事串成一条可取消的状态机，供 ChatPanel 调用
"""
