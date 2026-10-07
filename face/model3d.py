# -*- coding: utf-8 -*-
"""
model3d —— 3D 桌宠：GLB/glTF 加载 + 软件渲染
============================================
让用户上传一个 .glb / .gltf 模型（比如自己捏的 Q 版小人），桌宠就能以
3D 形态出现在桌面上：待机时缓慢自转、随呼吸轻微俯仰摆动。

为什么自己写解析器和渲染器，而不用 OpenGL / trimesh / pyrender：
  1. 项目红线——不引入需要编译安装的包。pyrender/OpenGL 直接赌用户的
     显卡驱动，桌宠这种小窗口完全不值得冒"别人电脑上一片黑"的风险。
  2. numpy 和 pillow 本来就在 requirements.txt 里，够了。
  3. 桌宠只有 160~200px，用 painter's algorithm（三角形按深度排序、
     从远到近逐面画）就足够好看；面片用平均色+同色描边消除缝隙。

支持的 glTF 2.0 子集（覆盖静态展示型模型的绝大多数）：
  - .glb 二进制容器；.gltf 文本 + 外部 .bin / data:URI
  - POSITION / COLOR_0 / TEXCOORD_0；节点树变换（matrix 或 TRS）
  - 材质 baseColorFactor；baseColorTexture 在加载时烘焙成顶点色
    （小窗口下贴图细节不可辨，省掉整条纹理管线）
  - doubleSided 双面材质（不因背面剔除出现"破洞"）
刻意不支持（安全忽略、绝不崩溃）：蒙皮动画（模型按绑定姿势摆着转）、
  稀疏访问器、KTX 压缩纹理、PBR 复杂光照（用"环境光+半球漫反射"近似）。
解析失败 / 文件损坏 / 超面数 → load 返回 None，上层自动回退 2D 矢量绘制。
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

# ---------------------------------------------------------------------------
# glTF 枚举常量（规范里的裸数字，不命名根本没法读）
# ---------------------------------------------------------------------------
_GLB_MAGIC = 0x46546C67           # "glTF"
_CHUNK_JSON = 0x4E4F534A          # "JSON"
_CHUNK_BIN = 0x004E4942           # "BIN\0"

_COMPONENT_TYPES = {
    5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2",
    5125: "u4", 5126: "f4",
}
_TYPE_CHANNELS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

# ---- 渲染参数（经验值：配合"归一化后半径≈1"，模型刚好占满画面） ----
_CAM_DIST = 3.1        # 相机距离（看向 -Z 方向）
_FOV_SCALE = 1.35      # 投影缩放，≈控制视场角
_AMBIENT = 0.55        # 环境光：背光面也不至于全黑
_DIFFUSE = 0.62        # 漫反射强度
_LIGHT_DIR = np.array([0.45, 0.62, 0.65], dtype=np.float32)  # 指向光源（左上前方）
_LIGHT_DIR /= float(np.linalg.norm(_LIGHT_DIR))

_MAX_TRIS_HARD = 60_000          # 面数硬上限：超过多半是照片级扫描模型，拒绝
_CACHE_THRESHOLD = 3_000         # 超过这个面数，渲染结果按视角分档缓存
_CACHE_STEP_RAD = 0.30           # 缓存重画的角度间隔（约 17°）
_MAX_TEX = 256                   # 纹理烘焙前降采样上限（内存/耗时兜底）


class Model3D:
    """加载完成的 3D 模型：世界坐标三角形 + 每面颜色 + 双面标记。"""

    def __init__(self, tris: np.ndarray, face_colors: np.ndarray,
                 double_sided: np.ndarray, source):
        self.tris = tris                    # (N,3,3) float32，已归一化 Y 向上
        self.face_colors = face_colors      # (N,3) 0..1 RGB（已含材质/纹理色）
        self.double_sided = double_sided    # (N,) bool
        self.source = Path(source)
        self.tri_count = len(tris)
        # 面法线只取决于模型本身，加载时算一次存下来（渲染是逐帧调的，别重算）
        fn = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        ln = np.sqrt((fn ** 2).sum(1))
        ln[ln < 1e-12] = 1e-12
        self._face_normal = (fn / ln[:, None]).astype(np.float32)
        # 重模型缓存（_cache_key 命中的视角直接 blit 上次的图）
        self._cache_key: tuple | None = None
        self._cache_pix: QImage | None = None

    # ==================================================================
    # 加载入口
    # ==================================================================
    @classmethod
    def load(cls, path) -> "Model3D | None":
        """解析 .glb/.gltf。任何异常都吞掉返回 None，上层回退 2D。

        为什么绝不抛异常：模型来自用户各处收集，格式问题防不胜防；
        上传了坏模型不能毁掉桌宠本身——最坏结果就是"还是原来的样子"。
        """
        try:
            path = Path(path)
            if not path.is_file():
                return None
            if path.suffix.lower() == ".glb":
                json_text, bin_chunk = _read_glb(path)
            elif path.suffix.lower() == ".gltf":
                json_text, bin_chunk = _read_gltf(path)
            else:
                return None
            if not json_text:
                return None
            return _from_gltf(json.loads(json_text), bin_chunk or b"", path)
        except Exception as e:  # noqa: BLE001
            print(f"[Model3D] 解析失败（将回退2D）：{path} — {type(e).__name__}: {e}")
            return None

    # ==================================================================
    # 渲染
    # ==================================================================
    def render(self, p: QPainter, cx: float, cy: float, radius: float,
               yaw: float, pitch: float = 0.1, tint: QColor | None = None,
               dim: float = 1.0) -> None:
        """把模型画到 QPainter 上。

        cx/cy/radius: 画面中心 + 外接半径（模型约占 2*radius）
        yaw/pitch:    绕 Y 自转角 + 俯仰角（待机动画 = yaw 缓涨 + pitch 轻摆）
        tint:         情绪叠加色（开心暖光/生气泛红），None=正常
        dim:          整体亮度系数（低落/饥饿/假死 <1）
        """
        if self.tri_count > _CACHE_THRESHOLD:
            key = (round(yaw / _CACHE_STEP_RAD), round(pitch, 2),
                   int(radius), round(dim, 2), tint and tint.name())
            if self._cache_key != key or self._cache_pix is None:
                self._cache_pix = self._render_image(radius, yaw, pitch,
                                                     tint, dim)
                self._cache_key = key
            _blit(p, self._cache_pix, cx, cy)
            return
        _blit(p, self._render_image(radius, yaw, pitch, tint, dim), cx, cy)

    def _render_image(self, radius: float, yaw: float, pitch: float,
                      tint: QColor | None, dim: float) -> QImage:
        side = int(radius * 2 + 16)
        side = max(48, min(side, 900))
        img = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 世界 → 相机：先绕 Y 自转，再按俯仰角倾斜（相机看向 -Z、原点在中心）
        cy_, cz_ = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        ry = np.array([[cy_, 0, cz_], [0, 1, 0], [-cz_, 0, cy_]], np.float32)
        rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], np.float32)
        view = rx @ ry

        v = self.tris.reshape(-1, 3) @ view.T          # (N*3,3) 相机空间
        faces = v.reshape(-1, 3, 3)
        nrm = self._face_normal @ view.T               # 法线同旋转

        # 透视投影
        zs = np.clip(_CAM_DIST - faces[:, :, 2], 0.15, None)
        f = side * 0.5 * _FOV_SCALE / zs
        sx = side * 0.5 + faces[:, :, 0] * f
        sy = side * 0.5 - faces[:, :, 1] * f           # 屏幕 Y 向下

        # 可见面：法线朝相机（n·(相机-面心)>0 近似为 n.z>0）或双面材质
        facing = nrm[:, 2] > 0
        keep = facing | self.double_sided

        # 面片平均深度：画家算法，远的先画
        depth = zs.mean(1)

        # 光照：环境光 + 半球漫反射（面法线·光源方向），双面背面用反法线
        nl = np.abs(nrm) @ _LIGHT_DIR
        lit = (nl * _DIFFUSE + _AMBIENT) * dim
        colors = (self.face_colors * lit[:, None]).clip(0, 1)
        if tint is not None:
            a = max(tint.alpha(), 40) / 255.0
            t = np.array([tint.red(), tint.green(), tint.blue()], np.float32) / 255.0
            colors = (colors * (1 - a) + t * a).clip(0, 1)

        # 屏幕外的小三角直接丢（桌宠只占窗口一块，省 30~60% 绘制量）
        cxs, cys = sx.mean(1), sy.mean(1)
        m = side * 0.55
        keep &= (cxs > -m) & (cxs < side + m) & (cys > -m) & (cys < side + m)

        idxs = np.nonzero(keep)[0]
        for i in idxs[np.argsort(depth[idxs])]:
            col = colors[i]
            qcol = QColor(int(col[0] * 255), int(col[1] * 255), int(col[2] * 255))
            pts = QPolygonF([QPointF(float(sx[i, k]), float(sy[i, k]))
                             for k in range(3)])
            p.setPen(QPen(qcol))     # 同色描边：消除面片缝隙，小图看起来更像实体
            p.setBrush(qcol)
            p.drawPolygon(pts)
        p.end()
        return img


# ---------------------------------------------------------------------------
# 模块级辅助：容器解析 / glTF → 三角形
# ---------------------------------------------------------------------------
def _blit(p: QPainter, img: QImage, cx: float, cy: float) -> None:
    side = img.width()
    p.drawImage(QRectF(cx - side / 2, cy - side / 2, side, side), img)


def _read_glb(path: Path):
    """glb = 12字节头 + [len+type+data] chunk 序列。取 JSON 和第一个 BIN。"""
    raw = path.read_bytes()
    if len(raw) < 12:
        return None, None
    magic, version, total = struct.unpack_from("<III", raw, 0)
    if magic != _GLB_MAGIC or version != 2 or total > len(raw):
        return None, None
    json_text, bin_chunk, off = None, None, 12
    while off + 8 <= len(raw):
        clen, ctype = struct.unpack_from("<II", raw, off)
        data = raw[off + 8: off + 8 + clen]
        if ctype == _CHUNK_JSON and json_text is None:
            json_text = data.decode("utf-8", errors="replace")
        elif ctype == _CHUNK_BIN and bin_chunk is None:
            bin_chunk = data
        off += 8 + clen
    return json_text, bin_chunk


def _read_gltf(path: Path):
    """文本 gltf：外部 .bin 或 data:URI 两种 buffer 都支持。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        g = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    uri = (g.get("buffers") or [{}])[0].get("uri", "")
    if uri.startswith("data:"):
        import base64
        return text, base64.b64decode(uri.split("base64,", 1)[1])
    if uri:
        p = path.parent / uri
        if p.is_file():
            return text, p.read_bytes()
    return text, None


