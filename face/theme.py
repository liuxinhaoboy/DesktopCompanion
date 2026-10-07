# -*- coding: utf-8 -*-
"""
theme —— 桌宠统一视觉主题
========================
把"颜色、圆角、控件 QSS"集中在一处，设置页 / 聊天窗 / 气泡共用，
保证整套界面看起来是同一个温暖的家，也方便以后整体换肤。

基调：暖白米色底 + 奶绿主行动色 + 暖橘点缀，柔和、低对比、不刺眼。
"""

from __future__ import annotations

# ---- 语义色板（代码里需要直接取色时用）----
BG = "#fffbf5"          # 窗口底色（暖白）
CARD = "#ffffff"        # 卡片白
SOFT = "#fbf3e7"        # 次级浅米块
BORDER = "#e7ddcd"      # 柔和描边
TEXT = "#3a3530"        # 主文字
MUTED = "#9a8f7d"       # 次要说明
GREEN = "#7fb77e"       # 主行动色（奶绿）
GREEN_HOVER = "#72ad71"
GREEN_PRESS = "#669e65"
ORANGE = "#e0995e"      # 点缀暖橘
BLUE = "#5b8fc4"        # 角色名/链接蓝
DANGER = "#d97066"      # 危险/断开

FONT = "Microsoft YaHei UI"


def app_qss() -> str:
    """整套应用级 QSS。设置页和聊天窗 setStyleSheet(app_qss())。"""
    return f"""
* {{ font-family: '{FONT}'; color:{TEXT}; }}
QWidget {{ background:{BG}; }}

/* ---- 标签页 ---- */
QTabWidget::pane {{ border:1px solid {BORDER}; border-radius:10px; background:{BG};
    top:-1px; }}
QTabBar {{ qproperty-drawBase:0; }}
QTabBar::tab {{
    background:{SOFT}; color:{MUTED}; padding:8px 18px;
    border:1px solid {BORDER}; border-bottom:none;
    border-top-left-radius:9px; border-top-right-radius:9px;
    margin-right:2px; font-size:12px;
}}
QTabBar::tab:hover {{ color:{TEXT}; background:#f6ecdd; }}
QTabBar::tab:selected {{
    background:{BG}; color:{TEXT}; font-weight:bold;
    border-top:2px solid {GREEN}; padding-top:7px;
}}

/* ---- 分组卡片 ---- */
QGroupBox {{
    background:{CARD}; border:1px solid {BORDER}; border-radius:10px;
    margin-top:14px; padding:10px 6px 6px 6px; font-weight:bold; color:#6b5d48;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left:12px; top:8px; padding:0 5px; color:{ORANGE};
}}

/* ---- 滚动区 ---- */
QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
    background:{BG}; border:none;
}}

/* ---- 按钮 ---- */
QPushButton {{
    background:#f6efe3; color:{TEXT}; border:1px solid #e0d3bf;
    border-radius:8px; padding:6px 14px; font-size:12px;
}}
QPushButton:hover {{ background:#efe3d1; border-color:#d8c6ad; }}
QPushButton:pressed {{ background:#e6d7c1; }}
QPushButton:disabled {{ color:#b7aea2; background:#f7f3ec; border-color:#ece5da; }}
QPushButton:checked {{ background:#e7f1e6; border-color:{GREEN}; }}
QPushButton[primary="true"] {{
    background:{GREEN}; color:white; border:none; font-weight:bold; padding:7px 18px;
}}
QPushButton[primary="true"]:hover {{ background:{GREEN_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background:{GREEN_PRESS}; }}
QPushButton[danger="true"] {{ background:#f6e1de; color:{DANGER}; border-color:#ecc4be; }}
QPushButton[danger="true"]:hover {{ background:#f0d2ce; }}

/* ---- 输入控件 ---- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {{
    border:1px solid #ddcfbb; border-radius:8px; padding:5px 8px;
    background:{CARD}; color:{TEXT}; selection-background:{GREEN};
    selection-color:white;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
    border:1.5px solid {GREEN}; background:#fffefb;
}}
QComboBox::drop-down {{ border:none; width:22px; }}
QComboBox QAbstractItemView {{
    background:{CARD}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:6px; selection-background:#e7f1e6; selection-color:{TEXT};
    outline:none; padding:3px;
}}

/* ---- 选择类 ---- */
QCheckBox, QRadioButton, QLabel {{ background:transparent; }}
QCheckBox {{ spacing:7px; padding:2px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width:16px; height:16px; }}
QCheckBox::indicator {{ border:1px solid #cfc0aa; border-radius:4px; background:white; }}
QCheckBox::indicator:hover {{ border-color:{GREEN}; }}
QCheckBox::indicator:checked {{ background:{GREEN}; border-color:{GREEN};
    image:none; }}
QRadioButton::indicator {{ border:1px solid #cfc0aa; border-radius:8px; background:white; }}
QRadioButton::indicator:checked {{ border:4px solid {GREEN}; }}

/* ---- 滚动条（细、柔和）---- */
QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
QScrollBar::handle {{ background:#ddcfbb; border-radius:5px; min-height:28px; }}
QScrollBar::handle:hover {{ background:#cdbda6; }}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{ background:none; border:none; height:0; width:0; }}

/* ---- 提示 ToolTip ---- */
QToolTip {{ background:#4a443d; color:white; border:none; padding:4px 7px;
    border-radius:5px; }}
"""


def hint_style() -> str:
    """次要说明小字的样式（QLabel 用）。"""
    return f"color:{MUTED}; font-size:11px;"
