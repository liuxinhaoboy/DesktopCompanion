# -*- coding: utf-8 -*-
"""
ChatPanel —— 聊天窗口
====================
和角色的对话在这里发生。要点：
  - 回复是流式的（打字机效果），靠 asyncio 任务驱动，界面绝不卡死
  - 她的每一句话都会经过 SoulEngine 写进历史/记忆/情绪
  - P4 起支持：按住🎤说话转文字、📎发图片（多模态）、🔊自动朗读
  - 消息以柔和色块呈现：你的消息浅绿块、她的回复浅蓝块、旁白事件居中斜体

语音/图片能力都通过可选的 voice_service 注入：没有它（单元测试、单独预览）
时，纯文字聊天照常工作，按钮自动隐藏，绝不硬依赖麦克风。
"""

from __future__ import annotations

import asyncio
import html
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (QColor, QFont, QTextCharFormat, QTextCursor,
                         QTextLength, QTextTableFormat)
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
                             QLineEdit, QPushButton, QLabel, QFileDialog)

from face.theme import app_qss, hint_style, BLUE, MUTED
from face.brand_header import brand_header

USER_BG = "#edf4ec"     # 你的消息块：浅奶绿
AI_BG = "#eef3f9"       # 她的回复块：浅奶蓝


class ChatPanel(QWidget):
    def __init__(self, engine, voice_service=None):
        super().__init__()
        self.engine = engine
        self.voice = voice_service      # 可为 None（无语音环境）
        self._streaming = False         # 一轮回复没完不许发下一条，防止乱序
        self._pending_imgs: list[tuple[str, str]] = []  # 待发图片 [(data_url, 文件名)]

        self.setWindowTitle(f"和{engine.name}聊天")
        self.resize(440, 620)
        self.setStyleSheet(app_qss())
        self._build_ui()
        self._wire_voice()
        self._show_history()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self.brand_header = brand_header(
            f"和 {self.engine.name} 聊聊",
            "给今天留一个舒服的小角落",
            badge="聊天",
            icon="✦",
            tone="blue",
        )
        root.addWidget(self.brand_header)

        self.transcript = QTextEdit(readOnly=True)
        self.transcript.setFont(QFont("Microsoft YaHei UI", 11))
        root.addWidget(self.transcript, 1)

        # 待发图片提示行（没选图时是一行空格占位，避免界面跳动）
        self.pending_lab = QLabel(" ")
        self.pending_lab.setStyleSheet(hint_style())
        root.addWidget(self.pending_lab)

        # 输入行：🎤 📎 ……输入框…… 发送
        row = QHBoxLayout()
        self.mic_btn = QPushButton("🎤")
        self.mic_btn.setToolTip("按住说话，松开发送")
        self.mic_btn.setFixedWidth(42)
        self.mic_btn.pressed.connect(self._mic_pressed)
        self.mic_btn.released.connect(self._mic_released)
        row.addWidget(self.mic_btn)

        self.image_btn = QPushButton("📎")
        self.image_btn.setToolTip("选一张图片发给她看")
        self.image_btn.setFixedWidth(42)
        self.image_btn.clicked.connect(self._pick_image)
        row.addWidget(self.image_btn)

        self.input = QLineEdit(placeholderText=f"和{self.engine.name}说点什么…")
        self.input.setFont(QFont("Microsoft YaHei UI", 11))
        self.input.returnPressed.connect(self.send)
        row.addWidget(self.input, 1)

        self.send_btn = QPushButton("发送")
        self.send_btn.setProperty("primary", "true")
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.send_btn)
        root.addLayout(row)

        # 语音控制行：停止朗读 + 自动朗读开关 + 语音状态提示
        vrow = QHBoxLayout()
        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setToolTip("停止当前朗读/识别")
        self.stop_btn.clicked.connect(self._stop_voice)
        vrow.addWidget(self.stop_btn)

        self.speaker_btn = QPushButton("🔊 自动朗读")
        self.speaker_btn.setCheckable(True)
        self.speaker_btn.setToolTip("开启后她的每条回复都会念出来")
        if self.voice is not None:
            self.speaker_btn.setChecked(self.voice.auto_speak())
        self.speaker_btn.toggled.connect(self._toggle_auto_speak)
        vrow.addWidget(self.speaker_btn)

        self.voice_state = QLabel(" ")
        self.voice_state.setStyleSheet(hint_style())
        vrow.addWidget(self.voice_state, 1)
        root.addLayout(vrow)

        self.status = QLabel(" ")
        self.status.setFont(QFont("Microsoft YaHei UI", 9))
        root.addWidget(self.status)
        self._refresh_status()

        if self.voice is None:
            self.mic_btn.hide()
            self.stop_btn.hide()
            self.speaker_btn.hide()

    def _wire_voice(self) -> None:
        if self.voice is None:
            return
        self.voice.hint.connect(lambda t: self.voice_state.setText(t))
        self.voice.failed.connect(lambda t: self.voice_state.setText(f"⚠ {t}"))
        self.voice.state_changed.connect(self._on_voice_state)
        self.voice.recorder.max_duration_reached.connect(
            lambda: asyncio.ensure_future(self._finish_voice()))

    # ------------------------------------------------------------------
    # 色块渲染（QTextTable，打字机可在块内逐段写入）
    # ------------------------------------------------------------------
    def _open_block(self, bg: str) -> QTextCursor:
        cur = self.transcript.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextTableFormat()
        fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        fmt.setCellPadding(7)
        fmt.setCellSpacing(3)          # 块间露出暖白底，形成间隔
        fmt.setBorder(0)
        table = cur.insertTable(1, 1, fmt)
        # 实测：table format 的背景不绘制，必须给单元格设 QTextCharFormat 背景
        cell = table.cellAt(0, 0)
        cf = QTextCharFormat()
        cf.setBackground(QColor(bg))
        cell.setFormat(cf)
        ccur = cell.firstCursorPosition()
        ccur.setCharFormat(cf)
        return ccur

    def _close_block(self) -> None:
        cur = self.transcript.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertBlock()
        self._scroll_bottom()

    def _write_block(self, bg: str, inner_html: str) -> None:
        ccur = self._open_block(bg)
        ccur.insertHtml(inner_html)
        self._close_block()

    def _append_event(self, text: str) -> None:
        """摸头/喂食这类旁白事件：居中、斜体、次要色，不加色块。"""
        cur = self.transcript.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertHtml(
            f"<center><i style='color:{MUTED}'>"
            f"{html.escape(text)}</i></center>")
        cur.insertBlock()
        self._scroll_bottom()

    def _scroll_bottom(self) -> None:
        sb = self.transcript.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _user_html(self, text: str) -> str:
        return f"<b>你：</b>{html.escape(text)}"

    def _ai_prefix(self) -> str:
        return f"<b style='color:{BLUE}'>{html.escape(self.engine.name)}：</b>"

    # ------------------------------------------------------------------
    def _status_html(self) -> str:
        s = self.engine.snapshot()
        color_map = {"兴奋": "#2e7d32", "开心": "#2e7d32", "平静": "#5c6bc0",
                     "低落": "#ef6c00", "难过": "#e53935", "生气": "#b71c1c"}
        color = color_map.get(s["mood_word"], "#555555")
        return (f"心情 <b style='color:{color}'>{s['mood_word']} {s['mood']}</b>　"
                f"好感 <b>{s['affinity']}</b>　信任 <b>{s['trust']}</b>")

    def _refresh_status(self) -> None:
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setText(self._status_html())

    # ------------------------------------------------------------------
    def _show_history(self) -> None:
        for m in self.engine._history[-30:]:
            if m["role"] == "user":
                if m["content"].startswith("("):      # 旁白事件
                    self._append_event(m["content"])
                else:
                    self._write_block(USER_BG, self._user_html(m["content"]))
            else:
                body = html.escape(m["content"]).replace("\n", "<br>")
                self._write_block(AI_BG, self._ai_prefix() + body)

    # ------------------------------------------------------------------
    # 图片附件
    # ------------------------------------------------------------------
    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选一张图片发给她", "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if not path:
            return
        from soul.vision import compress_image_to_data_url, ImagePrepareError
        max_kb = int(self.engine.cfg.get("vision", {}).get("max_image_kb", 500))
        try:
            data_url = compress_image_to_data_url(path, max_kb=max_kb)
        except ImagePrepareError as e:
            self.pending_lab.setText(f"⚠ {e}")
            return
        self._pending_imgs = [(data_url, Path(path).name)]
        self._refresh_pending()

    def _refresh_pending(self) -> None:
        if self._pending_imgs:
            names = "、".join(n for _, n in self._pending_imgs)
            self.pending_lab.setText(f"📎 待发图片：{names}（点发送即一起发出）")
        else:
            self.pending_lab.setText(" ")

    # ------------------------------------------------------------------
    # 语音
    # ------------------------------------------------------------------
    def _mic_pressed(self) -> None:
        if self.voice is None or self._streaming:
            return
        ok, msg = self.voice.start_recording()
        if not ok:
            self.voice_state.setText(f"⚠ {msg}")
            return
        self.mic_btn.setText("●")
        self.mic_btn.setStyleSheet("QPushButton{background:#d97066; color:white;}")

    def _mic_released(self) -> None:
        if self.voice is None:
            return
        if self.voice.state == "recording":
            asyncio.ensure_future(self._finish_voice())
        else:
            self._reset_mic_ui()

    async def _finish_voice(self) -> None:
        text = await self.voice.finish_recording()
        self._reset_mic_ui()
        if text:
            self.input.setText(text)
            self.send()

    def _reset_mic_ui(self) -> None:
        self.mic_btn.setText("🎤")
        self.mic_btn.setStyleSheet("")

    def _stop_voice(self) -> None:
        if self.voice is not None:
            self.voice.cancel()
        self._reset_mic_ui()
        self.voice_state.setText("已停止")

    def _toggle_auto_speak(self, on: bool) -> None:
        if self.voice is not None:
            self.voice._voice_cfg["auto_speak"] = bool(on)
        self.voice_state.setText("已开启自动朗读" if on else "已关闭自动朗读")

    def _on_voice_state(self, st: str) -> None:
        if st == "idle" and self.voice_state.text() not in (" ", ""):
            pass

    def closeEvent(self, ev) -> None:
        if self.voice is not None:
            try:
                self.voice.shutdown()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(ev)

    # ------------------------------------------------------------------
    # 发送与流式渲染
    # ------------------------------------------------------------------
    def send(self) -> None:
        text = self.input.text().strip()
        imgs = self._pending_imgs
        if (not text and not imgs) or self._streaming:
            return
        self.input.clear()
        self._pending_imgs = []
        self._refresh_pending()
        self._streaming = True
        self.send_btn.setEnabled(False)
        image_urls = [u for u, _ in imgs]
        image_name = imgs[0][1] if imgs else None
        asyncio.ensure_future(self._send_flow(text, image_urls, image_name))

    async def _send_flow(self, text: str,
                         image_urls: list[str] | None = None,
                         image_name: str | None = None) -> None:
        # 你的消息块（带图先标图片占位）
        shown = text or "（发了一张图片）"
        if image_urls:
            shown = f"🖼 [图片：{image_name or '未命名'}]\n{shown}"
        self._write_block(USER_BG, self._user_html(shown))

        reply_parts: list[str] = []
        # 记录"色块开着没关"：出错时靠它决定要不要收口
        block_open = False
        try:
            action_res = None
            if not image_urls:
                action_res = await self.engine.try_computer_action(text, self)
                # 不是电脑操作，再尝试浏览器动作（需扩展已配对）
                if action_res is None:
                    action_res = await self.engine.try_browser_action(text, self)

            if action_res is not None:
                # 动作结果确定文本，整块显示
                piece_html = html.escape(action_res.message).replace("\n", "<br>")
                self._write_block(AI_BG, self._ai_prefix() + piece_html)
                reply_parts.append(action_res.message)
            else:
                # 打开她的回复块，前缀先写入，之后打字机逐段在同一块内进行
                ccur = self._open_block(AI_BG)
                block_open = True
                ccur.insertHtml(self._ai_prefix())
                first = True
                async for piece in self.engine.stream_chat(
                        text, image_data_urls=image_urls, image_name=image_name):
                    reply_parts.append(piece)
                    piece_html = html.escape(piece).replace("\n", " ")
                    ccur.insertHtml(piece_html.lstrip() if first else piece_html)
                    first = False
                    self._scroll_bottom()
                    await asyncio.sleep(0.001)
                self._close_block()
                block_open = False
        except Exception as e:  # noqa: BLE001 —— 这一轮出错不能把整个聊天窗搞坏
            # 为什么必须在这里收口：_open_block 是把光标插进一个表格单元格里的，
            # 块没闭合的话光标就留在那个单元格内，下一条消息会嵌进旧单元格里，
            # 整段聊天记录排版就烂了。另外这里的异常原本会被 ensure_future
            # 静默吞掉，用户只会看到"她卡住了"而没有任何提示。
            if block_open:
                self._close_block()
                block_open = False
            print(f"[Chat] 本轮回复失败：{type(e).__name__}: {e}")
            self._write_block(AI_BG, self._ai_prefix()
                              + "……我刚刚走神了。（这轮出错了，再说一次试试？）")
        finally:
            self._streaming = False
            self.send_btn.setEnabled(True)
            self._refresh_status()
            full = "".join(reply_parts).strip()
            if full and self.voice is not None and self.voice.auto_speak():
                asyncio.ensure_future(self.voice.speak(full))


# ----------------------------------------------------------------------
# 使用示例：python -m face.chat_panel —— 弹出聊天窗直接试
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys, json
    from qasync import QEventLoop
    from PyQt6.QtWidgets import QApplication

    root = Path(__file__).resolve().parent.parent
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    from soul.engine import SoulEngine
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    eng = SoulEngine(root, cfg)

    panel = ChatPanel(eng)
    panel.show()

    with loop:
        loop.run_forever()
