"""叙事账本 (Ledger) — SQLite 事件溯源硬账本 (移植自 dsh-story 设计)

每个金钱/境界/关系/情感/物品/生死变化 = 一条追加事件(章节号+来源),
机器可校验, 支持余额查询与全量回放审计。

表结构:
  events(seq, chapter, field, target, delta, note, ghost, at)
  field: wallet(金钱) realm(境界) relation(关系) emotion(情感)
         items(物品) alive(生死) time(时间) custom(自定义)

用法:
  from brains.ledger import get_ledger
  lg = get_ledger(novel_id)
  lg.event(chapter=1, field="wallet", target="主角", delta=-50, note="买药")
  lg.balance("wallet", "主角")   # -> -50
  lg.audit()                     # 硬规则审计
"""

import sqlite3
import os
import threading
from datetime import datetime, timezone

# 连接池 (novel_id -> connection)
_connections: dict[str, sqlite3.Connection] = {}
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter INTEGER NOT NULL,
  field TEXT NOT NULL,
  target TEXT NOT NULL,
  delta REAL,
  note TEXT,
  ghost INTEGER DEFAULT 0,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_field ON events(field);
CREATE INDEX IF NOT EXISTS idx_events_chapter ON events(chapter);
CREATE INDEX IF NOT EXISTS idx_events_target ON events(target);
"""


def ledger_path(novel_id: str) -> str:
    """ledger.db 位置: ~/.novel-ai/novels/<id>/ledger.db"""
    root = os.path.expanduser("~/.novel-ai/novels")
    d = os.path.join(root, novel_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ledger.db")


def _conn(novel_id: str) -> sqlite3.Connection:
    with _lock:
        if novel_id not in _connections:
            db = ledger_path(novel_id)
            c = sqlite3.connect(db, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.executescript(SCHEMA)
            _connections[novel_id] = c
        return _connections[novel_id]


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class Ledger:
    """单部小说的叙事账本"""

    def __init__(self, novel_id: str):
        self.novel_id = novel_id
        self.db = _conn(novel_id)

    # ── 写入 ──
    def event(self, chapter: int, field: str, target: str, delta=None,
              note: str = "", ghost: int = 0) -> int:
        """追加一条事件, 返回 seq"""
        cur = self.db.execute(
            "INSERT INTO events(chapter, field, target, delta, note, ghost, at) VALUES(?,?,?,?,?,?,?)",
            (chapter, field, target, delta, note, ghost, _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def events_batch(self, rows: list[dict]) -> int:
        """批量入账 (rows: {chapter, field, target, delta, note})"""
        with self.db:
            for r in rows:
                self.db.execute(
                    "INSERT INTO events(chapter, field, target, delta, note, ghost, at) VALUES(?,?,?,?,?,?,?)",
                    (r.get("chapter", 0), r.get("field", "custom"),
                     r.get("target", ""), r.get("delta"),
                     r.get("note", ""), r.get("ghost", 0), _now()),
                )
        return len(rows)

    # ── 查询 ──
    def balance(self, field: str, target: str) -> float:
        """目标在某字段的净余额 (delta 求和)"""
        row = self.db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM events WHERE field=? AND target=?",
            (field, target),
        ).fetchone()
        return float(row[0])

    def balances(self, field: str) -> dict[str, float]:
        """某字段全部目标的余额"""
        rows = self.db.execute(
            "SELECT target, SUM(delta) FROM events WHERE field=? GROUP BY target",
            (field,),
        ).fetchall()
        return {t: float(s) for t, s in rows}

    def events(self, chapter: int | None = None, field: str | None = None,
               limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM events"
        conds, args = [], []
        if chapter is not None:
            conds.append("chapter=?")
            args.append(chapter)
        if field:
            conds.append("field=?")
            args.append(field)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        cur = self.db.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def last_event_of(self, field: str, target: str) -> dict | None:
        cur = self.db.execute(
            "SELECT * FROM events WHERE field=? AND target=? ORDER BY seq DESC LIMIT 1",
            (field, target),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def counts(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN ghost=1 THEN 1 ELSE 0 END),0) g FROM events"
        ).fetchone()
        chapters = self.db.execute("SELECT COUNT(DISTINCT chapter) FROM events").fetchone()[0]
        return {"events": row[0], "ghost": row[1], "chapters": chapters}

    # ── 审计 (硬规则入口) ──
    def audit(self) -> dict:
        from brains.invariants import run_audit
        return run_audit(self)


# ── 便捷获取 ──
_ledgers: dict[str, Ledger] = {}


def get_ledger(novel_id: str) -> Ledger:
    if novel_id not in _ledgers:
        _ledgers[novel_id] = Ledger(novel_id)
    return _ledgers[novel_id]


def close_all() -> None:
    """关闭全部连接 (测试/退出用)"""
    with _lock:
        for c in _connections.values():
            try:
                c.close()
            except Exception:
                pass
        _connections.clear()
        _ledgers.clear()
