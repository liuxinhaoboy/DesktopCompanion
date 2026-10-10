# -*- coding: utf-8 -*-
"""
SettingsPanel —— 设置控制中心
============================
右键桌宠 →「设置」打开。六个标签页：
  1. 宠物       选内置伙伴 / 导入自定义角色 / 大小 / 透明度 / 数据目录
  2. AI与多模态 文本模型、视觉模型、Base URL、密钥、超时、max token、测试连接
  3. 语音       录音/STT/TTS 设置（P4 接线，现在先存配置，测试按钮置灰）
  4. 电脑控制   工作目录、能力开关、白名单、确认策略（P5 接线）
  5. 浏览器插件 配对码、连接状态、安装说明（P6 接线）
  6. 隐私       录音保留、日志天数、清空当前角色记忆

设计原则（遵守交接包红线）：
  - 全程非阻塞：测试连接/导入/清记忆都用 asyncio 任务，绝不用 exec() 冻住 qasync
  - API key 输入框密码样式，已保存的密钥只显示末 4 位，留空=不修改
  - 保存统一走 AppController.apply_config()，不自己写 config.json
  - 后端还没做的功能（语音/浏览器）按钮置灰并写清"哪个阶段开放"，不假装能用
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPainter, QPixmap

from face.pet_renderer import PetRenderer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QLabel, QLineEdit, QPushButton, QSpinBox, QDoubleSpinBox, QCheckBox,
    QRadioButton, QButtonGroup, QComboBox, QFileDialog, QScrollArea,
    QGroupBox, QFrame, QMessageBox,
)

FONT = "Microsoft YaHei UI"
# 和聊天窗一致的暖色调，整套界面看起来是同一个"家"
from face.theme import app_qss
from face.brand_header import brand_header
STYLE = app_qss()


def _hint(text: str) -> QLabel:
    """一行灰色说明小字。"""
    lab = QLabel(text)
    from face.theme import hint_style
    lab.setStyleSheet(hint_style())
    lab.setWordWrap(True)
    return lab


def _section(title: str) -> QGroupBox:
    box = QGroupBox(title)
    lay = QFormLayout(box)
    lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    lay.setSpacing(8)
    box._form = lay   # 方便外部 addRow
    return box


def render_thumbnail(profile, px: int = 64) -> QPixmap:
    """角色缩略图：用角色自己的渲染器画一枚小图，卡片和列表都能用。
    3D 角色（有 model3d 且 display 允许）画的是真实模型渲染，
    2D 角色画矢量形象；phase 固定，不带动画——静态图更适合做预览。"""
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    renderer = PetRenderer(
        shape=getattr(profile, "shape", "cat"),
        mood_styles=getattr(profile, "mood_styles", None),
        avatar=getattr(profile, "avatar", None),
        model3d=getattr(profile, "model3d", None),
        display=getattr(profile, "display", "auto"),
    )
    qp = QPainter(pix)
    qp.setRenderHint(QPainter.RenderHint.Antialiasing)
    snap = {"mood_word": "平静", "mood": 60, "is_angry": False, "hunger": 80}
    # size 传 px：渲染器按窗口短边比例绘制，缩略图里正好撑满留边
    renderer.paint(qp, px, px, int(px * 0.85), 0.8, False, False, snap)
    qp.end()
    return pix


class SettingsPanel(QWidget):
    """设置窗口。持有 controller，所有改动最终通过它落盘/热更新。"""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.store = controller.config_store
        self._testing = False

        self.setWindowTitle("设置 · 小灵的家")
        self.resize(560, 640)
        self.setStyleSheet(STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.brand_header = brand_header(
            f"{controller.engine.name} 的小窝",
            "伙伴 · AI · 语音 · 安全权限，一处打理",
            badge="设置中心",
            icon="✦",
            tone="green",
        )
        root.addWidget(self.brand_header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_pet_tab(), "宠物")
        self.tabs.addTab(self._build_ai_tab(), "AI 与多模态")
        self.tabs.addTab(self._build_voice_tab(), "语音")
        self.tabs.addTab(self._build_hands_tab(), "电脑控制")
        self.tabs.addTab(self._build_browser_tab(), "浏览器插件")
        self.tabs.addTab(self._build_privacy_tab(), "隐私")
        # 商店页单独拆成 face/shop_tab.py：本文件已经很大了，别再往里堆控件
        from face.shop_tab import ShopTab
        self.shop_tab = ShopTab(self.controller)
        self.tabs.addTab(self.shop_tab, "商店")

        # ---- 底部：保存 / 取消 / 状态 ----
        bottom = QHBoxLayout()
        self.status_lab = _hint("改动点「保存」后生效；切换伙伴立即生效。")
        bottom.addWidget(self.status_lab, 1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.close)
        bottom.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("保存全部设置")
        self.save_btn.setProperty("primary", "true")
        self.save_btn.clicked.connect(self.on_save)
        bottom.addWidget(self.save_btn)
        root.addLayout(bottom)

    # ==================================================================
    # 标签页 1：宠物
    # ==================================================================
    def _build_pet_tab(self) -> QWidget:
        scroll = self._scroll()
        page = QWidget()
        lay = QVBoxLayout(page)

        # --- 选择伙伴（单选卡片）---
        pick = _section("选择伙伴（切换立即生效，各自的心情和记忆独立）")
        self._char_group = QButtonGroup(self)
        self._char_radios: dict[str, QRadioButton] = {}
        self._char_container = QVBoxLayout()
        self._populate_character_radios()
        pick._form.addRow(self._char_container)

        # 切换按钮
        switch_row = QHBoxLayout()
        self.switch_btn = QPushButton("切换到选中的伙伴")
        self.switch_btn.clicked.connect(self.on_switch_character)
        switch_row.addWidget(self.switch_btn)
        switch_row.addStretch(1)
        pick._form.addRow(switch_row)

        # 当前角色数据目录（让用户知道记忆存在哪）
        cur = self.controller.characters.current
        self.data_dir_lab = _hint(f"当前伙伴「{cur.name}」的数据目录："
                                  f"{self.controller.characters.data_dir_for(cur.id)}")
        pick._form.addRow(self.data_dir_lab)
        lay.addWidget(pick)

        # --- 导入自定义角色 ---
        imp = _section("导入自定义角色")
        imp_row = QHBoxLayout()
        self.import_btn = QPushButton("选择角色文件夹…")
        self.import_btn.clicked.connect(self.on_import_character)
        imp_row.addWidget(self.import_btn)
        imp_row.addStretch(1)
        imp._form.addRow(imp_row)
        imp._form.addRow(_hint("文件夹里需要有 manifest.json（必填 id、name）、"
                               "system_prompt.md、lines.json。导入后出现在上方列表。"))
        lay.addWidget(imp)

        # --- 自定义立绘 ---
        av = _section("自定义立绘（可选）")
        av_row = QHBoxLayout()
        self.avatar_btn = QPushButton("为选中的伙伴选 PNG 立绘…")
        self.avatar_btn.clicked.connect(self.on_pick_avatar)
        av_row.addWidget(self.avatar_btn)
        av_row.addStretch(1)
        av._form.addRow(av_row)
        av._form.addRow(_hint("选一张透明背景 PNG 当桌宠外观，会复制进角色文件夹，"
                              "当前伙伴立即更换；不选就用默认矢量形象。"))
        lay.addWidget(av)

        # --- 3D 模型 ---
        m3d = _section("3D 桌宠（可选）")
        self.display_combo = QComboBox()
        # 只留两项：渲染器只区分"是否为 2d"，原来那个"优先 3D"和"自动"
        # 行为完全等价，留着只会让人纠结选哪个。旧 manifest 里写着 "3d" 的
        # 角色仍然照常加载（loader 保留该取值，等同 auto）。
        self.display_combo.addItem("自动（有 3D 模型就用 3D）", "auto")
        self.display_combo.addItem("只用 2D 矢量/立绘", "2d")
        # 回显当前伙伴的显示模式：新建面板不会走 _load_current，必须在构造时主动同步
        _cur0 = self.controller.characters.current
        _disp0 = getattr(_cur0, "display", "auto") if _cur0 else "auto"
        for _i in range(self.display_combo.count()):
            if self.display_combo.itemData(_i) == _disp0:
                self.display_combo.setCurrentIndex(_i)
                break
        # 信号必须等选项填充与回显都完成后才连：构造期 addItem/setCurrentIndex
        # 都会触发 currentIndexChanged，先连上就会在面板没建完时误写 manifest
        self.display_combo.currentIndexChanged.connect(self.on_display_mode_changed)
        m3d._form.addRow("显示模式", self.display_combo)

        model_row = QHBoxLayout()
        self.model_btn = QPushButton("为选中的伙伴上传 3D 模型…")
        self.model_btn.clicked.connect(self.on_pick_model3d)
        model_row.addWidget(self.model_btn)
        self.remove_model_btn = QPushButton("移除 3D 模型")
        self.remove_model_btn.clicked.connect(self.on_remove_model3d)
        model_row.addWidget(self.remove_model_btn)
        model_row.addStretch(1)
        m3d._form.addRow(model_row)
        self.model_info = _hint("")
        m3d._form.addRow(self.model_info)
        self._refresh_model_info()   # 构造时同步一次：有模型显示模型名并启用"移除"
        m3d._form.addRow(_hint("支持 .glb / .gltf（二进制 glTF 最省事，单文件自带纹理）。"
                               "模型会复制进角色文件夹；上传后若显示模式允许，"
                               "当前伙伴立刻变 3D。复杂大模型（几十万面）会拖慢绘制，"
                               "建议用低模 Q 版。"))
        lay.addWidget(m3d)

        # --- 外观 ---
        look = _section("外观")
        self.size_spin = QSpinBox()
        self.size_spin.setRange(90, 320)
        self.size_spin.setSingleStep(10)
        self.size_spin.setValue(int(self.store.get("character", "size", 160)))
        self.size_spin.setSuffix(" px")
        look._form.addRow("体型大小", self.size_spin)
        look._form.addRow(_hint("改体型需要重启程序生效（会在保存时提示）。"))

        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.3, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setValue(float(self.store.get("character", "opacity", 1.0)))
        look._form.addRow("不透明度", self.opacity_spin)
        lay.addWidget(look)

        lay.addStretch(1)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def validate_character_folder(src: Path) -> tuple[dict | None, str | None]:
        """校验一个外部角色文件夹是否合法。返回 (manifest, 错误说明)；
        成功时错误为 None，失败时 manifest 为 None。抽成静态方法方便单测。"""
        import json
        manifest_path = src / "manifest.json"
        if not manifest_path.exists():
            return None, "文件夹里没有 manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return None, f"manifest.json 不是合法 JSON：{e}"
        if not isinstance(manifest, dict):
            return None, "manifest.json 顶层必须是对象"
        for field_name in ("id", "name"):
            if field_name not in manifest:
                return None, f"manifest.json 缺少必填字段：{field_name}"
        if not (src / "system_prompt.md").exists():
            return None, "缺少 system_prompt.md"
        # lines.json 也必须在：运行时加载角色（load_character）强制要求它，
        # 缺了会让角色加载失败被静默跳过——用户"导入成功"却发现角色消失，
        # 根本不知道原因。这里提前拦住，和 loader.validate_dir 保持一致。
        if not (src / "lines.json").exists():
            return None, "缺少 lines.json（角色会说的话，比如打招呼、被摸头时的反应）"
        try:
            lines_raw = json.loads((src / "lines.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return None, f"lines.json 不是合法 JSON：{e}"
        if not isinstance(lines_raw, dict):
            return None, "lines.json 顶层必须是对象"
        _has_line = any(
            (not k.startswith("_")) and isinstance(v, list)
            and any(isinstance(s, str) for s in v)
            for k, v in lines_raw.items())
        if not _has_line:
            return None, "lines.json 里一句台词都没有，她会变成哑巴"
        cid = str(manifest["id"]).strip()
        bad = set('\\/:*?"<>|')
        if not cid or any(c in bad for c in cid):
            return None, 'id 不能为空且不能包含 \\ / : * ? " < > | 等字符'
        return manifest, None

    def on_switch_character(self) -> None:
        for cid, radio in self._char_radios.items():
            if radio.isChecked():
                ok, msg = self.controller.switch_character(cid)
                if ok:
                    cur = self.controller.characters.current
                    self.data_dir_lab.setText(
                        f"当前伙伴「{cur.name}」的数据目录："
                        f"{self.controller.characters.data_dir_for(cur.id)}")
                    self._flash(f"已切换到「{cur.name}」")
                return

    def _selected_character_id(self) -> str | None:
        """返回当前单选选中的角色 id，没选中返回 None。"""
        for cid, radio in self._char_radios.items():
            if radio.isChecked():
                return cid
        return None

    def on_pick_avatar(self) -> None:
        """给选中角色挑一张 PNG 立绘。"""
        cid = self._selected_character_id()
        if not cid:
            self._flash("请先在上方选中一个伙伴。", error=True)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PNG 立绘", "", "PNG 图片 (*.png)")
        if not path:
            return
        ok, msg = self.controller.set_character_avatar(cid, path)
        self._flash(msg, error=not ok)

    def _current_display_index(self) -> int:
        """display 值 → 下拉索引（找不到回 auto）。"""
        for i in range(self.display_combo.count()):
            if self.display_combo.itemData(i) == (self.controller.characters.current.display
                                                  if self.controller.characters.current else "auto"):
                return i
        return 0

    def _refresh_model_info(self) -> None:
        cur = self.controller.characters.current
        if cur is None:
            self.model_info.setText("")
            return
        if cur.model3d:
            self.model_info.setText(f"当前伙伴「{cur.name}」的 3D 模型：{cur.model3d.name}")
            self.remove_model_btn.setEnabled(True)
        else:
            self.model_info.setText(f"当前伙伴「{cur.name}」还没有 3D 模型（现在显示的是 2D 形象）。")
            self.remove_model_btn.setEnabled(False)

    def on_pick_model3d(self) -> None:
        """给选中角色挂一个 .glb/.gltf 模型，成功且是当前伙伴则立刻 3D 化。"""
        cid = self._selected_character_id()
        if not cid:
            self._flash("请先在上方选中一个伙伴。", error=True)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 3D 模型", "", "3D 模型 (*.glb *.gltf);;GLB (*.glb);;glTF (*.gltf)")
        if not path:
            return
        ok, msg = self.controller.set_character_model3d(cid, path)
        self._flash(msg, error=not ok)
        if ok:
            self._refresh_model_info()

    def on_remove_model3d(self) -> None:
        cid = self._selected_character_id()
        if not cid:
            self._flash("请先在上方选中一个伙伴。", error=True)
            return
        ok, msg = self.controller.remove_character_model3d(cid)
        self._flash(msg, error=not ok)
        if ok:
            self._refresh_model_info()

    def on_display_mode_changed(self, _index: int) -> None:
        """显示模式下拉变了：写到当前选中角色（立绘同款三步：改manifest+重载+热刷新）。
        下拉在 _load_current 里被程序性刷新时由 _suppress_display_signal 抑制，避免回写。"""
        if getattr(self, "_suppress_display_signal", False):
            return
        cid = self._selected_character_id() or (
            self.controller.characters.current.id if self.controller.characters.current else None)
        if not cid:
            return
        mode = self.display_combo.currentData()
        ok, msg = self.controller.set_character_display(cid, mode)
        self._flash(msg, error=not ok)
        self._refresh_model_info()

    def on_import_character(self) -> None:
        """选一个外部角色文件夹，校验后复制进 characters/。"""
        folder = QFileDialog.getExistingDirectory(self, "选择角色文件夹")
        if not folder:
            return
        src = Path(folder)
        manifest, err = self.validate_character_folder(src)
        if err:
            self._flash(f"导入失败：{err}", error=True)
            return
        cid = str(manifest["id"]).strip()

        dest = self.controller.root / "characters" / cid
        if dest.exists():
            self._flash(f"已存在同 id 角色「{cid}」，为避免覆盖已取消。", error=True)
            return
        try:
            shutil.copytree(src, dest)
        except OSError as e:
            self._flash(f"复制失败：{e}", error=True)
            return

        # 重新扫描并刷新单选列表
        self.controller.characters.discover()
        self.controller.engine.available_characters = self.controller.characters.available
        self._populate_character_radios(select_id=cid)
        self._flash(f"已导入角色「{manifest['name']}」，可在上方选择切换。")

    def _populate_character_radios(self, select_id: str | None = None) -> None:
        """（重新）生成伙伴单选按钮。导入新角色后调用，旧按钮清空。"""
        # 清掉旧按钮
        for radio in self._char_radios.values():
            self._char_group.removeButton(radio)
            radio.setParent(None)
            radio.deleteLater()
        self._char_radios.clear()

        current_id = select_id or self.controller.characters.current.id
        for prof in self.controller.characters.available:
            radio = QRadioButton(f"{prof.name}　—　{prof.description}")
            radio.setChecked(prof.id == current_id)
            self._char_group.addButton(radio)
            self._char_radios[prof.id] = radio
            self._char_container.addWidget(radio)

    # ==================================================================
    # 标签页 2：AI 与多模态
    # ==================================================================
    def _build_ai_tab(self) -> QWidget:
        scroll = self._scroll()
        page = QWidget()
        lay = QVBoxLayout(page)

        # --- 文本模型 ---
        txt = _section("文本对话模型")
        llm = self.store.cfg.get("llm", {})
        self.base_url_edit = QLineEdit(str(llm.get("base_url", "")))
        txt._form.addRow("Base URL", self.base_url_edit)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(self._key_placeholder(llm.get("api_key", "")))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        self.show_key_btn = QPushButton("显示")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.toggled.connect(self._toggle_key_visible)
        key_row.addWidget(self.show_key_btn)
        txt._form.addRow("API 密钥", key_row)
        txt._form.addRow(_hint("已保存的密钥加密存放，这里只显示末 4 位；留空表示不修改。"))

        self.model_edit = QLineEdit(str(llm.get("model", "")))
        txt._form.addRow("主模型", self.model_edit)

        self.backup_edit = QLineEdit(", ".join(llm.get("backup_models", [])))
        txt._form.addRow("备用模型", self.backup_edit)
        txt._form.addRow(_hint("主模型连不上时按顺序尝试，多个用英文逗号分隔。"))

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setValue(int(llm.get("timeout_seconds", 90)))
        self.timeout_spin.setSuffix(" 秒")
        txt._form.addRow("请求超时", self.timeout_spin)

        self.maxtoken_spin = QSpinBox()
        self.maxtoken_spin.setRange(50, 8000)
        self.maxtoken_spin.setSingleStep(50)
        self.maxtoken_spin.setValue(int(llm.get("max_tokens", 800)))
        txt._form.addRow("单次最大 token", self.maxtoken_spin)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(lambda: asyncio.ensure_future(self.on_test_connection()))
        test_row.addWidget(self.test_btn)
        self.test_result = _hint("填好后点测试，会发一条最小请求验证地址和密钥能不能用。")
        test_row.addWidget(self.test_result, 1)
        txt._form.addRow(test_row)
        lay.addWidget(txt)

        # --- 视觉模型 ---
        vis = _section("视觉模型（发图片聊天用，P4 开放）")
        vision = self.store.cfg.get("vision", {})
        self.vision_enable = QCheckBox("启用图片多模态")
        self.vision_enable.setChecked(bool(vision.get("enabled", False)))
        self.vision_enable.toggled.connect(self._sync_vision_enabled)
        vis._form.addRow(self.vision_enable)

        self.vision_inherit = QCheckBox("视觉接口沿用文本模型的地址和密钥")
        self.vision_inherit.setChecked(bool(vision.get("inherit_from_llm", True)))
        self.vision_inherit.toggled.connect(self._sync_vision_enabled)
        vis._form.addRow(self.vision_inherit)

        self.vision_url = QLineEdit(str(vision.get("base_url", "")))
        self.vision_key = QLineEdit()
        self.vision_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.vision_key.setPlaceholderText(self._key_placeholder(vision.get("api_key", "")))
        self.vision_model = QLineEdit(str(vision.get("model", "")))
        vis._form.addRow("视觉 Base URL", self.vision_url)
        vis._form.addRow("视觉密钥", self.vision_key)
        vis._form.addRow("视觉模型", self.vision_model)

        self.img_kb_spin = QSpinBox()
        self.img_kb_spin.setRange(50, 5000)
        self.img_kb_spin.setSingleStep(50)
        self.img_kb_spin.setValue(int(vision.get("max_image_kb", 500)))
        self.img_kb_spin.setSuffix(" KB")
        vis._form.addRow("图片压缩上限", self.img_kb_spin)
        lay.addWidget(vis)
        self._sync_vision_enabled()

        lay.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def _toggle_key_visible(self, shown: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                  else QLineEdit.EchoMode.Password)
        self.show_key_btn.setText("隐藏" if shown else "显示")

    def _sync_vision_enabled(self) -> None:
        """视觉没启用 / 选择继承时，独立地址密钥输入框禁用。"""
        enabled = self.vision_enable.isChecked()
        independent = enabled and not self.vision_inherit.isChecked()
        self.vision_inherit.setEnabled(enabled)
        for w in (self.vision_url, self.vision_key, self.vision_model):
            w.setEnabled(independent)
        self.img_kb_spin.setEnabled(enabled)

    def _key_placeholder(self, stored: str) -> str:
        """已保存密钥（secret: 引用）→ 只提示末 4 位，不回显明文。"""
        if stored and stored.startswith("secret:"):
            # 真正的末 4 位要解密后取，避免在界面暴露，这里只提示"已保存"
            return "已保存（加密）。留空=不修改，重填=替换"
        if stored:
            tail = stored[-4:]
            return f"当前明文密钥末 4 位：{tail}。留空=不修改"
        return "还没填，粘贴 sk- 开头的密钥"

    async def on_test_connection(self) -> None:
        """用界面上当前填写的值发一条最小请求，成功/失败都说人话。"""
        if self._testing:
            return
        self._testing = True
        self.test_btn.setEnabled(False)
        self.test_result.setText("正在连接，请稍候…")
        try:
            # 界面值优先；密钥留空则用已保存的
            secrets = self.controller.secrets
            key = self.key_edit.text().strip()
            if not key:
                key = secrets.resolve(self.store.cfg.get("llm", {}).get("api_key", ""))
            base_url = self.base_url_edit.text().strip()
            model = self.model_edit.text().strip()
            if not base_url or not model:
                self.test_result.setText("Base URL 和模型名不能为空。")
                return
            if not key:
                self.test_result.setText("还没有 API 密钥，先填一个再测试。")
                return

            import time
            from openai import AsyncOpenAI
            t0 = time.time()
            client = AsyncOpenAI(base_url=base_url, api_key=key, timeout=20)
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping，请回复一个字：在"}],
                max_tokens=4, stream=False,
            )
            ms = int((time.time() - t0) * 1000)
            answer = resp.choices[0].message.content if resp.choices else ""
            self.test_result.setStyleSheet("color:#2e7d32;")
            self.test_result.setText(f"连接成功（{ms}ms），模型回复：{answer or '（空）'}")
        except Exception as e:  # noqa: BLE001 —— 任何失败都转成人话，不甩 traceback
            self.test_result.setStyleSheet("color:#c62828;")
            self.test_result.setText(f"连接失败：{type(e).__name__}: {str(e)[:120]}")
        finally:
            self._testing = False
            self.test_btn.setEnabled(True)

    # ==================================================================
    # 标签页 3：语音（P4 接线，先存配置）
    # ==================================================================
    def _build_voice_tab(self) -> QWidget:
        scroll = self._scroll()
        page = QWidget()
        lay = QVBoxLayout(page)
        voice = self.store.cfg.get("voice", {})

        rec = _section("录音与转写")
        self.voice_enable = QCheckBox("启用语音对话（聊天窗按住 🎤 说话）")
        self.voice_enable.setChecked(bool(voice.get("enabled", False)))
        rec._form.addRow(self.voice_enable)

        # 录音设备下拉：枚举系统麦克风，第一项是"系统默认"
        self.record_device_combo = QComboBox()
        self.record_device_combo.addItem("系统默认设备", "default")
        try:
            from voice.recorder import AudioRecorder
            for dev in AudioRecorder.list_input_devices():
                desc = dev.description()
                self.record_device_combo.addItem(desc, desc)
        except Exception:  # noqa: BLE001 —— 没音频设备时不能让设置窗打不开
            pass
        cur_dev = str(voice.get("record_device", "default"))
        di = self.record_device_combo.findData(cur_dev)
        self.record_device_combo.setCurrentIndex(di if di >= 0 else 0)
        rec._form.addRow("录音设备", self.record_device_combo)

        self.record_max_spin = QSpinBox()
        self.record_max_spin.setRange(5, 300)
        self.record_max_spin.setValue(int(voice.get("max_record_seconds", 60)))
        self.record_max_spin.setSuffix(" 秒")
        rec._form.addRow("单次最长录音", self.record_max_spin)

        self.stt_url = QLineEdit(str(voice.get("stt_base_url", "")))
        rec._form.addRow("语音转写地址", self.stt_url)
        self.stt_model = QLineEdit(str(voice.get("stt_model", "")))
        rec._form.addRow("转写模型", self.stt_model)

        # STT 密钥（密码框 + 显隐，留空=不修改，和文本密钥一致）
        stt_key_row = QHBoxLayout()
        self.stt_key = QLineEdit()
        self.stt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.stt_key.setPlaceholderText(self._key_placeholder(voice.get("stt_api_key", "")))
        self.stt_show_btn = QPushButton("显示")
        self.stt_show_btn.setFixedWidth(56)
        self.stt_show_btn.setCheckable(True)
        self.stt_show_btn.toggled.connect(
            lambda on: self.stt_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        stt_key_row.addWidget(self.stt_key)
        stt_key_row.addWidget(self.stt_show_btn)
        rec._form.addRow("转写密钥", stt_key_row)
        rec._form.addRow(_hint("转写走 OpenAI 兼容的 /audio/transcriptions；"
                               "只用 edge-tts 朗读、不做语音转文字可以不填。"))
        lay.addWidget(rec)

        tts = _section("语音合成（朗读）")
        self.tts_provider = QComboBox()
        self.tts_provider.addItems(["edge-tts", "openai_compatible"])
        idx = self.tts_provider.findText(str(voice.get("tts_provider", "edge-tts")))
        if idx >= 0:
            self.tts_provider.setCurrentIndex(idx)
        tts._form.addRow("合成提供方", self.tts_provider)
        self.tts_voice = QLineEdit(str(voice.get("tts_voice", "zh-CN-XiaoxiaoNeural")))
        tts._form.addRow("声音", self.tts_voice)
        tts._form.addRow(_hint("edge-tts 常用：zh-CN-XiaoxiaoNeural（女声）、"
                               "zh-CN-YunxiNeural（男声），需联网，不要密钥。"))
        self.auto_speak = QCheckBox("她回复后自动朗读")
        self.auto_speak.setChecked(bool(voice.get("auto_speak", False)))
        tts._form.addRow(self.auto_speak)

        self.voice_test_btn = QPushButton("▶ 试听当前声音（用上面填的设置）")
        self.voice_test_btn.clicked.connect(self.on_voice_test)
        tts._form.addRow(self.voice_test_btn)
        lay.addWidget(tts)
        lay.addWidget(_hint("按住聊天窗 🎤 说话，松开自动转文字并发送；"
                            "语音只在识别时临时使用，默认不保存录音。"))
        lay.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def on_voice_test(self) -> None:
        """用界面上当前填的 TTS 设置合成一句试听（不必先保存）。"""
        import asyncio
        self.voice_test_btn.setEnabled(False)
        asyncio.ensure_future(self._voice_test_flow())

    async def _voice_test_flow(self) -> None:
        from voice.tts import TTSClient, TTSError
        from voice.player import AudioPlayer
        try:
            voice_cfg = {
                "tts_provider": self.tts_provider.currentText(),
                "tts_voice": self.tts_voice.text().strip() or "zh-CN-XiaoxiaoNeural",
            }
            tts = TTSClient(voice_cfg, self.controller._runtime_cfg.get("llm", {}))
            audio, suffix = await tts.synthesize("你好呀，我就在你的桌面上，点这里可以试听我的声音。")
            if not hasattr(self, "_test_player") or self._test_player is None:
                self._test_player = AudioPlayer(parent=self)
            played = self._test_player.play_bytes(audio, suffix)
            self._flash("正在试听…" if played else "试听没播出来，检查系统音量。",
                        error=not played)
        except TTSError as e:
            self._flash(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            self._flash(f"试听失败：{type(e).__name__}", error=True)
        finally:
            self.voice_test_btn.setEnabled(True)

    # ==================================================================
    # 标签页 4：电脑控制（P5 接线，先存配置）
    # ==================================================================
    def _build_hands_tab(self) -> QWidget:
        scroll = self._scroll()
        page = QWidget()
        lay = QVBoxLayout(page)
        sb = self.store.cfg.get("sandbox", {})

        scope = _section("活动范围（她只能碰这个文件夹里的东西）")
        ws_row = QHBoxLayout()
        self.workspace_edit = QLineEdit(str(sb.get("workspace_root", "")))
        self.workspace_edit.setPlaceholderText("留空则不允许文件操作")
        ws_row.addWidget(self.workspace_edit, 1)
        pick_ws = QPushButton("浏览…")
        pick_ws.clicked.connect(self._pick_workspace)
        ws_row.addWidget(pick_ws)
        scope._form.addRow("工作目录", ws_row)
        self.rollback_chk = QCheckBox("移动/删除前生成回滚快照")
        self.rollback_chk.setChecked(bool(sb.get("enable_rollback_snapshot", True)))
        scope._form.addRow(self.rollback_chk)
        lay.addWidget(scope)

        cap = _section("能力开关（她能做哪些操作）")
        self.allow_files = QCheckBox("文件：列举/新建/移动/回收站删除")
        self.allow_files.setChecked(bool(sb.get("allow_files", True)))
        self.allow_clipboard = QCheckBox("剪贴板：预览后读写（覆盖前留快照）")
        self.allow_clipboard.setChecked(bool(sb.get("allow_clipboard", True)))
        self.allow_windows = QCheckBox("窗口：普通窗口的聚焦/最小化/恢复")
        self.allow_windows.setChecked(bool(sb.get("allow_windows", True)))
        self.allow_processes = QCheckBox("进程：启动白名单程序（默认关，最敏感）")
        self.allow_processes.setChecked(bool(sb.get("allow_processes", False)))
        for w in (self.allow_files, self.allow_clipboard, self.allow_windows,
                  self.allow_processes):
            cap._form.addRow(w)

        self.confirm_policy = QComboBox()
        self.confirm_policy.addItem("每次操作都问我", "always")
        self.confirm_policy.addItem("只有高危操作问我", "smart")
        cur_policy = str(sb.get("confirm_policy", "always"))
        for i in range(self.confirm_policy.count()):
            if self.confirm_policy.itemData(i) == cur_policy:
                self.confirm_policy.setCurrentIndex(i)
        cap._form.addRow("确认策略", self.confirm_policy)

        self.whitelist_edit = QLineEdit(", ".join(sb.get("process_whitelist", [])))
        cap._form.addRow("程序白名单", self.whitelist_edit)
        cap._form.addRow(_hint("允许她启动的程序名，英文逗号分隔，例如 notepad, calc。"
                               "删除永远只进回收站，绝不永久删除。"))
        lay.addWidget(cap)

        log_box = _section("操作记录")
        self.audit_btn = QPushButton("📜 查看她最近对电脑做过什么")
        self.audit_btn.clicked.connect(self.on_show_audit)
        log_box._form.addRow(self.audit_btn)
        log_box._form.addRow(_hint("每次操作都有脱敏记录：剪贴板只记长度、文件只记相对路径。"))
        lay.addWidget(log_box)
        lay.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def on_show_audit(self) -> None:
        """非阻塞弹窗展示最近的电脑操作审计（只读，不用 exec）。"""
        hands = getattr(self.controller, "hands", None)
        entries = hands.audit.recent(60) if hands is not None else []
        if not entries:
            body = "还没有任何电脑操作记录。"
        else:
            lines = []
            stage_word = {"executed": "已执行", "blocked": "已拦截",
                          "declined": "已取消", "failed": "失败"}
            for e in reversed(entries):
                mark = "✓" if e.get("ok") else "✗"
                lines.append(f"{e.get('ts','')} {mark} "
                             f"{e.get('action','')} → {e.get('target','')} "
                             f"[{stage_word.get(e.get('stage'),'')}] {e.get('message','')}")
            body = "\n".join(lines)
        box = QMessageBox(self)
        box.setWindowTitle("电脑操作记录（最近 60 条）")
        box.setText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.show()

    def _pick_workspace(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择她可以操作的文件夹")
        if folder:
            self.workspace_edit.setText(folder)

    # ==================================================================
    # 标签页 5：浏览器插件（P6 接线）
    # ==================================================================
    def _build_browser_tab(self) -> QWidget:
        scroll = self._scroll()
        page = QWidget()
        lay = QVBoxLayout(page)
        br = self.store.cfg.get("browser", {})

        # ---- 连接与配对 ----
        conn = _section("连接与配对")
        self.browser_status = _hint("● 读取状态中…")
        self.browser_status.setStyleSheet("font-size:12px;")
        conn._form.addRow(self.browser_status)

        self.pair_code_view = QLineEdit()
        self.pair_code_view.setReadOnly(True)
        self.pair_code_view.setPlaceholderText("点下面的「生成配对码」，把显示的地址和配对码填进扩展")
        conn._form.addRow("配对信息", self.pair_code_view)

        btn_row = QHBoxLayout()
        self.pair_btn = QPushButton("生成配对码并开始监听")
        self.unpair_btn = QPushButton("断开并关闭监听")
        self.unpair_btn.setProperty("danger", "true")
        self.pair_btn.clicked.connect(self.on_gen_pair)
        self.unpair_btn.clicked.connect(self.on_unpair)
        btn_row.addWidget(self.pair_btn)
        btn_row.addWidget(self.unpair_btn)
        btn_row.addStretch(1)
        conn._form.addRow(btn_row)
        lay.addWidget(conn)

        # ---- 能力开关（每个动作仍会逐次确认）----
        cap = _section("她能对浏览器做什么（每次操作仍会先请你确认）")
        self.br_allow_read = QCheckBox("读取网页正文（内容会发给模型理解）")
        self.br_allow_navigate = QCheckBox("打开网址")
        self.br_allow_click = QCheckBox("点击网页元素")
        self.br_allow_type = QCheckBox("在输入框填字（密码/验证码永远禁止）")
        self.br_allow_scroll = QCheckBox("滚动页面")
        self.br_allow_close = QCheckBox("关闭当前标签页（较敏感）")
        for cb, key in ((self.br_allow_read, "allow_read"),
                        (self.br_allow_navigate, "allow_navigate"),
                        (self.br_allow_click, "allow_click"),
                        (self.br_allow_type, "allow_type"),
                        (self.br_allow_scroll, "allow_scroll"),
                        (self.br_allow_close, "allow_close_tab")):
            cb.setChecked(bool(br.get(key, False)))
            cap._form.addRow(cb)
        lay.addWidget(cap)

        howto = _section("安装方式（Chrome / Edge 通用）")
        howto._form.addRow(_hint(
            "1. 地址栏输入 chrome://extensions（Edge 是 edge://extensions）\n"
            "2. 打开右上角「开发者模式」→「加载已解压的扩展程序」→ 选项目里的 browser_extension 文件夹\n"
            "3. 点上面「生成配对码」，再点浏览器里的扩展图标，把配对码粘进去即可\n"
            "桥只监听本机 127.0.0.1，不开放局域网/公网；支付页、密码框、最终提交一律不碰。"))
        lay.addWidget(howto)
        lay.addStretch(1)
        scroll.setWidget(page)

        # 桥连接状态变化时实时刷新本页
        listeners = getattr(self.controller, "_browser_state_listeners", None)
        if isinstance(listeners, list):
            listeners.append(lambda _st: self._refresh_browser_status())
        self._refresh_browser_status()
        return scroll

    # ---- P6：配对 / 断开 / 状态刷新（async，走 qasync 不冻界面）----
    def on_gen_pair(self) -> None:
        self.pair_code_view.setText("正在启动本机监听…")
        asyncio.ensure_future(self._gen_pair_flow())

    async def _gen_pair_flow(self) -> None:
        try:
            code = await self.controller.browser_start_pairing()
            bridge = self.controller.browser_bridge
            self.pair_code_view.setText(
                f"ws://127.0.0.1:{bridge.port}    配对码：{code}（5 分钟内有效）")
        except Exception as e:  # noqa: BLE001
            self.pair_code_view.setText(f"启动失败：{e}")
        self._refresh_browser_status()

    def on_unpair(self) -> None:
        asyncio.ensure_future(self._unpair_flow())

    async def _unpair_flow(self) -> None:
        await self.controller.browser_unpair()
        self.pair_code_view.clear()
        self._refresh_browser_status()

    def _refresh_browser_status(self) -> None:
        bridge = getattr(self.controller, "browser_bridge", None)
        if bridge is None:
            self.browser_status.setText("● 浏览器桥未启用")
            return
        if bridge.is_connected:
            self.browser_status.setText("● 已和浏览器扩展连接")
            self.browser_status.setStyleSheet("color:#2e7d32;font-size:12px;")
        elif bridge.state == "waiting":
            self.browser_status.setText("● 正在等待扩展配对…把配对码填进扩展")
            self.browser_status.setStyleSheet("color:#b8860b;font-size:12px;")
        else:
            self.browser_status.setText("● 未连接")
            self.browser_status.setStyleSheet("color:#c62828;font-size:12px;")
        self.unpair_btn.setEnabled(bridge.state != "stopped")

    # ==================================================================
    # 标签页 6：隐私
    # ==================================================================
    def _build_privacy_tab(self) -> QWidget:
        scroll = self._scroll()
        page = QWidget()
        lay = QVBoxLayout(page)
        priv = self.store.cfg.get("privacy", {})

        data = _section("数据与录音")
        self.save_audio = QCheckBox("保留录音文件（默认转写完成后立即删除）")
        self.save_audio.setChecked(bool(priv.get("save_audio", False)))
        data._form.addRow(self.save_audio)

        self.log_days_spin = QSpinBox()
        self.log_days_spin.setRange(1, 3650)
        self.log_days_spin.setValue(int(priv.get("log_retention_days", 30)))
        self.log_days_spin.setSuffix(" 天")
        data._form.addRow("日志保留", self.log_days_spin)
        lay.addWidget(data)

        danger = _section("清空当前伙伴的记忆")
        danger._form.addRow(_hint(
            "会清空当前伙伴最近的对话记录和长期情节记忆（情绪数值保留）。"
            "此操作不可撤销，只影响当前这一个伙伴。"))
        clear_row = QHBoxLayout()
        self.clear_mem_btn = QPushButton("清空「当前伙伴」的记忆")
        self.clear_mem_btn.setStyleSheet("QPushButton{color:#c62828;}")
        self.clear_mem_btn.clicked.connect(self.on_clear_memory)
        clear_row.addWidget(self.clear_mem_btn)
        clear_row.addStretch(1)
        danger._form.addRow(clear_row)
        lay.addWidget(danger)
        lay.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def on_clear_memory(self) -> None:
        """清空当前角色的对话历史 + 情节记忆。非阻塞确认（show，不用 exec 冻界面）。
        这里用 QMessageBox 的非阻塞方式：弹出来，用户点了再走回调。"""
        cur = self.controller.characters.current
        box = QMessageBox(self)
        box.setWindowTitle("确认清空")
        box.setText(f"确定要清空「{cur.name}」的对话记录和长期记忆吗？\n"
                    f"情绪数值会保留，此操作不可撤销。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        # 非阻塞：用 buttonClicked 信号，不调用 exec()
        def _after(btn):
            if box.standardButton(btn) == QMessageBox.StandardButton.Yes:
                eng = self.controller.engine
                eng._history.clear()
                eng._save_history()
                try:
                    eng.memory.clear()
                except Exception as e:  # noqa: BLE001
                    self._flash(f"对话已清空，但长期记忆清理失败：{e}", error=True)
                    return
                self._flash(f"已清空「{cur.name}」的记忆。")
            box.deleteLater()
        box.buttonClicked.connect(_after)
        box.show()

    # ==================================================================
    # 保存
    # ==================================================================
    def on_save(self) -> None:
        """把六个标签页的值收集成 config 分段，交给 controller 热更新。"""
        backup = [m.strip() for m in self.backup_edit.text().split(",") if m.strip()]
        new_cfg = {
            "llm": {
                "base_url": self.base_url_edit.text().strip(),
                "model": self.model_edit.text().strip(),
                "backup_models": backup,
                "timeout_seconds": self.timeout_spin.value(),
                "max_tokens": self.maxtoken_spin.value(),
            },
            "vision": {
                "enabled": self.vision_enable.isChecked(),
                "inherit_from_llm": self.vision_inherit.isChecked(),
                "base_url": self.vision_url.text().strip(),
                "model": self.vision_model.text().strip(),
                "max_image_kb": self.img_kb_spin.value(),
            },
            "voice": {
                "enabled": self.voice_enable.isChecked(),
                "record_device": self.record_device_combo.currentData(),
                "max_record_seconds": self.record_max_spin.value(),
                "stt_base_url": self.stt_url.text().strip(),
                "stt_model": self.stt_model.text().strip(),
                "tts_provider": self.tts_provider.currentText(),
                "tts_voice": self.tts_voice.text().strip(),
                "auto_speak": self.auto_speak.isChecked(),
            },
            "sandbox": {
                "workspace_root": self.workspace_edit.text().strip(),
                "enable_rollback_snapshot": self.rollback_chk.isChecked(),
                "allow_files": self.allow_files.isChecked(),
                "allow_processes": self.allow_processes.isChecked(),
                "allow_clipboard": self.allow_clipboard.isChecked(),
                "allow_windows": self.allow_windows.isChecked(),
                "confirm_policy": self.confirm_policy.currentData(),
                "process_whitelist": [
                    s.strip() for s in self.whitelist_edit.text().split(",") if s.strip()],
            },
            "browser": {
                "allow_read": self.br_allow_read.isChecked(),
                "allow_navigate": self.br_allow_navigate.isChecked(),
                "allow_click": self.br_allow_click.isChecked(),
                "allow_type": self.br_allow_type.isChecked(),
                "allow_scroll": self.br_allow_scroll.isChecked(),
                "allow_close_tab": self.br_allow_close.isChecked(),
            },
            "privacy": {
                "save_audio": self.save_audio.isChecked(),
                "log_retention_days": self.log_days_spin.value(),
            },
            "character": {
                "size": self.size_spin.value(),
                "opacity": self.opacity_spin.value(),
            },
        }
        # 密钥：填了才传（apply_config 内部会加密）；留空不传 = 保持原密钥
        key = self.key_edit.text().strip()
        if key:
            new_cfg["llm"]["api_key"] = key
        vkey = self.vision_key.text().strip()
        if vkey:
            new_cfg["vision"]["api_key"] = vkey
        sttkey = self.stt_key.text().strip()
        if sttkey:
            new_cfg["voice"]["stt_api_key"] = sttkey

        ok, msg = self.controller.apply_config(new_cfg)
        if ok:
            self._flash(msg)
        else:
            self._flash(msg, error=True)

    # ==================================================================
    # 小工具
    # ==================================================================
    def _load_current(self) -> None:
        """从 config_store 重新读一遍值刷新所有控件。
        面板已经开着、期间配置被别处改动时（如右键换角色）调用；
        密钥输入框故意不清空也不回填明文，只更新它的占位提示。"""
        cfg = self.store.cfg
        llm = cfg.get("llm", {})
        self.base_url_edit.setText(str(llm.get("base_url", "")))
        self.key_edit.clear()
        self.key_edit.setPlaceholderText(self._key_placeholder(llm.get("api_key", "")))
        self.model_edit.setText(str(llm.get("model", "")))
        self.backup_edit.setText(", ".join(llm.get("backup_models", [])))
        self.timeout_spin.setValue(int(llm.get("timeout_seconds", 90)))
        self.maxtoken_spin.setValue(int(llm.get("max_tokens", 800)))

        vision = cfg.get("vision", {})
        self.vision_enable.setChecked(bool(vision.get("enabled", False)))
        self.vision_inherit.setChecked(bool(vision.get("inherit_from_llm", True)))
        self.vision_url.setText(str(vision.get("base_url", "")))
        self.vision_key.clear()
        self.vision_model.setText(str(vision.get("model", "")))
        self.img_kb_spin.setValue(int(vision.get("max_image_kb", 500)))
        self._sync_vision_enabled()

        voice = cfg.get("voice", {})
        self.voice_enable.setChecked(bool(voice.get("enabled", False)))
        di = self.record_device_combo.findData(str(voice.get("record_device", "default")))
        self.record_device_combo.setCurrentIndex(di if di >= 0 else 0)
        self.record_max_spin.setValue(int(voice.get("max_record_seconds", 60)))
        self.stt_url.setText(str(voice.get("stt_base_url", "")))
        self.stt_model.setText(str(voice.get("stt_model", "")))
        self.stt_key.clear()
        self.stt_key.setPlaceholderText(self._key_placeholder(voice.get("stt_api_key", "")))
        idx = self.tts_provider.findText(str(voice.get("tts_provider", "edge-tts")))
        if idx >= 0:
            self.tts_provider.setCurrentIndex(idx)
        self.tts_voice.setText(str(voice.get("tts_voice", "")))
        self.auto_speak.setChecked(bool(voice.get("auto_speak", False)))

        sb = cfg.get("sandbox", {})
        self.workspace_edit.setText(str(sb.get("workspace_root", "")))
        self.rollback_chk.setChecked(bool(sb.get("enable_rollback_snapshot", True)))
        self.allow_files.setChecked(bool(sb.get("allow_files", True)))
        self.allow_processes.setChecked(bool(sb.get("allow_processes", False)))
        self.allow_clipboard.setChecked(bool(sb.get("allow_clipboard", True)))
        self.allow_windows.setChecked(bool(sb.get("allow_windows", True)))
        for i in range(self.confirm_policy.count()):
            if self.confirm_policy.itemData(i) == sb.get("confirm_policy", "always"):
                self.confirm_policy.setCurrentIndex(i)
        self.whitelist_edit.setText(", ".join(sb.get("process_whitelist", [])))

        priv = cfg.get("privacy", {})
        self.save_audio.setChecked(bool(priv.get("save_audio", False)))
        self.log_days_spin.setValue(int(priv.get("log_retention_days", 30)))

        ch = cfg.get("character", {})
        self.size_spin.setValue(int(ch.get("size", 160)))
        self.opacity_spin.setValue(float(ch.get("opacity", 1.0)))

        # 角色单选也刷新（可能在面板外切换/导入过）
        self._populate_character_radios()
        cur = self.controller.characters.current
        self._suppress_display_signal = True
        self.display_combo.setCurrentIndex(self._current_display_index())
        self._suppress_display_signal = False
        self._refresh_model_info()
        self.data_dir_lab.setText(
            f"当前伙伴「{cur.name}」的数据目录："
            f"{self.controller.characters.data_dir_for(cur.id)}")
        self.test_result.setText("填好后点测试，会发一条最小请求验证地址和密钥能不能用。")
        self.test_result.setStyleSheet("color:#9a8f7d;")

        # 商店页：余额与库存跟着当前角色走，换伙伴后必须重刷
        shop_tab = getattr(self, "shop_tab", None)
        if shop_tab is not None:
            shop_tab.refresh()

    def _scroll(self) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        return area

    def _flash(self, text: str, error: bool = False) -> None:
        self.status_lab.setStyleSheet("color:#c62828;" if error else "color:#2e7d32;")
        self.status_lab.setText(text)


# ----------------------------------------------------------------------
# 使用示例：python face/settings_panel.py —— 单独预览设置窗
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    from qasync import QEventLoop

    root = Path(__file__).resolve().parent.parent
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    from app.controller import AppController
    controller = AppController(root)
    panel = SettingsPanel(controller)
    panel.show()
    with loop:
        loop.run_forever()
