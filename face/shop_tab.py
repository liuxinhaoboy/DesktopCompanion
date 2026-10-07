# -*- coding: utf-8 -*-
"""shop_tab —— 设置面板的「商店」页（B 商城经济）"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QGroupBox, QFormLayout, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from face.theme import hint_style
from shop import KIND_ACCESSORY, KIND_LINES, KIND_SNACK

CATEGORY_TITLES = (
    (KIND_SNACK, "零食（买了可以喂她，比随手拖个文件香）"),
    (KIND_ACCESSORY, "配饰（买了可以给她戴上，看得见）"),
    (KIND_LINES, "台词包（买了她会多说几句）"),
)


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(hint_style())
    lab.setWordWrap(True)
    return lab


def _section(title: str) -> QGroupBox:
    box = QGroupBox(title)
    lay = QFormLayout(box)
    lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    lay.setSpacing(8)
    box._form = lay
    return box


class ShopTab(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._rows: dict[str, dict] = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        lay = QVBoxLayout(page)

        wallet = _section("小金库")
        self.balance_label = QLabel("0 枚")
        self.balance_label.setStyleSheet("font-size:16px; font-weight:600;")
        wallet._form.addRow("余额", self.balance_label)
        self.today_label = QLabel("0 / 30")
        wallet._form.addRow("今天已赚", self.today_label)
        self.streak_label = QLabel("0 天")
        wallet._form.addRow("连续陪她", self.streak_label)
        wallet._form.addRow(_hint(
            "币只能靠陪她赚：每天第一次聊天 +5（连续第 3 天起还有递增奖励）、"
            "喂她吃东西 +2、陪她完成一次小捣乱 +1。单日最多赚 30 枚。\n"
            "这里没有充值入口，也不会花你一分钱——她的零花钱只能由陪伴产生。"))
        lay.addWidget(wallet)

        for kind, title in CATEGORY_TITLES:
            box = _section(title)
            items = self.controller.catalog.all(kind)
            if not items:
                box._form.addRow(_hint("这一类暂时没有商品。"))
            for item in items:
                box._form.addRow(self._build_item_row(item))
            lay.addWidget(box)

        self.tip_label = _hint("")
        lay.addWidget(self.tip_label)
        lay.addStretch(1)
        scroll.setWidget(page)
        outer.addWidget(scroll)

    def _build_item_row(self, item) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        name = QLabel(f"<b>{item.name}</b>")
        price = QLabel(f"{item.price} 币")
        price.setStyleSheet("color:#8a7a4a;")
        h.addWidget(name)
        h.addWidget(price)
        h.addStretch(1)
        buy_btn = QPushButton("买下")
        buy_btn.clicked.connect(lambda _, i=item.id: self._on_buy(i))
        h.addWidget(buy_btn)
        use_btn = QPushButton("使用")
        use_btn.clicked.connect(lambda _, i=item.id: self._on_use(i))
        h.addWidget(use_btn)
        desc = QLabel(item.desc)
        desc.setStyleSheet(hint_style())
        desc.setWordWrap(True)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 6)
        wl.setSpacing(2)
        wl.addWidget(row)
        wl.addWidget(desc)
        self._rows[item.id] = {"item": item, "buy": buy_btn,
                               "use": use_btn, "price": price}
        return wrap

    def _on_buy(self, item_id: str) -> None:
        ok, msg = self.controller.buy_item(item_id)
        self._tip(msg, error=not ok)
        self.refresh()

    def _on_use(self, item_id: str) -> None:
        item = self.controller.catalog.get(item_id)
        if item is None:
            return
        if item.kind == KIND_SNACK:
            ok, msg = self.controller.feed_snack(item_id)
        else:
            ok, msg = self.controller.equip_item(item_id)
        self._tip(msg, error=not ok)
        self.refresh()

    def _tip(self, text: str, error: bool = False) -> None:
        color = "#c62828" if error else "#3d6b3d"
        self.tip_label.setStyleSheet(f"color:{color};")
        self.tip_label.setText(text)

    def refresh(self) -> None:
        eco = self.controller.economy
        wallet = self.controller.wallet()
        self.balance_label.setText(f"{wallet['coins']} 枚")
        self.today_label.setText(f"{wallet['earned_today']} / 30")
        self.streak_label.setText(f"{wallet['streak_days']} 天")
        equipped = eco.equipped_items()
        snacks = eco.snack_counts()
        for item_id, row in self._rows.items():
            item = row["item"]
            owned = eco.is_owned(item_id)
            coins = wallet["coins"]
            if item.kind == KIND_SNACK:
                left = snacks.get(item_id, 0)
                row["buy"].setEnabled(coins >= item.price)
                row["buy"].setText(f"买下（{item.price} 币）")
                row["use"].setVisible(True)
                row["use"].setEnabled(left > 0)
                row["use"].setText(f"喂她（还有 {left} 份）" if left else "喂她（已吃完）")
            elif item.kind == KIND_ACCESSORY:
                if owned:
                    row["buy"].setEnabled(False)
                    row["buy"].setText("已拥有")
                    wearing = equipped.get(item.slot) is not None and \
                        equipped[item.slot].id == item_id
                    row["use"].setVisible(True)
                    row["use"].setEnabled(True)
                    row["use"].setText("取下" if wearing else "戴上")
                else:
                    row["buy"].setEnabled(coins >= item.price)
                    row["buy"].setText(f"买下（{item.price} 币）")
                    row["use"].setVisible(False)
            else:
                row["use"].setVisible(False)
                if owned:
                    row["buy"].setEnabled(False)
                    row["buy"].setText("已拥有")
                else:
                    row["buy"].setEnabled(coins >= item.price)
                    row["buy"].setText(f"买下（{item.price} 币）")
