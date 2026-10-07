# -*- coding: utf-8 -*-
"""
PSE 的三重记忆实现
==================

1. Working memory（工作记忆）由 SoulEngine 维护：当前会话的滑动窗口。
2. Episodic memory（情节记忆）由本文件维护：ChromaDB + JSON 备胎。
3. Semantic memory（语义记忆）不放进这里，而是由 profile.py 和
   character_core.json 组成 System Prompt 的固定前缀。

为什么自己做一个小型向量编码器：
  ChromaDB 默认可能下载几十 MB 的 embedding 模型。对电脑小白来说，
  第一次运行时卡住、联网失败、模型缓存放哪儿都很难解释。
  这里用确定性的字符/词片段哈希向量，不下载任何东西；检索质量不是
  大模型级别，但足够让“上次提到 Python”找到相关回忆，而且完全可替换。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+")


@dataclass
class MemoryRecord:
    timestamp: str
    summary: str
    emotion_tag: str
    interaction_type: str

    def as_metadata(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "emotion_tag": self.emotion_tag,
            "interaction_type": self.interaction_type,
        }


class LocalHashEmbedding:
    """不联网的确定性向量编码器。相同文字永远得到相同向量。"""

    def __init__(self, dimensions: int = 128):
        self.dimensions = dimensions

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self.encode(text) for text in texts]

    def encode(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        tokens = _TOKEN_RE.findall(text.lower())
        # 中文按单字、英文按词；再加相邻二元片段，兼顾中文和英文短语。
        pieces = tokens + [f"{a}{b}" for a, b in zip(tokens, tokens[1:])]
        if not pieces:
            pieces = [text or "空"]
        for piece in pieces:
            digest = hashlib.blake2b(piece.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            values[index] += sign
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [round(v / norm, 8) for v in values]


class MemoryStore:
    """情节记忆存取器，ChromaDB 失败时自动降级到 JSON。"""

    def __init__(self, db_path: Path, fallback_path: Path | None = None,
                 use_chroma: bool = True):
        self.db_path = Path(db_path)
        self.fallback_path = Path(fallback_path or self.db_path.parent / "memory_fallback.json")
        self._lock = threading.RLock()
        self.embedding = LocalHashEmbedding()
        self.backend = "json"
        self._collection: Any = None
        self._records: list[MemoryRecord] = self._load_fallback()

        if use_chroma:
            self._try_open_chroma()

    # ------------------------------------------------------------------
    def _try_open_chroma(self) -> None:
        try:
            import chromadb
            self.db_path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.db_path))
            self._collection = client.get_or_create_collection(
                name="episodic_memory",
                configuration={"hnsw": {"space": "cosine"}},
            )
            self.backend = "chroma"
        except Exception as exc:  # noqa: BLE001 - 记忆库坏了不能拖垮桌宠
            self.backend = "json"
            self._collection = None
            print(f"[Memory] ChromaDB 不可用，已切换 JSON 备胎：{exc}")

    def _load_fallback(self) -> list[MemoryRecord]:
        if not self.fallback_path.exists():
            return []
        try:
            raw = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            result = []
            for item in raw if isinstance(raw, list) else []:
                if all(key in item for key in ("timestamp", "summary", "emotion_tag", "interaction_type")):
                    result.append(MemoryRecord(**{key: item[key] for key in (
                        "timestamp", "summary", "emotion_tag", "interaction_type")}))
            return result
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_fallback(self) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_path.write_text(
            json.dumps([asdict(record) for record in self._records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    def add(self, summary: str, emotion_tag: str, interaction_type: str = "chat",
            timestamp: str | None = None) -> MemoryRecord:
        """写入一条带情绪标签的情节记忆。"""
        record = MemoryRecord(
            timestamp=timestamp or time.strftime("%Y-%m-%dT%H:%M:%S"),
            summary=summary.strip()[:1000],
            emotion_tag=emotion_tag or "未知",
            interaction_type=interaction_type,
        )
        if not record.summary:
            return record

        with self._lock:
            if self.backend == "chroma" and self._collection is not None:
                try:
                    # timestamp + hash 保证重启后 id 仍稳定且不冲突。
                    memory_id = hashlib.sha1(
                        f"{record.timestamp}|{record.summary}".encode("utf-8")
                    ).hexdigest()
                    self._collection.upsert(
                        ids=[memory_id],
                        documents=[record.summary],
                        embeddings=[self.embedding.encode(record.summary)],
                        metadatas=[record.as_metadata()],
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[Memory] ChromaDB 写入失败，转 JSON：{exc}")
                    self.backend = "json"
                    self._collection = None
            # JSON 始终写一份：它既是备份，也方便新手直接打开查看。
            self._records.append(record)
            self._records = self._records[-1000:]
            try:
                self._save_fallback()
            except OSError as exc:
                print(f"[Memory] JSON 备份写入失败：{exc}")
        return record

    def recall(self, query: str, emotion_tag: str, top_k: int = 3) -> list[str]:
        """检索 Top-K 摘要，并强制按 emotion_tag 精确过滤。"""
        if not query.strip() or not emotion_tag:
            return []
        with self._lock:
            if self.backend == "chroma" and self._collection is not None:
                try:
                    count = self._collection.count()
                    if count == 0:
                        return []
                    result = self._collection.query(
                        query_embeddings=[self.embedding.encode(query)],
                        n_results=min(top_k, count),
                        where={"emotion_tag": emotion_tag},
                        include=["documents", "metadatas", "distances"],
                    )
                    docs = result.get("documents") or [[]]
                    return [doc for doc in docs[0] if doc][:top_k]
                except Exception as exc:  # noqa: BLE001
                    print(f"[Memory] ChromaDB 检索失败，转 JSON：{exc}")
                    self.backend = "json"
                    self._collection = None

            # JSON 备胎：先严格过滤情绪，再按共同片段数量排序。
            candidates = [r for r in self._records if r.emotion_tag == emotion_tag]
            query_tokens = set(_TOKEN_RE.findall(query.lower()))
            # 时间戳是 ISO 字符串，同格式下字典序就是时间序。
            # 原写法 `1 if record.timestamp else 0` 里 timestamp 恒为非空字符串，
            # 于是每条都 +1——新近度根本没参与排序，和注释说的"较新的轻微加权"不符。
            by_time = sorted(candidates, key=lambda r: r.timestamp or "")
            span = max(1, len(by_time) - 1)
            recency_rank = {id(r): i for i, r in enumerate(by_time)}
            scored: list[tuple[float, MemoryRecord]] = []
            for record in candidates:
                tokens = set(_TOKEN_RE.findall(record.summary.lower()))
                overlap = len(query_tokens & tokens)
                # 最多 +1 分（相关度按 *10 计）：只用来打破相关度相同的平局，
                # 不会让"新的但不相干"压过"旧的但很相关"。
                bonus = recency_rank.get(id(record), 0) / span
                scored.append((overlap * 10 + bonus, record))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [record.summary for _, record in scored[:top_k]]

    def count(self) -> int:
        with self._lock:
            if self.backend == "chroma" and self._collection is not None:
                try:
                    return int(self._collection.count())
                except Exception:  # pragma: no cover - 失败时读 JSON
                    pass
            return len(self._records)

    def clear(self) -> int:
        """清空全部情节记忆，返回清掉的条数。隐私页"清空记忆"按钮调用。

        为什么两边都要清：ChromaDB 是检索主库，JSON 是备份（也给新手直接打开看）。
        只清一边的话，另一边下次照样能被检索到，用户会觉得"明明删了怎么还记得"。
        失败不抛异常：隐私操作半途报错比不做更糟，这里尽力清干净并如实返回条数。
        """
        cleared = 0
        with self._lock:
            if self.backend == "chroma" and self._collection is not None:
                try:
                    all_ids = self._collection.get()["ids"]
                    if all_ids:
                        self._collection.delete(ids=list(all_ids))
                        cleared = len(all_ids)
                except Exception as exc:  # noqa: BLE001
                    print(f"[Memory] ChromaDB 清空失败：{exc}")
            json_count = len(self._records)
            self._records = []
            try:
                self._save_fallback()
            except OSError as exc:  # noqa: BLE001
                print(f"[Memory] JSON 备胎清空失败：{exc}")
        return max(cleared, json_count)

    async def remember_conversation(self, user_text: str, assistant_text: str,
                                    emotion_tag: str) -> MemoryRecord:
        """异步写入摘要；to_thread 防止磁盘/Chroma I/O 卡住 Qt。"""
        summary = self.make_summary(user_text, assistant_text)
        import asyncio
        return await asyncio.to_thread(self.add, summary, emotion_tag, "chat")

    @staticmethod
    def make_summary(user_text: str, assistant_text: str) -> str:
        """轻量摘要器：不用再调用一次 API，稳定、免费、不会递归套娃。"""
        user = " ".join(user_text.strip().split())[:220]
        assistant = " ".join(assistant_text.strip().split())[:320]
        return f"主人说：{user}\n小灵回应：{assistant}"[:800]


if __name__ == "__main__":
    import tempfile
    temp = Path(tempfile.mkdtemp())
    store = MemoryStore(temp / "db", temp / "fallback.json", use_chroma=False)
    store.add("主人喜欢在晚上写 Python 程序。", "开心")
    store.add("主人今天有点累，不想加班。", "低落")
    print("开心回忆：", store.recall("Python", "开心"))
    print("低落回忆：", store.recall("Python", "低落"))
