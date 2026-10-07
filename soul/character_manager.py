# -*- coding: utf-8 -*-
"""
CharacterManager —— 角色注册 / 切换 / 数据隔离 / 外观资产更新
=============================================================
每个角色都是一份"只读资产"（characters/<id>/ 里的人设、台词、外观），
而她"经历了什么"（情绪数值、聊天记录、记忆）是运行时状态，存在
data/characters/<id>/ 下，各角色互不干扰。

默认角色 xiaoling 的数据继续放在 data/ 根目录（向后兼容，老用户无感）。

切换角色的完整流程（switch_to）：
  1. flush 当前角色：情绪数值和聊天记录落盘
  2. 算出目标角色的数据目录（不存在就创建）
  3. SoulEngine.load_character() 重建 CESM/记忆/人设/历史
  4. 返回新角色的 CharacterProfile，界面层据此换渲染器

外观资产（立绘 / 3D 模型 / 显示模式）的更新套路统一是三步：
复制文件进角色目录 → 原子改写 manifest → 重载该角色替换引用。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from characters.character_loader import CharacterLoader, CharacterProfile


class CharacterManager:
    """管理所有可用角色和当前角色，负责安全切换。"""

    # 默认角色的数据放在 data/ 根目录，保持老版本数据不动
    DEFAULT_ID = "xiaoling"

    def __init__(self, root: Path, engine, characters_dir: Path | None = None):
        self.root = Path(root)
        self.engine = engine
        self.characters_dir = characters_dir or (self.root / "characters")
        self.loader = CharacterLoader(self.characters_dir)
        self.available: list[CharacterProfile] = []
        self.current: CharacterProfile | None = None

    # ------------------------------------------------------------------
    # 发现 / 初始加载
    # ------------------------------------------------------------------
    def discover(self) -> list[CharacterProfile]:
        """扫描 characters/ 目录，返回所有可用角色。
        一个角色包都没有时，降级用旧版 data/character_core.json 兜底。"""
        self.available = self.loader.list_characters()
        if not self.available:
            legacy = self.loader.load_legacy_character(
                self.root / "data" / "character_core.json")
            if legacy:
                self.available = [legacy]
        return self.available

    def load_initial(self, preferred_id: str | None = None) -> CharacterProfile:
        """程序启动时调用：加载上次用的角色（或指定角色）。
        SoulEngine 已经在 __init__ 里从 data/ 读了旧版人设，
        这里把角色包的人设/台词覆盖进去（CESM 数值不动）。"""
        if not self.available:
            self.discover()

        target = self._find(preferred_id) or self._find(self.DEFAULT_ID)
        if target is None and self.available:
            target = self.available[0]
        if target is None:
            raise RuntimeError("没有找到任何可用角色（characters/ 为空且无旧版人设）")

        data_dir = self.data_dir_for(target.id)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.engine.load_character(target, data_dir)
        self.current = target
        return target

    # ------------------------------------------------------------------
    # 切换
    # ------------------------------------------------------------------
    def switch_to(self, character_id: str) -> tuple[bool, str]:
        """切换到指定角色。返回 (是否成功, 人话说明)。
        流式回复中禁止切换（AppController 会先检查 is_busy）。"""
        if self.current and character_id == self.current.id:
            return True, "已经是这个伙伴啦"

        target = self._find(character_id)
        if target is None:
            return False, f"找不到角色：{character_id}"

        # 1. 当前角色落盘
        try:
            self.engine.flush()
        except Exception as exc:  # noqa: BLE001 —— 落盘失败不阻断切换
            print(f"[Character] 旧角色数据保存失败（继续切换）：{exc}")

        # 2. 目标数据目录
        new_dir = self.data_dir_for(target.id)
        new_dir.mkdir(parents=True, exist_ok=True)

        # 3. 灵魂层换角色（重建 CESM/记忆/人设/历史，LLM 客户端不动）
        self.engine.load_character(target, new_dir)
        old_name = self.current.name if self.current else "？"
        self.current = target
        return True, f"已从「{old_name}」切换到「{target.name}」"

    def data_dir_for(self, character_id: str) -> Path:
        """角色数据目录。默认角色用 data/ 根目录，其他角色各自独立。"""
        if character_id == self.DEFAULT_ID:
            return self.root / "data"
        return self.root / "data" / "characters" / character_id

    # ------------------------------------------------------------------
    # 自定义立绘
    # ------------------------------------------------------------------
    def set_avatar(self, character_id: str, png_src) -> tuple[CharacterProfile | None, str]:
        """把一张 PNG 设为某角色的立绘。
        复制进角色目录成 avatar.png、把 manifest.avatar 指过去、再重载该角色。
        返回 (新profile, 说明)；失败时 profile 为 None。
        为什么复制而不是引用原路径：用户的图片常在下载文件夹，可能被移动/删除，
        复制进角色目录后角色包才是自洽的，打包/分享也不丢图。"""
        target = self._find(character_id)
        if target is None:
            return None, f"找不到角色：{character_id}"
        if target.path is None:
            return None, "这个角色没有独立文件夹，无法设置立绘"
        src = Path(png_src)
        if not src.is_file():
            return None, "找不到这张图片文件"
        if src.suffix.lower() != ".png":
            return None, "立绘只支持 PNG 格式（透明背景效果最好）"

        char_dir = Path(target.path)
        # 1. 复制图片（同文件不重复写）
        dest = char_dir / "avatar.png"
        try:
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
        except OSError as e:
            return None, f"图片复制失败：{e}"

        # 2. manifest 里登记 avatar 字段（原子写回，保留原有其他字段）
        ok, msg = self._patch_manifest(char_dir, {"avatar": "avatar.png"})
        if not ok:
            return None, msg

        # 3. 重载该角色并替换列表/当前引用
        new_profile, msg = self._reload_character(character_id)
        if new_profile is None:
            return None, f"立绘已复制但{msg}"
        return new_profile, f"已为「{new_profile.name}」设置立绘"

    # ------------------------------------------------------------------
    # 3D 模型
    # ------------------------------------------------------------------
    def set_model3d(self, character_id: str, model_src) -> tuple[CharacterProfile | None, str]:
        """把一个 .glb/.gltf 模型挂到某角色身上。
        复制进角色目录成 model.glb / model.gltf、更新 manifest、重载角色。
        返回 (新profile, 说明)；失败时 profile 为 None。
        和立绘同理：复制而不是引用原路径，角色包才是自洽的。"""
        target = self._find(character_id)
        if target is None:
            return None, f"找不到角色：{character_id}"
        if target.path is None:
            return None, "这个角色没有独立文件夹，无法设置 3D 模型"
        src = Path(model_src)
        if not src.is_file():
            return None, "找不到这个模型文件"
        suffix = src.suffix.lower()
        if suffix not in (".glb", ".gltf"):
            return None, "只支持 .glb / .gltf 格式的 3D 模型"

        char_dir = Path(target.path)
        # 先试加载一遍：坏模型宁可不装，别等桌宠每帧回退 2D 时反复刷报错
        from face.model3d import Model3D
        if Model3D.load(src) is None:
            return None, "这个模型文件解析失败了，可能已损坏或不是标准 GLB/glTF"

        dest = char_dir / f"model{suffix}"
        try:
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            # 换格式重装（.gltf 换 .glb 等）时，旧模型文件改名保留：
            # loader 会按约定俗成的 model.glb/gltf 自动识别，不挪走会被误捡
            for stale in (char_dir / "model.glb", char_dir / "model.gltf"):
                if stale.is_file() and stale.resolve() != dest.resolve():
                    stale.rename(stale.with_name(stale.name + ".removed"))
        except OSError as e:
            return None, f"模型复制失败：{e}"

        # .gltf 常带外部 .bin / 贴图，一并复制，否则换机器打包会丢纹理
        if suffix == ".gltf":
            self._copy_gltf_deps(src, char_dir)

        ok, msg = self._patch_manifest(char_dir, {"model3d": dest.name})
        if not ok:
            return None, msg
        new_profile, msg = self._reload_character(character_id)
        if new_profile is None:
            return None, f"模型已复制但{msg}"
        return new_profile, f"已为「{new_profile.name}」挂上 3D 模型"

    @staticmethod
    def _copy_gltf_deps(src: Path, char_dir: Path) -> None:
        """把 .gltf 引用的同目录外部资源（.bin/贴图）复制进角色目录。
        单个文件复制失败只跳过：缺贴图顶多是素模，不影响可用性。
        （缺 .bin 会直接加载失败，但上面的解析探针已经拦下了那种情况。）"""
        try:
            gltf = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        deps: list[Path] = []
        for item in list(gltf.get("buffers", [])) + list(gltf.get("images", [])):
            uri = item.get("uri") if isinstance(item, dict) else None
            if uri and not uri.startswith("data:") and not Path(uri).is_absolute():
                p = (src.parent / uri).resolve()
                if p not in deps:
                    deps.append(p)
        for dep in deps:
            try:
                if dep.is_file():
                    shutil.copyfile(dep, char_dir / dep.name)
            except OSError:
                pass

    def remove_model3d(self, character_id: str) -> tuple[CharacterProfile | None, str]:
        """把 3D 模型从角色上摘下来。
        必须连文件一起处理：loader 会自动识别目录里的 model.glb，
        只清 manifest 字段的话"移除"根本不生效。
        文件改名成 .removed 保留（用户可能只是暂时想换回 2D），不直接删。"""
        target = self._find(character_id)
        if target is None:
            return None, f"找不到角色：{character_id}"
        if target.path is None:
            return None, "这个角色没有独立文件夹"
        char_dir = Path(target.path)
        for name in ("model.glb", "model.gltf"):
            f = char_dir / name
            if f.is_file():
                try:
                    f.rename(char_dir / (name + ".removed"))
                except OSError as e:
                    return None, f"模型文件处理失败：{e}"
        ok, msg = self._patch_manifest(char_dir, {"model3d": None})
        if not ok:
            return None, msg
        return self._reload_character(character_id)

    def set_display(self, character_id: str, mode: str) -> tuple[CharacterProfile | None, str]:
        """切换角色的显示模式：auto（有模型就用3D）/ 2d（强制矢量）/ 3d。"""
        if mode not in ("auto", "2d", "3d"):
            return None, f"未知显示模式：{mode}"
        target = self._find(character_id)
        if target is None:
            return None, f"找不到角色：{character_id}"
        if target.path is None:
            return None, "这个角色没有独立文件夹"
        ok, msg = self._patch_manifest(Path(target.path), {"display": mode})
        if not ok:
            return None, msg
        return self._reload_character(character_id)

    # ------------------------------------------------------------------
    # 公共小工具（manifest 原子改写 + 角色重载）
    # ------------------------------------------------------------------
    def _patch_manifest(self, char_dir: Path, fields: dict) -> tuple[bool, str]:
        """把若干字段原子写进 manifest（值为 None 表示删除该字段）。
        原子写：先写临时文件再 os.replace，中途断电也不会留半截 JSON。"""
        manifest_path = char_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key, val in fields.items():
                if val is None:
                    manifest.pop(key, None)
                else:
                    manifest[key] = val
            text = json.dumps(manifest, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(char_dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, manifest_path)
        except (OSError, json.JSONDecodeError) as e:
            return False, f"manifest 更新失败：{e}"
        return True, "已更新"

    def _reload_character(self, character_id: str) -> tuple[CharacterProfile | None, str]:
        """重载某角色包并替换列表/当前引用。外观资产变更后的统一收尾。"""
        try:
            new_profile = self.loader.load_character(character_id)
        except Exception as e:  # noqa: BLE001
            return None, f"角色重载失败：{e}"
        for i, p in enumerate(self.available):
            if p.id == character_id:
                self.available[i] = new_profile
        if self.current and self.current.id == character_id:
            self.current = new_profile
        return new_profile, "已更新"

    # ------------------------------------------------------------------
    def _find(self, character_id: str | None) -> CharacterProfile | None:
        if not character_id:
            return None
        for prof in self.available:
            if prof.id == character_id:
                return prof
        return None

    def list_names(self) -> list[tuple[str, str]]:
        """返回 [(id, 显示名), ...]，给菜单用。"""
        return [(p.id, p.name) for p in self.available]
