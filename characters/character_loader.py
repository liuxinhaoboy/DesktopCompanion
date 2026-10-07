# -*- coding: utf-8 -*-
"""
CharacterLoader —— 角色包加载器
================================
角色包规范（characters/<id>/ 目录）：
  manifest.json     元数据：id, name, version, description, renderer, shape, mood_styles
  system_prompt.md  角色的系统提示词（性格说明书）
  lines.json        所有台词（greeting/pat/feed/angry/nag/...）
  sprites/          立绘序列帧（P2 预留，目前用矢量绘制）

为什么要有角色包：
  以前人设硬编码在 data/character_core.json，和运行时数据混在一起。
  角色包把"她是谁"（人设/台词/外观）和"她经历了什么"（情绪/记忆/历史）
  分开：角色包是只读资产，data/ 是运行时状态。切换角色 = 换资产，不丢记忆。

降级策略：
  characters/ 目录不存在或为空时，回退到 data/character_core.json（兼容旧版）。
  某个角色包损坏时跳过它，不影响其他角色加载。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CharacterProfile:
    """一个角色的完整数据。"""
    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    renderer: str = "vector"          # "vector"（代码绘制）或 "sprites"（图片序列帧）
    shape: str = "cat"                # 矢量形状：cat/dog/slime（renderer=vector 时用）
    mood_styles: dict[str, dict] = field(default_factory=dict)
    system_prompt: str = ""
    lines: dict[str, list[str]] = field(default_factory=dict)
    path: Path | None = None
    # 自定义立绘：指向一张透明 PNG。有值时渲染器优先画图，没值就画矢量形状。
    # 为什么要这个字段：用户想用自己画的图当桌宠，不该被迫学代码改形状。
    avatar: Path | None = None
    # 3D 模型：指向一个 .glb/.gltf 文件。有值且 display 允许时渲染器画 3D。
    model3d: Path | None = None
    # 显示模式："auto"（有模型用3D，否则2D）/ "2d" / "3d"。
    # 为什么默认 auto：上传了模型的用户想直接看到 3D；没模型的角色不受影响。
    display: str = "auto"

    def to_character_dict(self) -> dict[str, Any]:
        """转成 SoulEngine 期望的 _character dict 格式（向后兼容）。"""
        d = {"name": self.name, "system_prompt": self.system_prompt}
        d.update(self.lines)
        return d

    def mood_style(self, mood_word: str) -> dict | None:
        """取某个情绪的外观配置（颜色是 [r,g,b] 数组，由界面层转 QColor）。"""
        return self.mood_styles.get(mood_word)


class CharacterLoader:
    """扫描和加载 characters/ 目录下的角色包。"""

    REQUIRED_MANIFEST_FIELDS = ("id", "name")

    def __init__(self, characters_dir: Path):
        self.dir = Path(characters_dir)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def list_characters(self) -> list[CharacterProfile]:
        """返回所有可用角色（按目录名排序）。损坏的角色包跳过并打印警告。"""
        profiles: list[CharacterProfile] = []
        if not self.dir.exists():
            return profiles
        for child in sorted(self.dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_") or child.name.startswith("."):
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.exists():
                continue   # 没有 manifest 的目录不是角色包，跳过
            try:
                profile = self.load_character(child.name)
                profiles.append(profile)
            except Exception as e:  # noqa: BLE001
                print(f"[Character] 角色包 '{child.name}' 加载失败，跳过：{e}")
        return profiles

    def list_names(self) -> list[str]:
        """返回所有可用角色的显示名。"""
        return [p.name for p in self.list_characters()]

    # ------------------------------------------------------------------
    # 加载单个角色
    # ------------------------------------------------------------------
    def load_character(self, character_id: str) -> CharacterProfile:
        """加载指定角色。文件缺失/格式错抛异常（调用方决定降级）。"""
        char_dir = self.dir / character_id
        if not char_dir.is_dir():
            raise FileNotFoundError(f"角色目录不存在：{char_dir}")

        manifest = self._load_manifest(char_dir / "manifest.json")
        system_prompt = self._load_system_prompt(char_dir / "system_prompt.md")
        lines = self._load_lines(char_dir / "lines.json")

        return CharacterProfile(
            id=manifest["id"],
            name=manifest["name"],
            version=manifest.get("version", "1.0.0"),
            description=manifest.get("description", ""),
            renderer=manifest.get("renderer", "vector"),
            shape=manifest.get("shape", "cat"),
            mood_styles=manifest.get("mood_styles", {}),
            system_prompt=system_prompt,
            lines=lines,
            path=char_dir,
            avatar=self._resolve_avatar(char_dir, manifest),
            model3d=self._resolve_model3d(char_dir, manifest),
            display=self._normalize_display(manifest.get("display", "auto")),
        )

    # ------------------------------------------------------------------
    # 3D 模型
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_display(raw) -> str:
        """display 字段容错：只认 2d/3d/auto，别的值一律回 auto。

        为什么：manifest 是用户手改的，写成 "3D"、"three" 都很常见，
        直接抛异常会让角色加载失败——这不是用户想看到的。
        """
        v = str(raw).strip().lower()
        return v if v in ("2d", "3d", "auto") else "auto"

    @staticmethod
    def _resolve_model3d(char_dir: Path, manifest: dict) -> Path | None:
        """manifest.model3d → 实际 .glb/.gltf 路径（相对/绝对/默认名三种写法）。"""
        raw = manifest.get("model3d")
        candidates: list[Path] = []
        if isinstance(raw, str) and raw.strip():
            p = Path(raw.strip())
            candidates.append(p if p.is_absolute() else char_dir / p)
        for name in ("model.glb", "model.gltf"):
            candidates.append(char_dir / name)
        for c in candidates:
            if c.is_file() and c.suffix.lower() in (".glb", ".gltf"):
                return c
        return None

    # ------------------------------------------------------------------
    # 自定义立绘
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_avatar(char_dir: Path, manifest: dict) -> Path | None:
        """manifest 里的 avatar 字段 → 实际 PNG 路径。

        三种写法都认：
          "avatar": "avatar.png"        相对角色目录
          "avatar": "D:/x/y.png"        绝对路径
          不写 avatar                   自动找角色目录下的 avatar.png
        找不到就返回 None（渲染器回退画矢量形状），导入时不会因为没图而失败。
        """
        raw = manifest.get("avatar")
        candidates: list[Path] = []
        if isinstance(raw, str) and raw.strip():
            p = Path(raw.strip())
            candidates.append(p if p.is_absolute() else char_dir / p)
        # 约定俗成的默认文件名
        candidates.append(char_dir / "avatar.png")
        for c in candidates:
            if c.is_file():
                return c
        return None

    # ------------------------------------------------------------------
    # 校验任意目录（设置面板"导入角色文件夹"用）
    # ------------------------------------------------------------------
    def validate_dir(self, folder: Path) -> tuple[bool, str]:
        """校验一个文件夹是不是合法角色包，返回 (是否合法, 人话说明)。

        为什么要单独一个方法而不是直接 load_character：
          load_character 只认 self.dir 下的子目录，而用户导入的角色在任意位置。
          这里复用同一套字段校验规则，保证"导入前检查"和"真正加载"结论一致。
        """
        folder = Path(folder)
        if not folder.is_dir():
            return False, f"「{folder}」不是一个文件夹"

        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            return False, "缺少 manifest.json（角色的身份证，写着名字、外形、颜色）"

        try:
            manifest = self._load_manifest(manifest_path)
        except Exception as exc:  # noqa: BLE001 —— 格式错误要翻译成人话
            return False, f"manifest.json 有问题：{exc}"

        if not (folder / "system_prompt.md").exists():
            return False, "缺少 system_prompt.md（角色的性格说明书）"
        try:
            self._load_system_prompt(folder / "system_prompt.md")
        except Exception as exc:  # noqa: BLE001
            return False, f"system_prompt.md 有问题：{exc}"

        if not (folder / "lines.json").exists():
            return False, "缺少 lines.json（角色会说的话，比如打招呼、被摸头时的反应）"
        try:
            lines = self._load_lines(folder / "lines.json")
        except Exception as exc:  # noqa: BLE001
            return False, f"lines.json 有问题：{exc}"
        if not lines:
            return False, "lines.json 里一句台词都没有，她会变成哑巴"

        # id 冲突检查：同一个 id 会导致数据目录互相覆盖
        new_id = manifest["id"]
        if (self.dir / new_id).exists():
            return False, (f"已经有一个角色用了 id「{new_id}」。"
                           f"请把 manifest.json 里的 id 改成独一无二的（比如 my-cat）")

        return True, f"校验通过：{manifest['name']}（id={new_id}）"

    def _load_manifest(self, path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(f"缺少 manifest.json：{path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("manifest.json 顶层必须是 JSON 对象")
        for field_name in self.REQUIRED_MANIFEST_FIELDS:
            if field_name not in raw:
                raise ValueError(f"manifest.json 缺少必填字段：{field_name}")
        # mood_styles 校验：如果有，必须是 dict
        ms = raw.get("mood_styles", {})
        if ms and not isinstance(ms, dict):
            raise ValueError("manifest.json 的 mood_styles 必须是对象")
        return raw

    def _load_system_prompt(self, path: Path) -> str:
        if not path.exists():
            raise FileNotFoundError(f"缺少 system_prompt.md：{path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("system_prompt.md 内容为空")
        return text

    def _load_lines(self, path: Path) -> dict[str, list[str]]:
        if not path.exists():
            raise FileNotFoundError(f"缺少 lines.json：{path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("lines.json 顶层必须是 JSON 对象")
        # 每个值必须是字符串列表
        lines: dict[str, list[str]] = {}
        for key, val in raw.items():
            if key.startswith("_"):
                continue
            if isinstance(val, list) and all(isinstance(s, str) for s in val):
                lines[key] = val
            else:
                print(f"[Character] 台词组 '{key}' 格式不对（应为字符串列表），忽略")
        return lines

    # ------------------------------------------------------------------
    # 旧版兼容：从 data/character_core.json 加载
    # ------------------------------------------------------------------
    def load_legacy_character(self, core_path: Path) -> CharacterProfile | None:
        """角色包目录为空时，从旧版 character_core.json 迁移加载。"""
        if not core_path.exists():
            return None
        try:
            raw = json.loads(core_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[Character] 旧版 character_core.json 读取失败：{e}")
            return None

        name = raw.get("name", "小灵")
        prompt = raw.get("system_prompt", "你是桌面上的小精灵。")
        lines_keys = ("greeting_lines", "pat_lines", "feed_lines", "angry_lines",
                      "nag_lines", "idle_care_lines", "cpu_panic_lines",
                      "freeze_complain_lines", "cover_lines")
        lines = {k: raw[k] for k in lines_keys if k in raw and isinstance(raw[k], list)}

        return CharacterProfile(
            id="xiaoling",
            name=name,
            version="legacy",
            description="（旧版角色文件迁移）",
            renderer="vector",
            shape="cat",
            system_prompt=prompt,
            lines=lines,
            path=core_path.parent,
        )


# ----------------------------------------------------------------------
# 使用示例：python -m characters.character_loader
# ----------------------------------------------------------------------
if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    loader = CharacterLoader(root / "characters")
    chars = loader.list_characters()
    if chars:
        for c in chars:
            print(f"角色：{c.name} (id={c.id}, v{c.version}, shape={c.shape})")
            print(f"  提示词：{c.system_prompt[:40]}...")
            print(f"  台词组：{list(c.lines.keys())}")
            print(f"  情绪样式：{list(c.mood_styles.keys())}")
    else:
        print("未找到角色包，尝试旧版迁移...")
        legacy = loader.load_legacy_character(root / "data" / "character_core.json")
        if legacy:
            print(f"旧版角色：{legacy.name}")
