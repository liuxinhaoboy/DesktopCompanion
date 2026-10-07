# -*- coding: utf-8 -*-
"""
shop —— 本地单机商城（B 商城经济）
==================================
catalog：商品表（纯数据 + 加载校验）
economy：赚币 / 购买 / 穿戴 / 喂零食

**绝不涉及真实货币、内购、联网支付。** coins 只能由陪伴行为产生。
"""

from .catalog import (Catalog, ShopItem, KIND_ACCESSORY, KIND_LINES,
                      KIND_SNACK, VALID_KINDS)
from .economy import Economy

__all__ = ["Catalog", "ShopItem", "Economy",
           "KIND_SNACK", "KIND_ACCESSORY", "KIND_LINES", "VALID_KINDS"]
