"""SessionMemory — Agent 过程记忆与实例隔离 (方案A)

DESIGN_DOC §5.1 过程记忆: 存储 Agent 的"为什么这样写"和"打算怎么做"——
而非"写了什么"(那是 GBrain content/ 的职责)。

核心设计:
  - 每个 Agent 独立命名空间: sessions/{agent_name}/
  - SQLite + FTS5: 决策摘要可全文检索
  - 注入时按"时效性 + 关键词相关性"取最近 N 条
  - 防膨胀: 每 Agent 最多保留 200 条, 超出时归档旧记录
"""

import os
import json
import sqlite3
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


# ── 数据模型 ──

@dataclass
class DecisionRecord:
    """一条 Agent 决策摘要"""
    id: str = ""                    # session-{agent}-{chapter:03d}
    agent_name: str = ""            # writer | reviewer | context | character | canon
    novel_id: str = ""
    chapter_order: int = 0
    phase: str = ""                 # context_ready | writing | reviewing | complete
    decision_type: str = ""         # creative_choice | search_strategy | review_pattern | arc_judgment | canon_classification
    summary: str = ""               # 50-200字的决策摘要
    intent: str = ""                # "打算怎么做"——供后续章节参考的创作意图
    tags: str = ""                  # 逗号分隔的关键词, 用于检索
    created_at: str = ""
    last_accessed_at: str = ""


