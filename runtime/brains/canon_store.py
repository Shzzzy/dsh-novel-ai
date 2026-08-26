"""Canon 冲突持久化存储 — P2-14 Canon 事实冲突检测面板

将管线中检测到的 canon 冲突存储到 brain 的 pages/canon/conflicts.json，
支持查询、解决标记和来源追踪。

数据模型:
  CanonConflictRecord:
    - id: 冲突唯一ID
    - fact_a: CanonEntry — 事实A
    - fact_b: CanonEntry — 事实B (冲突方)
    - severity: "hard" | "soft" | "speculative"
    - source_chapters: 来源章节列表
    - description: 冲突描述
    - suggestion: 解决建议
    - resolved: 是否已解决
    - resolved_at: 解决时间
    - resolution_note: 解决备注

存储路径: ~/.novel-ai/novels/{novel_id}/brain/pages/canon/conflicts.json
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from brains.gbrain_wrapper import brain_path_for

# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════


@dataclass
class ConflictFactInfo:
    """冲突涉及的单个事实信息"""
    fact_id: str = ""
    fact: str = ""
    type: str = "character_fact"  # character_fact | world_fact | plot_fact
    status: str = "canon"  # canon | soft_canon | speculative
    source_chapter: int = 0  # 来源章节序号
    source_event: str = ""    # 来源事件ID


@dataclass
class CanonConflictRecord:
    """Canon 冲突记录"""
    id: str = ""
    novel_id: str = ""

    # 冲突双方
    fact_a: ConflictFactInfo = field(default_factory=ConflictFactInfo)
    fact_b: ConflictFactInfo = field(default_factory=ConflictFactInfo)

    # 冲突元信息
    severity: str = "hard"  # hard | soft | speculative
    description: str = ""
    suggestion: str = ""

    # 来源追踪
    source_chapters: list[int] = field(default_factory=list)
    source_events: list[str] = field(default_factory=list)

    # 解决状态
    resolved: bool = False
    resolved_at: Optional[str] = None
    resolution_note: str = ""

    # 时间戳
    detected_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # 确保 fact_a/fact_b 嵌套正确
        if isinstance(self.fact_a, ConflictFactInfo):
            d["fact_a"] = asdict(self.fact_a)
        if isinstance(self.fact_b, ConflictFactInfo):
            d["fact_b"] = asdict(self.fact_b)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CanonConflictRecord":
        # 处理嵌套
        if "fact_a" in d and isinstance(d["fact_a"], dict):
            d["fact_a"] = ConflictFactInfo(**d["fact_a"])
        if "fact_b" in d and isinstance(d["fact_b"], dict):
            d["fact_b"] = ConflictFactInfo(**d["fact_b"])
        return cls(**{k: v for k, v in d.items()
                      if k != "fact_a" and k != "fact_b"}) if True else cls(
            fact_a=d.get("fact_a", ConflictFactInfo()),
            fact_b=d.get("fact_b", ConflictFactInfo()),
            **{k: v for k, v in d.items()
               if k not in ("fact_a", "fact_b")}
        )


@dataclass
class CanonFactSummary:
    """Canon 事实摘要 (用于事实列表)"""
    id: str = ""
    fact: str = ""
    type: str = "character_fact"
    status: str = "canon"
    source_chapter: int = 0
    source_event: str = ""
    conflict_count: int = 0  # 关联冲突数
    last_updated: str = ""


# ═══════════════════════════════════════════════════════════════
# 持久化引擎
# ═══════════════════════════════════════════════════════════════

def _conflicts_path(novel_id: str) -> str:
    """获取小说冲突存储路径"""
    bp = brain_path_for(novel_id)
    canon_dir = os.path.join(bp, "pages", "canon")
    os.makedirs(canon_dir, exist_ok=True)
    return os.path.join(canon_dir, "conflicts.json")


def _facts_path(novel_id: str) -> str:
    """获取小说事实存储路径"""
    bp = brain_path_for(novel_id)
    canon_dir = os.path.join(bp, "pages", "canon")
    os.makedirs(canon_dir, exist_ok=True)
    return os.path.join(canon_dir, "facts.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# 冲突 CRUD
# ═══════════════════════════════════════════════════════════════

def get_conflicts(novel_id: str) -> list[CanonConflictRecord]:
    """获取小说的所有冲突记录"""
    path = _conflicts_path(novel_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [CanonConflictRecord.from_dict(item) for item in data]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def save_conflicts(
    novel_id: str,
    conflicts: list[CanonConflictRecord],
    merge: bool = True,
) -> list[CanonConflictRecord]:
    """保存冲突记录。

    当 merge=True 时, 新冲突会与已有冲突合并（按 id 去重）。
    否则完全覆盖。
    """
    path = _conflicts_path(novel_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if merge and os.path.exists(path):
        existing = get_conflicts(novel_id)
        existing_ids = {e.id for e in existing}
        for c in conflicts:
            if c.id not in existing_ids:
                existing.append(c)
        all_conflicts = existing
    else:
        all_conflicts = conflicts

    # 去重
    seen = set()
    unique = []
    for c in all_conflicts:
        if c.id not in seen:
            seen.add(c.id)
            unique.append(c)

    with open(path, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in unique], f, ensure_ascii=False, indent=2)

    return unique


def resolve_conflict(
    novel_id: str,
    conflict_id: str,
    resolution_note: str = "",
    maintain_fact: str = "",  # "a" | "b" | "merge" | ""
) -> Optional[CanonConflictRecord]:
    """标记冲突为已解决。

    maintain_fact: "a" 保留事实A, "b" 保留事实B, "merge" 合并, "" 仅记录
    """
    conflicts = get_conflicts(novel_id)
    resolved = None

    for c in conflicts:
        if c.id == conflict_id:
            c.resolved = True
            c.resolved_at = _now_iso()
            c.resolution_note = resolution_note
            if maintain_fact and maintain_fact in ("a", "b", "merge"):
                c.resolution_note = (
                    f"[{maintain_fact.upper()}] {resolution_note}"
                    if resolution_note
                    else f"保留事实{maintain_fact.upper()}"
                )
            resolved = c
            break

    if resolved:
        save_conflicts(novel_id, conflicts, merge=False)

    return resolved


def delete_conflict(novel_id: str, conflict_id: str) -> bool:
    """删除冲突记录"""
    conflicts = get_conflicts(novel_id)
    new_list = [c for c in conflicts if c.id != conflict_id]
    if len(new_list) < len(conflicts):
        save_conflicts(novel_id, new_list, merge=False)
        return True
    return False


def get_unresolved_count(novel_id: str) -> int:
    """获取未解决冲突数量"""
    return sum(1 for c in get_conflicts(novel_id) if not c.resolved)


# ═══════════════════════════════════════════════════════════════
# 事实 CRUD
# ═══════════════════════════════════════════════════════════════

def get_facts(novel_id: str) -> list[CanonFactSummary]:
    """获取小说的所有 canon 事实"""
    path = _facts_path(novel_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [CanonFactSummary(**item) for item in data]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def save_facts(novel_id: str, facts: list[dict]) -> list[CanonFactSummary]:
    """保存/更新 canon 事实。

    事实按 id 去重合并：新事实追加，已有事实更新。
    """
    path = _facts_path(novel_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    existing = {f.id: f for f in get_facts(novel_id)} if os.path.exists(path) else {}

    for f_dict in facts:
        fid = f_dict.get("id", str(uuid.uuid4())[:8])
        if fid in existing:
            # 更新已有
            existing[fid].last_updated = _now_iso()
            for key in ("fact", "type", "status", "source_chapter", "source_event"):
                if key in f_dict and f_dict[key]:
                    setattr(existing[fid], key, f_dict[key])
        else:
            existing[fid] = CanonFactSummary(
                id=fid,
                fact=f_dict.get("fact", ""),
                type=f_dict.get("type", "character_fact"),
                status=f_dict.get("status", "canon"),
                source_chapter=f_dict.get("source_chapter", 0),
                source_event=f_dict.get("source_event", ""),
                conflict_count=0,
                last_updated=_now_iso(),
            )

    all_facts = list(existing.values())
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(f) for f in all_facts], f, ensure_ascii=False, indent=2)

    return all_facts


# ═══════════════════════════════════════════════════════════════
# 管线集成辅助
# ═══════════════════════════════════════════════════════════════

def record_conflicts_from_pipeline(
    novel_id: str,
    new_facts: list,   # list[CanonEntry]
    conflicts: list,   # list[ConflictInfo] from canon_agent.check_conflicts()
    chapter_order: int = 0,
    event_id: str = "",
) -> tuple[int, int]:
    """从管线结果中记录冲突和事实。

    返回: (新增冲突数, 新增事实数)
    """
    import uuid as _uuid

    # 1. 保存事实
    fact_dicts = []
    for fact in new_facts:
        fact_dicts.append({
            "id": getattr(fact, "id", str(_uuid.uuid4())[:8]),
            "fact": getattr(fact, "fact", str(fact)) if not hasattr(fact, "fact") else fact.fact,
            "type": getattr(fact, "type", "character_fact"),
            "status": getattr(fact, "status", "canon"),
            "source_chapter": chapter_order,
            "source_event": event_id or "",
        })
    save_facts(novel_id, fact_dicts)

    # 2. 保存冲突
    if not conflicts:
        return 0, len(fact_dicts)

    conflict_records = []
    for c in conflicts:
        # c is a ConflictInfo from canon_agent
        cid = getattr(c, "id", str(_uuid.uuid4())[:8])
        desc = getattr(c, "description", "")
        if not desc:
            desc = getattr(c, "fact", str(c))

        rec = CanonConflictRecord(
            id=cid,
            novel_id=novel_id,
            severity=getattr(c, "severity", "hard"),
            description=desc,
            suggestion=getattr(c, "resolution", ""),
            source_chapters=[chapter_order] if chapter_order > 0 else [],
            source_events=[event_id] if event_id else [],
            detected_at=_now_iso(),
            fact_a=ConflictFactInfo(
                fact_id=getattr(c, "fact_a_id", getattr(c, "existing_id", "")),
                fact=getattr(c, "existing_fact", ""),
                type="character_fact",
                status="canon",
            ),
            fact_b=ConflictFactInfo(
                fact_id=getattr(c, "fact_b_id", getattr(c, "new_id", "")),
                fact=desc,
                type="character_fact",
                status=getattr(c, "new_status", "speculative"),
                source_chapter=chapter_order,
            ),
        )
        conflict_records.append(rec)

    save_conflicts(novel_id, conflict_records, merge=True)
    return len(conflict_records), len(fact_dicts)


def get_canon_stats(novel_id: str) -> dict:
    """获取 Canon 统计摘要"""
    conflicts = get_conflicts(novel_id)
    facts = get_facts(novel_id)

    unresolved = [c for c in conflicts if not c.resolved]
    hard_conflicts = [c for c in unresolved if c.severity == "hard"]
    soft_conflicts = [c for c in unresolved if c.severity == "soft"]
    speculative = [c for c in unresolved if c.severity == "speculative"]

    return {
        "total_facts": len(facts),
        "total_conflicts": len(conflicts),
        "unresolved": len(unresolved),
        "resolved": len(conflicts) - len(unresolved),
        "hard_conflicts": len(hard_conflicts),
        "soft_conflicts": len(soft_conflicts),
        "speculative_conflicts": len(speculative),
    }
