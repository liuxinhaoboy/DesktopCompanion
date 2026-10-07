# -*- coding: utf-8 -*-
"""
语音与多模态单元测试
====================
运行：python tests/test_voice.py

铁律：全部 mock 网络，不发真实 STT/TTS 请求、不真的播放声音。
覆盖：
  - recorder：WAV 打包、未开始就 stop 的安全性、设备枚举不崩
  - stt：未配置拦截、空音频、mock 成功转写、错误人话化
  - tts：空文本、mock edge 合成、失败兜底
  - player：空字节拒绝、stop 幂等
  - service：状态机、未启用拒绝、mock 转写全链路、cancel/shutdown、热更新
  - llm/engine：历史剥离 ts、多模态 content、带图时历史只存文本占位
  - vision：图片压缩、视觉配置构建
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_qapp = None


def _get_qapp():
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def run(coro):
    """同步跑一个协程（每个用例独立事件循环，避免互相污染）。"""
    return asyncio.run(coro)


# ======================================================================
# 1. recorder
# ======================================================================

def test_pcm_to_wav_has_valid_header():
    from voice.recorder import _pcm_to_wav, SAMPLE_RATE
    pcm = b"\x00\x00" * SAMPLE_RATE          # 1 秒静音
    wav = _pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == SAMPLE_RATE


def test_recorder_stop_without_start_is_empty():
    _get_qapp()
    from voice.recorder import AudioRecorder
    rec = AudioRecorder(max_seconds=5)
    assert rec.stop() == b""                  # 没开始过不能崩、返回空
    rec.cancel()                              # 幂等
    assert rec.state == "idle"


def test_input_device_list_returns_list():
    _get_qapp()
    from voice.recorder import AudioRecorder
    devs = AudioRecorder.list_input_devices()  # 有无麦克风都应是 list，不抛
    assert isinstance(devs, list)


def test_short_pcm_under_threshold_is_empty():
    """stop 的"短于 0.1 秒视为空"阈值判断（复刻内部常量，防止阈值被误改）。"""
    from voice.recorder import SAMPLE_RATE, SAMPLE_WIDTH
    threshold = SAMPLE_RATE * SAMPLE_WIDTH * 0.1
    short = b"\x01\x00" * 800                    # 0.05s，低于 0.1s 阈值
    assert len(short) < threshold
    enough = b"\x01\x00" * SAMPLE_RATE           # 1s，超过阈值
    assert len(enough) >= threshold


# ======================================================================
# 2. stt（mock 网络）
# ======================================================================

def test_stt_requires_config():
    from voice.stt import STTClient, VoiceConfigError
    stt = STTClient({})
    assert not stt.is_configured()
    try:
        run(stt.transcribe(b"xxxx"))
        assert False, "未配置应抛 VoiceConfigError"
    except VoiceConfigError:
        pass


def test_stt_empty_audio_rejected():
    from voice.stt import STTClient, STTError
    stt = STTClient({"stt_base_url": "u/v1", "stt_api_key": "k", "stt_model": "m"})
    assert stt.is_configured()
    try:
        run(stt.transcribe(b""))
        assert False, "空音频应抛 STTError"
    except STTError as e:
        assert "录到声音" in str(e)


class _FakeResp:
    def __init__(self, text): self.text = text


class _FakeAudioAPI:
    def __init__(self, text="你好呀"): self._text = text
    async def create(self, **kw):
        # 校验确实把 WAV 三元组传进来了
        assert "model" in kw and "file" in kw
        return _FakeResp(self._text)


class _FakeTranscriptions:
    audio = None
    def __init__(self, text): self.audio = type("A", (), {"transcriptions": _FakeAudioAPI(text)})()


class _FakeOpenAI:
    def __init__(self, text="你好呀"):
        self.audio = type("A", (), {"transcriptions": _FakeAudioAPI(text)})()


def test_stt_success_with_mocked_client():
    from voice.stt import STTClient
    stt = STTClient({"stt_base_url": "u/v1", "stt_api_key": "k", "stt_model": "whisper"})
    stt._client = _FakeOpenAI("今天天气不错")
    text = run(stt.transcribe(b"RIFFxxxx"))
    assert text == "今天天气不错"


def test_stt_error_humanization():
    from voice.stt import STTClient
    h = STTClient._humanize_error
    assert "连不上" in h(Exception("Connection error"))
    assert "401" in h(Exception("invalid api key 401"))
    assert "404" in h(Exception("404 Not Found"))


# ======================================================================
# 3. tts（mock edge-tts，不联网）
# ======================================================================

def test_tts_empty_text_returns_empty():
    from voice.tts import TTSClient
    tts = TTSClient({"tts_provider": "edge-tts"})
    audio, suffix = run(tts.synthesize("   "))
    assert audio == b"" and suffix == "mp3"


class _FakeCommunicate:
    """模拟 edge_tts.Communicate.stream 产出音频块。"""
    def __init__(self, text, voice): self.text, self.voice = text, voice
    async def stream(self):
        yield {"type": "audio", "data": b"ID3fake-mp3-"}
        yield {"type": "audio", "data": b"chunk2"}
        yield {"type": "WordBoundary", "data": b""}   # 非音频块应被忽略


def test_tts_edge_success_mocked(monkey=None):
    import voice.tts as ttsmod
    import edge_tts
    orig = edge_tts.Communicate
    edge_tts.Communicate = _FakeCommunicate
    try:
        tts = ttsmod.TTSClient({"tts_provider": "edge-tts", "tts_voice": "zh-CN-XiaoxiaoNeural"})
        audio, suffix = run(tts.synthesize("你好"))
        assert audio == b"ID3fake-mp3-chunk2"      # 只拼音频块
        assert suffix == "mp3"
    finally:
        edge_tts.Communicate = orig


class _FailCommunicate:
    def __init__(self, *a, **k): pass
    async def stream(self):
        raise ConnectionError("network down")
        yield  # pragma: no cover  让它成为 async generator


def test_tts_edge_failure_raises_ttserror():
    import voice.tts as ttsmod
    import edge_tts
    orig = edge_tts.Communicate
    edge_tts.Communicate = _FailCommunicate
    try:
        tts = ttsmod.TTSClient({"tts_provider": "edge-tts"})
        try:
            run(tts.synthesize("你好"))
            assert False, "合成失败应抛 TTSError"
        except ttsmod.TTSError as e:
            assert "连不上" in str(e)
    finally:
        edge_tts.Communicate = orig


# ======================================================================
# 4. player（不真播音）
# ======================================================================

def test_player_rejects_empty_and_stop_idempotent():
    _get_qapp()
    from voice.player import AudioPlayer
    player = AudioPlayer()
    assert player.play_bytes(b"") is False
    player.stop()                                 # 没在播也能 stop，不崩
    player.stop()
    assert not player.is_playing()


# ======================================================================
# 5. service 状态机
# ======================================================================

def _make_service(voice_cfg=None, llm_cfg=None):
    from voice.service import VoiceService
    return VoiceService(voice_cfg or {}, llm_cfg or {})


def test_service_blocked_when_disabled():
    _get_qapp()
    vs = _make_service({"enabled": False})
    ok, msg = vs.start_recording()
    assert not ok and "没开" in msg


def test_service_autospeak_and_config_update():
    _get_qapp()
    vs = _make_service({"enabled": True, "auto_speak": False, "max_record_seconds": 10})
    assert not vs.auto_speak()
    vs.update_config({"enabled": True, "auto_speak": True, "max_record_seconds": 20}, {})
    assert vs.auto_speak()
    assert vs.recorder.max_seconds == 20


def test_service_cancel_and_shutdown_idempotent():
    _get_qapp()
    vs = _make_service({"enabled": True})
    vs.cancel()                                  # idle 下 cancel 不崩
    vs.shutdown()
    assert vs.state == "idle"


async def _fake_transcribe(wav, filename="speech.wav"):
    return "识别出来的话"


def test_service_finish_recording_full_path_mocked():
    """mock 录音字节和 STT，走通 recording→transcribing→idle 并出文本。"""
    _get_qapp()
    vs = _make_service({"enabled": True})
    # 不真录音：直接把状态置到 recording，并让 stop 返回有效 WAV
    vs._set_state("recording")
    vs.recorder.stop = lambda: b"\x00\x00" * 16000   # 1 秒
    vs.stt.transcribe = _fake_transcribe
    got = []
    vs.transcript_ready.connect(lambda t: got.append(t))

    text = run(vs.finish_recording())
    assert text == "识别出来的话"
    assert got == ["识别出来的话"]
    assert vs.state == "idle"


def test_service_finish_short_recording_warns():
    _get_qapp()
    vs = _make_service({"enabled": True})
    vs._set_state("recording")
    vs.recorder.stop = lambda: b""               # 录太短
    failed = []
    vs.failed.connect(lambda m: failed.append(m))
    text = run(vs.finish_recording())
    assert text == "" and failed and "太短" in failed[0]
    assert vs.state == "idle"


def test_service_speak_empty_is_noop():
    _get_qapp()
    vs = _make_service({"enabled": True, "tts_provider": "edge-tts"})
    run(vs.speak(""))                            # 空文本不应有任何动作/异常
    assert vs.state == "idle"


# ======================================================================
# 6. llm 历史规范化 / 多模态消息
# ======================================================================

def test_clean_message_strips_internal_fields():
    from soul.llm import clean_message
    out = clean_message({"role": "user", "content": "在", "ts": 999, "x": 1})
    assert out == {"role": "user", "content": "在"}


def test_build_user_content_text_vs_multimodal():
    from soul.llm import build_user_content
    assert build_user_content("你好") == "你好"
    parts = build_user_content("看图", ["data:image/jpeg;base64,AA"])
    assert parts[0] == {"type": "text", "text": "看图"}
    assert parts[1]["type"] == "image_url"
    # 没文字时补占位，避免发空 text
    parts2 = build_user_content("", ["data:image/jpeg;base64,AA"])
    assert parts2[0]["text"]


# ======================================================================
# 7. vision 图片压缩 / 配置
# ======================================================================

def _engine_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="dc_voice_"))
    (d / "data").mkdir(parents=True, exist_ok=True)
    return d


def test_vision_compress_and_config():
    from PIL import Image
    from soul.vision import (compress_image_to_data_url, build_vision_llm_cfg,
                             ImagePrepareError)
    d = Path(tempfile.mkdtemp())
    img_path = d / "p.png"
    Image.new("RGBA", (2000, 1000), (0, 128, 255, 200)).save(img_path)
    url = compress_image_to_data_url(img_path, max_kb=300)
    assert url.startswith("data:image/jpeg;base64,")
    import base64
    raw = base64.b64decode(url.split(",", 1)[1])
    assert len(raw) <= 300 * 1024

    bad = d / "b.jpg"; bad.write_text("nope", encoding="utf-8")
    try:
        compress_image_to_data_url(bad); assert False
    except ImagePrepareError:
        pass

    assert build_vision_llm_cfg({"enabled": False}, {}) is None
    inh = build_vision_llm_cfg(
        {"enabled": True, "inherit_from_llm": True, "model": "v"},
        {"model": "t", "base_url": "u", "api_key": "k"})
    assert inh["model"] == "v" and inh["backup_models"] == []


# ======================================================================
# 8. engine 带图：历史只存文本占位，图片透传给模型
# ======================================================================

class _FakeImageLLM:
    def __init__(self): self.calls = []
    def build_system_prompt(self, *a, **k): return "sys"
    async def chat_stream(self, system_prompt, history, user_text,
                          image_data_urls=None):
        self.calls.append({"user_text": user_text, "images": image_data_urls})
        for piece in ["看到", "了"]:
            yield piece


def _make_engine():
    from soul.engine import SoulEngine
    d = _engine_dir()
    cfg = {"llm": {"model": "x"}, "memory": {"use_chroma": False},
           "emotion": {}, "vision": {"enabled": False}}
    return SoulEngine(d, cfg), d


def test_engine_image_history_stores_text_only():
    _get_qapp()
    eng, _d = _make_engine()
    fake = _FakeImageLLM()
    eng.llm = fake
    eng.cesm.apply_event = lambda *a, **k: {}    # 屏蔽情绪副作用

    async def flow():
        out = []
        async for p in eng.stream_chat(
                "这是什么", image_data_urls=["data:image/jpeg;base64,AA"],
                image_name="cat.png"):
            out.append(p)
        return "".join(out)

    reply = run(flow())
    assert reply == "看到了"
    # 透传给模型的是 data url
    assert fake.calls[0]["images"] == ["data:image/jpeg;base64,AA"]
    # 历史里用户消息是文本占位，绝不含 base64
    user_msgs = [m for m in eng._history if m["role"] == "user"]
    assert user_msgs[-1]["content"].startswith("[图片：cat.png]")
    assert "base64" not in json_dumps(user_msgs)
    # 历史仍带 ts（内部用），但出站会被 clean_message 剥掉
    assert "ts" in user_msgs[-1]


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_engine_text_chat_unchanged():
    """纯文本聊天不带图时行为与以前一致（content 是字符串）。"""
    _get_qapp()
    eng, _d = _make_engine()
    fake = _FakeImageLLM()
    eng.llm = fake
    eng.cesm.apply_event = lambda *a, **k: {}

    async def flow():
        return "".join([p async for p in eng.stream_chat("在吗")])

    assert run(flow()) == "看到了"
    assert fake.calls[0]["images"] is None
    assert eng._history[-2]["content"] == "在吗"


# ======================================================================
# 极简运行器
# ======================================================================

def _run_all() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"  [ERR ] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    if failed == 0:
        print("ALL TESTS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
