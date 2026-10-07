# -*- coding: utf-8 -*-
"""
商城经济单元测试（B 功能）
==========================
运行：python tests/test_shop.py

测试范围：
  1. shop/catalog.py：加载、坏条目跳过、缺文件不崩、字段校验
  2. shop/economy.py：赚币规则、单日封顶、连续奖励、购买幂等、余额不足、
     零食消耗、喂零食加成、穿戴/脱下/槽位互斥、台词包并入
  3. soul/state.py：旧 state.json 自动补经济字段（向后兼容）
  4. face/pet_renderer.py：配饰叠加（矢量/立绘路径）与坏数据回退
  5. 红线：绝不涉及真实货币（没有任何充值/扣真实钱的入口）

测试原则：不联网、全部 tempfile、QApplication 存全局防 GC 崩溃。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication                        # noqa: E402
from PyQt6.QtCore import Qt                                     # noqa: E402
from PyQt6.QtGui import QPainter, QPixmap                       # noqa: E402

from shop import Catalog, Economy, ShopItem                     # noqa: E402
from soul.state import CESM                                     # noqa: E402

_qapp = None


def _make_qapp():
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dc_shop_"))


GOOD_ITEMS = [
    {"id": "snack_a", "name": "糖果", "kind": "snack", "price": 3, "boost": 2.0},
    {"id": "acc_hat", "name": "帽子", "kind": "accessory", "price": 5,
     "slot": "head", "shape": "hat", "color": [200, 100, 100]},
    {"id": "acc_scarf", "name": "围巾", "kind": "accessory", "price": 6,
     "slot": "neck", "shape": "scarf", "color": [100, 100, 200]},
    {"id": "lines_x", "name": "台词包", "kind": "lines", "price": 4,
     "lines_scene": "pat_lines", "lines": ["新台词一", "新台词二"]},
]


def make_catalog(d: Path, items=None) -> Catalog:
    d.mkdir(parents=True, exist_ok=True)
    p = d / "catalog.json"
    p.write_text(json.dumps({"items": items if items is not None else GOOD_ITEMS},
                            ensure_ascii=False), encoding="utf-8")
    return Catalog.load(p)


def make_eco(d: Path, items=None, coins: int = 0):
    cesm = CESM(d / "state.json", {})
    cesm.state.coins = coins
    eco = Economy(cesm, make_catalog(d / "shop", items))
    return cesm, eco


# ======================================================================
# 1. 商品表加载
# ======================================================================

def test_catalog_loads_good_items():
    d = temp_dir()
    cat = make_catalog(d)
    assert len(cat) == 4 and not cat.warnings
    assert cat.get("acc_hat").slot == "head"
    assert cat.get("lines_x").lines == ["新台词一", "新台词二"]
    assert cat.get("不存在") is None


def test_catalog_skips_bad_entries_without_dying():
    """一条写错不该让整张表消失——这是用户会手改的文件。"""
    d = temp_dir()
    bad = [
        GOOD_ITEMS[0],
        {"id": "", "name": "没id", "kind": "snack", "price": 1},          # 缺 id
        {"id": "x1", "name": "", "kind": "snack", "price": 1},            # 缺 name
        {"id": "x2", "name": "怪类型", "kind": "weapon", "price": 1},      # kind 不认识
        {"id": "x3", "name": "负价", "kind": "snack", "price": -5},        # 负价
        {"id": "x4", "name": "坏槽位", "kind": "accessory", "price": 1,
         "slot": "tail", "shape": "hat"},                                # slot 不认识
        {"id": "x5", "name": "坏外形", "kind": "accessory", "price": 1,
         "slot": "head", "shape": "crown"},                              # shape 不认识
        {"id": "x6", "name": "空台词", "kind": "lines", "price": 1,
         "lines_scene": "pat_lines", "lines": []},                       # 台词为空
        GOOD_ITEMS[1],
        {"id": "acc_hat", "name": "重复id", "kind": "accessory", "price": 1,
         "slot": "head", "shape": "bow"},                                # id 重复
    ]
    cat = make_catalog(d, bad)
    ids = [i.id for i in cat.items]
    assert ids == ["snack_a", "acc_hat"], f"应只保留两条合法商品，实际 {ids}"
    assert len(cat.warnings) == 8, f"应记 8 条警告，实际 {len(cat.warnings)}"


def test_catalog_missing_or_broken_file_is_empty():
    d = temp_dir()
    assert len(Catalog.load(d / "nope.json")) == 0        # 文件不存在
    p = d / "broken.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    cat = Catalog.load(p)
    assert len(cat) == 0 and cat.warnings                # 坏 JSON 也不崩


def test_catalog_hidden_items_are_not_listed():
    d = temp_dir()
    items = GOOD_ITEMS + [{"id": "egg", "name": "彩蛋", "kind": "accessory",
                           "price": 0, "slot": "head", "shape": "bow",
                           "hidden": True}]
    cat = make_catalog(d, items)
    assert "egg" not in [i.id for i in cat.all()]
    assert "egg" in [i.id for i in cat.all(include_hidden=True)]
    assert cat.get("egg") is not None, "隐藏商品仍应能被按 id 取到"


# ======================================================================
# 2. 赚币
# ======================================================================

def test_earn_first_chat_once_per_day():
    d = temp_dir()
    cesm, eco = make_eco(d)
    cesm.apply_event("chat")
    assert eco.balance() == 5, f"每天首聊应 +5，实际 {eco.balance()}"
    cesm.apply_event("chat")
    cesm.apply_event("chat")
    assert eco.balance() == 5, "同一天首聊奖励只给一次"


def test_earn_from_feed_and_chaos():
    d = temp_dir()
    cesm, eco = make_eco(d)
    cesm.feed("meal", "报告.docx")
    assert eco.balance() == 2, "喂一次 +2"
    cesm.apply_event("chaos_success")
    assert eco.balance() == 3, "陪她捣乱成功 +1"
    cesm.apply_event("pat_head")
    assert eco.balance() == 3, "摸头不赚币（否则可以狂点刷币）"


def test_daily_earn_cap():
    d = temp_dir()
    cesm, eco = make_eco(d)
    for _ in range(200):
        cesm.apply_event("chaos_success")
    assert eco.earned_today() == 30, f"单日封顶 30，实际 {eco.earned_today()}"
    assert eco.balance() == 30


def test_daily_cap_resets_next_day():
    d = temp_dir()
    cesm, eco = make_eco(d)
    for _ in range(200):
        cesm.apply_event("chaos_success")
    assert eco.balance() == 30
    # 换一天：封顶额度重置，还能继续赚
    eco._today = lambda: "2099-01-01"
    cesm.apply_event("chaos_success")
    assert eco.balance() == 31, "跨天后应该能继续赚"
    assert eco.earned_today() == 1


def test_streak_bonus_grows_then_caps():
    d = temp_dir()
    cesm, eco = make_eco(d)
    # 第 1、2 天：没有连续奖励
    eco._today = lambda: "2099-01-01"
    eco._yesterday = lambda: "2098-12-31"
    cesm.apply_event("chat")
    assert eco.balance() == 5, f"第 1 天 5 币，实际 {eco.balance()}"
    assert cesm.state.streak_days == 1

    # 第 3 天：+1
    eco._today = lambda: "2099-01-03"
    eco._yesterday = lambda: "2099-01-02"
    cesm.state.last_active_date = "2099-01-02"
    cesm.state.streak_days = 2
    cesm.state.last_first_chat_date = ""
    before = eco.balance()
    cesm.apply_event("chat")
    assert eco.balance() - before == 6, "第 3 天首聊 5+1"

    # 第 6 天：奖励封顶 +3
    eco._today = lambda: "2099-01-06"
    eco._yesterday = lambda: "2099-01-05"
    cesm.state.last_active_date = "2099-01-05"
    cesm.state.streak_days = 5
    cesm.state.last_first_chat_date = ""
    cesm.state.earned_today = 0
    cesm.state.last_earn_date = "2099-01-06"
    before = eco.balance()
    cesm.apply_event("chat")
    assert eco.balance() - before == 8, "第 6 天首聊 5+3（封顶）"


def test_streak_resets_after_gap():
    d = temp_dir()
    cesm, eco = make_eco(d)
    cesm.state.streak_days = 7
    cesm.state.last_active_date = "2099-01-01"
    eco._today = lambda: "2099-01-10"      # 断了 9 天
    eco._yesterday = lambda: "2099-01-09"
    cesm.apply_event("pat_head")
    assert cesm.state.streak_days == 1, "断签要从 1 重新算"


# ======================================================================
# 3. 消费
# ======================================================================

def test_purchase_deducts_and_is_idempotent():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=20)
    ok, msg = eco.purchase("acc_hat")
    assert ok and eco.balance() == 15, msg
    ok2, msg2 = eco.purchase("acc_hat")
    assert not ok2, "已拥有的非消耗品不能重复买"
    assert eco.balance() == 15, "重复购买不能扣钱"
    assert eco.owned_ids().count("acc_hat") == 1


def test_purchase_rejected_when_poor():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=2)
    ok, msg = eco.purchase("acc_hat")
    assert not ok and "还差 3 枚" in msg, msg
    assert eco.balance() == 2 and not eco.owned_ids()


def test_purchase_unknown_item():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=100)
    ok, msg = eco.purchase("不存在")
    assert not ok and eco.balance() == 100


def test_snack_is_consumable_and_stackable():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=20)
    assert eco.purchase("snack_a")[0]
    assert eco.purchase("snack_a")[0], "零食可以重复买"
    assert eco.balance() == 14
    assert eco.snack_counts() == {"snack_a": 2}, "两份库存"

    # 每次喂之前都要把饱腹压低：一份零食 boost=2.0 就顶两顿正餐，
    # 不重置的话第二次会被"已经吃饱了"挡掉，测的就不是库存逻辑了。
    cesm.state.hunger = 30
    ok, _ = eco.feed_snack("snack_a")
    assert ok and eco.snack_counts() == {"snack_a": 1}
    cesm.state.hunger = 30
    ok, _ = eco.feed_snack("snack_a")
    assert ok and eco.snack_counts() == {}, "喂完就不该再显示库存"

    # 吃撑了不该白白消耗库存
    cesm.state.hunger = 99
    eco.purchase("snack_a")
    ok, msg = eco.feed_snack("snack_a")
    assert not ok and "饱" in msg
    assert eco.snack_counts() == {"snack_a": 1}, "被拒绝时不能扣掉那份零食"


def test_feed_snack_without_stock_rejected():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=20)
    ok, msg = eco.feed_snack("snack_a")
    assert not ok and "还没买" in msg


def test_bought_snack_is_tastier_than_plain_feed():
    """文档要求"买的零食喂食更香"：同样饱腹起点下，加成必须高于普通投喂。"""
    d1 = temp_dir()
    cesm1, eco1 = make_eco(d1, coins=20)
    cesm1.state.hunger = 20
    cesm1.feed("meal", "随手拖的文档")          # 普通投喂 = 一顿正餐

    d2 = temp_dir()
    cesm2, eco2 = make_eco(d2, coins=20)
    cesm2.state.hunger = 20
    eco2.purchase("snack_a")
    eco2.feed_snack("snack_a")                  # 买来的零食 boost=2.0

    assert cesm2.state.hunger > cesm1.state.hunger, \
        f"零食应更香：{cesm2.state.hunger} vs {cesm1.state.hunger}"
    assert cesm2.state.mood > cesm1.state.mood, "情绪加成也该更高"


def test_equip_toggle_and_slot_exclusive():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=100)
    eco.purchase("acc_hat")
    eco.purchase("acc_scarf")
    assert eco.equip("acc_hat")[0]
    assert {s: i.id for s, i in eco.equipped_items().items()} == {"head": "acc_hat"}
    assert eco.equip("acc_scarf")[0]
    assert set(eco.equipped_items()) == {"head", "neck"}, "头/脖子可以各戴一件"
    ok, msg = eco.equip("acc_hat")
    assert ok and "head" not in eco.equipped_items(), "再点一次是脱下"


def test_equip_requires_ownership():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=100)
    ok, msg = eco.equip("acc_hat")
    assert not ok and "还没买" in msg
    assert not eco.equipped_items()


def test_equipped_items_filters_stale_ids():
    """存档里留着已删除/未拥有的商品 id 时，渲染层必须拿到空字典而不是崩。"""
    d = temp_dir()
    cesm, eco = make_eco(d, coins=100)
    cesm.state.equipped = {"head": "早就被删掉的东西", "neck": "acc_scarf"}
    assert eco.equipped_items() == {}, "没拥有的都不算数"
    eco.purchase("acc_scarf")
    assert set(eco.equipped_items()) == {"neck"}


def test_lines_pack_merges_and_persists():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=100)
    assert eco.extra_lines() == {}
    eco.purchase("lines_x")
    assert eco.extra_lines() == {"pat_lines": ["新台词一", "新台词二"]}
    # 存档里没拥有的台词包不该算数（换了角色/清了数据之后）
    cesm.state.owned = []
    eco.refresh()
    assert eco.extra_lines() == {}


# ======================================================================
# 4. 向后兼容：旧 state.json 自动补字段
# ======================================================================

def test_old_state_file_gets_economy_defaults():
    """老版本的 state.json 没有经济字段，读进来必须自动补默认值而不是崩。"""
    d = temp_dir()
    old = {"affinity": 60.0, "mood": 55.0, "trust": 41.0, "hunger": 70.0,
           "last_interaction": "", "last_mood_decay_ts": 0.0, "birth_ts": 0.0,
           "counters": {"total_chats": 3}}
    (d / "state.json").write_text(json.dumps(old), encoding="utf-8")
    cesm = CESM(d / "state.json", {})
    assert cesm.state.affinity == 60.0, "旧数据要保留"
    assert cesm.state.coins == 0
    assert cesm.state.owned == [] and cesm.state.snacks == {}
    assert cesm.state.equipped == {} and cesm.state.streak_days == 0
    eco = Economy(cesm, make_catalog(d / "shop"))
    assert eco.balance() == 0 and eco.equipped_items() == {}


def test_economy_state_roundtrip():
    d = temp_dir()
    cesm, eco = make_eco(d, coins=50)
    eco.purchase("acc_hat")
    eco.equip("acc_hat")
    eco.purchase("snack_a")
    # 重新读一遍（模拟重启）
    cesm2 = CESM(d / "state.json", {})
    eco2 = Economy(cesm2, make_catalog(d / "shop"))
    assert eco2.balance() == 42, f"50 - 帽子5 - 零食3 = 42，实际 {eco2.balance()}"
    assert eco2.owned_ids() == ["acc_hat"]
    assert eco2.snack_counts() == {"snack_a": 1}
    assert set(eco2.equipped_items()) == {"head"}, "买的帽子重启后还戴着"


# ======================================================================
# 5. 渲染层：配饰叠加
# ======================================================================

def _render(renderer, accessories=None, sleeping=False) -> QPixmap:
    pix = QPixmap(200, 220)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    snap = {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}
    try:
        renderer.paint(p, pix.width(), pix.height(), 140, 1.0, False, False,
                       snap, sleeping=sleeping, accessories=accessories)
    finally:
        p.end()
    return pix


def _opaque(pix: QPixmap) -> int:
    img = pix.toImage()
    return sum(1 for y in range(0, 220, 3) for x in range(0, 200, 3)
               if img.pixelColor(x, y).alpha() > 0)


def test_renderer_draws_accessories_on_vector():
    _make_qapp()
    from face.pet_renderer import PetRenderer
    hat = ShopItem(id="h", name="帽子", kind="accessory", price=1,
                   slot="head", shape="hat", color=(220, 90, 90))
    for shape in ("cat", "dog", "slime"):
        r = PetRenderer(shape=shape)
        plain = _render(r)
        with_hat = _render(r, {"head": hat})
        assert _opaque(with_hat) > 0, f"{shape} 戴帽子不该白屏"
        assert plain.toImage() != with_hat.toImage(), f"{shape} 戴上帽子画面应该变了"


def test_renderer_draws_accessories_on_avatar():
    _make_qapp()
    from PIL import Image
    from face.pet_renderer import PetRenderer
    d = temp_dir()
    Image.new("RGBA", (40, 60), (200, 200, 200, 255)).save(d / "avatar.png")
    r = PetRenderer(shape="cat", avatar=d / "avatar.png")
    scarf = ShopItem(id="s", name="围巾", kind="accessory", price=1,
                     slot="neck", shape="scarf", color=(200, 60, 60))
    assert _render(r, {"neck": scarf}).toImage() != _render(r).toImage()


def test_renderer_survives_unknown_accessory_shape():
    """商品表被改坏、或者槽位里塞了不认识的东西，渲染层必须静默跳过。"""
    _make_qapp()
    from face.pet_renderer import PetRenderer
    weird = ShopItem(id="w", name="？", kind="accessory", price=1,
                     slot="head", shape="完全不认识的形状", color=(1, 2, 3))
    pix = _render(PetRenderer(shape="cat"), {"head": weird})
    assert _opaque(pix) > 0, "不认识的配饰不该让角色白屏"


def test_renderer_handles_bad_color():
    _make_qapp()
    from face.pet_renderer import PetRenderer
    bad = ShopItem(id="b", name="坏色", kind="accessory", price=1,
                   slot="head", shape="bow", color="这不是颜色")
    assert _opaque(_render(PetRenderer(shape="cat"), {"head": bad})) > 0


def test_renderer_accessories_none_is_unchanged():
    _make_qapp()
    from face.pet_renderer import PetRenderer
    r = PetRenderer(shape="cat")
    assert _render(r).toImage() == _render(r, None).toImage()
    assert _render(r).toImage() == _render(r, {}).toImage()


# ======================================================================
# 6. 红线：不碰真实货币
# ======================================================================

def test_no_real_money_entry_points():
    """确认经济模块里没有任何支付/联网能力。

    为什么用 AST 而不是直接搜源码文本：模块的**文档字符串里本来就写着**
    "没有任何充值入口"这类说明，搜文本会把这句自证清白的话当成违规。
    所以这里只看真正的代码——import 了什么、用了哪些标识符。
    """
    import ast
    import inspect
    from shop import economy as eco_mod

    tree = ast.parse(inspect.getsource(eco_mod))

    forbidden_mods = {"requests", "httpx", "urllib", "socket", "aiohttp",
                      "wechatpay", "alipay", "stripe", "openai"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0].lower()
                assert root_mod not in forbidden_mods, f"不该 import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root_mod = (node.module or "").split(".")[0].lower()
            assert root_mod not in forbidden_mods, f"不该 from {node.module} import"

    used = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for bad in ("wechatpay", "alipay", "stripe", "checkout", "payment", "pay"):
        assert bad not in used, f"经济模块不该出现标识符「{bad}」"


# ======================================================================
# 运行器
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
            print(f"  [ERR ] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    if failed == 0:
        print("ALL TESTS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    print("[运行 商城经济 测试]")
    sys.exit(_run_all())
