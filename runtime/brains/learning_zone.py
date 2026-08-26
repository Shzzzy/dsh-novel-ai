"""AI 创作学习区 — DESIGN_DOC §15

核心职责:
  存储用户上传的写作参考资料（技巧、世界观、范文等）
  支持分类筛选、关键词搜索、统计
  AI 管线集成：写作时检索相关条目作为参考上下文

数据结构 (§15.2):
  LearningMaterial: id, title, category, content, wordCount, created_at, updated_at
  7 大分类: 写作技巧, 世界观参考, 人物塑造, 情节结构, 文风参考, 对话技巧, 其他

功能 (§15.3):
  - 增删改查
  - 分类筛选
  - 关键词搜索（标题 + 正文）
  - 数据统计（条目数 + 总字数）
  - AI 集成：检索相关条目作为写作上下文
"""

import json
import os
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 7 大分类 (§15.2) ──
CATEGORIES = [
    "写作技巧",
    "世界观参考",
    "人物塑造",
    "情节结构",
    "文风参考",
    "对话技巧",
    "其他",
]

# 存储路径
LEARNING_DIR = Path.home() / ".novel-ai" / "learning"
MATERIALS_FILE = LEARNING_DIR / "materials.json"


@dataclass
class LearningMaterial:
    """学习资料条目 (§15.2)"""
    id: str
    title: str
    category: str  # 必须是 CATEGORIES 之一
    content: str
    word_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "content": self.content,
            "wordCount": self.word_count,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LearningMaterial":
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            category=d.get("category", "其他"),
            content=d.get("content", ""),
            word_count=d.get("wordCount", 0),
            created_at=d.get("createdAt", ""),
            updated_at=d.get("updatedAt", ""),
        )


class LearningZone:
    """AI 创作学习区 — 全局单例，存储所有学习资料"""

    def __init__(self):
        self._materials: dict[str, LearningMaterial] = {}
        self._ensure_store()

    # ── 存储层 ──

    def _ensure_store(self):
        """初始化 JSON 存储文件"""
        LEARNING_DIR.mkdir(parents=True, exist_ok=True)
        if not MATERIALS_FILE.exists():
            MATERIALS_FILE.write_text("[]", encoding="utf-8")
            logger.info(f"学习区存储已初始化: {MATERIALS_FILE}")

    def _load(self) -> list[dict]:
        """从 JSON 文件加载所有条目"""
        try:
            if MATERIALS_FILE.exists():
                data = json.loads(MATERIALS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"学习区存储读取失败: {e}")
        return []

    def _save(self, materials: list[dict]):
        """保存到 JSON 文件"""
        try:
            MATERIALS_FILE.write_text(
                json.dumps(materials, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"学习区存储写入失败: {e}")
            raise

    def _refresh_cache(self):
        """从文件重新加载缓存"""
        data = self._load()
        self._materials = {m["id"]: LearningMaterial.from_dict(m) for m in data}

    # ── CRUD ──

    def add_material(self, title: str, category: str, content: str) -> LearningMaterial:
        """新增学习资料"""
        if category not in CATEGORIES:
            category = "其他"

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        word_count = len(content.replace(" ", "").replace("\n", ""))

        material = LearningMaterial(
            id=str(uuid.uuid4())[:12],
            title=title.strip(),
            category=category,
            content=content,
            word_count=word_count,
            created_at=now,
            updated_at=now,
        )

        data = self._load()
        data.append(material.to_dict())
        self._save(data)
        self._materials[material.id] = material

        logger.info(f"学习区新增: [{category}] {title} ({word_count}字)")
        return material

    def get_material(self, material_id: str) -> Optional[LearningMaterial]:
        """获取单条资料"""
        self._refresh_cache()
        return self._materials.get(material_id)

    def list_materials(
        self, category: Optional[str] = None, q: Optional[str] = None
    ) -> list[LearningMaterial]:
        """列出资料（支持分类筛选 + 关键词搜索）"""
        self._refresh_cache()
        materials = list(self._materials.values())

        if category and category != "全部":
            materials = [m for m in materials if m.category == category]

        if q:
            q_lower = q.lower()
            materials = [
                m
                for m in materials
                if q_lower in m.title.lower() or q_lower in m.content.lower()
            ]

        # 按更新时间倒序
        materials.sort(key=lambda m: m.updated_at, reverse=True)
        return materials

    def update_material(
        self, material_id: str, title: Optional[str] = None,
        category: Optional[str] = None, content: Optional[str] = None,
    ) -> Optional[LearningMaterial]:
        """更新资料"""
        self._refresh_cache()
        if material_id not in self._materials:
            return None

        material = self._materials[material_id]
        if title is not None:
            material.title = title.strip()
        if category is not None and category in CATEGORIES:
            material.category = category
        if content is not None:
            material.content = content
            material.word_count = len(content.replace(" ", "").replace("\n", ""))
        material.updated_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # 全量写回
        data = self._load()
        for i, d in enumerate(data):
            if d["id"] == material_id:
                data[i] = material.to_dict()
                break
        self._save(data)

        logger.info(f"学习区更新: [{material.category}] {material.title}")
        return material

    def delete_material(self, material_id: str) -> bool:
        """删除资料"""
        self._refresh_cache()
        if material_id not in self._materials:
            return False

        data = self._load()
        data = [d for d in data if d["id"] != material_id]
        self._save(data)
        del self._materials[material_id]

        logger.info(f"学习区删除: {material_id}")
        return True

    # ── 统计 ──

    def get_stats(self) -> dict:
        """获取统计信息：总数、总字数、分类分布"""
        self._refresh_cache()
        materials = list(self._materials.values())

        total = len(materials)
        total_words = sum(m.word_count for m in materials)

        by_category = {}
        for cat in CATEGORIES:
            cat_mats = [m for m in materials if m.category == cat]
            by_category[cat] = {
                "count": len(cat_mats),
                "words": sum(m.word_count for m in cat_mats),
            }

        return {
            "total": total,
            "totalWords": total_words,
            "byCategory": by_category,
        }

    # ── AI 集成 (§15.3) ──

    def retrieve_context(self, query: str, limit: int = 5) -> list[LearningMaterial]:
        """为 AI 管线检索相关学习资料

        基于关键词匹配度排序，返回最相关的 N 条。
        用于在写作 Agent 生成时，将相关资料注入 system prompt。
        """
        self._refresh_cache()
        if not query or not self._materials:
            return []

        query_lower = query.lower()
        scored: list[tuple[int, LearningMaterial]] = []

        for m in self._materials.values():
            score = 0
            # 标题匹配权重更高
            title_lower = m.title.lower()
            content_lower = m.content.lower()

            if query_lower in title_lower:
                score += 10
            if query_lower in content_lower:
                score += 1

            # 关键词分词匹配
            for kw in query_lower.split():
                if len(kw) >= 2:  # 忽略单字
                    if kw in title_lower:
                        score += 3
                    if kw in content_lower:
                        score += 1

            if score > 0:
                scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]


# ── 全局单例 ──
_learning_zone: Optional[LearningZone] = None


def get_learning_zone() -> LearningZone:
    """获取全局学习区实例"""
    global _learning_zone
    if _learning_zone is None:
        _learning_zone = LearningZone()
    return _learning_zone
