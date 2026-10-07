# -*- coding: utf-8 -*-
"""
Act —— 行动层（ACL 的手）
========================
把 ActionCandidate 变成真实世界的动静：
  L1 气泡说话 / L2 假死·半透明·剪贴板颜文字 / L3 挡窗口·索要互动

安全规则（架构文档红线）：
  - 所有 L2/L3 的破坏性动作（剪贴板替换、挡窗口）必须先弹确认框
  - 确认框永远用非阻塞方式实现（QMessageBox.show + Future），
    绝不用 exec()——exec 会冻住整个事件循环，让桌宠"真死"而不是"假死"
  - 每次行动写 data/action_log.json：她做了什么、用户什么反应，
    之后聊天时她能引用（"我刚才挡你窗口是因为…"）
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from PyQt6.QtWidgets import QMessageBox

from .decide import ActionCandidate


class ChaosActor:
    def __init__(self, engine, pet, root: Path, chaos_cfg: dict):
        self.engine = engine
        self.pet = pet                      # PetWindow 的引用（她物理上的身体）
        self.log_path = Path(root) / "data" / "action_log.json"
        self.chaos_cfg = chaos_cfg
        # 剪贴板函数做成成员而不是直接 import：单元测试可以替换替身，
        # 绝不会在跑测试时真把用户的剪贴板改了
        self._clipboard_set = self._default_clipboard_set
        self._clipboard_get = self._default_clipboard_get
        # 换剪贴板前留的旧内容。红线要求"覆盖前留快照可恢复"，
        # 之前这条 L2 动作直接覆盖、没留快照，用户的原内容就真丢了。
        self._clipboard_backup: str | None = None

    # ------------------------------------------------------------------
    @staticmethod
    def _default_clipboard_set(text: str) -> None:
        import pyperclip
        pyperclip.copy(text)

    @staticmethod
    def _default_clipboard_get() -> str:
        import pyperclip
        return pyperclip.paste() or ""

    # ------------------------------------------------------------------
    def _log(self, action: str, level: str, detail: str, reaction: str) -> None:
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "action": action, "level": level,
                 "detail": detail, "user_reaction": reaction}
        try:
            log = []
            if self.log_path.exists():
                log = json.loads(self.log_path.read_text(encoding="utf-8"))
            log = (log if isinstance(log, list) else [])[-199:] + [entry]
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[ACL] 行动日志写失败(不影响使用): {e}")

    def _line(self, key: str, fallback: list[str] | None = None) -> str:
        pool = self.engine._character.get(key) or fallback or ["……"]
        return random.choice(pool)

    # ------------------------------------------------------------------
    async def _confirm(self, title: str, text: str) -> bool:
        """
        非阻塞确认框。为什么这么绕：
        QMessageBox.exec() 会开一个局部事件循环并把外层 asyncio 冻住，
        期间感知循环、动画、心跳全部停摆——用户看到的是"程序真卡死了"。
        show() + finished 信号 + asyncio.Future 则完全合作式。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        box = QMessageBox(self.pet)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)   # 默认拒绝更安全

        def _done(res):
            if not fut.done():
                fut.set_result(res == QMessageBox.StandardButton.Yes)
            # 用完必须回收：这个框以宠物窗为父，Qt 默认不随关闭销毁，
            # 每次确认都留一个，聊久了会一直堆积。
            # 项目里其它三处确认框（hands/browser/settings）都已 deleteLater，
            # 只有这里漏了。
            box.deleteLater()
        box.finished.connect(_done)
        box.show()
        return await fut

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def perform(self, cand: ActionCandidate) -> None:
        handler = getattr(self, f"_do_{cand.action_id}", None)
        if handler is None:
            print(f"[ACL] 矩阵里的动作 {cand.action_id} 还没实现，跳过")
            return
        try:
            await handler(cand)
        except Exception as e:  # noqa: BLE001 —— 捣乱失败不许影响主程序
            print(f"[ACL] 动作 {cand.action_id} 执行失败: {e}")

    # ==================================================================
    # L1：无害级
    # ==================================================================
    async def _do_bubble_nag(self, cand: ActionCandidate) -> None:
        self.pet.say(self._line("nag_lines", ["……主人最近好像很忙的样子。"]))
        self._log("bubble_nag", "L1", "气泡吐槽", "无")

    async def _do_idle_care(self, cand: ActionCandidate) -> None:
        minutes = int(cand.context["perception"].mouse_idle_seconds // 60)
        self.pet.say(self._line("idle_care_lines",
                                [f"咦，{minutes}分钟没动静了……是去休息了吗？",
                                 "（看你不在，我就自己发会儿呆。）"]))
        self._log("idle_care", "L1", f"闲置{minutes}分钟关心", "无")

    async def _do_high_cpu_panic(self, cand: ActionCandidate) -> None:
        self.pet.say(self._line("cpu_panic_lines",
                                ["好烫好烫！CPU 都快冒烟了，风扇在哭！",
                                 "电脑要烧起来了……你的代码在谋杀我 (><)"]))
        self.engine.cesm.apply_event("high_cpu")     # 她自己也不舒服
        self._log("high_cpu_panic", "L1", "高负载抱怨", "无")

    async def _do_hour_chime(self, cand: ActionCandidate) -> None:
        self.pet.say(cand.context["text"])
        self._log("hour_chime", "L1", cand.context["text"], "无")

    # ==================================================================
    # L2：干扰级
    # ==================================================================
    async def _do_fake_freeze(self, cand: ActionCandidate) -> None:
        """假装卡住：脸变灰、动画停 6 秒。被双击就抱怨（PetWindow 里实现）。"""
        complain = self._line("freeze_complain_lines",
                              ["……太累了，让我装死一会儿。",
                               "没电了……充电方式是摸头，快。"])
        self.pet.start_fake_freeze(seconds=6, complain=complain)
        self._log("fake_freeze", "L2", "假死6秒", "无")

    async def _do_semi_transparent(self, cand: ActionCandidate) -> None:
        self.pet.set_temp_opacity(0.45, seconds=10)
        self.pet.say("嘿嘿，我隐身啦~（并没有完全隐）")
        self._log("semi_transparent", "L2", "半透明10秒", "无")
        await asyncio.sleep(10)

    async def _do_clipboard_kaomoji(self, cand: ActionCandidate) -> None:
        ok = await self._confirm(
            "小灵的小恶作剧",
            "要把你的剪贴板内容换成颜文字吗？\n（原来的内容会被覆盖掉哦，真的）")
        if not ok:
            self.engine.cesm.apply_event("chaos_rejected")
            self.pet.say("唔……好吧，你不好玩。")
            self._log("clipboard_kaomoji", "L2", "被拒绝", "rejected")
            return
        kaomoji = random.choice(["(｡•̀ᴗ-)✧", "(¬‿¬)", "(๑>ᴗ<๑)", "(ↀДↀ)", "٩(◕‿◕)۶"])
        try:
            # 覆盖前留快照：红线要求"改剪贴板必须能恢复"。
            # 读旧内容失败不算致命（大不了这次没有备份），但不能因此中断恶作剧。
            try:
                self._clipboard_backup = self._clipboard_get()
            except Exception:  # noqa: BLE001
                self._clipboard_backup = None
            self._clipboard_set(kaomoji)
            tip = "（想找回原来的内容：右键我 →「恢复剪贴板」）" \
                if self._clipboard_backup is not None else ""
            self.pet.say(f"嘿嘿，剪贴板被我吃掉了！现在是：{kaomoji}\n{tip}")
            self.engine.cesm.apply_event("chaos_success")
            # 日志只记长度不记正文，避免把用户的剪贴板内容写进磁盘
            self._log("clipboard_kaomoji", "L2",
                      f"替换为 {kaomoji}（原内容 {len(self._clipboard_backup or '')} 字已留快照）",
                      "accepted")
        except Exception as e:  # noqa: BLE001
            self.pet.say("呜……剪贴板被什么护着，我咬不动。")
            self._log("clipboard_kaomoji", "L2", f"失败:{e}", "failed")

    # ------------------------------------------------------------------
    def has_clipboard_backup(self) -> bool:
        """有没有被她换掉的剪贴板内容可以恢复。"""
        return self._clipboard_backup is not None

    def restore_clipboard(self) -> bool:
        """把剪贴板恢复成她恶作剧之前的内容。成功返回 True。"""
        if self._clipboard_backup is None:
            return False
        try:
            self._clipboard_set(self._clipboard_backup)
        except Exception:  # noqa: BLE001
            return False
        self._clipboard_backup = None
        return True

    # ==================================================================
    # L3：强干预级
    # ==================================================================
    async def _do_cover_window(self, cand: ActionCandidate) -> None:
        per = cand.context["perception"]
        ok = await self._confirm(
            "小灵想刷存在感",
            f"她现在就想赖在你那个「{per.active_window_title[:24]}」窗口上面。\n"
            "允许她挪过去挡一下吗？")
        if not ok:
            self.engine.cesm.apply_event("chaos_rejected")
            self.pet.say("哼，不让挡就算了……记仇了。")
            self._log("cover_window", "L3", "被拒绝", "rejected")
            return
        moved = self.pet.move_over_active_window()
        if moved:
            self.pet.say(self._line("cover_lines",
                                    ["让开让开，本精灵才是主角！",
                                     "工作什么的最讨厌了，看我！"]))
            self.engine.cesm.apply_event("chaos_success")
            self._log("cover_window", "L3", f"挡住 {per.active_window_title[:40]}", "accepted")
        else:
            self._log("cover_window", "L3", "找不到目标窗口", "failed")

    async def _do_demand_attention(self, cand: ActionCandidate) -> None:
        """索要互动：这个框本身就是确认。用户的选择直接决定数值走向。"""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        box = QMessageBox(self.pet)
        box.setWindowTitle("小灵要互动！")
        box.setText("她叉着腰堵在你面前：\n「理我！现在！立刻！」")
        hug = box.addButton("抱抱她", QMessageBox.ButtonRole.YesRole)
        feed = box.addButton("给零食", QMessageBox.ButtonRole.YesRole)
        box.addButton("现在没空", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(feed)

        def _done():
            if fut.done():
                return
            clicked = box.clickedButton()
            if clicked is hug:
                fut.set_result("hug")
            elif clicked is feed:
                fut.set_result("feed")
            else:
                fut.set_result("reject")
            box.deleteLater()      # 同上：确认框用完要回收，否则越堆越多
        box.finished.connect(_done)
        box.show()
        result = await fut

        if result == "hug":
            self.pet.say("唔！(*/ω＼*) 突然抱……也不是不行啦。")
            self.engine.cesm.apply_event("praise")
            # 让下一轮聊天知道刚才发生了什么
            self.engine._push_history("user", "(主人刚刚抱了抱你)")
            self._log("demand_attention", "L3", "用户抱了她", "hug")
        elif result == "feed":
            line, _delta = self.engine.react("feed")
            self.pet.say(line)
            self._log("demand_attention", "L3", "用户给了零食", "feed")
        else:
            self.pet.say("……哦。那你去忙吧。（明显低气压）")
            self.engine.cesm.apply_event("chaos_rejected")
            self._log("demand_attention", "L3", "被冷落", "rejected")


if __name__ == "__main__":
    print("这是库模块。整体行为用 python main.py 启动桌宠后观察。")
