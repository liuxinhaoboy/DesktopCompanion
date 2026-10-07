# -*- coding: utf-8 -*-
"""
files —— 工作目录内的文件操作（P5）
==================================
这里的每个方法都只接收 PolicyGate 已二次校验过的真实路径（Path），
自身不再判断"越不越界"——但也因此**绝不能**被 executor 以外的地方直接调用
去操作任意路径。

能力边界（红线）：
  - 删除一律 send2trash 进回收站，永不 os.remove / shutil.rmtree；
  - 移动/重命名/复制返回 rollback 信息，供撤销；
  - 预览只读文本，遇到二进制/超大文件明确拒绝，不硬读。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .schema import ActionResult, ActionKind

PREVIEW_MAX_BYTES = 200 * 1024     # 预览文件最大 200KB
PREVIEW_MAX_CHARS = 4000          # 回给模型的预览最多 4000 字
LIST_MAX_ENTRIES = 500            # 单个目录最多列出 500 项


class FileHands:
    def __init__(self, gate):
        self.gate = gate

    def _short(self, p: Path) -> str:
        return self.gate.rel(p)

    # ------------------------------------------------------------------
    def list_dir(self, real: Path) -> ActionResult:
        try:
            if not real.is_dir():
                return ActionResult.fail(ActionKind.FILE_LIST,
                                         f"「{self._short(real)}」不是文件夹。")
            items = sorted(real.iterdir(), key=lambda x: (not x.is_dir(),
                                                          x.name.lower()))
            truncated = len(items) > LIST_MAX_ENTRIES
            entries = []
            for p in items[:LIST_MAX_ENTRIES]:
                try:
                    kind = "文件夹" if p.is_dir() else "文件"
                    size = "" if p.is_dir() else f"（{_human_size(p.stat().st_size)}）"
                    entries.append(f"[{kind}] {p.name}{size}")
                except OSError:
                    continue
            body = "\n".join(entries) or "（空文件夹）"
            if truncated:
                body += f"\n…条目超过 {LIST_MAX_ENTRIES}，只显示前 {LIST_MAX_ENTRIES} 项"
            return ActionResult.success(
                ActionKind.FILE_LIST,
                f"「{self._short(real)}」里有 {len(entries)} 项：\n{body}",
                data={"entries": entries, "count": len(entries)})
        except OSError as e:
            return ActionResult.fail(ActionKind.FILE_LIST, f"读取文件夹失败：{e}")

    # ------------------------------------------------------------------
    def preview(self, real: Path) -> ActionResult:
        try:
            if not real.is_file():
                return ActionResult.fail(ActionKind.FILE_PREVIEW, "只能预览文件。")
            size = real.stat().st_size
            if size > PREVIEW_MAX_BYTES:
                return ActionResult.fail(
                    ActionKind.FILE_PREVIEW,
                    f"文件有 {_human_size(size)}，超过预览上限，为避免卡顿不读取。")
            raw = real.read_bytes()[:PREVIEW_MAX_BYTES]
            if b"\x00" in raw[:4096]:           # 含空字节基本可判定是二进制
                return ActionResult.fail(ActionKind.FILE_PREVIEW,
                                         "这是二进制文件（图片/程序/压缩包等），没法当文字预览。")
            text = raw.decode("utf-8", errors="replace")[:PREVIEW_MAX_CHARS]
            return ActionResult.success(
                ActionKind.FILE_PREVIEW,
                f"「{self._short(real)}」内容：\n{text}",
                data={"text": text})
        except OSError as e:
            return ActionResult.fail(ActionKind.FILE_PREVIEW, f"读取文件失败：{e}")

    # ------------------------------------------------------------------
    def mkdir(self, real: Path) -> ActionResult:
        try:
            if real.exists():
                return ActionResult.fail(ActionKind.FILE_MKDIR,
                                         f"「{self._short(real)}」已经存在了。")
            real.mkdir(parents=True, exist_ok=False)
            return ActionResult.success(ActionKind.FILE_MKDIR,
                                        f"已新建文件夹「{self._short(real)}」。",
                                        reversible=True,
                                        rollback={"op": "mkdir", "path": str(real)})
        except OSError as e:
            return ActionResult.fail(ActionKind.FILE_MKDIR, f"新建文件夹失败：{e}")

    # ------------------------------------------------------------------
    def copy(self, real: Path, dest_real: Path) -> ActionResult:
        try:
            if dest_real.exists():
                return ActionResult.fail(ActionKind.FILE_COPY,
                                         f"目标「{self._short(dest_real)}」已存在，怕覆盖，先停下。")
            dest_real.parent.mkdir(parents=True, exist_ok=True)
            if real.is_dir():
                shutil.copytree(real, dest_real)
            else:
                shutil.copy2(real, dest_real)
            return ActionResult.success(
                ActionKind.FILE_COPY,
                f"已复制「{self._short(real)}」→「{self._short(dest_real)}」。",
                reversible=True,
                rollback={"op": "copy", "created": str(dest_real)})
        except OSError as e:
            return ActionResult.fail(ActionKind.FILE_COPY, f"复制失败：{e}")

    # ------------------------------------------------------------------
    def move(self, real: Path, dest_real: Path) -> ActionResult:
        try:
            if dest_real.exists():
                return ActionResult.fail(ActionKind.FILE_MOVE,
                                         f"目标「{self._short(dest_real)}」已存在，怕覆盖，先停下。")
            dest_real.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(real), str(dest_real))
            return ActionResult.success(
                ActionKind.FILE_MOVE,
                f"已移动「{self._short(real)}」→「{self._short(dest_real)}」。",
                reversible=True,
                rollback={"op": "move", "from": str(dest_real), "to": str(real)})
        except OSError as e:
            return ActionResult.fail(ActionKind.FILE_MOVE, f"移动失败：{e}")

    # ------------------------------------------------------------------
    def rename(self, real: Path, new_name: str) -> ActionResult:
        try:
            new_path = real.parent / new_name
            if new_path.exists():
                return ActionResult.fail(ActionKind.FILE_RENAME,
                                         f"「{new_name}」已经存在，怕覆盖，先停下。")
            old_name = real.name
            real.rename(new_path)
            return ActionResult.success(
                ActionKind.FILE_RENAME,
                f"已把「{old_name}」改名为「{new_name}」。",
                reversible=True,
                rollback={"op": "rename", "path": str(new_path), "old_name": old_name})
        except OSError as e:
            return ActionResult.fail(ActionKind.FILE_RENAME, f"重命名失败：{e}")

    # ------------------------------------------------------------------
    def delete(self, real: Path) -> ActionResult:
        """只进回收站，永不永久删除。"""
        try:
            from send2trash import send2trash
            short = self._short(real)
            send2trash(str(real))
            # 回收站是系统级撤销，这里标记可逆，提示用户去回收站还原
            return ActionResult.success(
                ActionKind.FILE_DELETE,
                f"已把「{short}」放进回收站（没永久删除，需要可去回收站还原）。",
                reversible=True, rollback={"op": "trash"})
        except Exception as e:  # noqa: BLE001 —— send2trash 失败原因多样，统一成人话
            return ActionResult.fail(ActionKind.FILE_DELETE,
                                     f"放进回收站失败：{type(e).__name__}（没有做任何删除）")

    # ------------------------------------------------------------------
    def undo(self, rollback: dict) -> ActionResult:
        """按 rollback 信息尽力撤销一次操作（撤销动作本身也只在工作目录内）。"""
        op = rollback.get("op")
        try:
            if op == "move":
                src, dst = Path(rollback["from"]), Path(rollback["to"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return ActionResult.success("undo", "已移回原位。")
            if op == "rename":
                p = Path(rollback["path"])
                p.rename(p.parent / rollback["old_name"])
                return ActionResult.success("undo", "已改回原名。")
            if op == "mkdir":
                p = Path(rollback["path"])
                if p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    return ActionResult.success("undo", "已删除新建的空文件夹。")
                return ActionResult.fail("undo", "文件夹已非空，不敢自动删除。")
            if op == "copy":
                # 复制出来的副本撤销时也只进回收站
                from send2trash import send2trash
                p = Path(rollback["created"])
                if p.exists():
                    send2trash(str(p))
                return ActionResult.success("undo", "已把复制出的副本放进回收站。")
            if op == "trash":
                return ActionResult.fail("undo", "删除走的是系统回收站，请手动从回收站还原。")
            return ActionResult.fail("undo", "没有可用于撤销的信息。")
        except OSError as e:
            return ActionResult.fail("undo", f"撤销失败：{e}")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
