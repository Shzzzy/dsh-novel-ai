"""
P1-07: 伏笔冷却期检测 — Foreshadowing Cooldown Tracker

DESIGN_DOC §6.3.2:
  - 物品伏笔: 30-50 章冷却期，冷却期内回收 → 标记"读者还没忘"
  - 身份伏笔: 80-120 章冷却期，中间需 ≥2 次暗示 → 否则"揭露太突兀"
  - 意象伏笔: 贯穿全书，每次出现需深化 → 否则"意象未深化"
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── 配置 ──

@dataclass
class CooldownConfig:
    """冷却期配置。阈值可被 novel.targetWords 按比例缩放。"""
    # 最小冷却期（章）
    item_min_chapters: int = 30
    item_max_chapters: int = 50
    identity_min_chapters: int = 80
    identity_max_chapters: int = 120
    # 身份伏笔至少需要的暗示次数
    identity_min_hints: int = 2
    # 意象伏笔：每次出现至少增加的"含义层次"
    imagery_min_depth_increment: int = 1


# ── 跟踪器状态模型 ──

@dataclass
class ForeshadowingTrace:
    """一条伏笔的完整跟踪信息"""
    id: str
    description: str
    cooldown_type: str  # "item" | "identity" | "imagery"
    # 章节位置
    planted_chapter: int
    hint_chapters: list[int] = field(default_factory=list)
    revealed_chapter: int | None = None
    # 含义层次（仅意象）
    depth_levels: list[str] = field(default_factory=list)


@dataclass
class CooldownAlert:
    """冷却期违规告警"""
    level: str  # "warning" | "info"
    foreshadowing_id: str
    foreshadowing_description: str
    cooldown_type: str
    message: str
    planted_chapter: int
    revealed_chapter: int | None = None
    hint_count: int = 0
    required_hints: int = 0


# ── 跟踪器 ──

class ForeshadowingTracker:
    """跨事件追踪伏笔生命周期，检测冷却期违规。

    用法:
        tracker = ForeshadowingTracker(config)
        tracker.register_plant("fs-1", "伞面花纹", "item", chapter=1)
        tracker.register_hint("fs-1", chapter=3)
        tracker.register_reveal("fs-1", chapter=5)
        alerts = tracker.check_all(current_chapter=5)
    """

    def __init__(self, config: CooldownConfig | None = None):
        self.config = config or CooldownConfig()
        self._items: dict[str, ForeshadowingTrace] = {}

    # ── 注册 ──

    def register_plant(
        self,
        fs_id: str,
        description: str,
        cooldown_type: str = "item",
        chapter: int = 1,
    ) -> None:
        """注册一条伏笔的埋设"""
        if fs_id in self._items:
            logger.warning("伏笔 %s 已存在，跳过重复埋设", fs_id)
            return
        self._items[fs_id] = ForeshadowingTrace(
            id=fs_id,
            description=description,
            cooldown_type=cooldown_type,
            planted_chapter=chapter,
        )

    def register_hint(self, fs_id: str, chapter: int, depth_desc: str = "") -> None:
        """注册一次暗示/触碰"""
        trace = self._items.get(fs_id)
        if not trace:
            logger.warning("伏笔 %s 未埋设，无法注册暗示", fs_id)
            return
        if chapter not in trace.hint_chapters:
            trace.hint_chapters.append(chapter)
        if depth_desc and trace.cooldown_type == "imagery":
            trace.depth_levels.append(depth_desc)

    def register_reveal(self, fs_id: str, chapter: int) -> None:
        """注册回收"""
        trace = self._items.get(fs_id)
        if not trace:
            logger.warning("伏笔 %s 未埋设，无法注册回收", fs_id)
            return
        trace.revealed_chapter = chapter

    # ── 检测 ──

    def check_all(self, current_chapter: int) -> list[CooldownAlert]:
        """检查所有伏笔的冷却期违规"""
        alerts: list[CooldownAlert] = []
        for trace in self._items.values():
            alerts.extend(self._check_one(trace, current_chapter))
        return alerts

    def _check_one(self, trace: ForeshadowingTrace, current_chapter: int) -> list[CooldownAlert]:
        """检查单条伏笔"""
        alerts: list[CooldownAlert] = []
        cfg = self.config

        if trace.cooldown_type == "item":
            alerts.extend(self._check_item_cooldown(trace, current_chapter, cfg))
        elif trace.cooldown_type == "identity":
            alerts.extend(self._check_identity_cooldown(trace, current_chapter, cfg))
        elif trace.cooldown_type == "imagery":
            alerts.extend(self._check_imagery_depth(trace, current_chapter, cfg))

        return alerts

    def _check_item_cooldown(
        self, trace: ForeshadowingTrace, current_chapter: int, cfg: CooldownConfig
    ) -> list[CooldownAlert]:
        """物品伏笔：冷却期内回收 → 警告"""
        if trace.revealed_chapter is None:
            return []  # 尚未回收，不告警

        gap = trace.revealed_chapter - trace.planted_chapter
        if gap < cfg.item_min_chapters:
            return [CooldownAlert(
                level="warning",
                foreshadowing_id=trace.id,
                foreshadowing_description=trace.description,
                cooldown_type="item",
                planted_chapter=trace.planted_chapter,
                revealed_chapter=trace.revealed_chapter,
                message=(
                    f"物品伏笔「{trace.description}」冷却不足——埋设于第{trace.planted_chapter}章，"
                    f"回收于第{trace.revealed_chapter}章，仅隔{gap}章。"
                    f"建议至少间隔{cfg.item_min_chapters}章。读者还没忘呢！"
                ),
            )]
        return []

    def _check_identity_cooldown(
        self, trace: ForeshadowingTrace, current_chapter: int, cfg: CooldownConfig
    ) -> list[CooldownAlert]:
        """身份伏笔：冷却期 + 暗示次数"""
        alerts: list[CooldownAlert] = []

        if trace.revealed_chapter is not None:
            gap = trace.revealed_chapter - trace.planted_chapter
            if gap < cfg.identity_min_chapters:
                alerts.append(CooldownAlert(
                    level="warning",
                    foreshadowing_id=trace.id,
                    foreshadowing_description=trace.description,
                    cooldown_type="identity",
                    planted_chapter=trace.planted_chapter,
                    revealed_chapter=trace.revealed_chapter,
                    message=(
                        f"身份伏笔「{trace.description}」冷却不足——埋设于第{trace.planted_chapter}章，"
                        f"回收于第{trace.revealed_chapter}章，仅隔{gap}章。"
                        f"建议至少间隔{cfg.identity_min_chapters}章。"
                    ),
                ))

        # 暗示次数检查
        hint_count = len(trace.hint_chapters)
        if trace.revealed_chapter is not None and hint_count < cfg.identity_min_hints:
            alerts.append(CooldownAlert(
                level="warning",
                foreshadowing_id=trace.id,
                foreshadowing_description=trace.description,
                cooldown_type="identity",
                planted_chapter=trace.planted_chapter,
                revealed_chapter=trace.revealed_chapter,
                hint_count=hint_count,
                required_hints=cfg.identity_min_hints,
                message=(
                    f"身份伏笔「{trace.description}」暗示不足——"
                    f"目前仅有{hint_count}次暗示，至少需要{cfg.identity_min_hints}次。"
                    f"直接揭露会显得突兀。"
                ),
            ))

        return alerts

    def _check_imagery_depth(
        self, trace: ForeshadowingTrace, current_chapter: int, cfg: CooldownConfig
    ) -> list[CooldownAlert]:
        """意象伏笔：每次出现需深化"""
        # 简单规则：如果多次出现但深度描述无变化
        if len(trace.depth_levels) >= 2:
            # 检查最后两次是否有进展
            unique_depths = set(trace.depth_levels)
            if len(unique_depths) == 1 and len(trace.depth_levels) >= 2:
                return [CooldownAlert(
                    level="info",
                    foreshadowing_id=trace.id,
                    foreshadowing_description=trace.description,
                    cooldown_type="imagery",
                    planted_chapter=trace.planted_chapter,
                    message=(
                        f"意象伏笔「{trace.description}」已出现{len(trace.depth_levels)}次，"
                        f"但含义未深化。每次出现应对意象赋予新的层次。"
                    ),
                )]
        return []

    # ── 摘要 ──

    def summary(self) -> dict:
        """返回所有伏笔的状态摘要，供 Context Agent 嵌入上下文"""
        items = []
        for t in self._items.values():
            items.append({
                "id": t.id,
                "description": t.description,
                "type": t.cooldown_type,
                "planted": t.planted_chapter,
                "hinted_at": t.hint_chapters,
                "revealed_at": t.revealed_chapter,
                "depth_levels": len(t.depth_levels),
            })
        return {
            "total": len(self._items),
            "resolved": sum(1 for t in self._items.values() if t.revealed_chapter is not None),
            "pending": sum(1 for t in self._items.values() if t.revealed_chapter is None),
            "items": items,
        }

    def from_events(self, events: list[dict], current_chapter: int) -> list[CooldownAlert]:
        """从事件列表批量注册，然后返回告警。

        events 格式 (eventStore event):
        {
            id: str, order: int, title: str,
            chapterRange: { start: int, end: int },
            foreshadowing: [{ id: str, action: "plant"|"progress"|"reveal", description: str }]
        }
        """
        self._items.clear()

        for evt in sorted(events, key=lambda e: e.get("order", 0)):
            ch = evt.get("chapterRange", {}).get("start", evt.get("order", 1))
            for fs in evt.get("foreshadowing", []):
                action = fs.get("action", "")
                desc = fs.get("description", fs.get("id", ""))
                fs_id = fs.get("id", desc)
                # 推断伏笔类型
                fs_type = self._infer_type(desc)

                if action == "plant":
                    self.register_plant(fs_id, desc, fs_type, ch)
                elif action == "progress":
                    self.register_hint(fs_id, ch, desc)
                elif action == "reveal":
                    self.register_reveal(fs_id, ch)

        return self.check_all(current_chapter)

    @staticmethod
    def _infer_type(description: str) -> str:
        """从描述推断伏笔类型（启发式）"""
        keywords_identity = {"身世", "身份", "血缘", "出身", "父亲", "母亲", "父母",
                            "亲生", "秘密", "来历", "前世", "转世", "真实身份"}
        keywords_imagery = {"花", "月", "雨", "雪", "灯", "风", "星", "云", "水",
                           "桂花", "梅花", "桃花", "月亮", "灯笼", "伞", "镜子", "窗"}
        for kw in keywords_identity:
            if kw in description:
                return "identity"
        for kw in keywords_imagery:
            if kw in description:
                return "imagery"
        return "item"
