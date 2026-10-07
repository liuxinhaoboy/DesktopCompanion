# -*- coding: utf-8 -*-
"""
vision.py —— 图片多模态辅助
==========================
两件事：
  1. 把用户选的本地图片压缩、转码成 data:image/jpeg;base64（发给多模态模型）
  2. 根据设置里的 vision 段，决定带图请求发给哪个模型（继承文本模型 / 独立视觉模型）

历史里绝不存 Base64（又大又费钱），只存"[图片：文件名]"占位——这层约定在
SoulEngine.stream_chat 里落地，本模块只负责"这一次请求要用的图"。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

# 多模态不需要原图那么大：长边压到 1568（主流视觉模型的推荐尺寸），省 token
MAX_LONG_EDGE = 1568
# 兼容的输入后缀
SUPPORTED_SUFFIX = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
# JPEG 逐档降质的下限：再低画质就糊得没法看了，宁可这次发送失败
MIN_JPEG_QUALITY = 20


class ImagePrepareError(Exception):
    """图片打不开/格式不支持等可预期问题，消息可直接给用户看。"""


def compress_image_to_data_url(path, max_kb: int = 500) -> str:
    """读取本地图片 → 压缩 → data URL 字符串。

    策略：先按长边等比缩小，再从 quality=85 逐档降到 20，直到体积达标；
    始终转 JPEG（透明底垫白，避免 PNG 透明区在某些模型里变黑）。"""
    try:
        from PIL import Image
    except ImportError as e:  # Pillow 是已装依赖，这只是防御
        raise ImagePrepareError("缺少 Pillow 图片库，无法处理图片。") from e

    p = Path(path)
    if not p.is_file():
        raise ImagePrepareError("找不到这张图片，重新选一次试试。")
    if p.suffix.lower() not in SUPPORTED_SUFFIX:
        raise ImagePrepareError(
            f"暂不支持 {p.suffix or '这种'} 格式，用 jpg/png/webp 都行。")

    try:
        img = Image.open(p)
        img.load()
    except Exception as e:  # noqa: BLE001 —— 损坏/加密/不是真图片
        raise ImagePrepareError("这张图打不开，可能已经损坏了。") from e

    # 透明/调色板/灰度统一转成铺白底的 RGB，JPEG 不支持 alpha
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    # 长边等比缩小
    long_edge = max(img.size)
    if long_edge > MAX_LONG_EDGE:
        ratio = MAX_LONG_EDGE / long_edge
        img = img.resize((max(1, int(img.size[0] * ratio)),
                          max(1, int(img.size[1] * ratio))),
                         Image.LANCZOS)

    max_bytes = max(20, int(max_kb)) * 1024
    quality = 85
    data = b""
    # 步长 12 从 85 出发会落在 85/73/61/49/37/25，再减就掉到 13 直接退出循环——
    # 下限 20 那一档永远试不到。这里显式夹到下限，保证最后一档一定跑过。
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes or quality <= MIN_JPEG_QUALITY:
            break
        quality = max(MIN_JPEG_QUALITY, quality - 12)
    if not data:
        raise ImagePrepareError("图片压缩失败，换一张试试。")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def build_vision_llm_cfg(vision_cfg: dict | None,
                         llm_cfg: dict | None) -> dict | None:
    """算出"带图请求该用哪套连接参数"。
    返回 None 表示没启用视觉（调用方就退回主文本模型，很多模型本身能看图）。"""
    vision = vision_cfg or {}
    llm = llm_cfg or {}
    if not vision.get("enabled", False):
        return None

    if vision.get("inherit_from_llm", True):
        merged = dict(llm)
        # 勾了继承但单独填了视觉模型名/地址/密钥，就用填的覆盖
        if vision.get("model"):
            merged["model"] = vision["model"]
        if vision.get("base_url"):
            merged["base_url"] = vision["base_url"]
        if vision.get("api_key"):
            merged["api_key"] = vision["api_key"]
        merged["backup_models"] = []   # 视觉模型不做备用模型轮询，避免发到纯文本模型
        return merged

    # 完全独立的视觉端点
    return {
        "base_url": vision.get("base_url") or llm.get("base_url"),
        "api_key": vision.get("api_key") or llm.get("api_key"),
        "model": vision.get("model") or llm.get("model"),
        "timeout_seconds": llm.get("timeout_seconds", 90),
        "max_tokens": llm.get("max_tokens", 800),
        "backup_models": [],
    }
