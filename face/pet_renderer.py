# -*- coding: utf-8 -*-
"""
PetRenderer —— 桌宠外观渲染器
==============================
把"怎么画"从 PetWindow 里抽出来：PetWindow 只管窗口、交互和动画状态，
PetRenderer 负责根据角色形状（cat/dog/slime）和当前情绪把小家伙画出来。

为什么要抽这一层：
  P2 要支持多角色切换，不同角色长得不一样（猫有尖耳、狗有垂耳、史莱姆没耳朵）。
  如果绘制逻辑散在 PetWindow.paintEvent 里，每加一个角色就要改窗口代码。
  现在 PetWindow 只调 renderer.paint()，换角色 = 换一个 Renderer。

支持两种渲染模式（manifest.json 的 renderer 字段）：
  - "vector"：QPainter 代码绘制（内置 cat/dog/slime 三种形状）
  - "sprites"：PNG 序列帧（P2 预留接口，后续阶段填充）
"""

from __future__ import annotations

import math
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import (QColor, QPainter, QPen, QBrush, QRadialGradient,
                         QPainterPath, QPixmap)


def _rgb_to_qcolor(rgb) -> QColor:
    """manifest 里的 [r,g,b] 数组 → QColor。异常时返回灰色兜底。"""
    try:
        return QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except (TypeError, IndexError, ValueError):
        return QColor(180, 180, 180)


# 假死时的固定灰色
_FROZEN_STYLE = {"body": QColor(172, 172, 176), "eye": QColor(72, 72, 78)}

# 图片型角色：mood_word -> sprites/ 下的动作帧文件名。
# 为什么写在这里：一套立绘按情绪换姿势，是"她开心就蹦、生气就叉腰"的核心。
# 没匹配上的情绪（或文件缺失）自动退回 avatar.png，再不行才画矢量。
_MOOD_SPRITES = {
    "生气": "angry.png",
    "开心": "happy.png",
    "兴奋": "greet.png",
    "难过": "sad.png",
    "低落": "sleepy.png",
    "平静": "read.png",
}