def _from_gltf(g: dict, bin_chunk: bytes, path: Path) -> "Model3D | None":
    nodes = g.get("nodes", [])
    if not nodes or not g.get("meshes"):
        return None

    def read_accessor(i):
        """accessors[i] → bufferView → bin_chunk，返回 (count, channels) float32。"""
        acc = g["accessors"][i]
        if acc.get("sparse") or "bufferView" not in acc:
            return None                      # 稀疏访问器不支持（见模块头注释）
        bv = g["bufferViews"][acc["bufferView"]]
        code = _COMPONENT_TYPES.get(acc["componentType"])
        chan = _TYPE_CHANNELS.get(acc["type"])
        if not code or not chan:
            return None
        item = np.dtype(code).itemsize * chan
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = bv.get("byteStride") or item
        count = acc["count"]
        need = start + (stride * count)
        if need > len(bin_chunk):            # 越界 = 文件被截断/引用外部缺失 buffer
            return None
        if stride == item:
            arr = np.frombuffer(bin_chunk, dtype=code,
                                count=count * chan, offset=start)
            return arr.reshape(count, chan).astype(np.float32)
        step = stride // np.dtype(code).itemsize
        arr = np.empty((count, chan), dtype=np.float32)
        for r in range(count):
            o = start + r * stride // np.dtype(code).itemsize
            arr[r] = np.frombuffer(bin_chunk, dtype=code, count=chan,
                                   offset=o * np.dtype(code).itemsize)
        return arr

    def read_indices(prim, vert_count):
        if "indices" not in prim:
            return np.arange(vert_count, dtype=np.int64)   # 非索引三角形列表
        ia = g["accessors"][prim["indices"]]
        if ia.get("sparse") or "bufferView" not in ia:
            return None
        bv = g["bufferViews"][ia["bufferView"]]
        code = _COMPONENT_TYPES.get(ia["componentType"])
        if not code:
            return None
        start = bv.get("byteOffset", 0) + ia.get("byteOffset", 0)
        end = start + ia["count"] * np.dtype(code).itemsize
        if end > len(bin_chunk):
            return None
        return np.frombuffer(bin_chunk, dtype=code,
                             count=ia["count"], offset=start).astype(np.int64)

    def node_matrix(n):
        m = np.eye(4, dtype=np.float32)
        if n.get("matrix"):
            return np.array(n["matrix"], np.float32).reshape(4, 4).T  # 列主序
        if n.get("rotation"):
            x, y, z, w = n["rotation"]
            m[:3, :3] = [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
        if n.get("scale"):
            m[:3, :3] *= np.array(n["scale"], np.float32)[:, None]
        if n.get("translation"):
            m[:3, 3] = np.array(n["translation"], np.float32)
        return m

    # ---- 父指针 + 世界矩阵（自根往下乘一次，供全部节点复用） ----
    parents = [None] * len(nodes)
    for ni, n in enumerate(nodes):
        for c in n.get("children", []):
            if c < len(parents):
                parents[c] = ni
    roots = [i for i in range(len(nodes)) if parents[i] is None]
    world = {}

    def stack_world(ni, acc):
        world[ni] = acc
        for c in nodes[ni].get("children", []):
            if c in world or c >= len(nodes) or parents[c] != ni:
                continue
            stack_world(c, acc @ node_matrix(nodes[c]))
    for r in roots:
        stack_world(r, node_matrix(nodes[r]))

    # ---- 纹理烘焙：贴图 → 顶点采样用的像素图（降采样，只留 RGB） ----
    palette_cache: dict[int, "Image.Image"] = {}

    def palette_of(tex_index):
        if tex_index in palette_cache:
            return palette_cache[tex_index]
        try:
            ts = g["textures"][tex_index]
            src = g["images"][ts["source"]]
            pil = None
            if "bufferView" in src:
                bv = g["bufferViews"][src["bufferView"]]
                blob = bin_chunk[bv.get("byteOffset", 0):
                                 bv.get("byteOffset", 0) + bv["byteLength"]]
                import io
                pil = Image.open(io.BytesIO(blob))
            elif str(src.get("uri", "")).startswith("data:"):
                import base64, io
                pil = Image.open(io.BytesIO(
                    base64.b64decode(src["uri"].split("base64,", 1)[1])))
            elif src.get("uri") and (path.parent / src["uri"]).is_file():
                pil = Image.open(path.parent / src["uri"])
            if pil is None:
                return None
            pil = pil.convert("RGB")
            if max(pil.size) > _MAX_TEX:
                pil = pil.resize((_MAX_TEX, _MAX_TEX), Image.BILINEAR)
        except Exception:  # noqa: BLE001 单张贴图坏，模型其他面照常渲染
            pil = None
        palette_cache[tex_index] = pil
        return pil

    materials = g.get("materials") or [{}]

    def prim_color(prim, idx, vert_count):
        """顶点色 > 纹理烘焙 > baseColorFactor，返回 (M,3) 每顶点色。"""
        mat = materials[prim.get("material")] \
            if prim.get("material") is not None \
            and prim["material"] < len(materials) else {}
        pbr = mat.get("pbrMetallicRoughness", {})
        base = np.array(pbr.get("baseColorFactor", [0.8, 0.8, 0.8, 1.0]),
                        np.float32)[:3]
        attrs = prim.get("attributes", {})

        if "COLOR_0" in attrs:
            c = read_accessor(attrs["COLOR_0"])
            if c is not None and len(c) >= vert_count:
                return np.clip(c[idx][:, :3] * base, 0, 1)

        tex = pbr.get("baseColorTexture")
        if tex is not None and "index" in tex and "TEXCOORD_0" in attrs:
            pil = palette_of(tex["index"])
            uv = read_accessor(attrs["TEXCOORD_0"])
            if pil is not None and uv is not None and len(uv) >= vert_count:
                w, h = pil.size
                px = np.clip((uv[idx][:, 0] % 1.0 * w).astype(np.int32), 0, w - 1)
                py = np.clip(((1 - uv[idx][:, 1]) % 1.0 * h).astype(np.int32),
                             0, h - 1)
                flat = np.asarray(pil).reshape(-1, 3)[py * w + px]
                return np.clip(flat.astype(np.float32) / 255.0 * base, 0, 1)
        return np.tile(np.clip(base, 0, 1), (len(idx), 1))

    tris, cols, ds, total = [], [], [], 0
    for ni, n in enumerate(nodes):
        if "mesh" not in n or ni not in world:
            continue
        W = world[ni]
        for prim in g["meshes"][n["mesh"]].get("primitives", []):
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs or total >= _MAX_TRIS_HARD:
                continue
            pos = read_accessor(attrs["POSITION"])
            if pos is None:
                continue
            idx = read_indices(prim, len(pos))
            if idx is None:
                continue
            t = pos[idx] @ W[:3, :3].T + W[:3, 3]     # 网格坐标 → 世界坐标
            if t.size == 0 or len(t) % 3:
                continue
            t = t.reshape(-1, 3, 3)                   # 顶点串 → 三角形列表
            per_vert = prim_color(prim, idx, len(pos))
            per_tri = per_vert.reshape(-1, 3, 3).mean(axis=1)   # 面平均色
            mi = prim.get("material")
            mat = materials[mi] if mi is not None and mi < len(materials) else {}
            tris.append(t.astype(np.float32))
            cols.append(per_tri.astype(np.float32))
            ds.append(np.full(len(t), bool(mat.get("doubleSided", False))))
            total += len(t)

    if not tris:
        return None
    tris = np.concatenate(tris).astype(np.float32)
    cols = np.clip(np.concatenate(cols), 0, 1).astype(np.float32)
    ds = np.concatenate(ds)
    tris, _span = _normalize(tris)
    model = Model3D(tris, cols, ds, path)
    if model.tri_count > _MAX_TRIS_HARD:
        return None
    return model


def _normalize(tris):
    """平移到原点 + 缩放到"最大边长≈2"（半径≈1），桌宠尺寸由上层 radius 控制。"""
    flat = tris.reshape(-1, 3)
    lo, hi = flat.min(0), flat.max(0)
    center = (lo + hi) / 2
    span = max(float((hi - lo).max()), 1e-6)
    return ((tris - center) * (2.0 / span)).astype(np.float32), span


# ---------------------------------------------------------------------------
# 演示 / 测试用：程序化生成最小 GLB
# ---------------------------------------------------------------------------
def make_test_glb(path) -> Path:
    """生成一个八面体 GLB（顶点+索引+材质色+双面），供测试与效果演示。

    测试不能依赖网络下载模型；这个函数同时是"GLB 容器格式"的可执行文档：
    12 字节头（magic/version/total）+ JSON chunk + BIN chunk，均 4 字节对齐。
    """
    path = Path(path)
    verts = np.array([
        [0, 1, 0], [1, 0, 0], [0, 0, 1], [-1, 0, 0],
        [0, 0, -1], [0, -1, 0]], dtype=np.float32)
    faces = np.array([
        [0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 4, 1],
        [5, 2, 1], [5, 3, 2], [5, 4, 3], [5, 1, 4]], dtype=np.uint16)
    bin_data = verts.tobytes() + faces.tobytes() + b"\x00\x00"
    gltf = {
        "asset": {"version": "2.0", "generator": "deskpet-test"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [0, 0, 0]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0},
                                    "indices": 1, "material": 0}]}],
        "materials": [{"doubleSided": True, "pbrMetallicRoughness": {
            "baseColorFactor": [0.55, 0.8, 0.95, 1.0]}}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(verts),
             "type": "VEC3", "min": verts.min(0).tolist(),
             "max": verts.max(0).tolist()},
            {"bufferView": 1, "componentType": 5123,
             "count": len(faces) * 3, "type": "SCALAR"}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": verts.nbytes},
            {"buffer": 0, "byteOffset": verts.nbytes,
             "byteLength": faces.nbytes}],
        "buffers": [{"byteLength": len(bin_data)}]}
    json_bytes = json.dumps(gltf).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    bin_data = bin_data + b"\x00" * (-len(bin_data) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_data)
    blob = (struct.pack("<III", _GLB_MAGIC, 2, total)
            + struct.pack("<II", len(json_bytes), _CHUNK_JSON) + json_bytes
            + struct.pack("<II", len(bin_data), _CHUNK_BIN) + bin_data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path
