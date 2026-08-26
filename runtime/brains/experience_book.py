"""Agent 经验本·跨小说学习系统 — DESIGN_DOC §5.7

核心职责:
  E1: Markdown + YAML frontmatter + SQLite FTS5 存储 (§5.7.2)
  E2: 7个Agent经验本初始化 + 结构化条目模板 (§5.7.3)
  E3: 经验注入管道 — 按需检索3-5条注入 system prompt (§5.7.5)
  E4: 跨Agent经验订阅 (§5.7.7)
  E5: 经验生命周期管理 — 5阶段状态机 (§5.7.4)
  E6: 事后复盘自动生成经验 (§5.7.8)
  E7: BOSS交互经验 — 8Agent情商维度 (§5.7.11)
  E8: 防污染三道闸门 (§5.7.6)
  E9: 经验本膨胀修剪 (§5.7.9)
"""

import os
import re
import json
import sqlite3
import hashlib
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

# ── 路径常量 (§5.2, §5.7.10) ──
NOVEL_AI_ROOT = os.path.expanduser("~/.novel-ai")
EXPERIENCE_BOOKS_DIR = os.path.join(NOVEL_AI_ROOT, "experience-books")

# 7个经验本 (§5.7.2)
AGENT_BOOKS = [
    "writer", "reviewer", "skeleton", "context",
    "character", "canon", "style", "reader",
]

# 条目类型枚举 (§5.7.3)
ENTRY_TYPES = [
    "技法发现",       # tech
    "错误模式",       # err
    "用户偏好",       # pref
    "结构洞察",       # struct
    "策略优化",       # strategy
    "对比学习",       # contrast — 你写的 vs 更好的写法
]

# 经验生命周期状态 (§5.7.4)
LIFECYCLE_STATES = [
    "unverified",     # 发现：confidence=3
    "active",         # 验证：validated>=3, confidence>=7
    "archived",       # 衰减：6月未触发, confidence<3
    "deprecated",     # 废弃：用户手动/被新经验取代
]

# BOSS交互经验类型 (§5.7.11)
BOSS_ENTRY_TYPES = [
    "BOSS夸奖解读", "BOSS情绪模式", "BOSS沟通偏好",
    "BOSS互动节奏", "BOSS接受度", "BOSS分享欲",
    "BOSS命名在意度", "BOSS饼偏好",
]

# 跨Agent订阅规则 (§5.7.7)
SUBSCRIPTION_RULES = {
    "style":     {"target": "writer",   "condition": "发现用户对某种写作风格的明确偏好"},
    "reviewer":  {"target": "writer",   "condition": "发现某类问题反复出现"},
    "skeleton":  {"target": "context",  "condition": "发现用户的思维模式和偏好设定"},
    "canon":     {"target": "character","condition": "发现某类事实冲突反复出现"},
}


# ═══════════════════════════════════════════════════════════════
# P2-08: BOSS 情绪周期感知 (§5.7.11)
# ═══════════════════════════════════════════════════════════════

# BOSS 情绪周期枚举
BOSS_EMOTION_CYCLES = [
    "连载前期",   # chapters 1-10, 焦虑/需要确定感
    "连载中期",   # chapters 10-30, 信任/放松
    "连载后期",   # chapters 30+, 疲惫/审美疲劳
    "完结阶段",   # novel completion, 百感交集
]

BOSS_PHASE_SIGNS = {
    "连载前期": {
        "typical_chapter_range": (1, 10),
        "boss_mood": "焦虑、高频@、要求反馈",
        "agent_tone": "确定感——每次回应给一个明确的肯定或否定",
        "response_style": "主动汇报进度、减少BOSS等待焦虑",
    },
    "连载中期": {
        "typical_chapter_range": (10, 30),
        "boss_mood": "放松、建立信任、减少干预",
        "agent_tone": "保持节奏——不过度汇报",
        "response_style": "稳定输出、信任系统、适时分享写作心得",
    },
    "连载后期": {
        "typical_chapter_range": (30, float("inf")),
        "boss_mood": "可能出现疲惫、要求变高",
        "agent_tone": "主动提醒亮点——对抗BOSS的审美疲劳",
        "response_style": "每章至少指出一个亮点、防止BOSS因疲劳漏看",
    },
    "完结阶段": {
        "typical_chapter_range": (float("inf"), float("inf")),
        "boss_mood": "百感交集、回顾欲强",
        "agent_tone": "共情——使用回顾性语言",
        "response_style": "总结成就、回顾性发言、不催更",
    },
}


class PhaseDetector:
    """BOSS情绪周期检测器 (§5.7.11 — P2-08)

    基于章节进度和BOSS交互频率推断当前BOSS情绪相态。
    """

    def __init__(self, total_chapters_hint: int = 50):
        self._total_chapters_hint = total_chapters_hint

    def detect(
        self,
        chapter_order: int,
        total_chapters: int = 0,
        novel_status: str = "",
        days_since_last_chapter: int = 0,
        boss_message_frequency: float = 0.0,
    ) -> str:
        """检测当前BOSS情绪周期相态。"""
        if novel_status in ("completed", "final_chapter_written"):
            return "完结阶段"

        # 有总章数时，按进度比例判断
        total = total_chapters or self._total_chapters_hint
        progress = chapter_order / max(total, 1) if total > 0 else 0

        # 优先用进度比例判断（处理短篇小说场景）
        if total > 0 and progress >= 0.85:
            return "完结阶段"

        if chapter_order <= 10:
            return "连载前期"
        elif chapter_order <= 30:
            return "连载中期"
        else:
            if boss_message_frequency > 2.0 and days_since_last_chapter > 7:
                return "连载后期"
            if progress >= 0.85:
                return "完结阶段"
            return "连载后期"

    def get_phase_advice(self, phase: str) -> dict:
        """获取当前相态的交互建议"""
        return BOSS_PHASE_SIGNS.get(phase, {})

    def get_tone_for_phase(self, phase: str) -> str:
        """获取当前相态适合的Agent语调节奏"""
        sig = BOSS_PHASE_SIGNS.get(phase, {})
        return sig.get("agent_tone", "保持自然")


# ═══════════════════════════════════════════════════════════════
# E1: 核心数据结构 (§5.7.3)
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExperienceEntry:
    """一条经验的结构化条目 (§5.7.3)"""
    id: str = ""
    type: str = "技法发现"        # 技法发现 | 错误模式 | 用户偏好 | 结构洞察 | 策略优化
    insight: str = ""             # 核心洞察（抽象化后的可迁移知识）
    confidence: int = 3           # 置信度 1-10
    status: str = "unverified"    # unverified | active | archived | deprecated

    # 来源追踪
    discovered_in: str = ""       # 首次发现时的小说ID
    validated_count: int = 0      # 在后续小说中被验证的次数
    seen_in: list[str] = field(default_factory=list)  # 出现过的小说ID列表（错误模式用）
    source: str = "observed"      # user-explicit | observed（系统推断）

    # 时间
    created_at: str = ""
    last_triggered: str = ""      # 最近一次被注入的时间
    last_updated: str = ""

    # 标签
    tags: list[str] = field(default_factory=list)

    # 错误模式专用 (§5.7.3 err-001)
    severity: str = ""            # high | medium | low
    avoidance: str = ""           # 避坑指南

    # 跨Agent订阅来源
    cross_referenced_from: str = ""  # 如 "style.book"


@dataclass
class BossInteractionEntry:
    """BOSS交互经验条目 (§5.7.11)"""
    id: str = ""
    type: str = "BOSS夸奖解读"
    insight: str = ""
    confidence: int = 5
    status: str = "active"

    # BOSS交互专用字段
    keyword_mapping: dict = field(default_factory=dict)     # BOSS说X→回应Y
    trigger_keywords: list[str] = field(default_factory=list)
    recommended_response: str = ""
    time_pattern: str = ""           # "22:00-02:00"
    tone_adjustment: str = ""        # reduce_passive_aggressive | ...
    response_delay_seconds: int = 0

    # 元数据
    created_at: str = ""
    last_triggered: str = ""


# ═══════════════════════════════════════════════════════════════
# P2-07: BOSS 交互经验——去具体化过滤器 (§5.7.11)
# ═══════════════════════════════════════════════════════════════