class PetRenderer:
    """根据角色形状和情绪快照绘制桌宠。一个角色对应一个 Renderer 实例。"""

    def __init__(self, shape: str = "cat", mood_styles: dict | None = None,
                 sprites_dir: Path | None = None, avatar: Path | None = None,
                 model3d: Path | None = None, display: str = "auto"):
        self.shape = shape if shape in ("cat", "dog", "slime") else "cat"
        # ---- 3D 桌宠支持 ----
        # display：auto=有模型就3D / 2d=强制矢量 / 3d=有模型用模型没模型回退2D
        self._display = display if display in ("2d", "3d", "auto") else "auto"
        self._model3d_path = Path(model3d) if model3d else None
        self._model = None               # 懒加载的 Model3D 实例
        self._model_failed = False       # 加载失败过一次就不再反复读盘
        # 把 manifest 的 [r,g,b] 颜色表转成 QColor 表
        self._styles: dict[str, dict[str, QColor]] = {}
        for mood, colors in (mood_styles or {}).items():
            self._styles[mood] = {
                "body": _rgb_to_qcolor(colors.get("body")),
                "eye": _rgb_to_qcolor(colors.get("eye")),
            }
        self._default_style = self._styles.get("平静", {
            "body": QColor(168, 200, 236), "eye": QColor(44, 58, 74)})
        self._sprites_dir = sprites_dir
        self._sprite_cache: dict[str, QPixmap] = {}
        # 自定义立绘：用户放一张透明 PNG 就能当桌宠，不用改代码。
        # 图片坏了/不存在时 _avatar_pixmap 为 None，自动回退画矢量形状。
        self._avatar_path = Path(avatar) if avatar else None
        self._avatar_pixmap: QPixmap | None = None
        self._avatar_load_failed = False

    def update_styles(self, mood_styles: dict) -> None:
        """切换角色时更新颜色表。"""
        self._styles.clear()
        for mood, colors in (mood_styles or {}).items():
            self._styles[mood] = {
                "body": _rgb_to_qcolor(colors.get("body")),
                "eye": _rgb_to_qcolor(colors.get("eye")),
            }
        self._default_style = self._styles.get("平静", self._default_style)

    # ------------------------------------------------------------------
    # 自定义立绘加载
    # ------------------------------------------------------------------
    def _get_avatar(self) -> QPixmap | None:
        """懒加载立绘。只尝试一次，失败就永久回退矢量绘制（避免每帧读盘）。"""
        if self._avatar_pixmap is not None or self._avatar_load_failed:
            return self._avatar_pixmap
        self._avatar_load_failed = True
        if not self._avatar_path or not self._avatar_path.is_file():
            return None
        pix = QPixmap(str(self._avatar_path))
        if pix.isNull():
            print(f"[Renderer] 立绘无法读取，回退矢量绘制：{self._avatar_path}")
            return None
        self._avatar_pixmap = pix
        self._avatar_load_failed = False
        return pix

    def _load_sprite(self, path: Path) -> QPixmap | None:
        """加载一张动作帧并缓存。读失败返回 None（当帧不画，退回默认）。"""
        key = str(path)
        cached = self._sprite_cache.get(key)
        if cached is not None:
            return cached
        pix = QPixmap(str(path))
        if pix.isNull():
            return None
        self._sprite_cache[key] = pix
        return pix

    def _pick_sprite(self, snap: dict) -> QPixmap | None:
        """按情绪从 sprites/ 选动作帧。没有多帧目录就退回单张 avatar.png。"""
        d = self._sprites_dir
        if d is None or not d.is_dir():
            return self._get_avatar()
        # 睡觉优先于一切：她都睡着了，不该还叉着腰生气。
        # sleepy.png 缺失就继续走下面的情绪帧逻辑，不会白屏。
        if snap.get("_sleeping"):
            candidate = d / "sleepy.png"
            if candidate.is_file():
                pix = self._load_sprite(candidate)
                if pix is not None:
                    return pix
        # 生气优先：is_angry 标志哪怕 mood_word 还没更新也立刻叉腰
        if snap.get("is_angry"):
            candidate = d / "angry.png"
            if candidate.is_file():
                pix = self._load_sprite(candidate)
                if pix is not None:
                    return pix
        fname = _MOOD_SPRITES.get(snap.get("mood_word", "平静"))
        if fname:
            candidate = d / fname
            if candidate.is_file():
                pix = self._load_sprite(candidate)
                if pix is not None:
                    return pix
        return self._get_avatar()

    def _paint_avatar(self, p: QPainter, w: int, h: int, size: int,
                      phase: float, snap: dict, pix: QPixmap) -> None:
        """画自定义立绘，并叠加和矢量角色一致的情绪效果。

        为什么图片也要做情绪效果：用户换了自己的图，仍然希望"她难过时看起来
        蔫蔫的、生气时泛红"。否则自定义角色会变成一张没有生命的贴图。
        """
        mood_word = snap.get("mood_word", "平静")
        angry = snap.get("is_angry", False)
        hunger = snap.get("hunger", 100)
        mood = snap.get("mood", 50)

        # 呼吸起伏：和矢量角色同一套参数，视觉节奏一致
        breathe = 1 + 0.012 * math.sin(phase)
        # 生气时轻微左右抖动，表达"她在闹脾气"
        jitter = 0.0
        if angry:
            jitter = math.sin(phase * 6) * size * 0.012

        # 关键：把整张立绘完整 contain 进窗口，而不是固定宽=size。
        # Q 版全身立绘比矢量圆高得多，按宽度硬塞会顶出窗口、把头顶裁掉。
        # 取"窗口宽/图宽"和"窗口高/图高"里较小的那个，保证从头到脚都在窗口里。
        margin = 10
        base = min((w - margin) / pix.width(), (h - margin) / pix.height())
        base *= breathe
        dw = pix.width() * base
        dh = pix.height() * base
        dx = (w - dw) / 2 + jitter
        dy = h - dh - 4

        target = QRectF(dx, dy, dw, dh)
        p.setOpacity(1.0)

        # 低落/难过：整体压暗，像蒙了层灰
        dim = 1.0
        if mood_word in ("低落", "难过"):
            dim = 0.82
        elif mood < 40:
            dim = 0.9
        if hunger < 40:
            dim *= 0.92
        # 睡觉：整体压暗，像屋里关了灯
        if snap.get("_sleeping"):
            dim *= 0.72

        if dim < 1.0:
            p.setOpacity(dim)
        p.drawPixmap(target, pix, QRectF(pix.rect()))
        p.setOpacity(1.0)

        # 生气/开心时叠一层情绪色，用叠加模式保持原图细节
        tint = None
        if angry:
            tint = QColor(232, 120, 120, 52)
        elif mood_word in ("兴奋", "开心"):
            tint = QColor(255, 200, 140, 34)
        if tint is not None:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            p.fillRect(target, tint)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 假死时盖一层灰
        if snap.get("_frozen"):
            p.fillRect(target, QColor(172, 172, 176, 130))

    # ------------------------------------------------------------------
    # 3D 模型加载与绘制
    # ------------------------------------------------------------------
    def _get_model(self):
        """懒加载 Model3D。失败只打印一次并永久回退 2D（不逐帧读盘）。"""
        if self._display == "2d":
            return None
        if not self._model3d_path:
            return None
        if self._model is not None or self._model_failed:
            return self._model
        from face.model3d import Model3D      # 延迟导入：不用3D就完全不碰numpy解析
        self._model = Model3D.load(self._model3d_path)
        self._model_failed = self._model is None
        return self._model

    def _paint_model3d(self, p: QPainter, w: int, h: int, size: int,
                       phase: float, snap: dict, model) -> None:
        """画 3D 模型：缓慢自转 + 呼吸俯仰 + 情绪色调，和 2D 角色同一套节奏。"""
        mood_word = snap.get("mood_word", "平静")
        angry = snap.get("is_angry", False)
        hunger = snap.get("hunger", 100)
        mood = snap.get("mood", 50)

        cx, cy = w / 2, h - size / 2 - 6
        radius = size / 2 * (1 + 0.012 * math.sin(phase))   # 呼吸：和2D同参数
        yaw = phase * 0.16                                  # 慢速自转（一圈约40秒）
        pitch = 0.12 + 0.05 * math.sin(phase * 0.7)         # 轻微俯仰摆动
        if angry:
            yaw += math.sin(phase * 3) * 0.1                # 生气时晃得厉害点

        tint = None
        if snap.get("_frozen"):
            tint, dim = QColor(120, 120, 126, 150), 0.6
        elif snap.get("_sleeping"):
            # 睡觉：偏冷偏暗的夜色，和假死的"死灰"区分开
            tint, dim = QColor(110, 125, 165, 95), 0.62
        elif angry:
            tint, dim = QColor(232, 120, 120, 60), 1.0
        elif mood_word in ("兴奋", "开心"):
            tint, dim = QColor(255, 200, 140, 42), 1.0
        else:
            dim = 1.0
        if mood_word in ("低落", "难过"):
            dim *= 0.82
        elif mood < 40:
            dim *= 0.9
        if hunger < 40:
            dim *= 0.92

        model.render(p, cx, cy, radius, yaw, pitch, tint=tint, dim=dim)

    # ------------------------------------------------------------------
    # 统一绘制入口
    # ------------------------------------------------------------------
    def paint(self, p: QPainter, w: int, h: int, size: int,
              phase: float, blink: bool, frozen: bool, snap: dict,
              sleeping: bool = False, accessories: dict | None = None) -> None:
        """
        参数：
          p        已开启抗锯齿的 QPainter
          w/h      窗口宽高
          size     角色基准尺寸（config.character.size）
          phase    呼吸相位（连续递增的浮点数）
          blink    是否正在眨眼
          frozen   是否假死（ACL 的 L2 捣乱）
          snap     CESM 情绪快照（mood_word/mood/is_angry/hunger...）
          sleeping 是否睡着（昼夜作息）。假死优先——睡着了被点破还是假死的样子。
          accessories 当前穿戴的配饰 {槽位: 带 .shape/.color 的对象}，画在本体之上。
        """
        # 假死/睡态通过参数传入（snapshot 里没有这两个字段）。
        if frozen:
            snap = dict(snap)
            snap["_frozen"] = True
        elif sleeping:
            snap = dict(snap)
            snap["_sleeping"] = True

        # 3D 优先：用户传了模型（且没强制2D）就画模型；模型坏了解析失败，
        # _get_model 返回 None，下面立绘/矢量路径原样接管——绝不白屏。
        model = self._get_model()
        if model is not None:
            self._paint_model3d(p, w, h, size, phase, snap, model)
            if accessories:
                # 3D 是绕中心自转的，配饰只能"贴"在固定锚点上——
                # 头会转但帽子不转，看久了有点怪，但比完全不显示好。
                mcx, mcy = w / 2, h - size / 2 - 6
                mr = size / 2
                self._draw_accessories(p, accessories,
                                       head=(mcx, mcy - mr * 0.92),
                                       neck=(mcx, mcy + mr * 0.72), unit=mr * 0.40)
            return

        pix = self._pick_sprite(snap)
        if pix is not None:
            self._paint_avatar(p, w, h, size, phase, snap, pix)
            if accessories:
                # 立绘是按 contain 缩放到窗口里的，用同一套算法反推头/脖子的位置，
                # 否则换个角色配饰就飘到别处去了。
                margin = 10
                scale = min((w - margin) / pix.width(), (h - margin) / pix.height())
                scale *= 1 + 0.012 * math.sin(phase)
                dw, dh = pix.width() * scale, pix.height() * scale
                dx, dy = (w - dw) / 2, h - dh - 4
                self._draw_accessories(p, accessories,
                                       head=(dx + dw / 2, dy + dh * 0.20),
                                       neck=(dx + dw / 2, dy + dh * 0.74),
                                       unit=dw * 0.15)
            return

        style = self._resolve_style(snap, frozen)
        angry = (not frozen) and (not sleeping) and snap.get("is_angry", False)
        happy = (not frozen) and (not sleeping) and snap.get("mood", 50) >= 65

        cx = w / 2
        base_r = size / 2
        cy = h - base_r - 6

        # 呼吸：±1.2% 起伏
        r = base_r * (1 + 0.012 * math.sin(phase))
        cy += base_r - r

        # 睡着 = 一直闭着眼（和眨眼的画法复用同一条分支）
        if self.shape == "dog":
            self._paint_dog(p, cx, cy, r, style, blink or sleeping, snap["mood_word"],
                            angry, happy, sleeping)
        elif self.shape == "slime":
            self._paint_slime(p, cx, cy, r, style, blink or sleeping, snap["mood_word"],
                              angry, happy, phase, sleeping)
        else:
            self._paint_cat(p, cx, cy, r, style, blink or sleeping, snap["mood_word"],
                            angry, happy, sleeping)

        if accessories:
            self._draw_accessories(p, accessories,
                                   head=(cx, cy - r * 0.86),
                                   neck=(cx, cy + r * 0.80), unit=r * 0.42)

    # ------------------------------------------------------------------
    # 配饰叠加（商城买的帽子/蝴蝶结/围巾/眼镜）
    # 三种渲染路径共用这里的形状绘制，只是锚点与 unit 不同。
    # ------------------------------------------------------------------
    def _draw_accessories(self, p: QPainter, accessories: dict,
                          head, neck, unit: float) -> None:
        if not accessories or unit <= 0:
            return
        # 先画脖子上的（围巾），再画头上的——头上的应该压在最上面
        item = accessories.get("neck")
        if item is not None:
            self._draw_accessory(p, getattr(item, "shape", ""),
                                 getattr(item, "color", (200, 200, 200)),
                                 neck, unit)
        item = accessories.get("head")
        if item is not None:
            self._draw_accessory(p, getattr(item, "shape", ""),
                                 getattr(item, "color", (200, 200, 200)),
                                 head, unit)

    def _draw_accessory(self, p: QPainter, shape: str, color,
                        center, unit: float) -> None:
        c = color if isinstance(color, QColor) else _rgb_to_qcolor(color)
        cx, cy = center
        line_w = max(1.4, unit * 0.07)

        if shape == "hat":
            p.setPen(QPen(c.darker(140), line_w))
            p.setBrush(c)
            p.drawEllipse(QPointF(cx, cy), unit * 1.15, unit * 0.26)   # 帽檐
            path = QPainterPath()                                      # 帽顶
            path.moveTo(cx - unit * 0.60, cy)
            path.quadTo(cx - unit * 0.52, cy - unit * 1.10, cx, cy - unit * 1.10)
            path.quadTo(cx + unit * 0.52, cy - unit * 1.10, cx + unit * 0.60, cy)
            path.closeSubpath()
            p.drawPath(path)
            p.setPen(Qt.PenStyle.NoPen)                                # 帽带
            p.setBrush(c.darker(155))
            p.drawRect(QRectF(cx - unit * 0.58, cy - unit * 0.20,
                              unit * 1.16, unit * 0.15))
        elif shape == "bow":
            p.setPen(QPen(c.darker(140), line_w * 0.8))
            p.setBrush(c)
            for sx in (-1, 1):
                path = QPainterPath()
                path.moveTo(cx, cy)
                path.quadTo(cx + sx * unit * 1.00, cy - unit * 0.70,
                            cx + sx * unit * 1.05, cy + unit * 0.15)
                path.quadTo(cx + sx * unit * 0.78, cy + unit * 0.55, cx, cy)
                p.drawPath(path)
            p.setBrush(c.lighter(125))
            p.drawEllipse(QPointF(cx, cy), unit * 0.21, unit * 0.21)
        elif shape == "scarf":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(cx - unit * 1.00, cy - unit * 0.22,
                                     unit * 2.00, unit * 0.44),
                              unit * 0.20, unit * 0.20)
            path = QPainterPath()                                      # 垂下来那一截
            path.moveTo(cx + unit * 0.45, cy + unit * 0.10)
            path.lineTo(cx + unit * 0.95, cy + unit * 1.20)
            path.lineTo(cx + unit * 0.45, cy + unit * 1.32)
            path.lineTo(cx + unit * 0.12, cy + unit * 0.20)
            path.closeSubpath()
            p.setBrush(c.darker(112))
            p.drawPath(path)
        elif shape == "glasses":
            p.setPen(QPen(c, max(1.6, unit * 0.10)))
            p.setBrush(QColor(255, 255, 255, 46))
            for sx in (-1, 1):
                p.drawEllipse(QPointF(cx + sx * unit * 0.60, cy),
                              unit * 0.40, unit * 0.40)
            p.drawLine(QPointF(cx - unit * 0.20, cy), QPointF(cx + unit * 0.20, cy))

    def _resolve_style(self, snap: dict, frozen: bool) -> dict[str, QColor]:
        if frozen:
            return dict(_FROZEN_STYLE)
        style = self._styles.get(snap.get("mood_word", "平静"),
                                 self._default_style)
        style = dict(style)   # 拷贝，下面可能改色
        # 饥饿时整体变萎
        if snap.get("hunger", 100) < 40:
            style["body"] = QColor(style["body"]).darker(118)
        return style

    # ------------------------------------------------------------------
    # cat —— 尖耳 + ω 嘴（原始造型）
    # ------------------------------------------------------------------
    def _paint_cat(self, p, cx, cy, r, style, blink, mood_word, angry, happy,
                   sleeping=False):
        body, eye_c = style["body"], style["eye"]
        self._draw_body(p, cx, cy, r, body, ry_ratio=0.96)
        self._draw_cat_ears(p, cx, cy, r, body)
        self._draw_face(p, cx, cy, r, eye_c, mood_word, angry, happy, blink,
                        mouth_style="cat", sleeping=sleeping)

    def _draw_cat_ears(self, p, cx, cy, r, color):
        p.setPen(QPen(color.darker(135), 2))
        p.setBrush(QColor(color.lighter(105)))
        for sx in (-1, 1):
            ex = cx + sx * r * 0.52
            ey = cy - r * 0.82
            path = QPainterPath()
            path.moveTo(ex - 12 * sx, ey + 26)
            path.quadTo(ex - 4 * sx, ey - 16, ex + 16 * sx, ey - 6)
            path.quadTo(ex + 14 * sx, ey + 18, ex - 12 * sx, ey + 26)
            p.drawPath(path)

    # ------------------------------------------------------------------
    # dog —— 垂耳 + 圆鼻 + 开心吐舌头
    # ------------------------------------------------------------------
    def _paint_dog(self, p, cx, cy, r, style, blink, mood_word, angry, happy,
                   sleeping=False):
        body, eye_c = style["body"], style["eye"]
        # 垂耳要画在身体后面（先画耳朵再画身体，耳朵上半被身体遮住像垂下来）
        self._draw_dog_ears(p, cx, cy, r, body)
        self._draw_body(p, cx, cy, r, body, ry_ratio=0.94)
        self._draw_face(p, cx, cy, r, eye_c, mood_word, angry, happy, blink,
                        mouth_style="dog", sleeping=sleeping)

    def _draw_dog_ears(self, p, cx, cy, r, color):
        """下垂的椭圆耳朵，贴在脑袋左右两侧。"""
        ear_color = color.darker(112)
        p.setPen(QPen(color.darker(135), 2))
        p.setBrush(ear_color)
        for sx in (-1, 1):
            ex = cx + sx * r * 0.88
            ey = cy - r * 0.15
            # 垂耳：竖着的椭圆，用路径画成上窄下宽的水滴感
            path = QPainterPath()
            path.moveTo(ex - sx * r * 0.06, ey - r * 0.55)
            path.quadTo(ex + sx * r * 0.42, ey - r * 0.2,
                        ex + sx * r * 0.30, ey + r * 0.45)
            path.quadTo(ex + sx * r * 0.05, ey + r * 0.62,
                        ex - sx * r * 0.18, ey + r * 0.30)
            path.quadTo(ex - sx * r * 0.22, ey - r * 0.25,
                        ex - sx * r * 0.06, ey - r * 0.55)
            p.drawPath(path)

    # ------------------------------------------------------------------
    # slime —— 无耳扁团 + 顶部高光
    # ------------------------------------------------------------------
    def _paint_slime(self, p, cx, cy, r, style, blink, mood_word,
                     angry, happy, phase, sleeping=False):
        body, eye_c = style["body"], style["eye"]
        # 史莱姆更扁更宽，底部微微波动
        w_r = r * 1.12
        h_r = r * 0.86
        wobble = 0.015 * math.sin(phase * 0.7)   # 缓慢形变
        grad = QRadialGradient(cx - w_r * 0.25, cy - h_r * 0.35, w_r * 1.7)
        grad.setColorAt(0.0, body.lighter(122))
        grad.setColorAt(1.0, body.darker(108))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(body.darker(130), 2))
        # 用路径画一个上圆下平的史莱姆轮廓
        path = QPainterPath()
        path.moveTo(cx - w_r, cy + h_r * 0.85)
        path.quadTo(cx - w_r * 1.05, cy - h_r, cx, cy - h_r * (0.98 + wobble))
        path.quadTo(cx + w_r * 1.05, cy - h_r, cx + w_r, cy + h_r * 0.85)
        # 底部波浪边
        path.quadTo(cx + w_r * 0.5, cy + h_r * (1.0 + wobble),
                    cx, cy + h_r * 0.92)
        path.quadTo(cx - w_r * 0.5, cy + h_r * (1.0 - wobble),
                    cx - w_r, cy + h_r * 0.85)
        p.drawPath(path)

        # 顶部大高光（史莱姆的果冻质感）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawEllipse(QPointF(cx - w_r * 0.3, cy - h_r * 0.55),
                      w_r * 0.28, h_r * 0.16)

        self._draw_face(p, cx, cy + h_r * 0.08, w_r * 0.88, eye_c,
                        mood_word, angry, happy, blink,
                        mouth_style="slime", sleeping=sleeping)

    # ------------------------------------------------------------------
    # 共用部件
    # ------------------------------------------------------------------
    def _draw_body(self, p, cx, cy, r, color, ry_ratio=0.96):
        grad = QRadialGradient(cx - r * 0.25, cy - r * 0.35, r * 1.7)
        grad.setColorAt(0.0, color.lighter(118))
        grad.setColorAt(1.0, color.darker(112))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(color.darker(135), 2))
        p.drawEllipse(QPointF(cx, cy), r, r * ry_ratio)

    def _draw_face(self, p, cx, cy, r, eye_color, mood_word,
                   angry, happy, blink, mouth_style="cat", sleeping=False):
        eye_dx = r * 0.34
        eye_y = cy - r * 0.08
        er = r * 0.115

        # ---- 眼睛 ----
        for sx in (-1, 1):
            e = QPointF(cx + sx * eye_dx, eye_y)
            if blink:
                pen = QPen(eye_color, max(3.0, r * 0.05))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(QRectF(e.x() - er, e.y() - er / 2, er * 2, er),
                          180 * 16, 180 * 16)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(eye_color)
                p.drawEllipse(e, er, er * 1.18)
                p.setBrush(QColor(255, 255, 255, 230))
                p.drawEllipse(QPointF(e.x() - er * 0.3, e.y() - er * 0.42),
                              er * 0.32, er * 0.32)
                if angry:
                    pen = QPen(QColor(90, 40, 35), max(3.0, r * 0.045))
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    p.setPen(pen)
                    p.drawLine(QPointF(e.x() - sx * er * 1.2, e.y() - er * 2.1),
                               QPointF(e.x() + sx * er * 0.9, e.y() - er * 1.5))

        # ---- 嘴巴（三种角色略有差异）----
        mouth_y = cy + r * 0.30
        pen = QPen(eye_color, max(2.4, r * 0.04))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        mw = r * 0.16

        if sleeping:
            # 睡着了：微微张着嘴（有呼吸的感觉），不画红晕也不画生气的表情
            p.setBrush(QColor(eye_color).lighter(140))
            p.drawEllipse(QPointF(cx, mouth_y + mw * 0.15), mw * 0.32, mw * 0.26)
            p.setBrush(Qt.BrushStyle.NoBrush)
        elif angry or mood_word in ("低落", "难过"):
            p.drawArc(QRectF(cx - mw, mouth_y - mw / 2, mw * 2, mw),
                      195 * 16, 150 * 16)
        elif happy or mood_word in ("兴奋", "开心"):
            if mouth_style == "dog":
                self._draw_dog_mouth(p, cx, mouth_y, mw, r)
            elif mouth_style == "slime":
                # 史莱姆开心时是一个小小的 U
                p.drawArc(QRectF(cx - mw * 0.7, mouth_y - mw * 0.3,
                                 mw * 1.4, mw * 1.2), 200 * 16, 140 * 16)
            else:
                # 猫：ω 形嘴
                p.drawArc(QRectF(cx - mw, mouth_y - mw / 2, mw, mw),
                          185 * 16, 170 * 16)
                p.drawArc(QRectF(cx, mouth_y - mw / 2, mw, mw),
                          185 * 16, 170 * 16)
        else:
            p.drawLine(QPointF(cx - mw * 0.6, mouth_y + 2),
                       QPointF(cx + mw * 0.6, mouth_y + 2))

        # dog 多一个鼻子
        if mouth_style == "dog":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(eye_color).darker(120))
            p.drawEllipse(QPointF(cx, mouth_y - r * 0.12),
                          r * 0.07, r * 0.055)

        # ---- 红晕 ----
        if happy and not angry:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(240, 130, 130, 80))
            for sx in (-1, 1):
                p.drawEllipse(QPointF(cx + sx * r * 0.55, eye_y + r * 0.28),
                              r * 0.13, r * 0.075)

    def _draw_dog_mouth(self, p, cx, mouth_y, mw, r):
        """小狗开心时张嘴 + 粉色舌头。"""
        # 张嘴：一个小椭圆
        p.setPen(QPen(QColor(80, 50, 40), max(2.0, r * 0.035)))
        p.setBrush(QColor(90, 55, 50))
        p.drawEllipse(QPointF(cx, mouth_y + mw * 0.2), mw * 0.7, mw * 0.55)
        # 舌头
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(240, 140, 150))
        p.drawEllipse(QPointF(cx, mouth_y + mw * 0.45),
                      mw * 0.32, mw * 0.42)
