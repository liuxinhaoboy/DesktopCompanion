# -*- coding: utf-8 -*-
"""
economy —— 本地单机经济（B 商城经济）
=====================================
闭环：**陪伴行为赚币 → 买零食/配饰/台词包 → 喂她、给她戴上、听她说新台词**。

三条不可越界的红线：
  1. **绝不涉及真实货币、内购、联网支付**。coins 只能由陪伴行为产生，
     没有任何充值入口，也没有任何地方会花用户的钱。
  2. **不做惩罚性通胀**：捣乱被拒绝不扣币、冷落不扣币。
     币只增不减（除了消费），否则她会变成"逼你上线"的工具。
  3. **数值全在 catalog.json 里**，调平衡不用改代码。

数据存在 state.json（每角色独立）里，跟好感/情绪走同一套持久化与损坏自愈。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .catalog import KIND_ACCESSORY, KIND_LINES, Catalog, ShopItem

# 赚币规则（基础值；改这里要同步 README 的说明）
EARN_FIRST_CHAT = 5     # 每天第一次跟她说话
EARN_FEED = 2           # 成功喂一次（拖文件或喂零食）
EARN_CHAOS = 1          # 陪她完成一次 L1 小捣乱（她得逞了）
DAILY_EARN_CAP = 30     # 单日封顶：防止挂机刷币，也让"每天来一次"就够了

# 连续互动奖励：第 3 天起每天多给 1/2/3 币（封顶 3）
STREAK_BONUS_FROM_DAY = 3
STREAK_BONUS_MAX = 3

EARN_RULES = {
    "feed": EARN_FEED,
    "chaos_success": EARN_CHAOS,
}

# 喂零食时的"正餐基准"：HUNGER_EVENTS["meal"] = 35，
# 商品 boost=1.0 就相当于拖一个文档喂她一顿。
MEAL_BASE_HUNGER = 35


class Economy:
    """她的小金库。所有操作都是本地事务，失败只会返回人话说明，不会抛给界面。"""

    def __init__(self, cesm, catalog: Catalog):
        self._cesm = cesm
        self.catalog = catalog or Catalog()
        self._extra_lines: dict[str, list[str]] = {}
        self._cesm.add_event_listener(self.on_event)
        self._sync_lines()

    # ------------------------------------------------------------------
    # 日期（抽成方法是为了测试能替换掉，不用真的改系统时间）
    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @classmethod
    def _yesterday(cls) -> str:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # 赚币
    # ------------------------------------------------------------------
    def on_event(self, event: str, delta: dict) -> None:
        """CESM 事件监听入口。挂在这里而不是散在各模块：CESM 是唯一事件入口，
        挂别处迟早漏掉某条路径（以后新增互动方式时尤其明显）。"""
        if event in ("chat", "feed", "pat_head", "chaos_success"):
            self._touch_day()
        if event == "chat":
            self._earn_first_chat_of_day()
        elif event in EARN_RULES:
            self.earn(event)

    def _touch_day(self) -> None:
        """记一次"今天有互动"，维护连续天数。"""
        st = self._cesm.state
        today = self._today()
        if st.last_active_date == today:
            return
        # 昨天也来过 → 连续 +1；否则断签，从 1 重新开始
        st.streak_days = (st.streak_days + 1
                          if st.last_active_date == self._yesterday() else 1)
        st.last_active_date = today
        self._cesm.save()

    def _earn_first_chat_of_day(self) -> int:
        st = self._cesm.state
        today = self._today()
        if st.last_first_chat_date == today:
            return 0
        st.last_first_chat_date = today
        got = self.earn("first_chat_of_day",
                        amount=EARN_FIRST_CHAT + self.streak_bonus())
        self._cesm.save()
        return got

    def streak_bonus(self) -> int:
        """连续互动奖励：第 3 天 +1、第 4 天 +2、第 5 天起 +3（封顶）。"""
        days = int(getattr(self._cesm.state, "streak_days", 0))
        if days < STREAK_BONUS_FROM_DAY:
            return 0
        return min(days - STREAK_BONUS_FROM_DAY + 1, STREAK_BONUS_MAX)

    def earn(self, kind: str, amount: int | None = None) -> int:
        """赚币。返回**实际**到账数量（被封顶时可能少于名义值，甚至为 0）。"""
        st = self._cesm.state
        self._roll_day()
        amt = EARN_RULES.get(kind, 0) if amount is None else int(amount)
        if amt <= 0:
            return 0
        room = max(0, DAILY_EARN_CAP - int(st.earned_today))
        got = min(amt, room)
        if got:
            st.coins = int(st.coins) + got
            st.earned_today = int(st.earned_today) + got
            self._cesm.save()
        return got

    def _roll_day(self) -> None:
        """跨天就把"今天赚了多少"清零（封顶是按天算的）。"""
        st = self._cesm.state
        today = self._today()
        if st.last_earn_date != today:
            st.last_earn_date = today
            st.earned_today = 0

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def balance(self) -> int:
        return int(self._cesm.state.coins)

    def earned_today(self) -> int:
        self._roll_day()
        return int(self._cesm.state.earned_today)

    def owned_ids(self) -> list[str]:
        return list(self._cesm.state.owned)

    def snack_counts(self) -> dict:
        return {k: int(v) for k, v in self._cesm.state.snacks.items() if int(v) > 0}

    def is_owned(self, item_id: str) -> bool:
        item = self.catalog.get(item_id)
        if item is None:
            return False
        if item.is_consumable:
            return self.snack_counts().get(item_id, 0) > 0
        return item_id in self._cesm.state.owned

    def equipped_items(self) -> dict:
        """当前穿戴：{槽位: ShopItem}。**已经过滤掉失效的 id**——
        商品表被改过、或者存档里留着已删商品的 id 时，这里直接跳过，
        渲染器就不会因为拿不到外观而报错。"""
        out: dict[str, ShopItem] = {}
        for slot, item_id in dict(self._cesm.state.equipped).items():
            item = self.catalog.get(item_id)
            if item is None or item.kind != KIND_ACCESSORY:
                continue
            if item_id not in self._cesm.state.owned:
                continue
            out[slot] = item
        return out

    def extra_lines(self) -> dict:
        """台词包带来的额外台词：{台词池: [句子...]}。"""
        return {k: list(v) for k, v in self._extra_lines.items()}

    # ------------------------------------------------------------------
    # 消费
    # ------------------------------------------------------------------
    def purchase(self, item_id: str) -> tuple[bool, str]:
        """买。配饰/台词包买断（重复购买拒绝，保证幂等）；零食按份数累加。"""
        item = self.catalog.get(item_id)
        if item is None:
            return False, "商店里没有这件东西。"
        st = self._cesm.state
        if not item.is_consumable and item_id in st.owned:
            return False, f"「{item.name}」你已经有了。"
        if int(st.coins) < item.price:
            need = item.price - int(st.coins)
            return False, f"还差 {need} 枚币，多陪陪我嘛~"
        st.coins = int(st.coins) - item.price
        if item.is_consumable:
            st.snacks[item_id] = int(st.snacks.get(item_id, 0)) + 1
        else:
            st.owned.append(item_id)
        self._cesm.save()
        if item.kind == KIND_LINES:
            self._sync_lines()
        return True, f"买下了「{item.name}」。"

    def equip(self, item_id: str) -> tuple[bool, str]:
        """穿戴 / 再点一次脱下。只能穿已拥有的配饰。"""
        item = self.catalog.get(item_id)
        if item is None or item.kind != KIND_ACCESSORY:
            return False, "这件不能穿戴。"
        st = self._cesm.state
        if item_id not in st.owned:
            return False, "还没买这个呢。"
        if st.equipped.get(item.slot) == item_id:
            st.equipped.pop(item.slot, None)
            self._cesm.save()
            return True, f"把「{item.name}」取下来了。"
        st.equipped[item.slot] = item_id
        self._cesm.save()
        return True, f"给她戴上「{item.name}」啦~"

    def feed_snack(self, item_id: str) -> tuple[bool, str]:
        """喂一份买来的零食。走 CESM 的喂食通道，但用商品自己的加成倍数，
        所以比随手拖个文件香（文档要求"买的零食喂食更香"）。"""
        item = self.catalog.get(item_id)
        if item is None or not item.is_consumable:
            return False, "这个不能喂。"
        st = self._cesm.state
        count = int(st.snacks.get(item_id, 0))
        if count <= 0:
            return False, "这个零食你还没买（或者已经喂完了）。"
        if float(st.hunger) >= 95:
            return False, "她已经吃得饱饱的，等会儿再喂吧。"
        st.snacks[item_id] = count - 1
        if st.snacks[item_id] <= 0:
            st.snacks.pop(item_id, None)
        delta = self._cesm.feed("meal", item.name, boost=item.boost)
        self._cesm.save()
        return True, f"喂了她一份「{item.name}」——{delta.get('comment', '')}"

    # ------------------------------------------------------------------
    def _sync_lines(self) -> None:
        """把已拥有的台词包展开成 {台词池: [句子]}。买完/换角色后都要重算。"""
        merged: dict[str, list[str]] = {}
        for item in self.catalog.all(KIND_LINES, include_hidden=True):
            if item.id not in self._cesm.state.owned:
                continue
            if not item.lines_scene:
                continue
            merged.setdefault(item.lines_scene, []).extend(item.lines)
        self._extra_lines = merged

    def refresh(self) -> None:
        """换角色/换存档后重新对齐（台词包跟着角色数据走）。"""
        self._sync_lines()
