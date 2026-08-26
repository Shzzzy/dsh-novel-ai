"""核心数据模型 —— 小说、卷、事件、情节、章节、上下文包"""

import uuid
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> str:
    return datetime.now().isoformat()


@dataclass
class Volume:
    """卷 — 长篇小说分卷 (§2.1)，可选"""
    id: str = field(default_factory=new_id)
    novel_id: str = ""
    order: int = 0
    title: str = ""
    description: str = ""
    event_ids: list[str] = field(default_factory=list)   # 该卷包含的事件ID列表


@dataclass
class Novel:
    """小说 — 顶层创作单元 (§2.1)"""
    id: str = field(default_factory=new_id)
    title: str = "未命名"
    summary: str = ""
    style_template_id: str = ""
    target_words: int = 600000
    current_words: int = 0
    status: str = "draft"     # draft | writing | completed | paused
    volumes: list[Volume] = field(default_factory=list)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)


@dataclass
class Event:
    id: str = field(default_factory=new_id)
    novel_id: str = ""
    order: int = 0
    title: str = ""
    description: str = ""
    type: str = "key"           # key | transition | functional
    locked: bool = False
    chapter_range_start: int = 0
    chapter_range_end: int = 0


@dataclass
class Plot:
    id: str = field(default_factory=new_id)
    event_id: str = ""
    novel_id: str = ""
    order: int = 0
    title: str = ""
    description: str = ""
    locked: bool = False


@dataclass
class Chapter:
    id: str = field(default_factory=new_id)
    novel_id: str = ""
    plot_id: str = ""
    order: int = 0
    title: str = ""
    content: str = ""
    word_count: int = 0
    status: str = "idle"
    review_type: str = "quick"


@dataclass
class ContextPackage:
    """上下文 Agent 输出——写作 Agent 的输入"""
    chapter_direction: str = ""
    tone: str = ""
    key_beats: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    character_states: dict = field(default_factory=dict)
    foreshadowing_to_progress: list[str] = field(default_factory=list)
    canon_facts_to_reference: list[str] = field(default_factory=list)
    cooldown_alerts: list[dict] = field(default_factory=list)  # P1-07: 冷却期告警


@dataclass
class ReviewReport:
    """审核 Agent 输出"""
    chapter_id: str = ""
    overall_score: float = 0.0
    needs_revision: bool = False
    issues: list[dict] = field(default_factory=list)
    revision_notes: str = ""
    character_changes: list[dict] = field(default_factory=list)
    new_facts: list[dict] = field(default_factory=list)


@dataclass
class Personality:
    """性格与行为 (§2.2)"""
    traits: list[str] = field(default_factory=list)       # ["勇敢", "冲动", "重情义"]
    mbti: Optional[str] = None
    speechStyle: str = ""                                  # "沉默寡言，每句不超过15字"
    habits: list[str] = field(default_factory=list)        # 习惯动作
    fears: list[str] = field(default_factory=list)         # 恐惧
    desires: list[str] = field(default_factory=list)       # 核心欲望


@dataclass
class KeyPastEvent:
    """关键过往事件 (§2.2)"""
    description: str = ""
    impact: str = ""           # 对性格/行为的影响


@dataclass
class Background:
    """角色背景 (§2.2)"""
    origin: str = ""           # 出身
    familyHistory: str = ""    # 家族背景
    keyPastEvents: list[KeyPastEvent] = field(default_factory=list)


@dataclass
class Ability:
    """角色能力 (§2.2)"""
    name: str = ""
    level: str = ""            # 当前掌握程度
    acquiredAt: str = ""       # 在哪个情节/章节中获得
    limitations: str = ""


@dataclass
class RelationshipEvolution:
    """关系演变节点 (§2.2)"""
    chapterId: str = ""
    description: str = ""


@dataclass
class Relationship:
    """人际关系 (§2.2)"""
    targetCharacterId: str = ""
    type: str = "friend"       # family | friend | romantic | rival | enemy | master_student
    intimacy: int = 0          # -100 到 100
    evolution: list[RelationshipEvolution] = field(default_factory=list)


@dataclass
class CharacterArc:
    """角色弧线 (§2.2)"""
    startingState: str = ""    # 初始状态
    currentState: str = ""     # 当前状态（动态更新）
    targetState: str = ""      # 目标终点
    progress: int = 0          # 0-100


@dataclass
class CharacterCard:
    """人物卡 — 对齐 DESIGN_DOC §2.2 完整接口"""
    id: str = field(default_factory=new_id)
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    role: str = "supporting"   # protagonist | antagonist | supporting | cameo
    status: str = "active"     # active | deceased | departed | unknown
    gender: str = ""
    age: dict = field(default_factory=lambda: {"initial": 0, "current": 0})
    appearance: str = ""
    personality: Personality = field(default_factory=Personality)
    background: Background = field(default_factory=Background)
    abilities: list[Ability] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    arc: CharacterArc = field(default_factory=CharacterArc)
    firstAppearance: str = ""  # 情节/章节 ID
    chapterAppearances: list[str] = field(default_factory=list)
    lastUpdated: str = ""
    generatedBy: str = ""      # 人物卡 Agent 版本


class CanonStatus:
    """Canon 事实状态枚举"""
    CANON = "canon"
    SOFT_CANON = "soft_canon"
    SPECULATIVE = "speculative"


@dataclass
class CanonEntry:
    """Canon 事实条目"""
    id: str = field(default_factory=new_id)
    type: str = "character_fact"
    fact: str = ""
    status: str = CanonStatus.CANON
    conflicts: list[str] = field(default_factory=list)



@dataclass
class WorldEntry:
    """世界观条目"""
    id: str = field(default_factory=new_id)
    category: str = "power_system"
    title: str = ""
    content: str = ""

@dataclass
class ForeshadowingItem:
    """伏笔追踪项 — P2-4 冷却期检测"""
    id: str = field(default_factory=new_id)
    description: str = ""
    planted_chapter: int = 0
    expected_reveal_chapter: int = 0
    status: str = "planted"  # planted | in_progress | resolved
    cooldown_type: str = "item"  # item | identity | imagery
    MIN_COOLDOWN: dict = field(default_factory=lambda: {"item": 30, "identity": 80, "imagery": 0})
    def cooldown_ok(self) -> bool:
        if self.status != "resolved": return True
        gap = self.expected_reveal_chapter - self.planted_chapter
        return gap >= self.MIN_COOLDOWN.get(self.cooldown_type, 30)


@dataclass
class StyleTemplate:
    """文风模板"""
    id: str = field(default_factory=new_id)
    name: str = ""
    tags: list[str] = field(default_factory=list)
    stylePrompt: str = ""
    parameters: dict = field(default_factory=dict)
