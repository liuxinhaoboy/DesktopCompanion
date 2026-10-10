# -*- coding: utf-8 -*-
"""温暖、统一的窗口抬头组件：聊天窗和设置面板共用。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
)


_TONES = {
    "green": ("#6f9e68", "#f0f7ed", "#dcebd8"),
    "blue": ("#6689b2", "#eff5fb", "#dce8f4"),
}


def brand_header(title: str, subtitle: str, badge: str = "", *,
                 icon: str = "✦", tone: str = "green") -> QFrame:
    """创建紧凑品牌抬头，提供清晰标题、说明和一枚柔和状态标签。"""
    accent, background, border = _TONES.get(tone, _TONES["green"])

    frame = QFrame()
    frame.setObjectName("brandHeader")
    frame.setMinimumHeight(72)
    frame.setMaximumHeight(84)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    frame.setStyleSheet(f"""
        QFrame#brandHeader {{
            background: {background};
            border: 1px solid {border};
            border-radius: 14px;
        }}
        QLabel {{ background: transparent; }}
        QLabel#brandHeaderIcon {{
            background: #ffffff;
            color: {accent};
            border: 1px solid {border};
            border-radius: 20px;
            font-size: 20px;
            font-weight: 700;
        }}
        QLabel#brandHeaderTitle {{
            color: #3a3530;
            font-size: 15px;
            font-weight: 700;
        }}
        QLabel#brandHeaderSubtitle {{
            color: #857b6c;
            font-size: 10px;
        }}
        QLabel#brandHeaderBadge {{
            background: #ffffff;
            color: {accent};
            border: 1px solid {border};
            border-radius: 10px;
            padding: 4px 9px;
            font-size: 9px;
            font-weight: 600;
        }}
    """)

    row = QHBoxLayout(frame)
    row.setContentsMargins(12, 8, 13, 8)
    row.setSpacing(11)

    emblem = QLabel(icon)
    emblem.setObjectName("brandHeaderIcon")
    emblem.setAlignment(Qt.AlignmentFlag.AlignCenter)
    emblem.setFixedSize(40, 40)
    row.addWidget(emblem)

    copy = QVBoxLayout()
    copy.setContentsMargins(0, 0, 0, 0)
    copy.setSpacing(2)
    title_label = QLabel(title)
    title_label.setObjectName("brandHeaderTitle")
    title_label.setTextFormat(Qt.TextFormat.PlainText)
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("brandHeaderSubtitle")
    subtitle_label.setTextFormat(Qt.TextFormat.PlainText)
    copy.addWidget(title_label)
    copy.addWidget(subtitle_label)
    row.addLayout(copy, 1)

    if badge:
        badge_label = QLabel(badge)
        badge_label.setObjectName("brandHeaderBadge")
        badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(badge_label, 0, Qt.AlignmentFlag.AlignRight)

    return frame
