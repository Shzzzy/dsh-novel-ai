"""Chapter Version History — P2-11 章节版本历史

每次章节内容变更时自动保存版本快照。
支持列表、加载、diff、恢复。

存储路径: ~/.novel-ai/novels/{novel_id}/versions/{chapter_id}/v{NNNN}.json
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from brains.gbrain_wrapper import brain_path_for

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

MAX_VERSIONS_PER_CHAPTER = 100      # 单章节最多保留版本数
MIN_SAVE_INTERVAL_S = 300           # 5 分钟 debounce（同源自动保存）
AUTO_SAVE_SOURCES = {"pipeline", "ws_edit", "auto"}  # 受 debounce 控制的来源

# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════


@dataclass
class VersionMeta:
    """版本元数据（不含正文，用于列表展示）"""
    version_id: str
    chapter_id: str
    novel_id: str
    index: int                    # 版本序号 (1-based)
    word_count: int
    title: str = ""
    source: str = "auto"          # auto | manual | restore | pipeline
    label: str = ""               # 用户手动快照命名
    sha256: str = ""              # 内容哈希（去重）
    created_at: str = ""          # ISO 时间戳
    restored_from: str = ""       # 若为恢复版本，标记源版本ID

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VersionMeta":
        return cls(**{k: d.get(k, "") for k in cls.__dataclass_fields__})


@dataclass
class ChapterVersion:
    """完整版本数据（含正文）"""
    meta: VersionMeta
    content: str

    def to_dict(self) -> dict:
        return {"meta": self.meta.to_dict(), "content": self.content}

    @classmethod
    def from_dict(cls, d: dict) -> "ChapterVersion":
        return cls(
            meta=VersionMeta.from_dict(d.get("meta", {})),
            content=d.get("content", ""),
        )


# ═══════════════════════════════════════════════════════════════
# 版本存储引擎
# ═══════════════════════════════════════════════════════════════


class ChapterVersionStore:
    """章节版本存储引擎 (§16.1)"""

    _counter: int = 0  # 类级计数器，确保同毫秒内版本ID唯一

    def __init__(self, novel_id: str, chapter_id: str):
        self.novel_id = novel_id
        self.chapter_id = chapter_id
        self._root = Path(brain_path_for(novel_id)) / ".." / "versions" / chapter_id
        self._root.mkdir(parents=True, exist_ok=True)

    # ── 内部 ──

    def _version_path(self, version_id: str) -> Path:
        return self._root / f"{version_id}.json"

    def _index_path(self) -> Path:
        return self._root / "_index.json"

    def _load_index(self) -> list[VersionMeta]:
        """加载版本索引（仅元数据）"""
        ip = self._index_path()
        if not ip.exists():
            return []
        try:
            raw = json.loads(ip.read_text(encoding="utf-8"))
            return [VersionMeta.from_dict(r) for r in raw]
        except (json.JSONDecodeError, KeyError):
            return []

    def _save_index(self, metas: list[VersionMeta]) -> None:
        """保存版本索引"""
        data = [m.to_dict() for m in metas]
        tmp = self._index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._index_path())

    def _content_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # ── 公共 API ──

    def save_version(
        self,
        content: str,
        word_count: int = 0,
        title: str = "",
        source: str = "auto",
        label: str = "",
    ) -> Optional[str]:
        """保存一个新版本。若内容未变化则返回 None（去重）。

        Debounce: 同源 (auto/pipeline) 且与最新版本间隔 < 5min 的旧版本将被替换。
        """
        if not content.strip():
            return None

        new_hash = self._content_hash(content)
        metas = self._load_index()
        now_ts = time.time()
        iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))

        # 去重：内容哈希与最新版本相同则跳过
        if metas and metas[-1].sha256 == new_hash:
            return None

        # Debounce: 同源自动保存，若距离上次 < 5min 则替换最新版本
        if source in AUTO_SAVE_SOURCES and metas:
            last = metas[-1]
            last_ts = _parse_iso(last.created_at)
            if last.source in AUTO_SAVE_SOURCES and (now_ts - last_ts) < MIN_SAVE_INTERVAL_S:
                # 替换最新版本
                return self._overwrite_version(metas, last.version_id, content,
                                               word_count, title, source, label,
                                               new_hash, iso_now, metas.index(last))

        # 正常追加
        ChapterVersionStore._counter += 1
        version_id = f"v{int(now_ts * 1000)}-{ChapterVersionStore._counter}"
        index = len(metas) + 1

        meta = VersionMeta(
            version_id=version_id,
            chapter_id=self.chapter_id,
            novel_id=self.novel_id,
            index=index,
            word_count=word_count,
            title=title,
            source=source,
            label=label,
            sha256=new_hash,
            created_at=iso_now,
        )

        # 写入版本文件
        version = ChapterVersion(meta=meta, content=content)
        self._version_path(version_id).write_text(
            json.dumps(version.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        metas.append(meta)

        # 裁剪旧版本
        if len(metas) > MAX_VERSIONS_PER_CHAPTER:
            removed = metas[: len(metas) - MAX_VERSIONS_PER_CHAPTER]
            for rm in removed:
                p = self._version_path(rm.version_id)
                if p.exists():
                    p.unlink()
            metas = metas[-MAX_VERSIONS_PER_CHAPTER:]

        self._save_index(metas)
        return version_id

    def _overwrite_version(
        self, metas: list[VersionMeta],
        version_id: str, content: str,
        word_count: int, title: str, source: str, label: str,
        sha256: str, created_at: str, idx: int,
    ) -> str:
        """覆盖已有版本（debounce 场景）"""
        meta = VersionMeta(
            version_id=version_id,
            chapter_id=self.chapter_id,
            novel_id=self.novel_id,
            index=idx + 1,
            word_count=word_count,
            title=title,
            source=source,
            label=label,
            sha256=sha256,
            created_at=created_at,
        )
        version = ChapterVersion(meta=meta, content=content)
        self._version_path(version_id).write_text(
            json.dumps(version.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        metas[idx] = meta
        self._save_index(metas)
        return version_id

    def get_versions(self) -> list[VersionMeta]:
        """列出所有版本元数据（不含正文）"""
        return self._load_index()

    def get_version(self, version_id: str) -> Optional[ChapterVersion]:
        """获取完整版本（含正文）"""
        p = self._version_path(version_id)
        if not p.exists():
            return None
        try:
            return ChapterVersion.from_dict(
                json.loads(p.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def get_latest(self) -> Optional[ChapterVersion]:
        """获取最新版本"""
        metas = self._load_index()
        if not metas:
            return None
        return self.get_version(metas[-1].version_id)

    def diff_versions(
        self, version_id_a: str, version_id_b: str
    ) -> Optional[dict]:
        """生成两个版本的 unified diff"""
        va = self.get_version(version_id_a)
        vb = self.get_version(version_id_b)
        if not va or not vb:
            return None

        a_lines = va.content.splitlines(keepends=True)
        b_lines = vb.content.splitlines(keepends=True)

        diff_lines = list(
            difflib.unified_diff(
                a_lines, b_lines,
                fromfile=f"{version_id_a} ({va.meta.created_at})",
                tofile=f"{version_id_b} ({vb.meta.created_at})",
                lineterm="",
            )
        )

        # 统计
        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

        return {
            "version_a": va.meta.to_dict(),
            "version_b": vb.meta.to_dict(),
            "diff": "\n".join(diff_lines),
            "stats": {"lines_added": added, "lines_removed": removed},
        }

    def restore_version(self, version_id: str, source: str = "restore") -> Optional[str]:
        """恢复到指定版本 — 建立新版本（内容=旧版本内容，标记 restored_from）"""
        v = self.get_version(version_id)
        if not v:
            return None

        return self.save_version(
            content=v.content,
            word_count=v.meta.word_count,
            title=f"恢复自: {version_id}",
            source=source,
            label=f"恢复到 v{v.meta.index}",
        )

    def delete_version(self, version_id: str) -> bool:
        """删除某个版本"""
        p = self._version_path(version_id)
        if p.exists():
            p.unlink()
        metas = [m for m in self._load_index() if m.version_id != version_id]
        self._save_index(metas)
        return True

    def stats(self) -> dict:
        """统计信息"""
        metas = self._load_index()
        return {
            "chapter_id": self.chapter_id,
            "novel_id": self.novel_id,
            "total_versions": len(metas),
            "latest_word_count": metas[-1].word_count if metas else 0,
            "first_saved": metas[0].created_at if metas else None,
            "last_saved": metas[-1].created_at if metas else None,
        }


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════

def _parse_iso(iso_str: str) -> float:
    """解析 ISO 时间戳为 epoch 秒"""
    if not iso_str:
        return 0.0
    try:
        import datetime
        dt = datetime.datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# 全局缓存（单例模式，随进程生命周期）
# ═══════════════════════════════════════════════════════════════

_stores: dict[str, ChapterVersionStore] = {}


def get_version_store(novel_id: str, chapter_id: str) -> ChapterVersionStore:
    """获取或创建指定章节的版本存储"""
    key = f"{novel_id}/{chapter_id}"
    if key not in _stores:
        _stores[key] = ChapterVersionStore(novel_id, chapter_id)
    return _stores[key]


def auto_save_chapter_version(
    novel_id: str,
    chapter_id: str,
    content: str,
    word_count: int = 0,
    title: str = "",
    source: str = "pipeline",
) -> Optional[str]:
    """快捷函数：自动保存章节版本（管线调用）"""
    store = get_version_store(novel_id, chapter_id)
    return store.save_version(
        content=content,
        word_count=word_count or _count_chinese_words(content),
        title=title,
        source=source,
    )


def _count_chinese_words(text: str) -> int:
    """估算中文字数"""
    count = 0
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            count += 1
    return count
