# -*- coding: utf-8 -*-
"""Bubble —— 角色头顶的说话气泡"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QPointF
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QFontMetrics
from PyQt6.QtWidgets import QWidget, QApplication


class BubbleWindow(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        font = QFont("Microsoft YaHei UI", 11)
        font.setBold(False)
        self.setFont(font)
        self._text = ""
        self._hide_timer = QTimer(self, singleShot=True)
        self._hide_timer.timeout.connect(self.hide)

    def show_text(self, text: str, anchor: dict, duration_ms: int = 6000) -> None:
        self._text = text.strip()
        if not self._text:
            return
        fm: QFontMetrics = self.fontMetrics()
        max_inner_w = 230
        inner = fm.boundingRect(QRect(0, 0, max_inner_w, 800),
                                int(Qt.TextFlag.TextWordWrap), self._text)
        pad_x, pad_y, tail_h = 14, 11, 10
        w = int(inner.width()) + pad_x * 2
        h = int(inner.height()) + pad_y * 2 + tail_h
        self.setFixedSize(w, h)
        x = int(anchor["x"] + (anchor["w"] - w) / 2)
        y = int(anchor["y"] - h)
        scr = QApplication.primaryScreen()
        screen_top = scr.availableGeometry().y() if scr else 0
        if y < screen_top:
            y = int(anchor["y"] + anchor.get("h", 0) + 8)
        self.move(x, y)
        self.update()
        self.show()
        self.raise_()
        self._hide_timer.start(duration_ms)

    def paintEvent(self, _event) -> None:
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad_x, pad_y, tail_h = 14, 11, 10
        body = QRectF(0, 0, self.width(), self.height() - tail_h)
        path = QPainterPath()
        path.addRoundedRect(body, 12, 12)
        cx = self.width() / 2
        tail = QPainterPath()
        tail.moveTo(cx - 9, body.bottom())
        tail.lineTo(cx + 9, body.bottom())
        tail.lineTo(cx, self.height() - 1)
        tail.closeSubpath()
        full = path.united(tail)
        p.fillPath(full, QColor(255, 250, 242, 245))
        p.strokePath(full, QPen(QColor(120, 105, 90, 180), 1.4))
        p.setPen(QColor(70, 60, 55))
        p.drawText(body.adjusted(pad_x, pad_y, -pad_x, -pad_y),
                   Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                   self._text)


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    b = BubbleWindow()
    b.show_text("我是气泡！(｡•̀ᴗ-)✧\n你正在打字也不会被我打扰哦。",
                {"x": 500, "y": 500, "w": 160, "h": 160}, 4000)
    sys.exit(app.exec())