class SessionMemory:
    """Agent 过程记忆管理器。

    每个 (novel_id, agent_name) 有一个独立的记忆空间。
    写入: Agent 在每章完成后调用 record_decision()
    读取: Context Agent 在组装上下文时调用 retrieve_recent()

    隔离保证:
      - Writer 的决策写入 sessions/writer/ → Reviewer 不可读取
      - Reviewer 的模式写入 sessions/reviewer/ → Writer 不可读取
      - 跨 Agent 的经验共享通过 experience_book (跨小说) 而非 session_memory (单小说)
    """

    MAX_RECORDS_PER_AGENT = 200  # 每 Agent 最多保留条数
    INJECT_LIMIT = 5              # 注入到 ContextPackage 的最多条数

    def __init__(self, brain_base: str):
        self._brain_base = brain_base
        self._lock = threading.Lock()
        self._ensure_dirs()
        self._ensure_db()

    def _session_dir(self) -> str:
        return os.path.join(self._brain_base, "pages", "agent", "sessions")

    def _db_path(self) -> str:
        return os.path.join(self._session_dir(), "sessions.db")

    def _ensure_dirs(self):
        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        for agent in ("writer", "reviewer", "context", "character", "canon", "skeleton", "style"):
            os.makedirs(os.path.join(session_dir, agent), exist_ok=True)

    def _ensure_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path())
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    novel_id TEXT NOT NULL,
                    chapter_order INTEGER NOT NULL,
                    phase TEXT,
                    decision_type TEXT,
                    summary TEXT NOT NULL,
                    intent TEXT,
                    tags TEXT,
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts
                USING fts5(id, agent_name, summary, intent, tags,
                           content='decisions', content_rowid='rowid')
            """)
            # 触发器: 自动同步 FTS5
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS dec_ai AFTER INSERT ON decisions BEGIN
                    INSERT INTO decisions_fts(rowid, id, agent_name, summary, intent, tags)
                    VALUES (new.rowid, new.id, new.agent_name, new.summary, new.intent, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS dec_ad AFTER DELETE ON decisions BEGIN
                    INSERT INTO decisions_fts(decisions_fts, rowid, id, agent_name, summary, intent, tags)
                    VALUES ('delete', old.rowid, old.id, old.agent_name, old.summary, old.intent, old.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS dec_au AFTER UPDATE ON decisions BEGIN
                    INSERT INTO decisions_fts(decisions_fts, rowid, id, agent_name, summary, intent, tags)
                    VALUES ('delete', old.rowid, old.id, old.agent_name, old.summary, old.intent, old.tags);
                    INSERT INTO decisions_fts(rowid, id, agent_name, summary, intent, tags)
                    VALUES (new.rowid, new.id, new.agent_name, new.summary, new.intent, new.tags);
                END;
            """)
            conn.commit()
            conn.close()

    # ── 写入 API ──

    def record_decision(
        self,
        agent_name: str,
        novel_id: str,
        chapter_order: int,
        decision_type: str,
        summary: str,
        intent: str = "",
        tags: list[str] | None = None,
        phase: str = "",
    ) -> str:
        """写入一条决策摘要。Agent 调用此方法记录"我刚才做了什么判断"。

        参数:
            agent_name: Agent 名称 (writer/reviewer/context/character/canon)
            novel_id: 小说 ID
            chapter_order: 章节序号
            decision_type: creative_choice | search_strategy | review_pattern | arc_judgment | canon_classification
            summary: 50-200字决策摘要
            intent: 创作意图——"打算怎么做"
            tags: 检索关键词
            phase: 管线阶段

        返回: 记录 ID
        """
        record_id = f"dec-{agent_name}-{novel_id}-ch{chapter_order:03d}-{datetime.now().strftime('%H%M%S')}"
        now = datetime.now().isoformat()

        with self._lock:
            conn = sqlite3.connect(self._db_path())
            conn.execute("""
                INSERT OR REPLACE INTO decisions
                (id, agent_name, novel_id, chapter_order, phase, decision_type,
                 summary, intent, tags, created_at, last_accessed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record_id, agent_name, novel_id, chapter_order, phase,
                decision_type, summary, intent,
                ",".join(tags) if tags else "",
                now, now,
            ))
            conn.commit()

            # 防膨胀: 超出上限时删除最旧的记录
            count = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE agent_name=? AND novel_id=?",
                (agent_name, novel_id)
            ).fetchone()[0]
            if count > self.MAX_RECORDS_PER_AGENT:
                excess = count - self.MAX_RECORDS_PER_AGENT
                conn.execute("""
                    DELETE FROM decisions WHERE id IN (
                        SELECT id FROM decisions
                        WHERE agent_name=? AND novel_id=?
                        ORDER BY created_at ASC LIMIT ?
                    )
                """, (agent_name, novel_id, excess))
                conn.commit()

            conn.close()
        return record_id

    # ── 读取 API ──

    def retrieve_recent(
        self,
        agent_name: str,
        novel_id: str,
        limit: int | None = None,
        chapter_order: int | None = None,
        decision_type: str | None = None,
    ) -> list[DecisionRecord]:
        """检索指定 Agent 的最近决策摘要。

        按 created_at 降序，注入到 ContextPackage 供后续章节参考。
        排除当前章节的决策（只注入历史决策）。

        参数:
            agent_name: 要检索的 Agent 名称
            novel_id: 小说 ID
            limit: 返回条数上限 (默认 INJECT_LIMIT)
            chapter_order: 当前章节序号——只返回 < 此序号的记录
            decision_type: 可选过滤决策类型
        """
        if limit is None:
            limit = self.INJECT_LIMIT

        with self._lock:
            conn = sqlite3.connect(self._db_path())
            sql = "SELECT * FROM decisions WHERE agent_name=? AND novel_id=?"
            params = [agent_name, novel_id]

            if chapter_order is not None:
                sql += " AND chapter_order < ?"
                params.append(chapter_order)
            if decision_type:
                sql += " AND decision_type = ?"
                params.append(decision_type)

            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            conn.close()

        records = []
        for row in rows:
            records.append(DecisionRecord(
                id=row[0], agent_name=row[1], novel_id=row[2],
                chapter_order=row[3], phase=row[4] or "",
                decision_type=row[5] or "", summary=row[6] or "",
                intent=row[7] or "", tags=row[8] or "",
                created_at=row[9] or "", last_accessed_at=row[10] or "",
            ))

        # 更新访问时间
        now = datetime.now().isoformat()
        with self._lock:
            conn = sqlite3.connect(self._db_path())
            for r in records:
                conn.execute(
                    "UPDATE decisions SET last_accessed_at=? WHERE id=?",
                    (now, r.id)
                )
            conn.commit()
            conn.close()

        return records

    def search(
        self,
        agent_name: str,
        novel_id: str,
        query: str,
        limit: int = 5,
    ) -> list[DecisionRecord]:
        """全文检索 Agent 的决策摘要。

        使用 FTS5 搜索, FTS5 中文分词不佳时回退到 LIKE。
        """
        with self._lock:
            conn = sqlite3.connect(self._db_path())

            # FTS5 搜索
            try:
                rows = conn.execute("""
                    SELECT d.* FROM decisions d
                    JOIN decisions_fts f ON d.rowid = f.rowid
                    WHERE decisions_fts MATCH ? AND d.agent_name=? AND d.novel_id=?
                    ORDER BY d.created_at DESC LIMIT ?
                """, (query, agent_name, novel_id, limit)).fetchall()
            except Exception:
                rows = []

            # LIKE 回退
            if not rows:
                like = f"%{query}%"
                rows = conn.execute("""
                    SELECT * FROM decisions
                    WHERE agent_name=? AND novel_id=?
                    AND (summary LIKE ? OR intent LIKE ? OR tags LIKE ?)
                    ORDER BY created_at DESC LIMIT ?
                """, (agent_name, novel_id, like, like, like, limit)).fetchall()

            conn.close()

        return [DecisionRecord(
            id=r[0], agent_name=r[1], novel_id=r[2], chapter_order=r[3],
            phase=r[4] or "", decision_type=r[5] or "", summary=r[6] or "",
            intent=r[7] or "", tags=r[8] or "",
            created_at=r[9] or "", last_accessed_at=r[10] or "",
        ) for r in rows]

    # ── 注入格式化 ──

    def format_for_injection(self, records: list[DecisionRecord], target_agent: str = "writer") -> str:
        """将检索到的决策摘要格式化为可注入 system prompt 的文本。

        参数:
            records: retrieve_recent() 返回的决策列表
            target_agent: 目标 Agent 名称 (用于定制语气)
        """
        if not records:
            return ""

        lines = [f"\n【{target_agent}·历史创作决策】"]

        # 按类型分组
        by_type: dict[str, list[DecisionRecord]] = {}
        for r in records:
            by_type.setdefault(r.decision_type, []).append(r)

        type_labels = {
            "creative_choice": "创作技法选择",
            "search_strategy": "上下文检索策略",
            "review_pattern": "审核发现模式",
            "arc_judgment": "人物弧线判断",
            "canon_classification": "事实分类决策",
        }

        for dtype, label in type_labels.items():
            group = by_type.get(dtype, [])
            if not group:
                continue
            lines.append(f"\n  [{label}]")
            for r in group[:3]:  # 每类最多3条
                lines.append(f"  · 第{r.chapter_order}章: {r.summary}")
                if r.intent:
                    lines.append(f"    后续打算: {r.intent}")

        lines.append("\n  请参考以上历史决策，保持创作连贯性。")
        return "\n".join(lines)

    def format_isolation_boundary(self, target_agent: str) -> str:
        """生成 Agent 隔离声明——注入到 system prompt。

        让 Agent 明确知道: 这是你自己的记忆，不是其他 Agent 的。
        """
        return (
            f"\n【记忆隔离声明】"
            f"\n  以下是你({target_agent})在前文章节中的创作决策。"
            f"\n  这些决策仅属于你——不包括其他 Agent 的判断或偏好。"
            f"\n  请基于自己的历史决策保持连贯，但独立做出新的专业判断。"
        )

    # ── 统计与管理 ──

    def count(self, agent_name: str, novel_id: str) -> int:
        with self._lock:
            conn = sqlite3.connect(self._db_path())
            row = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE agent_name=? AND novel_id=?",
                (agent_name, novel_id)
            ).fetchone()
            conn.close()
        return row[0] if row else 0

    def list_agents_with_memory(self, novel_id: str) -> list[str]:
        """列出有过程记忆的 Agent 列表"""
        with self._lock:
            conn = sqlite3.connect(self._db_path())
            rows = conn.execute(
                "SELECT DISTINCT agent_name FROM decisions WHERE novel_id=?",
                (novel_id,)
            ).fetchall()
            conn.close()
        return [r[0] for r in rows]

    def clear(self, agent_name: str, novel_id: str):
        """清除指定 Agent 的所有决策记录 (用于重置)"""
        with self._lock:
            conn = sqlite3.connect(self._db_path())
            conn.execute(
                "DELETE FROM decisions WHERE agent_name=? AND novel_id=?",
                (agent_name, novel_id)
            )
            conn.commit()
            conn.close()
