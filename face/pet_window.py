# -*- coding: utf-8 -*-
"""
PetWindow —— 桌宠本体窗口
========================
透明无边框置顶、会呼吸、会眨眼、表情随情绪变、
能拖动、双击=摸头（数值真实变化）、右键菜单、头顶冒泡。

绘制职责已抽到 face/pet_renderer.py：
  PetWindow 只管窗口、交互和动画状态，具体"长什么样"由 PetRenderer 决定。
  换角色时调 apply_character() 换一个 Renderer 即可，窗口代码不用动。
"""

from __future__ import annotations

from pathlib import Path
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QWidget, QApplication, QMenu

from .bubble import BubbleWindow
from .pet_renderer import PetRenderer


class PetWindow(QWidget):
    """桌面上的本体。所有外部交互信号从这里发出去，它自己不认识 SoulEngine 之外的东西。"""

    chat_requested = pyqtSignal()             # 用户想打开聊天框
    settings_requested = pyqtSignal()         # 用户想打开设置面板
    character_switch_requested = pyqtSignal(str)  # 用户想切换角色（参数=角色id）
    clipboard_restore_requested = pyqtSignal()    # 用户想恢复被她换掉的剪贴板

    def __init__(self, cfg: dict, engine):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint      # 无边框
            | Qt.WindowType.WindowStaysOnTopHint   # 永远置顶
            | Qt.WindowType.Tool                   # 不占任务栏
            | Qt.WindowType.WindowDoesNotAcceptFocus  # 点击绝不抢你正在打字的焦点
        )
        self.engine = engine
        self.cfg = cfg
        self.size = int(cfg.get("character", {}).get("size", 160))

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)              # 接收拖过来的文件投喂

        self.bubble = BubbleWindow()
        self._load_recipes()

        # ---- 渲染器：默认 cat，apply_character 时替换 ----
        profile = getattr(engine, "character_profile", None)
        shape = getattr(profile, "shape", "cat") if profile else "cat"
        styles = getattr(profile, "mood_styles", None) if profile else None
        avatar = getattr(profile, "avatar", None) if profile else None
        model3d = getattr(profile, "model3d", None) if profile else None
        display = getattr(profile, "display", "auto") if profile else "auto"
        sprites = (profile.path / "sprites") if profile and getattr(profile, "path", None) else None
        self.renderer = PetRenderer(shape=shape, mood_styles=styles, avatar=avatar,
                                    sprites_dir=sprites,
                                    model3d=model3d, display=display)

        # ---- 动画状态 ----
        self._phase = 0.0            # 呼吸相位
        self._blink = False          # 是否正闭眼
        self._drag_offset = None     # 拖动时的按点偏移
        self._frozen = False         # ACL 假死状态
        self._frozen_complain = ""   # 被点破时的抱怨台词
        self._sleeping = False       # 昼夜作息：睡着了（与假死是两回事）
        self._greet_period = "day"   # 昼夜作息：开场白用哪个时段（由 controller 设）

        # 呼吸定时器：20fps 让小圆有活着的感觉，成本可忽略
        self._breath_timer = QTimer(self, interval=50)
        self._breath_timer.timeout.connect(self._breathe)
        self._breath_timer.start()

        # 睡觉时的 Zzz 气泡：90 秒一次，够你不时瞄一眼知道她睡着了，
        # 又不至于刷屏到烦。只在睡着期间跑。
        self._zzz_timer = QTimer(self, interval=90_000)
        self._zzz_timer.timeout.connect(self._show_zzz)

        # 眨眼定时器：随机间隔才像活物
        self._schedule_blink()

        self._fit_window(profile)
        self._move_to_start(cfg.get("window", {}).get("start_position", "bottom_right"))

    # ------------------------------------------------------------------
    # 角色切换
    # ------------------------------------------------------------------
    def apply_character(self, profile) -> None:
        """切换角色：换渲染器的形状和颜色。窗口/动画/情绪状态都保留。"""
        shape = getattr(profile, "shape", "cat")
        styles = getattr(profile, "mood_styles", None)
        sprites = (profile.path / "sprites") if getattr(profile, "path", None) else None
        self.renderer = PetRenderer(shape=shape, mood_styles=styles,
                                    sprites_dir=sprites,
                                    avatar=getattr(profile, "avatar", None),
                                    model3d=getattr(profile, "model3d", None),
                                    display=getattr(profile, "display", "auto"))
        self._fit_window(profile)
        self.update()

    def _fit_window(self, profile) -> None:
        """按角色外形调整窗口大小。
        为什么：矢量圆是方的，窗口 size+40 就够；但 Q 版是全身高立绘，
        用方窗口会把头顶裁掉。有立绘/多帧图的角色，按图的宽高比把窗口加高。
        """
        av = getattr(profile, "avatar", None)
        spr_dir = None
        if getattr(profile, "path", None):
            cand = profile.path / "sprites"
            if cand.is_dir():
                spr_dir = cand
        is_image = av is not None or spr_dir is not None
        if is_image and av is not None and Path(av).is_file():
            pm = QPixmap(str(av))
            ratio = (pm.height() / pm.width()) if not pm.isNull() else 1.5
            win_w = int(self.size * 1.12)
            win_h = int(win_w * ratio) + 14
            self.resize(win_w, win_h)
        else:
            self.resize(self.size + 40, self.size + 36)

    # ------------------------------------------------------------------
    # 出场位置 / 开场白
    # ------------------------------------------------------------------
    def _move_to_start(self, where: str) -> None:
        scr = QApplication.primaryScreen().availableGeometry()
        if where == "center":
            pass  # 测试用居中，平时不进这个分支
        x = scr.right() - self.width() - 24
        y = scr.bottom() - self.height() - 8
        self.move(x, y)

    async def greet(self) -> None:
        """main.py 启动后调一次：停一拍再冒开场白，视觉上有"她刚醒来"的节奏。"""
        import asyncio
        await asyncio.sleep(1.2)
        self.say(self._greeting_for(self._greet_period))

    def set_greet_period(self, period: str) -> None:
        """昼夜作息：开场白该用哪个时段（sleep/morning/day/night）。"""
        self._greet_period = str(period or "day")

    def _greeting_for(self, period: str) -> str:
        """按时段挑开场白。角色包里有对应台词池就用角色自己的，没有就用通用兜底。"""
        pool_key = {"sleep": "sleep_lines", "morning": "morning_lines",
                    "night": "night_lines"}.get(period)
        if pool_key:
            pool = self.engine._character.get(pool_key)
            if pool:
                import random
                return random.choice(pool)
        if period == "sleep":
            return "（她缩成一团睡得正香）……Zzz"
        if period == "morning":
            return "早上好呀……唔，再让我赖五分钟~"
        if period == "night":
            return "这么晚啦？你还不睡吗，我可要先睡了哦。"
        return self.engine.greet_line()

    def say(self, text: str, duration_ms: int | None = None) -> None:
        dur = duration_ms or self.cfg.get("ui", {}).get("bubble_duration_ms", 6000)
        self.bubble.show_text(text,
                              {"x": self.x(), "y": self.y(),
                               "w": self.width(), "h": self.height()},
                              dur)

    # ------------------------------------------------------------------
    # 动画驱动
    # ------------------------------------------------------------------
    def _breathe(self) -> None:
        # 睡着时呼吸放慢一半：这是"她在睡觉"最直观的信号，
        # 比换个颜色更容易一眼看出来。
        self._phase += 0.025 if self._sleeping else 0.05
        self.update()

    def _schedule_blink(self) -> None:
        if self._sleeping:      # 睡着不眨眼（渲染器把她画成一直闭眼）
            return
        import random
        delay = random.randint(2600, 5200)
        QTimer.singleShot(delay, self._do_blink)

    def _do_blink(self) -> None:
        if self._sleeping:
            return
        self._blink = True
        self.update()
        QTimer.singleShot(140, lambda: self._set_open())
        # 10% 概率连眨两下，更真
        import random
        if random.random() < 0.1:
            QTimer.singleShot(300, self._do_blink)

    def _set_open(self) -> None:
        self._blink = False
        self.update()
        self._schedule_blink()

    # ------------------------------------------------------------------
    # ACL 身体能力：捣乱循环通过这三个方法操纵她的"身体"
    # ------------------------------------------------------------------
    def start_fake_freeze(self, seconds: int, complain: str) -> None:
        """假死：表情变灰、呼吸停住。双击可以"点破"，她会抱怨。"""
        self._frozen = True
        self._frozen_complain = complain
        self.update()
        QTimer.singleShot(seconds * 1000, self._unfreeze)

    def _unfreeze(self) -> None:
        if self._frozen:
            self._frozen = False
            self.update()

    def set_temp_opacity(self, value: float, seconds: int) -> None:
        """临时半透明（小隐身），到点恢复。"""
        self.setWindowOpacity(max(0.1, min(1.0, value)))
        QTimer.singleShot(seconds * 1000, lambda: self.setWindowOpacity(1.0))

    def move_over_active_window(self) -> bool:
        """L3 挡窗口：挪到用户当前活动窗口的中央上方。成功返回 True。"""
        try:
            import pygetwindow as gw
            win = gw.getActiveWindow()
            if win is None:
                return False
            self.move(win.left + (win.width - self.width()) // 2,
                      win.top + (win.height - self.height()) // 3)
            return True
        except Exception:  # noqa: BLE001 —— 挪不了就当无事发生
            return False

    def is_frozen(self) -> bool:
        return self._frozen

    # ------------------------------------------------------------------
    # 昼夜作息：睡 / 醒
    # ------------------------------------------------------------------
    def set_sleeping(self, on: bool) -> None:
        """切到睡态或醒来。由 AppController 按 config 的作息时间驱动。

        和假死（start_fake_freeze）是两套东西：假死是她在捣乱、被双击会抱怨；
        睡觉是真的睡，双击只会摸到头。所以这里不碰 _frozen。
        """
        on = bool(on)
        if on == self._sleeping:
            return
        self._sleeping = on
        self.update()
        if on:
            self._zzz_timer.start()
        else:
            self._zzz_timer.stop()
            self._blink = False
            self._schedule_blink()   # 醒过来重新开始眨眼睛

    def _show_zzz(self) -> None:
        """睡着时时不时冒一个 Zzz。她真睡了，不是卡住。"""
        if not self._sleeping:
            return
        self.say("Zzz……", duration_ms=3500)

    def is_sleeping(self) -> bool:
        return self._sleeping

    # ------------------------------------------------------------------
    # 绘制：全部委托给 PetRenderer
    # ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        snap = self.engine.snapshot()
        self.renderer.paint(
            p, self.width(), self.height(), self.size,
            self._phase, self._blink, self._frozen, snap,
            sleeping=self._sleeping,
            accessories=self.engine.accessories())

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------
    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = ev.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, ev) -> None:
        if self._drag_offset is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _ev) -> None:
        self._drag_offset = None

    def mouseDoubleClickEvent(self, _ev) -> None:
        """双击 = 摸头。假死中双击 = 点破装死，她会抱怨然后复活。"""
        if self._frozen:
            self._unfreeze()
            self.say(self._frozen_complain or "……太累了，饶了我吧。")
            return
        # 正常摸头走 engine.react 保证情绪系统是唯一入口
        line, delta = self.engine.react("pat_head")
        extra = ""
        if delta["affinity"] > 0.01:
            extra = f"\n(好感+{delta['affinity']:.1f})"
        elif delta["affinity"] < -0.01:
            extra = f"\n(好感{delta['affinity']:.1f})"
        self.say(line + extra)

    # ------------------------------------------------------------------
    # 拖拽喂养：把文件拖到她身上听一口咬吃的声音
    # ------------------------------------------------------------------
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        urls = ev.mimeData().urls()
        if not urls:
            return
        from pathlib import Path
        files = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if not files:
            self.say("……这是网页链接呀，我吃不了这个。\n"
                     "把真正的文件（比如下载好的图片、文档）拖给我才行~")
            return
        for raw in files[:3]:                      # 一次最多吃三个
            self._feed_file(Path(raw))

    def _load_recipes(self) -> None:
        """加载 face/recipe_map.json（拖来什么文件就视作哪顿饭）。"""
        import json
        from pathlib import Path as _P
        recipe_path = _P(__file__).parent / "recipe_map.json"
        self._recipes = {}
        try:
            raw = json.loads(recipe_path.read_text(encoding="utf-8"))
            for key, info in raw.items():
                if key.startswith("_"):
                    continue
                exts = [e.lower() for e in info.get("extensions", [])]
                self._recipes[key] = {"label": info.get("label", key), "exts": exts,
                                      "comment": info.get("comment", "")}
        except (OSError, json.JSONDecodeError):
            self._recipes = {}

    def _guess_recipe(self, file_name: str) -> str:
        """后缀 → 餐型。"""
        from pathlib import Path as _P
        ext = _P(file_name).suffix.lower()
        for kind, info in self._recipes.items():
            if ext in info["exts"]:
                return kind
        return "unknown"

    def _feed_file(self, path_like) -> None:
        """真正喂食：走 CESM 的 feed 方法（她记住这件事也写文件）。"""
        from pathlib import Path as _P
        p = _P(path_like)
        name = p.name
        kind = self._guess_recipe(name)

        delta = self.engine.cesm.feed(kind, name)
        if delta["full"]:
            self.say(f"吃不下了……但「{name}」闻起来好好，明天再喂哦~")
            return

        self.say(self.engine.feed_line() + f"\n{delta['comment']}")
        self.engine._push_history("user", f"(主人喂了你一个{name})")
        self.engine._save_history()

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def contextMenuEvent(self, ev) -> None:
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{font-family:'Microsoft YaHei UI';font-size:13px;}")
        act_chat = menu.addAction("&聊天")
        act_feed = menu.addAction("&喂文件（拖文件给她吃的）")
        menu.addSeparator()

        # 换伙伴子菜单：列出所有可用角色
        profiles = getattr(self.engine, "available_characters", [])
        if profiles:
            switch_menu = menu.addMenu("换&伙伴")
            current_id = getattr(getattr(self.engine, "character_profile", None),
                                 "id", None)
            for prof in profiles:
                act = switch_menu.addAction(prof.name)
                act.setCheckable(True)
                act.setChecked(prof.id == current_id)
                act.setData(prof.id)
            menu.addSeparator()

        act_status = menu.addAction("&看看她的状态")
        # 她换过剪贴板之后，用户需要一个找回原内容的口子
        # （红线要求"改剪贴板必须能恢复"，没有入口等于没留快照）
        act_clip = menu.addAction("&恢复剪贴板")
        act_settings = menu.addAction("&设置")
        menu.addSeparator()
        act_quit = menu.addAction("&让她下班")

        chosen = menu.exec(ev.globalPos())
        if chosen is act_chat:
            self.chat_requested.emit()
        elif chosen is act_settings:
            self.settings_requested.emit()
        elif chosen is act_feed:
            snap = self.engine.snapshot()
            if snap.get("hunger", 100) >= 95:
                self.say("现在不饿啦，等一会儿再喂我哦~")
            else:
                self.say(f"把文件拖到我的小身体上，我会吃掉的~\n"
                         f"现在我的饥饿度是 {snap.get('hunger')}（{snap.get('hunger_word')}）"
                         "\n支持的文件：txt/md/log/json/pdf/pptx/docx/图片……")
        elif chosen is act_status:
            s = self.engine.snapshot()
            self.say(f"心情:{s['mood_word']}({s['mood']})\n"
                     f"好感:{s['affinity']}  信任:{s['trust']}\n"
                     f"吃饱:{s['hunger']}\n"
                     f"聊过 {s['counters']['total_chats']} 次",
                     duration_ms=9000)
        elif chosen is act_clip:
            self.clipboard_restore_requested.emit()
        elif chosen is act_quit:
            self.bubble.hide()
            QApplication.quit()
        elif chosen is not None and chosen.data():
            # 角色切换
            self.character_switch_requested.emit(chosen.data())


# ----------------------------------------------------------------------
# 使用示例：python face/pet_window.py —— 只显示角色本体做视觉验证
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys, json, asyncio
    from pathlib import Path
    from qasync import QEventLoop

    root = Path(__file__).resolve().parent.parent
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    from soul.engine import SoulEngine
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    eng = SoulEngine(root, cfg)
    pet = PetWindow(cfg, eng)
    pet.show()
    pet.say("样式验证中~ 双击摸摸我试试", 5000)

    with loop:
        loop.run_forever()