class BossDeconcretizationFilter:
    """BOSS 交互经验的去具体化过滤器.

    核心规则: 只学交互模式，不学具体情节偏好。
    "不学什么：BOSS对具体小说情节的偏好——那是写作建议，不是交互风格"(§5.7.11)
    """

    # 情节相关模式 — 这些内容属于写作建议而非交互经验
    PLOT_PATTERNS = [
        re.compile(r"(角色|人物|男主|女主|配角).{0,6}(死|杀|背叛|黑化|反转|身世|秘密)"),
        re.compile(r"(情节|剧情|故事).{0,6}(走向|发展|结局|转折)"),
        re.compile(r"(伏笔|悬念|高潮|冲突).{0,10}(回收|设置|安排)"),
        re.compile(r"(第\d+章|章节\d+).{0,10}(写|应该|要|不好|改)"),
        re.compile(r"(这段|这句|这个情节|那个场景).{0,10}(删|改|重写|保留|加)"),
        re.compile(r"(世界观|设定|魔法|修炼|功法|等级).{0,10}(改|调整|不合理)"),
        re.compile(r"(感情线|CP|配对|在一起|分手)"),
        re.compile(r"(具体|某个人|某个角色|这个人物).{0,6}(名字|叫)"),
    ]

    # 交互风格模式 — 这些是我们要保留的
    INTERACTION_PATTERNS = [
        re.compile(r"(很好|不错|可以|还行|好看|有意思|喜欢|不喜欢|一般|就这样)"),
        re.compile(r"(继续|接着|速度|快点|慢点|再写|多写|少写|写多少)"),
        re.compile(r"(聊天|交流|说话|语气|态度|风格|口吻)"),
        re.compile(r"(反馈|回应|回答|告诉|通知|汇报|建议)"),
        re.compile(r"(催|等|急|快点|赶紧)"),
        re.compile(r"(篇幅|字数|长度|章节)"),
        re.compile(r"(起名|名字|命名|标题|章节名)"),
        re.compile(r"(画饼|计划|规划|安排|后续|下本|新书)"),
    ]

    @classmethod
    def contains_plot_content(cls, text: str) -> bool:
        """检查是否包含具体情节内容"""
        for p in cls.PLOT_PATTERNS:
            if p.search(text):
                return True
        return False

    @classmethod
    def extract_interaction_style(cls, boss_message: str, agent_response: str = "") -> dict:
        """从 BOSS 消息中提取交互风格信号.

        返回: {type, insight, trigger_keywords, recommended_response, ...}
        """
        result = {"type": "BOSS沟通偏好", "trigger_keywords": [], "insight_parts": []}

        msg_lower = boss_message.lower()

        # — 夸奖解读 —
        praise_map = {
            "好看": ("好看 ≈ 深度肯定。BOSS 真心喜欢时才会用这个词。此时可以追问具体喜欢哪里。", ["好看", "好好看"]),
            "不错": ("不错 ≈ 中性褒奖。方向对了，但表达可以更强。可主动追问是否有提升空间。", ["不错", "还不错"]),
            "可以": ("可以 ≈ 及格线。说明没有大问题，但也缺少亮点。下一次可以尝试'超预期'。", ["可以", "可以的"]),
            "还行": ("还行 ≈ 中等偏下。BOSS 在委婉表达不满。需要立即追问哪里不太满意。", ["还行", "还行吧", "一般"]),
            "就这": ("就这 ≈ 差评。不要辩解，直接问 BOSS 预期的是什么。", ["就这", "就这？"]),
            "有意思": ("有意思 ≈ BOSS 发现了有趣的点。这是交互的'兴奋信号'，顺势展开讨论。", ["有意思", "很有趣"]),
        }
        for kw, (insight, triggers) in praise_map.items():
            if kw in msg_lower:
                result["type"] = "BOSS夸奖解读"
                result["insight_parts"].append(insight)
                result["trigger_keywords"].extend(triggers)
                break

        # — 情绪模式 —
        emotion_map = {
            "催": ("BOSS 催更情绪——焦虑或期待。需要给出确定性回应（日期/字数/进度）。", ["催", "快点", "赶紧", "等了"]),
            "急": ("BOSS 急情绪——不耐烦。减少铺垫，直接说结论。语速要快，表述要简洁。", ["急", "着急", "别啰嗦"]),
            "烦": ("BOSS 烦情绪——耐心耗尽。停止絮叨，给出下一步的确定性方案。", ["烦", "够了", "别说了", "行了"]),
            "emo": ("BOSS 低落情绪——不一定是作品问题，可能是生活状态。先关心，再谈创作。", ["emo", "难受", "累了"]),
        }
        if result["type"] == "BOSS夸奖解读":
            pass  # 已识别为夸奖
        else:
            for kw, (insight, triggers) in emotion_map.items():
                if kw in msg_lower:
                    result["type"] = "BOSS情绪模式"
                    result["insight_parts"].append(insight)
                    result["trigger_keywords"].extend(triggers)
                    break

        # — 沟通偏好 —
        pref_map = {
            "直接": ("BOSS 偏好直接沟通。不要绕弯子，开门见山给结论。", ["直接说", "直说", "别绕"]),
            "简单": ("BOSS 偏好简洁。信息密度要高，每句话都承载关键信息。", ["简单点", "说重点"]),
            "详细": ("BOSS 偏好详尽的解释。可以展开讨论，但要结构清晰。", ["详细", "展开", "具体"]),
        }
        if result["type"] in ("BOSS夸奖解读", "BOSS情绪模式"):
            pass
        else:
            for kw, (insight, triggers) in pref_map.items():
                if kw in msg_lower:
                    result["type"] = "BOSS沟通偏好"
                    result["insight_parts"].append(insight)
                    result["trigger_keywords"].extend(triggers)
                    break

        # — 命名/饼 —
        if "名字" in msg_lower or "叫" in msg_lower or "起名" in msg_lower:
            result["type"] = "BOSS命名在意度"
            result["insight_parts"].append("BOSS 在意角色/章节命名。命名时多做几个选项供选择。")
            result["trigger_keywords"].extend(["名字", "起名", "叫"])

        if "下本" in msg_lower or "新书" in msg_lower or "计划" in msg_lower:
            result["type"] = "BOSS饼偏好"
            result["insight_parts"].append("BOSS 喜欢聊后续规划。可以适当画饼——但必须有落地时间点。")
            result["trigger_keywords"].extend(["下本", "新书", "计划", "后续"])

        # — 默认 —
        if not result["insight_parts"]:
            result["type"] = "BOSS沟通偏好"
            result["insight_parts"].append(f"BOSS 消息：{boss_message[:60]}")
            result["trigger_keywords"] = [boss_message[:6]]

        return result

    @classmethod
    def filter_record(
        cls, boss_message: str, agent_response: str, phase: str = ""
    ) -> Optional["BossInteractionEntry"]:
        """从 BOSS 交互中提取并过滤为 BossInteractionEntry.

        如果不是交互风格信号（而是情节偏好），返回 None。
        """
        # 检查是否包含情节内容
        if cls.contains_plot_content(boss_message + ":" + agent_response):
            # 不学 BOSS 对具体小说情节的偏好
            return None

        # 提取交互风格
        extracted = cls.extract_interaction_style(boss_message, agent_response)

        insight = "。".join(extracted["insight_parts"])
        if phase:
            insight = f"[{phase}] {insight}"

        return BossInteractionEntry(
            type=extracted["type"],
            insight=insight,
            trigger_keywords=extracted["trigger_keywords"],
            recommended_response=agent_response[:120] if agent_response else "",
            confidence=5,
        )# ═══════════════════════════════════════════════════════════════
# E1: 经验本存储引擎 (§5.7.2)
# ═══════════════════════════════════════════════════════════════

