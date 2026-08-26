"""GBrain wrapper — SQLite FTS5 知识引擎 (DESIGN_DOC §5, §19.8).

替换原stub为真实sqlite3实现。每个小说拥有独立 brain 实例。
热路径上通过 sqlite3 直连, 不经过子进程。
"""

import sqlite3
import os
import threading
from pathlib import Path

# 连接池——每个brain_path一个连接, WAL模式, 线程安全
_connections: dict[str, sqlite3.Connection] = {}
_lock = threading.Lock()

# 基础路径——所有小说的 brain 实例根目录
GBRAIN_ROOT = os.path.expanduser("~/.novel-ai/novels")


def brain_path_for(novel_id: str) -> str:
    """将 novel_id 映射为 GBrain 文件系统路径。

    novel_id="novel-5" → ~/.novel-ai/novels/novel-5/brain/
    """
    path = os.path.join(GBRAIN_ROOT, novel_id, "brain")
    os.makedirs(path, exist_ok=True)
    return path


def _get_conn(brain_path: str) -> sqlite3.Connection:
    """获取 SQLite 连接, 自动创建数据库和FTS5表。"""
    with _lock:
        if brain_path not in _connections:
            os.makedirs(brain_path, exist_ok=True)
            db_path = os.path.join(brain_path, "index.db")
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            _ensure_tables(conn)
            _connections[brain_path] = conn
        return _connections[brain_path]


def _ensure_tables(conn: sqlite3.Connection):
    """创建 pages 表和 FTS5 全文索引(如果不存在)。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # FTS5 外部内容表——保持与 pages 同步
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
        USING fts5(id UNINDEXED, title, content, content=pages, content_rowid=rowid)
    """)
    # 触发器: INSERT/UPDATE/DELETE 自动同步 FTS5
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, id, title, content)
            VALUES (new.rowid, new.id, new.title, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, id, title, content)
            VALUES ('delete', old.rowid, old.id, old.title, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, id, title, content)
            VALUES ('delete', old.rowid, old.id, old.title, old.content);
            INSERT INTO pages_fts(rowid, id, title, content)
            VALUES (new.rowid, new.id, new.title, new.content);
        END
    """)
    conn.commit()


# ═══════════════════════════════════════════════════════════════
# 读接口 (Agent Phase A 调用, 直接SQL查询, <10ms)
# ═══════════════════════════════════════════════════════════════

async def brain_search(brain_path: str, subdir: str, query: str,
                       limit: int = 10) -> list[dict]:
    """在 gbrain 指定子目录中全文搜索。"""
    if not query or not query.strip():
        return []
    try:
        conn = _get_conn(brain_path)
        pattern = f"{subdir}/%" if subdir else "%"
        rows = conn.execute("""
            SELECT id, title, snippet(pages_fts, 1, '<mark>', '</mark>', '...', 40) AS snippet
            FROM pages_fts
            WHERE pages_fts MATCH ? AND id LIKE ?
            LIMIT ?
        """, (query, pattern, limit)).fetchall()
        return [
            {"id": r["id"], "title": r["title"], "snippet": r["snippet"]}
            for r in rows
        ]
    except sqlite3.OperationalError:
        # FTS5 MATCH 语法错误时忽略
        return []


async def brain_read_page(brain_path: str, subdir: str,
                          page_id: str) -> str:
    """读取 Markdown 页面内容。先从文件系统读, 回退到SQLite。"""
    page_id = page_id.replace("/", "-")  # 安全化ID
    file_path = os.path.join(brain_path, "pages", subdir, f"{page_id}.md")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    full_id = f"{subdir}/{page_id}"
    try:
        conn = _get_conn(brain_path)
        row = conn.execute(
            "SELECT content FROM pages WHERE id = ?", (full_id,)
        ).fetchone()
        return row["content"] if row else ""
    except sqlite3.OperationalError:
        return ""


# ═══════════════════════════════════════════════════════════════
# 写接口 (Agent Phase B 调用)
# ═══════════════════════════════════════════════════════════════

def brain_write_page(brain_path: str, subdir: str, page_id: str,
                     title: str, content: str) -> bool:
    """写入 Markdown 页面 + 同步更新 SQLite FTS5 索引。"""
    page_id = page_id.replace("/", "-")
    full_id = f"{subdir}/{page_id}"

    # 1. 写文件系统 (人类可读)
    file_path = os.path.join(brain_path, "pages", subdir, f"{page_id}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{content}")
    except OSError:
        return False

    # 2. 更新 SQLite 索引 (机器可检索)
    try:
        conn = _get_conn(brain_path)
        conn.execute("""
            INSERT OR REPLACE INTO pages (id, title, content, updated_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (full_id, title, content))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False


# ═══════════════════════════════════════════════════════════════
# 生命周期管理
# ═══════════════════════════════════════════════════════════════

async def brain_search_semantic(brain_path: str, query: str,
                                limit: int = 10) -> list[dict]:
    """语义搜索——在当前阶段回退到 FTS5 全文检索。

    DESIGN_DOC §19.8: sqlite-vec 就绪前使用 FTS5 替代。
    FTS5 已覆盖章节全文/人物卡/Canon 事实的精确+模糊搜索。
    """
    # 提取关键词——简单分词
    keywords = " OR ".join(query.replace("，", " ").replace("。", " ").split())
    if not keywords.strip():
        return []
    return await brain_search(brain_path, "", keywords, limit)


def brain_close(brain_path: str):
    """关闭 SQLite 连接。小说切换时调用。"""
    with _lock:
        if brain_path in _connections:
            _connections[brain_path].close()
            del _connections[brain_path]