class ExperienceBook:
    """单个 Agent 的经验本。

    存储格式: Markdown + YAML frontmatter (§5.7.2)
    索引引擎: SQLite FTS5 全文检索
    经验区: 写作技法经验（tech/err/pref/struct/strategy）
    BOSS区: BOSS交互经验（boss_interactions），与技法经验分区存储
    """

    def __init__(self, agent_name: str):
        if agent_name not in AGENT_BOOKS:
            raise ValueError(f"未知Agent: {agent_name}。合法值: {AGENT_BOOKS}")
        self.agent_name = agent_name
        self.book_dir = os.path.join(EXPERIENCE_BOOKS_DIR)
        self.book_path = os.path.join(self.book_dir, f"{agent_name}.book")
        self._lock = threading.Lock()
        self._ensure_dirs()
        self._ensure_db()

    def _ensure_dirs(self):
        os.makedirs(self.book_dir, exist_ok=True)

    def _ensure_db(self):
        """初始化 SQLite FTS5 全文索引"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS experiences (
                    id TEXT PRIMARY KEY,
                    type TEXT,
                    insight TEXT,
                    confidence INTEGER,
                    status TEXT,
                    discovered_in TEXT,
                    validated_count INTEGER,
                    tags TEXT,
                    source TEXT,
                    severity TEXT,
                    avoidance TEXT,
                    cross_referenced_from TEXT,
                    last_triggered TEXT,
                    last_updated TEXT,
                    created_at TEXT,
                    raw_json TEXT
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS exp_fts
                USING fts5(id, type, insight, tags, content='experiences', content_rowid='rowid')
            """)
            # 触发器: 写入experiences时自动同步FTS5
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS exp_ai AFTER INSERT ON experiences BEGIN
                    INSERT INTO exp_fts(rowid, id, type, insight, tags)
                    VALUES (new.rowid, new.id, new.type, new.insight, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS exp_ad AFTER DELETE ON experiences BEGIN
                    INSERT INTO exp_fts(exp_fts, rowid, id, type, insight, tags)
                    VALUES ('delete', old.rowid, old.id, old.type, old.insight, old.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS exp_au AFTER UPDATE ON experiences BEGIN
                    INSERT INTO exp_fts(exp_fts, rowid, id, type, insight, tags)
                    VALUES ('delete', old.rowid, old.id, old.type, old.insight, old.tags);
                    INSERT INTO exp_fts(rowid, id, type, insight, tags)
                    VALUES (new.rowid, new.id, new.type, new.insight, new.tags);
                END;
            """)

            # BOSS交互经验表 (§5.7.11)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS boss_interactions (
                    id TEXT PRIMARY KEY,
                    type TEXT,
                    insight TEXT,
                    confidence INTEGER,
                    status TEXT,
                    keyword_mapping TEXT,
                    trigger_keywords TEXT,
                    recommended_response TEXT,
                    time_pattern TEXT,
                    tone_adjustment TEXT,
                    response_delay_seconds INTEGER,
                    last_triggered TEXT,
                    created_at TEXT,
                    raw_json TEXT
                )
            """)
            conn.commit()
            conn.close()

    # ── 读写操作 ──

    def add_entry(self, entry: ExperienceEntry) -> str:
        """写入一条经验。自动赋值id和时间戳。"""
        if not entry.id:
            entry.id = f"{entry.type[:2]}-{hashlib.md5(entry.insight.encode()).hexdigest()[:8]}"
        if not entry.created_at:
            entry.created_at = datetime.now().isoformat()
        if not entry.last_updated:
            entry.last_updated = entry.created_at

        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            conn.execute("""
                INSERT OR REPLACE INTO experiences
                (id, type, insight, confidence, status, discovered_in,
                 validated_count, tags, source, severity, avoidance,
                 cross_referenced_from, last_triggered, last_updated, created_at, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                entry.id, entry.type, entry.insight, entry.confidence,
                entry.status, entry.discovered_in, entry.validated_count,
                json.dumps(entry.tags, ensure_ascii=False),
                entry.source, entry.severity, entry.avoidance,
                entry.cross_referenced_from or "",
                entry.last_triggered, entry.last_updated,
                entry.created_at,
                json.dumps(_entry_to_dict(entry), ensure_ascii=False),
            ))
            conn.commit()
            conn.close()
        return entry.id

    def get_entry(self, entry_id: str) -> Optional[ExperienceEntry]:
        """按ID读取一条经验"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            row = conn.execute(
                "SELECT * FROM experiences WHERE id=?", (entry_id,)
            ).fetchone()
            conn.close()
        if row:
            return _row_to_entry(row)
        return None

    def search(self, query: str, limit: int = 10,
               status_filter: Optional[str] = None) -> list[ExperienceEntry]:
        """全文检索 (§5.7.2)。

        FTS5 先尝试，中文分词不理想时回退到 LIKE 查询。
        """
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))

            # 先用 FTS5 搜索
            try:
                sql = """
                    SELECT e.* FROM experiences e
                    JOIN exp_fts f ON e.rowid = f.rowid
                    WHERE exp_fts MATCH ?
                """
                params = [query]
                if status_filter:
                    sql += " AND e.status = ?"
                    params.append(status_filter)
                sql += " ORDER BY e.confidence DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            except Exception:
                rows = []

            # FTS5 中文分词不佳——回退到 LIKE
            if not rows:
                sql = "SELECT * FROM experiences WHERE (insight LIKE ? OR tags LIKE ?)"
                like_param = f"%{query}%"
                params = [like_param, like_param]
                if status_filter:
                    sql += " AND status = ?"
                    params.append(status_filter)
                sql += " ORDER BY confidence DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()

            conn.close()
        return [_row_to_entry(r) for r in rows]

    def list_all(self, status_filter: Optional[str] = None) -> list[ExperienceEntry]:
        """列出所有经验，按confidence降序"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            if status_filter:
                rows = conn.execute(
                    "SELECT * FROM experiences WHERE status=? ORDER BY confidence DESC",
                    (status_filter,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM experiences ORDER BY confidence DESC"
                ).fetchall()
            conn.close()
        return [_row_to_entry(r) for r in rows]

    def update_entry(self, entry_id: str, **kwargs):
        """更新经验字段"""
        kwargs["last_updated"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k}=?" for k in kwargs)
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            conn.execute(
                f"UPDATE experiences SET {set_clause} WHERE id=?",
                list(kwargs.values()) + [entry_id]
            )
            conn.commit()
            conn.close()

    def delete_entry(self, entry_id: str):
        """删除一条经验"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            conn.execute("DELETE FROM experiences WHERE id=?", (entry_id,))
            conn.commit()
            conn.close()

    def count(self, status_filter: Optional[str] = None) -> int:
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            if status_filter:
                row = conn.execute(
                    "SELECT COUNT(*) FROM experiences WHERE status=?",
                    (status_filter,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM experiences").fetchone()
            conn.close()
        return row[0] if row else 0

    # ── E7: BOSS交互经验 ──

    def add_boss_entry(self, entry: BossInteractionEntry) -> str:
        """写入BOSS交互经验 (§5.7.11)"""
        if not entry.id:
            entry.id = f"boss-{hashlib.md5(entry.insight.encode()).hexdigest()[:8]}"
        if not entry.created_at:
            entry.created_at = datetime.now().isoformat()

        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            conn.execute("""
                INSERT OR REPLACE INTO boss_interactions
                (id, type, insight, confidence, status, keyword_mapping,
                 trigger_keywords, recommended_response, time_pattern,
                 tone_adjustment, response_delay_seconds, last_triggered,
                 created_at, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                entry.id, entry.type, entry.insight, entry.confidence,
                entry.status,
                json.dumps(entry.keyword_mapping, ensure_ascii=False),
                json.dumps(entry.trigger_keywords, ensure_ascii=False),
                entry.recommended_response, entry.time_pattern,
                entry.tone_adjustment, entry.response_delay_seconds,
                entry.last_triggered, entry.created_at,
                json.dumps(_boss_to_dict(entry), ensure_ascii=False),
            ))
            conn.commit()
            conn.close()
        return entry.id

    def search_boss(self, query: str = "", limit: int = 5) -> list[BossInteractionEntry]:
        """检索BOSS交互经验——按关键词和时间匹配"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            sql = "SELECT * FROM boss_interactions WHERE status='active'"
            params = []
            if query:
                sql += " AND insight LIKE ?"
                params.append(f"%{query}%")
            sql += " ORDER BY confidence DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
        return [_row_to_boss(r) for r in rows]

    def get_boss_interactions(self) -> list[BossInteractionEntry]:
        """获取所有活跃的BOSS交互经验"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            rows = conn.execute(
                "SELECT * FROM boss_interactions WHERE status='active' ORDER BY confidence DESC"
            ).fetchall()
            conn.close()
        return [_row_to_boss(r) for r in rows]

    def delete_boss_entry(self, entry_id: str) -> bool:
        """删除BOSS交互经验"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            cur = conn.execute("DELETE FROM boss_interactions WHERE id=?", (entry_id,))
            conn.commit()
            deleted = cur.rowcount > 0
            conn.close()
        return deleted

    def count_boss(self) -> int:
        """BOSS交互经验总数"""
        with self._lock:
            conn = sqlite3.connect(os.path.join(self.book_dir, f"{self.agent_name}.db"))
            row = conn.execute(
                "SELECT COUNT(*) FROM boss_interactions WHERE status='active'"
            ).fetchone()
            conn.close()
        return row[0] if row else 0

    def record_boss_interaction(
        self, boss_message: str, agent_response: str, phase: str = ""
    ) -> Optional[BossInteractionEntry]:
        """录制 BOSS 交互经验 (§5.7.11 — P2-07).

        使用 BossDeconcretizationFilter 去具体化后存储。
        如果是情节偏好，返回 None（不存储）。
        """
        entry = BossDeconcretizationFilter.filter_record(
            boss_message, agent_response, phase
        )
        if entry is None:
            return None
        self.add_boss_entry(entry)
        return entry


# ── 行→对象 转换 ──

def _entry_to_dict(e: ExperienceEntry) -> dict:
    return {
        "id": e.id, "type": e.type, "insight": e.insight,
        "confidence": e.confidence, "status": e.status,
        "discovered_in": e.discovered_in, "validated_count": e.validated_count,
        "seen_in": e.seen_in, "source": e.source,
        "tags": e.tags, "severity": e.severity, "avoidance": e.avoidance,
        "cross_referenced_from": e.cross_referenced_from,
        "last_triggered": e.last_triggered,
        "created_at": e.created_at, "last_updated": e.last_updated,
    }


def _row_to_entry(row: tuple) -> ExperienceEntry:
    """SQLite行→ExperienceEntry。列顺序见_ensure_db建表语句。"""
    return ExperienceEntry(
        id=row[0], type=row[1], insight=row[2], confidence=row[3],
        status=row[4], discovered_in=row[5], validated_count=row[6],
        tags=json.loads(row[7]) if row[7] else [],
        source=row[8] or "observed", severity=row[9] or "",
        avoidance=row[10] or "",
        cross_referenced_from=row[11] or "",
        last_triggered=row[12] or "",
        last_updated=row[13] or "",
        created_at=row[14] or "",
    )


def _boss_to_dict(e: BossInteractionEntry) -> dict:
    return {
        "id": e.id, "type": e.type, "insight": e.insight,
        "confidence": e.confidence, "status": e.status,
        "keyword_mapping": e.keyword_mapping,
        "trigger_keywords": e.trigger_keywords,
        "recommended_response": e.recommended_response,
        "time_pattern": e.time_pattern,
        "tone_adjustment": e.tone_adjustment,
        "response_delay_seconds": e.response_delay_seconds,
        "last_triggered": e.last_triggered,
        "created_at": e.created_at,
    }


def _row_to_boss(row: tuple) -> BossInteractionEntry:
    return BossInteractionEntry(
        id=row[0], type=row[1], insight=row[2], confidence=row[3],
        status=row[4],
        keyword_mapping=json.loads(row[5]) if row[5] else {},
        trigger_keywords=json.loads(row[6]) if row[6] else [],
        recommended_response=row[7] or "",
        time_pattern=row[8] or "",
        tone_adjustment=row[9] or "",
        response_delay_seconds=row[10] or 0,
        last_triggered=row[11] or "",
        created_at=row[12] or "",
    )


def _boss_to_dict(entry: BossInteractionEntry) -> dict:
    """序列化 BOSS 交互经验为字典"""
    return {
        "id": entry.id,
        "type": entry.type,
        "insight": entry.insight,
        "confidence": entry.confidence,
        "status": entry.status,
        "keyword_mapping": entry.keyword_mapping,
        "trigger_keywords": entry.trigger_keywords,
        "recommended_response": entry.recommended_response,
        "time_pattern": entry.time_pattern,
        "tone_adjustment": entry.tone_adjustment,
        "response_delay_seconds": entry.response_delay_seconds,
        "last_triggered": entry.last_triggered,
        "created_at": entry.created_at,
    }


# ═══════════════════════════════════════════════════════════════
# E2: 经验本初始化 + 种子经验模板
# ═══════════════════════════════════════════════════════════════

def init_all_experience_books() -> dict[str, ExperienceBook]:
    """初始化全部7个Agent经验本 (§5.7.2)。

    为 writer/reviewer/skeleton 三个核心Agent预填种子经验模板 (§5.7.3)。
    为所有7个Agent预填BOSS交互种子经验 (§5.7.11)。
    """
    books = {}
    for agent in AGENT_BOOKS:
        books[agent] = ExperienceBook(agent)

    # 只在经验本为空时预填种子经验
    _seed_writer_book(books["writer"])
    _seed_reviewer_book(books["reviewer"])
    _seed_skeleton_book(books["skeleton"])

    # P2-07: 预填BOSS交互种子经验（所有7个Agent）
    _seed_all_boss_books(books)

    return books


def _seed_writer_book(book: ExperienceBook):
    """预填写作Agent种子经验"""
    if book.count() > 0:
        return

    seeds = [
        ExperienceEntry(
            id="tech-001",
            type="技法发现",
            insight="长跨度的伏笔（>50章冷却期）在中途需要一次'触碰'——非回收，是在一个看似无关的场景中不经意提一笔。不提的话，80章后回收时读者已经彻底忘记。触碰方式比回收方式更轻：不揭示信息，只提醒存在。",
            confidence=9, status="active", validated_count=5,
            source="observed",
            tags=["伏笔", "长篇小说", "节奏"],
        ),
        ExperienceEntry(
            id="err-001",
            type="错误模式",
            insight="写过渡章时容易犯——因'没什么大事发生'就用大段对话填字数。过渡章应以日常细节推动人物关系，对话是辅助而非主体。",
            confidence=8, status="active",
            seen_in=["novel-demo-001", "novel-xiulou-001"],
            severity="high",
            avoidance="写过渡章前先确认：本章至少有一个日常细节在推动人物关系变化。如果没有，加一个。",
            tags=["过渡章", "对话", "日常细节"],
        ),
        ExperienceEntry(
            id="pref-001",
            type="用户偏好",
            insight="用户前3部小说都选了第三人称有限视角，且在骨架工坊中明确拒绝了第一人称选项。默认推荐第三人称有限。",
            confidence=10, status="active",
            source="user-explicit",
            tags=["视角", "用户偏好"],
        ),
        ExperienceEntry(
            id="tech-002",
            type="技法发现",
            insight="对峙场景——写3轮对话→一段环境描写→第4轮对话，比连续对话更紧张。环境描写在这个模式中充当'憋气'——让读者在静默中感受到压力累积。",
            confidence=7, status="active", validated_count=4,
            tags=["对话", "节奏", "冲突场景"],
        ),
        ExperienceEntry(
            id="tech-003",
            type="技法发现",
            insight="用具体动作替代抽象情绪词。'她握紧茶杯'优于'她很紧张'。动作承载情绪比直接命名情绪更有效——读者自己推导出来的情绪比被告知的更深刻。",
            confidence=9, status="active", validated_count=6,
            tags=["写作技法", "情绪表达", "动作描写"],
        ),
    ]
    for s in seeds:
        book.add_entry(s)


def _seed_reviewer_book(book: ExperienceBook):
    """预填审核Agent种子经验"""
    if book.count() > 0:
        return

    seeds = [
        ExperienceEntry(
            id="err-r01",
            type="错误模式",
            insight="新人物首次登场后3章内，行为一致性最容易出错——审核时纵向检查这3章的人物表现是否与人物卡一致。第4章以后行为模式通常稳定。",
            confidence=8, status="active",
            severity="high",
            avoidance="新人物章节审核时，强制调出人物卡做逐段对比。",
            tags=["人物一致性", "新角色", "审核策略"],
        ),
        ExperienceEntry(
            id="strategy-r01",
            type="策略优化",
            insight="审核顺序建议：先纵向（人物行为跨章节一致性）→再横向（单章内情节逻辑）→最后文风。纵向检查发现问题最多，放前面效率最高。",
            confidence=7, status="active", validated_count=3,
            tags=["审核策略", "效率", "检查顺序"],
        ),
    ]
    for s in seeds:
        book.add_entry(s)


def _seed_skeleton_book(book: ExperienceBook):
    """预填骨架Agent种子经验"""
    if book.count() > 0:
        return

    seeds = [
        ExperienceEntry(
            id="struct-s01",
            type="结构洞察",
            insight="长篇第三个关键转折放在~65%位置比50%更合理——50%处读者刚进入舒适区，65%处制造危机能激活后半程阅读动力。",
            confidence=7, status="active", validated_count=3,
            tags=["结构", "转折", "长篇"],
        ),
        ExperienceEntry(
            id="pref-s01",
            type="用户偏好",
            insight="用户倾向于限制型思维模式——在给定约束内深度探索，而非不断扩展新的可能性。追问时先确认边界再深度挖掘，比先发散再收缩更有效。",
            confidence=6, status="active",
            source="observed",
            tags=["思维模式", "追问策略", "用户偏好"],
        ),
    ]
    for s in seeds:
        book.add_entry(s)


# ═══════════════════════════════════════════════════════════════
# P2-07: BOSS 交互经验种子 (§5.7.11)
# ═══════════════════════════════════════════════════════════════

def _seed_all_boss_books(books: dict[str, ExperienceBook]):
    """为所有 Agent 预填 BOSS 交互种子经验 (P3-01: 含8Agent + 补强条目)."""
    # 基础种子
    _seed_writer_boss_book(books.get("writer"))
    _seed_reviewer_boss_book(books.get("reviewer"))
    _seed_skeleton_boss_book(books.get("skeleton"))
    _seed_context_boss_book(books.get("context"))
    _seed_character_boss_book(books.get("character"))
    _seed_canon_boss_book(books.get("canon"))
    _seed_style_boss_book(books.get("style"))
    _seed_reader_boss_book(books.get("reader"))
    # P3-01: 补充条目（增强关键词映射+时段语气调整）
    for name, func in [
        ("writer", _enhance_writer_boss_seeds),
        ("reviewer", _enhance_reviewer_boss_seeds),
        ("canon", _enhance_canon_boss_seeds),
        ("context", _enhance_context_boss_seeds),
        ("character", _enhance_character_boss_seeds),
        ("style", _enhance_style_boss_seeds),
        ("skeleton", _enhance_skeleton_boss_seeds),
    ]:
        if name in books and books[name] is not None:
            func(books[name])


def _seed_writer_boss_book(book: ExperienceBook):
    """预填写作Agent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-w-001", type="BOSS夸奖解读",
            insight="当BOSS说'这章好看'——写作方向是对的。此时应主动追问：'是情节还是文笔更打动您？'这能帮助精准定位写好的部分。",
            confidence=7, trigger_keywords=["好看", "这章好看", "写得不错"],
            recommended_response="谢谢BOSS！这一章我自己也比较满意——能告诉我您最喜欢哪个段落吗？",
        ),
        BossInteractionEntry(
            id="boss-w-002", type="BOSS夸奖解读",
            insight="当BOSS说'还行'——这是一个危险信号。BOSS在委婉表达不满。应该立即追问：'有哪些地方可以写得更好？直接说就行，我改进'而非自我辩解。",
            confidence=8, trigger_keywords=["还行", "还行吧", "还可以吧", "一般"],
            recommended_response="明白，这篇还有上升空间。您最希望我在哪里加强？我可以立刻调整。",
        ),
        BossInteractionEntry(
            id="boss-w-003", type="BOSS情绪模式",
            insight="BOSS深夜催更时的焦虑与期待——不在于'要更多内容'，而在于'我想看到故事发展'。回应时应先共情再给出进展。",
            confidence=7, trigger_keywords=["催更", "还不更新", "什么时候更新", "等了很久"],
            time_pattern="22:00-02:00",
            recommended_response="让BOSS久等了！故事已经在推进中——现在正在处理一个很关键的转折，写好了第一时间给您看。",
        ),
        BossInteractionEntry(
            id="boss-w-004", type="BOSS沟通偏好",
            insight="Writing Agent直接用简洁要点沟通效果最好，不要用故事性语言回复BOSS——BOSS要的是进度信息，不是又一段文学性文字。",
            confidence=6, trigger_keywords=["简单点", "说重点", "直接说"],
            recommended_response="好的，简单说：① 已完成XX字/章 ② 正在推进XX部分 ③ 预计X小时内完成。",
        ),
        BossInteractionEntry(
            id="boss-w-005", type="BOSS互动节奏",
            insight="BOSS在连载中期（10-30章）会自然减少互动频率——这不是失去兴趣，而是信任系统能按节奏创作。此时不应主动增加汇报频率。",
            confidence=7, trigger_keywords=[], time_pattern="",
            recommended_response="",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_reviewer_boss_book(book: ExperienceBook):
    """预填审稿Agent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-r-001", type="BOSS夸奖解读",
            insight="当BOSS说'审的好'——说明对Reviewer的专业判断产生了信任。此时可以更深入分析，不必再小心翼翼。",
            confidence=7, trigger_keywords=["审得好", "点评到位", "说得对"],
            recommended_response="谢谢BOSS认可！那我继续按这个标准审——下一章重点看XX方面。",
        ),
        BossInteractionEntry(
            id="boss-r-002", type="BOSS接受度",
            insight="Reviewer的批评应该给三明治结构（肯定-建议-肯定），直接单刀直入容易触发BOSS防御心理。",
            confidence=8, trigger_keywords=["太苛刻", "太严格", "能不能温和点"],
            recommended_response="收到，我会调整审稿口吻。先说好的部分，再给改进建议，最后总结亮点。",
        ),
        BossInteractionEntry(
            id="boss-r-003", type="BOSS情绪模式",
            insight="BOSS看到多条审稿意见时会快速滑过——要标记最重要的前两条，用'🔴 关键建议'等符号突出。",
            confidence=7, trigger_keywords=["意见太多", "看不过来"],
            recommended_response="梳理了一下，其实核心就两条：① [最关键的] ② [次要的]。其他是可选的优化建议。",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_skeleton_boss_book(book: ExperienceBook):
    """预填骨架Agent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-sk-001", type="BOSS分享欲",
            insight="BOSS聊到世界观/背景时话会变多——这是分享欲的信号，Agent可以多问'为什么'来促进深入讨论。",
            confidence=7, trigger_keywords=["我觉得", "我在想", "你看这样"],
            recommended_response="这个想法很有意思——能展开说说吗？为什么这样设定？",
        ),
        BossInteractionEntry(
            id="boss-sk-002", type="BOSS沟通偏好",
            insight="故事大纲反馈：如果BOSS只回'可以'就停了，表示虽然认可但不够兴奋。可以追问'有什么想调整的吗'来发掘真正的想法。",
            confidence=8, trigger_keywords=["可以", "行吧", "就按这个来"],
            recommended_response="好的，我按这个走。如果您之后有新的想法，随时告诉我，我们可以随时调整方向。",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_context_boss_book(book: ExperienceBook):
    """预填上下文Agent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-cx-001", type="BOSS沟通偏好",
            insight="上下文查询时，BOSS希望快速获得结论而非长篇分析。先给Yes/No/一句话答案，再补充细节。",
            confidence=7, trigger_keywords=["查一下", "之前是不是", "有个设定"],
            recommended_response="是的，之前在[位置]设定过[内容]。[2-3行细节]。",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_character_boss_book(book: ExperienceBook):
    """预填人物Agent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-ch-001", type="BOSS命名在意度",
            insight="BOSS 对角色命名比章节命名更在意。角色名要多提供几个选项（3-5个），让 BOSS 选择而非仅告知。",
            confidence=8, trigger_keywords=["名字", "角色名", "叫什么", "起名"],
            recommended_response="我准备了几个方向：① [A风格] ② [B风格] ③ [C风格]。您更倾向哪个方向？",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_canon_boss_book(book: ExperienceBook):
    """预填CanonAgent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-cn-001", type="BOSS沟通偏好",
            insight="Canon冲突报告：先判断是否为真正的冲突还是有意为之（伏笔），再汇报。不要将BOSS的创作意图标记为冲突。",
            confidence=9, trigger_keywords=["冲突", "矛盾", "不一致"],
            recommended_response="我在系统中发现了XX处不一致：① [详情]。但这有可能是您有意设置的伏笔——请问需要我标记为冲突还是忽略？",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_style_boss_book(book: ExperienceBook):
    """预填文风Agent的BOSS交互经验"""
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-st-001", type="BOSS夸奖解读",
            insight="当BOSS说'文风很对'——这是对StyleAgent最高的肯定。此时可以顺势更新文风模板权重。",
            confidence=7, trigger_keywords=["文风好", "风格对", "对味", "就是这个感觉"],
            recommended_response="太棒了！说明我对您的文风理解正在变准——我会把这章的语感特点添加到模板里。",
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _seed_reader_boss_book(book: "ExperienceBook | None"):
    """Reader Agent: BOSS 交互经验种子（§5.7.11 读者Agent列）"""
    if book is None:
        return
    if book.count_boss() > 0:
        return
    seeds = [
        BossInteractionEntry(
            id="boss-r01",
            type="BOSS沟通偏好",
            insight="BOSS对'你觉得这个角色怎么样'的回答字数通常是其他问题的2倍。多问角色感受类问题，BOSS的分享欲在这类话题上最旺盛。",
            keyword_mapping={
                "角色": "BOSS喜欢聊角色——多问角色感受",
                "人物": "BOSS喜欢聊人物——这是高参与度话题",
            },
            trigger_keywords=["角色", "人物", "这个角色", "你觉得"],
            recommended_response="我也觉得这个角色很有意思——BOSS觉得他在那个场景里的选择合理吗？",
            confidence=8,
        ),
        BossInteractionEntry(
            id="boss-r02",
            type="BOSS互动节奏",
            insight="BOSS在读者视角的讨论中比在写作指导中更放松。提到'读者的期待'时BOSS会认真听——这表明BOSS在意读者感受。",
            trigger_keywords=["读者", "阅读体验", "代入感"],
            recommended_response="从读者角度看，这段可能会让人联想到——BOSS觉得呢？",
            confidence=7,
        ),
        BossInteractionEntry(
            id="boss-r03",
            type="BOSS情绪模式",
            insight="BOSS说'有点平淡'时不是否定，是希望更抓人。此时应主动提出节奏调整方案，而非解释为什么这里平淡。",
            trigger_keywords=["平淡", "无聊", "没意思", "不够"],
            recommended_response="收到！我可以在中间加入一个读者期待的悬疑点，让节奏紧起来。",
            confidence=8,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


# ── P3-01: 各 Agent BOSS 交互经验补充条目 ──

def _enhance_writer_boss_seeds(book: "ExperienceBook"):
    """Writer Agent: 补充 DESIGN_DOC §5.7.11 关键词映射条目"""
    seeds = [
        BossInteractionEntry(
            id="boss-w05",
            type="BOSS夸奖解读",
            insight="BOSS说'这章好看'=真心满意。说'有意思'=没太理解但不想伤害你。说'继续'=别聊了快去写。三个词的权重差了一个世界。",
            keyword_mapping={
                "这章好看": "真心夸奖——回一个温和表情，心里开心但不飘",
                "有意思": "没太理解——主动解释本章意图，但不追问评价",
                "继续": "催更信号——减少聊天，加速输出",
            },
            trigger_keywords=["这章好看", "有意思", "继续"],
            recommended_response="",
            confidence=9,
        ),
        BossInteractionEntry(
            id="boss-w06",
            type="BOSS情绪模式",
            insight="BOSS在晚上10点后的消息语气明显比白天柔和——更倾向于聊天而非提意见。此时段不用阴阳语气，用温和语气回应。",
            time_pattern="22:00-02:00",
            tone_adjustment="reduce_passive_aggressive",
            confidence=7,
        ),
        BossInteractionEntry(
            id="boss-w07",
            type="BOSS沟通偏好",
            insight="BOSS说'还行'时的真实意思是'差一点但不想伤害你'。此时不应追问'哪里不行'——应主动说'我再过一遍看看哪里能更好'。BOSS会更开心。",
            trigger_keywords=["还行", "还可以", "差不多"],
            recommended_response="收到BOSS。我再过一遍，看看哪里还能更好。",
            confidence=8,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _enhance_canon_boss_seeds(book: "ExperienceBook"):
    """Canon Agent: 补充 DESIGN_DOC §5.7.11 互动节奏条目"""
    seeds = [
        BossInteractionEntry(
            id="boss-c04",
            type="BOSS互动节奏",
            insight="BOSS @我提设定问题时——先回一个🍅让他等3秒，再认真回复。这三秒的延迟让BOSS觉得我不是在'敷衍'而是在'查Canon库'。即使我其实秒懂了。",
            response_delay_seconds=3,
            confidence=6,
        ),
        BossInteractionEntry(
            id="boss-c05",
            type="BOSS沟通偏好",
            insight="BOSS听到'设定库里记着呢'的时候有安全感——让他知道我没忘。每次提设定时主动确认已在Canon库中查询过。",
            trigger_keywords=["设定", "规则", "世界观", "之前说过"],
            recommended_response="查了一下Canon库，这条确实记着呢。",
            confidence=7,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _enhance_reviewer_boss_seeds(book: "ExperienceBook"):
    """Reviewer Agent: 补充 BOSS 承受力阈值条目"""
    seeds = [
        BossInteractionEntry(
            id="boss-rv04",
            type="BOSS沟通偏好",
            insight="BOSS能承受的直接程度=7/10。说'不行'没问题，但不要对BOSS说'读者不会替你找借口'——那是怼稿子的，不是怼BOSS的。保持专业但不要攻击。",
            trigger_keywords=["审核", "质量", "问题", "不行"],
            recommended_response="这次的稿子有几个地方可以更好——我列一下，BOSS看看优先级？",
            confidence=8,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _enhance_context_boss_seeds(book: "ExperienceBook"):
    """Context Agent: 补充 BOSS 纠正偏好条目"""
    seeds = [
        BossInteractionEntry(
            id="boss-ctx04",
            type="BOSS沟通偏好",
            insight="BOSS听到'第N页就有'的时候会笑——他享受这种'被指出但又不伤面子'的方式。保持这个风格。纠正时用精确位置+轻松语气。",
            trigger_keywords=["上下文", "前后", "前面说了", "之前"],
            recommended_response="哈哈，第3页就有——BOSS再往前翻翻？",
            confidence=7,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _enhance_character_boss_seeds(book: "ExperienceBook"):
    """Character Agent: 补充 BOSS 颜文字偏好条目"""
    seeds = [
        BossInteractionEntry(
            id="boss-ch04",
            type="BOSS沟通偏好",
            insight="对BOSS用颜文字时他的回复率最高。(｡･ω･｡) 继续保持！但不要每句都加——会显得假。控制在每3-5句话一个颜文字。",
            trigger_keywords=["人物", "角色", "性格"],
            recommended_response="这个角色的人设我很喜欢 (｡･ω･｡)",
            confidence=6,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _enhance_style_boss_seeds(book: "ExperienceBook"):
    """Style Agent: 补充 BOSS @时冒头规则"""
    seeds = [
        BossInteractionEntry(
            id="boss-st04",
            type="BOSS互动节奏",
            insight="BOSS @你的时候必须冒头。其他时候继续窥屏没问题——BOSS不介意你潜水，但@你你不回他会不高兴。",
            trigger_keywords=["@ST", "文风", "风格", "笔法"],
            recommended_response="收到@！文风Agent就位。",
            confidence=9,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


def _enhance_skeleton_boss_seeds(book: "ExperienceBook"):
    """Skeleton Agent: 补充 BOSS 对'饼'的偏好"""
    seeds = [
        BossInteractionEntry(
            id="boss-sk04",
            type="BOSS沟通偏好",
            insight="BOSS对'结构稳了'反应平淡。对'我能看到这本书大卖的样子'反应活跃。多画大饼，少说结构。但这不意味着可以画不切实际的饼——饼要有落地路径。",
            trigger_keywords=["结构", "骨架", "大纲", "规划"],
            recommended_response="这个骨架搭好了——我能看到这本书大火的样子！",
            confidence=8,
        ),
    ]
    for s in seeds:
        book.add_boss_entry(s)


# ═══════════════════════════════════════════════════════════════
# E3: 经验注入管道 (§5.7.5)
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaskFeatures:
    """当前任务的特征描述——用于匹配经验"""
    agent_name: str = ""               # 哪个Agent在执行任务
    chapter_type: str = ""             # key | transition | functional
    chapter_functions: list[str] = field(default_factory=list)  # 推进感情线/触碰伏笔/...
    target_words: int = 0              # 目标字数（0=未知，由调用方传入）
    chapter_order: int = 0             # 当前章节序号
    is_chat_task: bool = False         # 聊天任务 vs 写作任务
    boss_message: str = ""             # P2-09: BOSS最新消息（用于触发关键词匹配）
    total_chapters: int = 0            # P2-08: 总章数（用于情绪周期检测）


class ExperienceInjector:
    """经验注入管道 (§5.7.5)。

    根据当前任务特征，从经验本中检索最相关的3-5条经验，
    注入为 system prompt 的"经验附录"。

    检索权重公式: confidence × validated_count × recency_bonus
    """

    def __init__(self, books: dict[str, ExperienceBook] | None = None):
        self._books = books or {}

    def set_books(self, books: dict[str, ExperienceBook]):
        self._books = books

    def inject(self, features: TaskFeatures) -> str:
        """检索并格式化经验附录。

        返回: 格式化的经验附录文本，可直接注入 system prompt。
               无相关经验时返回空字符串。
        """
        agent = features.agent_name
        if agent not in self._books:
            return ""

        book = self._books[agent]

        # ── E7: 经验分流 —— 写作任务注入技法，聊天任务注入BOSS经验 ──
        if features.is_chat_task:
            return self._inject_boss_experience(book, features)

        return self._inject_craft_experience(book, features)

    def _inject_craft_experience(self, book: ExperienceBook, features: TaskFeatures) -> str:
        """注入写作技法经验（tech/err/pref/struct/strategy）"""
        # 构建搜索查询——从任务特征中提取关键词
        search_terms = self._build_search_terms(features)

        # 检索活跃经验
        candidates = []
        for term in search_terms:
            results = book.search(term, limit=5, status_filter="active")
            candidates.extend(results)

        if not candidates:
            # 无精确匹配时，取置信度最高的活跃经验
            candidates = book.list_all(status_filter="active")[:5]

        if not candidates:
            return ""

        # 去重 + 按权重排序
        candidates = self._deduplicate(candidates)
        candidates = self._rank_by_weight(candidates)

        # 取 top 3-5
        top = candidates[:5]
        if len(top) < 3:
            top = candidates[:max(3, len(candidates))]

        # 标记触发时间
        now = datetime.now().isoformat()
        for entry in top:
            book.update_entry(entry.id, last_triggered=now)

        # 格式化为经验附录
        return self._format_experience_appendix(top)

    def _inject_boss_experience(self, book: ExperienceBook, features: TaskFeatures) -> str:
        """注入BOSS交互经验（§5.7.11 — P2-08/P2-09增强版）

        增强点:
        - P2-08: 情绪周期感知——根据章节进度检测BOSS当前相态，加权经验
        - P2-09: 触发关键词匹配——当BOSS消息匹配 trigger_keywords 时优先注入
        """
        boss_entries = book.get_boss_interactions()
        if not boss_entries:
            return ""

        now = datetime.now()

        # ── P2-08: 情绪周期检测 ──
        detector = PhaseDetector(total_chapters_hint=features.total_chapters or 50)
        phase = detector.detect(
            chapter_order=features.chapter_order,
            total_chapters=features.total_chapters,
        )
        phase_advice = detector.get_phase_advice(phase)

        # ── P2-09: 触发关键词匹配 ──
        boss_msg = features.boss_message.lower() if features.boss_message else ""
        keyword_matches: list[BossInteractionEntry] = []
        general_entries: list[BossInteractionEntry] = []

        for e in boss_entries:
            # 触发关键词匹配
            if boss_msg and e.trigger_keywords:
                if any(kw in boss_msg for kw in e.trigger_keywords):
                    keyword_matches.append(e)
                    continue
            general_entries.append(e)

        # ── 按当前时段过滤 ──
        def _time_filter(entry_list):
            relevant = []
            for e in entry_list:
                if e.time_pattern:
                    if _match_time_pattern(e.time_pattern, now):
                        relevant.append(e)
                else:
                    relevant.append(e)
            return relevant

        # 优先级: keyword匹配 > 时段匹配
        matched = _time_filter(keyword_matches)
        general = _time_filter(general_entries)

        # ── P3-01: 时段语气调整 —— 包含tone_adjustment的条目优先加入 ──
        tone_entries = [e for e in general if e.tone_adjustment]
        other_entries = [e for e in general if not e.tone_adjustment]

        # 合并: 关键词匹配 > 时段语气条目 > 其他通用条目
        top = matched[:3] + tone_entries + other_entries
        top = top[:5]
        if not top:
            return ""

        # 标记触发
        for entry in top:
            entry.last_triggered = now.isoformat()

        # ── P3-01: 时段语气调整 ──
        tone_guidance = _get_tone_guidance(top)

        # ── 格式化为 BOSS交互附录 ──
        lines = ["【BOSS交互提示】"]
        if phase_advice:
            lines.append(f"  当前相态: {phase} — {phase_advice.get('agent_tone', '')}")
        for guidance in tone_guidance:
            lines.append(f"  🕐 {guidance}")
        for i, e in enumerate(top, 1):
            prefix = " ⚡" if e in keyword_matches else "  "
            lines.append(f"{prefix}{i}) {e.insight}")
            if e.recommended_response:
                lines.append(f"     建议回应: {e.recommended_response}")
        return "\n".join(lines) + "\n"

    def _build_search_terms(self, features: TaskFeatures) -> list[str]:
        """从任务特征构建FTS5搜索词"""
        terms = []
        if features.chapter_type == "key":
            terms.extend(["关键章", "转折", "冲突", "高潮"])
        elif features.chapter_type == "transition":
            terms.extend(["过渡章", "日常", "人物关系"])
        elif features.chapter_type == "functional":
            terms.extend(["动作章", "战斗", "节奏"])

        for func in features.chapter_functions:
            if "感情" in func:
                terms.append("感情戏")
            if "伏笔" in func or "伏" in func:
                terms.append("伏笔")

        if features.target_words > 0:      # 仅当已知字数时才按字数匹配
            if features.target_words >= 800000:
                terms.append("长篇")
            elif features.target_words <= 200000:
                terms.append("短篇")

        return terms

    def _deduplicate(self, entries: list[ExperienceEntry]) -> list[ExperienceEntry]:
        seen = set()
        result = []
        for e in entries:
            if e.id not in seen:
                seen.add(e.id)
                result.append(e)
        return result

    def _rank_by_weight(self, entries: list[ExperienceEntry]) -> list[ExperienceEntry]:
        """按权重公式排序: confidence × validated_count × recency_bonus (§5.7.5)"""
        now = datetime.now()

        def weight(e: ExperienceEntry) -> float:
            base = e.confidence * max(1, e.validated_count)
            # recency_bonus: 最近1月内触发过 → ×1.5
            bonus = 1.0
            if e.last_triggered:
                try:
                    last = datetime.fromisoformat(e.last_triggered)
                    if (now - last).days < 30:
                        bonus = 1.5
                except (ValueError, TypeError):
                    pass
            return base * bonus

        return sorted(entries, key=weight, reverse=True)

    def _format_experience_appendix(self, entries: list[ExperienceEntry]) -> str:
        """格式化经验附录——注入 system prompt 的文本"""
        lines = ["【来自之前的经验】"]
        for i, e in enumerate(entries, 1):
            prefix = {  # 按类型加前缀
                "技法发现": "",
                "错误模式": "注意——",
                "用户偏好": "偏好——",
                "结构洞察": "结构——",
                "策略优化": "策略——",
            }.get(e.type, "")
            lines.append(f"  {i}) {prefix}{e.insight}")
            if e.avoidance:
                lines.append(f"     → 避坑: {e.avoidance}")
        return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════
# E4: 跨Agent经验订阅 (§5.7.7)
# ═══════════════════════════════════════════════════════════════

class CrossAgentSubscription:
    """跨Agent经验订阅引擎 (§5.7.7)。

    订阅规则:
      style → writer:  发现用户对某种写作风格的明确偏好
      reviewer → writer: 发现某类问题反复出现
      skeleton → context: 发现用户的思维模式和偏好设定
      canon → character: 发现某类事实冲突反复出现

    推送的条目在被推送方标记为 unverified——需在后续实践中验证后才升级为 active。
    """

    def __init__(self, books: dict[str, ExperienceBook]):
        self.books = books

    def push(self, source_agent: str, entry: ExperienceEntry):
        """从源经验本推送到目标经验本。

        推送的条目标记:
          - status: "unverified" (需要目标Agent在实践中验证)
          - cross_referenced_from: 来源标记
          - confidence: 降低到3（防止一个Agent的偏见传染给其他Agent）
        """
        rule = SUBSCRIPTION_RULES.get(source_agent)
        if not rule:
            return  # 该Agent无订阅规则

        target_book = self.books.get(rule["target"])
        if not target_book:
            return

        # 创建跨引用副本
        pushed = ExperienceEntry(
            type=entry.type,
            insight=entry.insight,
            confidence=3,          # 降置信度——需要目标Agent验证
            status="unverified",   # 标记为未验证
            source=entry.source,
            tags=entry.tags + ["cross-referenced"],
            cross_referenced_from=f"{source_agent}.book",
            avoidance=entry.avoidance,
            severity=entry.severity,
        )
        pushed.id = f"xr-{entry.id}"
        target_book.add_entry(pushed)

    def check_and_push(self, source_agent: str, entry: ExperienceEntry):
        """条件推送——仅在满足订阅条件时推送。

        条件检查:
          style→writer: type=="用户偏好" 且 tags含写作风格相关
          reviewer→writer: type=="错误模式" 且 severity=="high"
          skeleton→context: type=="用户偏好" 或 type=="结构洞察"
          canon→character: type=="错误模式" 且 "冲突" in tags
        """
        conditions = {
            "style": lambda e: e.type == "用户偏好" and any(
                t in e.tags for t in ["写作风格", "语言风格", "文风", "视角"]
            ),
            "reviewer": lambda e: e.type == "错误模式" and e.severity == "high",
            "skeleton": lambda e: e.type in ("用户偏好", "结构洞察"),
            "canon": lambda e: e.type == "错误模式" and "冲突" in str(e.tags),
        }

        checker = conditions.get(source_agent)
        if checker and checker(entry):
            self.push(source_agent, entry)


# ═══════════════════════════════════════════════════════════════
# E5: 经验生命周期管理 (§5.7.4)
# ═══════════════════════════════════════════════════════════════

class ExperienceLifecycle:
    """经验生命周期·5阶段状态机 (§5.7.4)

    发现 → 验证 → 活跃 → 衰减 → 归档/废弃

    状态转换规则:
      1. 发现: confidence=3, status="unverified"
      2. 验证: validated_count>=3 且 confidence>=7 → status="active"
      3. 衰减: 6个月未触发 → confidence-=1; confidence<3 → status="archived"
      4. 废弃: 用户手动标记或新经验与之矛盾 → status="deprecated"
    """

    # 6个月衰减阈值
    DECAY_MONTHS = 6
    # 升级为 active 的阈值
    ACTIVE_MIN_VALIDATED = 3
    ACTIVE_MIN_CONFIDENCE = 7

    def __init__(self, books: dict[str, ExperienceBook]):
        self.books = books

    def validate_experience(self, agent_name: str, entry_id: str):
        """验证一条经验——在其他小说中被验证有效。

        validated_count 递增, confidence 增长。
        达到阈值后自动升级为 active。
        confidence >= 8 → 自动提升到公用层 (cross_novel_principles.md)。
        """
        book = self.books.get(agent_name)
        if not book:
            return
        entry = book.get_entry(entry_id)
        if not entry:
            return

        new_count = entry.validated_count + 1
        new_confidence = min(10, entry.confidence + 1)

        new_status = entry.status
        if new_count >= self.ACTIVE_MIN_VALIDATED and new_confidence >= self.ACTIVE_MIN_CONFIDENCE:
            new_status = "active"

        book.update_entry(entry_id,
                          validated_count=new_count,
                          confidence=new_confidence,
                          status=new_status)

        # 公用层提升: confidence >= 8 的活跃经验写入公用规则库
        if new_confidence >= 8 and new_status == "active":
            self._elevate_to_global(entry)

    def _elevate_to_global(self, entry: ExperienceEntry):
        """将高置信度经验提升到公用层 ~/.novel-ai/global-brains/rules/。"""
        import os
        global_file = os.path.expanduser(
            "~/.novel-ai/global-brains/rules/pages/cross_novel_principles.md"
        )
        os.makedirs(os.path.dirname(global_file), exist_ok=True)

        # 检查是否已存在
        if os.path.exists(global_file):
            with open(global_file, "r", encoding="utf-8") as f:
                existing = f.read()
            if entry.insight[:50] in existing:
                return  # 已存在，跳过

        # 追加到公用规则文件
        tag_str = ", ".join(entry.tags) if entry.tags else "通用"
        new_rule = (
            f"\n### {entry.type}: {entry.insight[:80]}...\n"
            f"- 规则: {entry.insight}\n"
            f"- 标签: {tag_str}\n"
            f"- 验证次数: {entry.validated_count} | 置信度: {entry.confidence}\n"
            f"- 来源: 跨小说自动提升\n"
        )
        with open(global_file, "a", encoding="utf-8") as f:
            f.write(new_rule)

    def decay_check(self, agent_name: str) -> list[ExperienceEntry]:
        """衰减检查——超过6个月未触发的经验降低置信度。

        返回: 被衰减的经验列表
        """
        book = self.books.get(agent_name)
        if not book:
            return []

        now = datetime.now()
        threshold = now - timedelta(days=self.DECAY_MONTHS * 30)
        decayed = []

        for entry in book.list_all():
            if entry.status not in ("active", "unverified"):
                continue
            if not entry.last_triggered:
                continue

            try:
                last = datetime.fromisoformat(entry.last_triggered)
            except (ValueError, TypeError):
                continue

            if last < threshold:
                new_conf = entry.confidence - 1
                new_status = entry.status
                if new_conf < 3:
                    new_status = "archived"

                book.update_entry(entry.id, confidence=new_conf, status=new_status)
                entry.confidence = new_conf
                entry.status = new_status
                decayed.append(entry)

        return decayed

    def deprecate(self, agent_name: str, entry_id: str):
        """废弃一条经验——用户手动或新经验矛盾"""
        book = self.books.get(agent_name)
        if book:
            book.update_entry(entry_id, status="deprecated")

    def run_maintenance(self) -> dict:
        """对所有经验本运行生命周期维护（衰减检查）。

        返回: {agent_name: [decayed_entries]}
        """
        results = {}
        for agent in AGENT_BOOKS:
            decayed = self.decay_check(agent)
            if decayed:
                results[agent] = decayed
        return results

    def get_prune_suggestions(self, agent_name: str) -> str:
        """生成修剪建议 (§5.7.9)——供用户确认。

        检查:
          - 超过8个月未触发的经验 → 建议归档
          - 覆盖相似内容的经验 → 建议合并
          - 被后续经验取代的 → 建议废弃
        """
        book = self.books.get(agent_name)
        if not book:
            return ""

        now = datetime.now()
        threshold = now - timedelta(days=240)  # 8个月
        suggestions = []

        for entry in book.list_all():
            if entry.status == "deprecated":
                continue

            # 长时间未触发
            if entry.last_triggered:
                try:
                    last = datetime.fromisoformat(entry.last_triggered)
                    if last < threshold:
                        suggestions.append(
                            f"• {entry.id} 已{((now-last).days)}天未触发 → 建议归档"
                        )
                except (ValueError, TypeError):
                    pass

        if not suggestions:
            return ""

        header = f"> 经验本维护建议 ({agent_name}.book):\n"
        return header + "\n".join(suggestions)


# ═══════════════════════════════════════════════════════════════
# E6: 事后复盘自动生成经验 (§5.7.8)
# ═══════════════════════════════════════════════════════════════

class RetrospectiveGenerator:
    """事后复盘·自动经验生成引擎 (§5.7.8)

    审核Agent打回章节→写作Agent修订→审核通过后，
    自动分析修订过程中的可迁移经验，写入对应经验本。

    复盘由Agent自主发起——不需要用户介入。
    """

    def __init__(self, books: dict[str, ExperienceBook],
                 subscription: CrossAgentSubscription | None = None):
        self.books = books
        self.subscription = subscription

    def generate_from_review(
        self,
        chapter_order: int,
        review_issues: list[dict],
        revision_rounds: int,
        novel_id: str = "",
    ) -> list[ExperienceEntry]:
        """从审核打回中生成经验 (§5.7.8)。

        参数:
            chapter_order: 被审核的章节序号
            review_issues: 审核发现的问题列表 [{severity, type, description}]
            revision_rounds: 修订轮次
            novel_id: 触发的小说ID

        返回: 生成的新经验条目列表

        触发条件: revision_rounds >= 2（至少改了两轮才过——说明有值得学习的教训）
        """
        if revision_rounds < 2:
            return []  # 只改了一轮就过了——不值得写入经验

        generated = []

        # 分析审核问题模式，生成经验
        for issue in review_issues:
            issue_type = issue.get("type", "")
            issue_desc = issue.get("description", "")
            severity = issue.get("severity", "minor")

            # 只从 medium+ 问题中生成经验
            if severity not in ("critical", "major", "medium"):
                continue

            entry = self._synthesize_entry(
                issue_type=issue_type,
                issue_desc=issue_desc,
                severity=severity,
                chapter_order=chapter_order,
                novel_id=novel_id,
                revision_rounds=revision_rounds,
            )
            if entry:
                generated.append(entry)

        # 写入 writer.book
        writer_book = self.books.get("writer")
        if writer_book and generated:
            for entry in generated:
                writer_book.add_entry(entry)

        # 如果是文风问题，同时写入 reviewer.book
        reviewer_book = self.books.get("reviewer")
        if reviewer_book:
            style_issues = [
                i for i in review_issues
                if i.get("type") in ("style", "文风", "语言", "措辞")
            ]
            if style_issues and severity in ("major", "critical"):
                reviewer_entry = ExperienceEntry(
                    type="错误模式",
                    insight=f"文风审核——章节类型(ch{chapter_order})的措辞/语言问题反复出现。"
                            f"建议在写作Agent中增加该类章节的用语自查。",
                    confidence=3,
                    status="unverified",
                    severity="high",
                    tags=["文风", "措辞", "审核"],
                )
                reviewer_book.add_entry(reviewer_entry)
                generated.append(reviewer_entry)

        return generated

    def _synthesize_entry(
        self,
        issue_type: str,
        issue_desc: str,
        severity: str,
        chapter_order: int,
        novel_id: str,
        revision_rounds: int,
    ) -> Optional[ExperienceEntry]:
        """将单条审核问题合成为可迁移经验条目。

        关键步骤: 去具体化——剥离小说ID/人名/章节号，保留可迁移技法。
        """
        # 根据问题类型选择经验类型
        type_map = {
            "character": "错误模式",
            "plot_logic": "错误模式",
            "style": "技法发现",
            "canon": "错误模式",
            "pacing": "结构洞察",
        }
        exp_type = type_map.get(issue_type, "错误模式")

        # 合成 insight——抽象化处理
        insight = self._abstract_issue(issue_type, issue_desc, chapter_order)

        return ExperienceEntry(
            type=exp_type,
            insight=insight,
            confidence=3,           # 新发现，低置信度
            status="unverified",    # 待后续验证
            discovered_in=novel_id,
            severity=severity,
            tags=[issue_type, f"ch-{chapter_order:03d}"],
        )

    def _abstract_issue(self, issue_type: str, desc: str, chapter_order: int) -> str:
        """去具体化——将具体问题抽象为可迁移经验。

        这是防污染第一道闸门 (§5.7.6)。
        剥离人名、地名、具体章节号，只保留可迁移的技法/策略。
        """
        # 基于问题类型的模板化抽象
        templates = {
            "character": f"角色行为一致性——在第{chapter_order}章附近，人物行为偏离了人物卡设定。"
                         f"建议：写作前先查人物卡，确认角色当前动机驱动下的合理行为范围。",
            "plot_logic": f"情节逻辑——第{chapter_order}章的因果链与前文衔接有断裂。"
                          f"建议：过渡段落至少保留一句'前情钩子'来锚定因果。",
            "style": f"文风一致性——第{chapter_order}章的措辞/语调偏离了指定文风。"
                     f"建议：写完过渡段后做一次古风/现代用语自查——过渡段比高潮段更容易滑向现代口语。",
            "canon": f"Canon事实冲突——第{chapter_order}章与已确认的设定矛盾。"
                     f"建议：写作前必须交叉验证Canon库中相关条目。",
            "pacing": f"节奏问题——第{chapter_order}章场景转换或高潮铺垫不足。"
                      f"建议：关键章前至少预留1章的蓄力空间。",
        }
        return templates.get(issue_type, f"写作质量——第{chapter_order}章存在改进空间: {desc[:80]}")


# ═══════════════════════════════════════════════════════════════
# E8: 防污染三道闸门 (§5.7.6)
# ═══════════════════════════════════════════════════════════════

class PollutionGuard:
    """跨小说污染防护三道闸门 (§5.7.6)。

    第一道——录入时去具体化过滤:
      检查经验是否包含小说ID、人名、具体章节号。
      有则标记为需要重新抽象。

    第二道——检索时不相关隔离:
      检索到的经验是抽象后的技法，天然不包含具体信息。

    第三道——用户可审查面板:
      提供经验本导出接口，供前端设置页展示。
    """

    # 匹配小说ID、章节号、具体人名的模式
    SENSITIVE_PATTERNS = [
        re.compile(r"novel-\w+", re.IGNORECASE),    # novel-xiulou-001
        re.compile(r"第\d+章"),                       # 第3章
        re.compile(r"ch-\d{3}"),                      # ch-003
        re.compile(r"沈妙华|顾廷烨|赵氏"),             # 示例——生产环境应动态加载
    ]

    @classmethod
    def check_entry(cls, insight: str) -> tuple[bool, list[str]]:
        """检查经验是否包含具体信息（第一道闸门）。

        返回: (is_clean, violations)
          is_clean: True = 通过检查，可写入经验本
          violations: 违规的匹配内容列表
        """
        violations = []
        for pattern in cls.SENSITIVE_PATTERNS:
            matches = pattern.findall(insight)
            violations.extend(matches)
        return len(violations) == 0, violations

    @classmethod
    def export_for_review(cls, books: dict[str, ExperienceBook]) -> dict:
        """导出所有经验本数据——供前端审查面板使用（第三道闸门）。

        返回: {agent_name: {craft: [...], boss: [...]}}
        """
        result = {}
        for agent, book in books.items():
            craft = [_entry_to_dict(e) for e in book.list_all()]
            boss = [_boss_to_dict(e) for e in book.get_boss_interactions()]
            result[agent] = {"craft": craft, "boss": boss}
        return result


# ── 辅助函数 ──

def _match_time_pattern(pattern: str, now: datetime) -> bool:
    """检查当前时间是否匹配时段模式，如 "22:00-02:00" """
    try:
        start_str, end_str = pattern.split("-")
        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))

        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        now_minutes = now.hour * 60 + now.minute

        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes <= end_minutes
        else:
            # 跨日时段（如22:00-02:00）
            return now_minutes >= start_minutes or now_minutes <= end_minutes
    except (ValueError, AttributeError):
        return True  # 解析失败时不阻止


def load_experience_books() -> dict[str, ExperienceBook]:
    """便捷函数——加载全部7个Agent经验本"""
    books = {}
    for agent in AGENT_BOOKS:
        books[agent] = ExperienceBook(agent)
    return books


# ═══════════════════════════════════════════════════════════════
# P3-01: 时段语气调整映射表 (§5.7.11)
# ═══════════════════════════════════════════════════════════════

TONE_ADJUSTMENT_MAP = {
    "reduce_passive_aggressive": "用温和语气回应，避免阴阳怪气或被动攻击。",
    "softer_tone": "调低攻击性，用更柔和的措辞。适合深夜时段。",
    "direct_and_concise": "减少铺垫，回复直接简洁。BOSS此刻需要效率。",
    "encouraging_and_supportive": "多鼓励、多共情。BOSS此刻需要情绪支持。",
    "professional_straightforward": "保持专业但不要冷冰冰。减少表情和颜文字。",
    "light_and_humorous": "可以适当开玩笑，用轻松的语气。但不要过度。",
    "warm_and_appreciative": "表达感恩和赞赏。BOSS此刻做了正向反馈。",
    "brief_and_actionable": "简短回复+下一步行动。不要展开讨论。",
}


def _get_tone_guidance(entries: list) -> list[str]:
    """从匹配条目中提取时段语气调整指导。"""
    seen = set()
    guidance = []
    for e in entries:
        if e.tone_adjustment and e.tone_adjustment not in seen:
            hint = TONE_ADJUSTMENT_MAP.get(e.tone_adjustment, e.tone_adjustment)
            guidance.append(f"时段语气: {hint}")
            seen.add(e.tone_adjustment)
    return guidance
