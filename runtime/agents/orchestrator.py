"""Main orchestrator — coordinates all agents for chapter generation.

v2 enhancements (Phase 1b):
- ProviderPool for tiered model selection per agent role
- PipelineStateManager integration for durable execution
- DAG-aware task launching (context + character retrieval in parallel)
- Review tier routing (full/quick/skip per DESIGN_DOC §7.3.1)
- Checkpoint recording after each phase
"""

import logging
from typing import AsyncIterator, Optional

from agents.base import BaseAgent, AgentConfig
from agents.provider_pool import ProviderPool
from models.novel import Chapter, Plot, Event, ContextPackage
from sync.sync_layer import SyncLayer

logger = logging.getLogger(__name__)

# E3: 经验注入管道 (§5.7.5)
try:
    from brains.experience_book import (
        ExperienceInjector, TaskFeatures, ExperienceEntry,
        RetrospectiveGenerator, CrossAgentSubscription,
        init_all_experience_books,
    )
    _EXP_BOOK_AVAILABLE = True
except ImportError:
    _EXP_BOOK_AVAILABLE = False

# 学习区集成 (§15.3)
try:
    from brains.learning_zone import get_learning_zone as _get_lz
    _LEARNING_ZONE_AVAILABLE = True
except ImportError:
    _LEARNING_ZONE_AVAILABLE = False

    def _get_lz():
        """降级：学习区不可用时返回 None"""
        return None

get_learning_zone = _get_lz

MAX_REVISION_ROUNDS = 3

# 全局经验本实例（所有编排器共享）
_experience_books: dict = {}
_experience_injector: Optional[ExperienceInjector] = None
_retrospective_generator: Optional[RetrospectiveGenerator] = None
_cross_subscription: Optional[CrossAgentSubscription] = None


async def _generate_contrast_example(
    chapter_excerpt: str, chapter_order: int, config,
) -> dict | None:
    """生成对比学习示例: 你写的 vs 更好的写法。

    返回: {"safe_pattern": "...", "rewrite": "...", "why_better": "..."}
    """
    prompt = f"""你是写作教练。下面是一段AI生成的第{chapter_order}章小说段落。请做两件事:

1. 从这段文字中找出1个"安全写法"——主角在关键场景中没有承担风险、或结尾用了感悟式总结、或配角功能化
2. 改写这一段，让主角付出物理代价、或做出危险选择、或结尾让读者心跳加速

## 原文
{chapter_excerpt[:600]}

## 要求
- 改写后必须让读者"今晚睡不着"
- 改写后必须删除所有感悟式结尾（如"水很深""只是开头"）
- 改写后必须有一个角色展现真实的私心或恐惧

请输出JSON(不要markdown代码块):
{{"safe_pattern": "原文中的安全模式(一句话, 不超50字)", "rewrite": "改写后的段落(150-300字)", "why_better": "为什么改写后更好(一句话, 不超50字)"}}"""

    try:
        from agents.base import SimpleAgent
        agent = SimpleAgent(config)
        response = await agent.generate(prompt)
        import json, re
        # 清洗 markdown 代码块
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception:
        return None


async def _extract_positive_principle(
    chapter_excerpt: str, chapter_order: int, config,
) -> str | None:
    """从本章中提取1条'做得好'的技法——正面强化。"""
    prompt = f"""你是写作技法发现专家。请从以下第{chapter_order}章的正文片段中，找出1个"写得好的技法"。

## 提取规则
1. 不要评价内容好不好——找出具体的写作技法（如: 用动作替代情绪、对话潜台词、细节刻画人物）
2. 原则必须可迁移——能搬到另一部完全不同的小说使用
3. 不能包含任何具体人名、地名、情节
4. 50-150字

## 正文片段
{chapter_excerpt[:600]}

请输出一条写作技法（纯文本）:"""

    try:
        from agents.base import SimpleAgent
        agent = SimpleAgent(config)
        response = await agent.generate(prompt)
        principle = response.strip()[:250]
        if len(principle) < 20:
            return None
        return principle
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# P2-11: 章节版本历史 — 管线自动保存
# ═══════════════════════════════════════════════════════════════

def _auto_save_version(chapter, ch_id: str) -> None:
    """管线完成时自动保存章节版本快照。不影响管线正常流程。"""
    try:
        from brains.chapter_version_store import auto_save_chapter_version
        novel_id = getattr(chapter, 'novel_id', '') or 'novel-unknown'
        auto_save_chapter_version(
            novel_id=novel_id,
            chapter_id=ch_id,
            content=chapter.content or "",
            word_count=chapter.word_count or 0,
            title=chapter.title or "",
            source="pipeline",
        )
    except Exception:
        pass  # 版本保存失败不阻塞管线


async def _extract_writing_principle(
    review_issues: list[dict],
    chapter_excerpt: str,
    chapter_order: int,
    config,
) -> str | None:
    """从审核反馈中提炼可迁移的写作原则——不存句子，存技法。

    输入: Reviewer的审核意见 + 本章正文片段
    输出: 一条50-200字的可迁移写作原则 (不含人名/地名/情节)
    """
    if not review_issues or not chapter_excerpt:
        return None

    issues_text = "\n".join(
        f"- [{i.get('type','?')}] {i.get('description','')[:200]}"
        for i in review_issues[:3]
    )

    prompt = f"""你是写作技法提炼专家。你的任务是把"具体写作问题"提炼成"可迁移的写作原则"。

## 提炼规则
1. 绝对不能包含任何具体人名、地名、情节细节。原则必须能搬到另一部完全不同的小说直接使用。
2. 绝对不能包含原句。不是"不要写咸香入味"——而是"紧张场景写角色对食物的犹豫，不写食物味道"。
3. 每条原则必须回答: "下一次写类似的场景，我应该怎么做？"
4. 原则必须简短、具体、可执行。50-200字。

## 审核反馈
{issues_text}

## 第{chapter_order}章正文片段
{chapter_excerpt[:500]}

请输出一条写作原则(纯文本，不要JSON，不要markdown):"""

    try:
        from agents.base import SimpleAgent
        agent = SimpleAgent(config)
        response = await agent.generate(prompt)
        principle = response.strip()[:300]
        # 过滤掉明显无效的输出
        if len(principle) < 30 or "抱歉" in principle or "无法" in principle:
            return None
        return principle
    except Exception:
        return None


def _init_experience_books():
    """延迟初始化经验本（首次使用时）"""
    global _experience_books, _experience_injector, _retrospective_generator, _cross_subscription
    if _experience_books or not _EXP_BOOK_AVAILABLE:
        return
    _experience_books = init_all_experience_books()
    _experience_injector = ExperienceInjector(_experience_books)
    _cross_subscription = CrossAgentSubscription(_experience_books)
    _retrospective_generator = RetrospectiveGenerator(_experience_books, subscription=_cross_subscription)


def determine_review_type(chapter_order: int,
                          event_type: str = "key",
                          has_character_intro: bool = False,
                          has_foreshadowing_reveal: bool = False,
                          has_major_conflict: bool = False) -> str:
    """判定本章审核级别。🔴=完整  🟡=快速  🟢=跳过

    DESIGN_DOC §7.4.4/§19.4: 章节类型判定决定审核流程和修订轮次。
    """
    # 关键事件 → 完整审核
    if event_type == "key":
        return "full"

    # 人物登场/伏笔回收/重大冲突 → 完整审核
    if has_character_intro or has_foreshadowing_reveal or has_major_conflict:
        return "full"

    # 有显著剧情推进 → 快速审核
    if event_type == "transition":
        return "quick"

    # 动作章/过渡章 → 跳过审核
    return "skip"

# 延迟导入 PipelineStateManager 以避免循环依赖
# 实际使用由 main.py 通过 set_pipeline_state() 注入


class Orchestrator(BaseAgent):
    """Coordinates the full writing pipeline with state machine integration.

    Uses PipelineStateManager for checkpointing and crash recovery.
    Uses ProviderPool for agent-specific model selection.
    Uses SessionMemory for Agent process memory & isolation (方案A).
    """

    def __init__(
        self,
        config: AgentConfig,
        sync_layer: SyncLayer,
        provider_pool: ProviderPool | None = None,
    ):
        super().__init__(config)
        self.sync = sync_layer
        self.pool = provider_pool or ProviderPool.default(config.api_key)
        self._pipeline_state = None  # Set by main.py after brain init
        self._session_memory = None  # Set by main.py after brain init

    def set_pipeline_state(self, state_manager):
        """Inject the PipelineStateManager for checkpoint tracking."""
        self._pipeline_state = state_manager

    def set_session_memory(self, session_memory):
        """Inject the SessionMemory for agent process memory (方案A)."""
        self._session_memory = session_memory

    def _record_decision(self, agent_name: str, novel_id: str, chapter_order: int,
                         decision_type: str, summary: str, intent: str = "",
                         tags: list[str] | None = None, phase: str = ""):
        """Record an agent's decision to SessionMemory (non-blocking)."""
        if self._session_memory:
            try:
                self._session_memory.record_decision(
                    agent_name=agent_name, novel_id=novel_id,
                    chapter_order=chapter_order, decision_type=decision_type,
                    summary=summary, intent=intent, tags=tags, phase=phase,
                )
            except Exception:
                pass  # 记忆记录失败不阻塞管线

    # ── 学习区集成 (§15.4) ──

    def _get_learning_context(self, chapter_title: str, chapter_type: str) -> str:
        """从学习区检索与当前章节相关的写作资料

        使用章节标题 + 类型作为检索关键词，返回最相关的 TOP 5
        学习资料作为额外的上下文注入 system prompt。
        """
        if not _LEARNING_ZONE_AVAILABLE:
            return ""
        try:
            zone = get_learning_zone()
            # 组合查询：章节标题 + 类型关键词
            query = f"{chapter_type} {chapter_title}"
            materials = zone.retrieve_context(query, limit=5)
            if not materials:
                return ""

            # 格式化为 system prompt 附录
            lines = ["\n## 📝 学习区参考\n"]
            for i, m in enumerate(materials, 1):
                lines.append(
                    f"{i}. **{m.title}** [{m.category}] (参考字数: {m.word_count})\n"
                    f"{m.content[:300]}{'...' if len(m.content) > 300 else ''}"
                )
            return "\n\n".join(lines)
        except Exception:
            # 学习区检索失败不阻塞管线
            pass
        return ""

    def build_prompt(self, **kwargs) -> str:
        return ""

    # ── P1-03: delegate_task — 标准化 Agent 委派 ──

    async def delegate_task(
        self,
        agent_role: str,
        agent_name: str,
        task_type: str,
        fn,  # callable that returns a coroutine (factory pattern)
        timeout_s: float = 120.0,
        max_retries: int = 2,
        retry_delay_s: float = 3.0,
        chapter_id: str = "",
        novel_id: str = "",
    ) -> dict:
        """Standardized agent task delegation with timeout, retry, and logging.

        Args:
            fn: A callable that returns a coroutine (e.g., lambda: agent.run(x)).
                Must be a factory — each call creates a fresh coroutine.

        Returns:
            {"ok": bool, "result": Any, "error": str|None,
             "retries": int, "elapsed_s": float, "agent": str, "task": str}
        """
        import asyncio, time

        start = time.monotonic()
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                coro = fn() if callable(fn) else fn
                result = await asyncio.wait_for(coro, timeout=timeout_s)
                elapsed = time.monotonic() - start
                self._record_decision(
                    agent_name=agent_name, novel_id=novel_id,
                    chapter_order=0,  # injected downstream
                    decision_type=f"{task_type}_done",
                    summary=f"{agent_role} completed {task_type} in {elapsed:.1f}s",
                    tags=[agent_role, task_type, f"attempt_{attempt+1}"],
                )
                return {
                    "ok": True, "result": result, "error": None,
                    "retries": attempt, "elapsed_s": round(elapsed, 2),
                    "agent": agent_role, "task": task_type,
                }
            except asyncio.TimeoutError:
                last_error = f"Timeout after {timeout_s}s"
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay_s)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay_s)

        elapsed = time.monotonic() - start
        return {
            "ok": False, "result": None, "error": last_error,
            "retries": max_retries, "elapsed_s": round(elapsed, 2),
            "agent": agent_role, "task": task_type,
        }

    async def run_pipeline(
        self,
        chapter: Chapter,
        plot: Plot,
        event: Event,
        context_pkg: ContextPackage,
        style_prompt: str,
        review_type: str = "quick",
        target_words: int = 0,          # 从Novel传入 (§6.2.3字数贯穿)
        expected_chapter_words: int = 0, # 本章目标字数 (from skeleton)
    ) -> AsyncIterator[dict]:
        """
        Run the full writing pipeline for a single chapter.

        Phases with state transitions:
          1. IDLE → CONTEXT_READY (context agent)
          2. CONTEXT_READY → WRITING (writer agent, streaming)
          3. WRITING → REVIEWING (reviewer agent + revision loop)
          4. REVIEWING → COMPLETE (character + canon updates)

        review_type: "full" (🔴, ≤3 rounds), "quick" (🟡, ≤1), "skip" (🟢, 0)
        """
        ch_id = f"ch-{chapter.order:03d}"

        # ── 章节屏障 (D5修复) ──
        if self._pipeline_state:
            if not self._pipeline_state.is_chapter_unlocked(ch_id):
                yield {"agent": "orchestrator",
                       "text": f">>> ⏳ 等待上一章 Phase B 完成... <<<"}
                # 轮询等待(简化版——生产环境应使用 asyncio.Event)
                import asyncio
                for _ in range(30):  # 最多等待30秒
                    await asyncio.sleep(1)
                    if self._pipeline_state.is_chapter_unlocked(ch_id):
                        break

        # ── Phase 1: CONTEXT_READY ──
        if self._pipeline_state:
            self._pipeline_state.transition(ch_id, "CONTEXT_READY")

        yield {"agent": "orchestrator",
               "text": f">>> 启动流水线: 第{chapter.order}章 <<<"}
        # 章节字数约束 (§6.2.3) — 从总字数计算单章产出
        if expected_chapter_words <= 0 and target_words > 0:
            if target_words <= 100_000:
                expected_chapter_words = 3500
            elif target_words <= 300_000:
                expected_chapter_words = 3000
            else:
                expected_chapter_words = 2800

        yield {"agent": "orchestrator",
               "text": f"事件: {event.title} | 情节: {plot.title} | 审核: {review_type}"}
        if target_words > 0:
            yield {"agent": "orchestrator",
                   "text": f"字数规划: 总{target_words//10000}w | 本章~{expected_chapter_words}字"}

        # ── Phase 2: WRITING ──
        if self._pipeline_state:
            self._pipeline_state.transition(ch_id, "WRITING")

        from agents.writer_agent import WriterAgent
        writer = WriterAgent(self.config)
        writer_cfg = self.pool.get_config("writer")
        yield {"agent": "writer", "text": f"> 模型: {writer_cfg.model}"}

        # E3: 经验注入——写作前检索相关经验注入 system prompt (§5.7.5)
        _init_experience_books()
        experience_appendix = ""
        if _experience_injector:
            features = TaskFeatures(
                agent_name="writer",
                chapter_type=event.type if event else "key",
                chapter_functions=(
                    ["推进感情线"] if any("感情" in f.get("description", "")
                    for f in context_pkg.foreshadowing_to_progress if hasattr(f, 'get')) else []
                ),
                target_words=target_words,
                chapter_order=chapter.order,
            )
            experience_appendix = _experience_injector.inject(features)

        # 方案A: 过程记忆注入——检索 Writer 前文章节的创作决策 + 讨论结论 (§5.1)
        memory_appendix = ""
        discussion_appendix = ""
        if self._session_memory:
            try:
                # Writer 历史决策
                past_decisions = self._session_memory.retrieve_recent(
                    agent_name="writer", novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
                    chapter_order=chapter.order,
                )
                if past_decisions:
                    memory_appendix = self._session_memory.format_for_injection(
                        past_decisions, target_agent="writer"
                    )

                # 讨论结论——Agent团队对本章事件/情节的讨论共识
                discussion_notes = self._session_memory.search(
                    agent_name="discussion", novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
                    query=f"ch{chapter.order:03d}",
                    limit=3
                )
                if discussion_notes:
                    discussion_texts = [d.summary for d in discussion_notes[:3]]
                    discussion_appendix = "\n【Agent团队讨论共识】\n" + "\n".join(
                        f"  · {t}" for t in discussion_texts
                    ) + "\n  请参考以上团队讨论意见进行写作。"

                if past_decisions or discussion_notes:
                    combined = memory_appendix
                    if discussion_appendix:
                        combined += "\n" + discussion_appendix
                    combined += self._session_memory.format_isolation_boundary("writer") if memory_appendix else ""
                    yield {"agent": "orchestrator",
                           "text": f"> 已注入 {len(past_decisions) if past_decisions else 0} 条历史决策"
                                   f" + {len(discussion_notes) if discussion_notes else 0} 条讨论共识"}
                    memory_appendix = combined
            except Exception:
                pass

        # 将经验附录 + 过程记忆附加到style_prompt
        augmented_style = style_prompt
        if experience_appendix:
            augmented_style = style_prompt + "\n\n" + experience_appendix
            yield {"agent": "orchestrator", "text": f"> 已注入经验"}
        if memory_appendix:
            augmented_style += "\n\n" + memory_appendix

        # 学习区集成 (§15.4)
        learning_appendix = self._get_learning_context(
            chapter_title=chapter.title if chapter else "",
            chapter_type=chapter.chapter_type if chapter else "",
        )
        if learning_appendix:
            augmented_style += "\n\n" + learning_appendix
            yield {"agent": "orchestrator", "text": f"> 已注入学习区资料"}

        full_text = ""
        async for token in writer.generate_stream(
            context_pkg=context_pkg,
            style_prompt=augmented_style,
            chapter_title=chapter.title,
            target_words=expected_chapter_words,  # §6.2.3 单章产出约束
        ):
            full_text += token
            yield {"agent": "writer", "text": token}  # 逐 token 流式输出
            if len(full_text) % 500 == 0:
                yield {"agent": "writer", "text": f"> 生成中... {len(full_text)} 字"}

        chapter.content = full_text
        chapter.word_count = len(full_text)
        yield {"agent": "writer", "text": f"> 完成: {len(full_text)} 字"}

        # 方案A: 录制 Writer 的创作决策 (§5.1)
        self._record_decision(
            agent_name="writer",
            novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
            chapter_order=chapter.order,
            decision_type="creative_choice",
            summary=f"第{chapter.order}章《{chapter.title}》: 生成{len(full_text)}字, "
                    f"风格='{style_prompt[:50]}', 事件类型={event.type if event else 'key'}",
            intent=f"本章写了{chapter.title}——后续需关注伏笔推进和人物弧线连贯",
            tags=[f"ch{chapter.order:03d}", event.type if event else "key"],
            phase="writing",
        )

        # G3修复: 写入 GBrain content/ (DESIGN_DOC §19.3 Phase B)
        try:
            from brains.gbrain_wrapper import brain_write_page, brain_path_for
            brain_write_page(
                brain_path_for("novel-unknown"), "content",
                f"chapter-{chapter.order:03d}",
                f"第{chapter.order}章 {chapter.title}",
                full_text
            )
        except Exception as e:
            yield {"agent": "orchestrator",
                   "text": f"⚠️ GBrain 写入失败 — 章节内容未持久化: {str(e)[:80]}"}

        if self._pipeline_state:
            self._pipeline_state.record_checkpoint(ch_id, "writing_done")

        # ── P1-03: Phase 3+4 DAG 并行 ──
        # 审核（Phase 3）与 人物/Canon 提取（Phase 4）互不依赖
        # — 可并发执行以缩短管线总耗时。
        from agents.character_agent import CharacterAgent
        from agents.canon_agent import CanonAgent
        import asyncio

        # Phase 4: 启动人物+Canon 提取（与审核并发）
        char_agent = CharacterAgent(self.config)
        canon_agent = CanonAgent(self.config)

        char_task = asyncio.create_task(char_agent.extract_and_update(chapter))
        canon_task = asyncio.create_task(canon_agent.extract_facts(chapter, plot, event))
        char_canon_future = asyncio.gather(char_task, canon_task)

        # Phase 3: REVIEWING（与 Phase 4 重叠执行）
        if review_type == "skip":
            yield {"agent": "reviewer", "text": "> 跳过审核 (🟢 动作章)"}
            char_changes, new_facts = await char_canon_future
        else:
            if self._pipeline_state:
                self._pipeline_state.transition(ch_id, "REVIEWING")

            max_rounds = MAX_REVISION_ROUNDS if review_type == "full" else 1

            # 启动审核协程，收集事件用于后回放
            review_events = []
            async def _run_review_and_collect():
                async for ev in self._run_review_loop(
                    chapter, context_pkg, writer, ch_id, max_rounds
                ):
                    review_events.append(ev)
                return True

            review_task = asyncio.create_task(_run_review_and_collect())

            # 等待审核和 Phase 4 同时完成
            results = await asyncio.gather(review_task, char_canon_future, return_exceptions=True)
            review_result, char_canon_result = results

            # 回放审核事件
            if isinstance(review_result, Exception):
                yield {"agent": "orchestrator",
                       "text": f"> ⚠️ 审核异常: {review_result}"}
            else:
                for ev in review_events:
                    yield ev

            if isinstance(char_canon_result, Exception):
                yield {"agent": "orchestrator",
                       "text": f"> ⚠️ 人物/Canon提取异常: {char_canon_result}"}
                char_changes, new_facts = [], []
            else:
                char_changes, new_facts = char_canon_result

            # E6: 事后复盘（审核完成后）
            actual_rounds = getattr(self, '_last_review_rounds', 0)
            review_issues = getattr(self, '_last_review_issues', [])

            if actual_rounds >= 2 and _retrospective_generator:
                try:
                    generated = _retrospective_generator.generate_from_review(
                        chapter_order=chapter.order,
                        review_issues=review_issues,
                        revision_rounds=actual_rounds,
                        novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
                    )
                    if generated:
                        yield {"agent": "orchestrator",
                                "text": f"> 复盘: 从 {actual_rounds} 轮修订中提炼了 {len(generated)} 条经验"}
                except Exception:
                    pass  # 复盘失败不阻塞管线

        conflicts = await canon_agent.check_conflicts(new_facts)

        # P2-14: 持久化 canon 冲突到存储
        if conflicts or new_facts:
            try:
                from brains.canon_store import record_conflicts_from_pipeline
                nid = getattr(chapter, 'novel_id', '') or ''
                eid = getattr(event, 'id', '') if event else ''
                new_c, new_f = record_conflicts_from_pipeline(
                    novel_id=nid,
                    new_facts=new_facts,
                    conflicts=conflicts,
                    chapter_order=chapter.order,
                    event_id=eid,
                )
            except Exception:
                pass  # 持久化失败不阻塞管线

        yield {"agent": "character", "text": f"> 人物更新: {len(char_changes)} 条"}
        yield {"agent": "canon", "text": f"> Canon: {len(new_facts)} 事实"
               f" | 冲突: {len(conflicts)}"}

        # 方案A: 录制 Reviewer/Character/Canon 的决策 (§5.1)
        if review_type != "skip":
            self._record_decision(
                agent_name="reviewer", novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
                chapter_order=chapter.order, decision_type="review_pattern",
                summary=f"第{chapter.order}章审核: {review_type}级, "
                        f"修订{getattr(self, '_last_review_rounds', 0)}轮, "
                        f"问题{len(getattr(self, '_last_review_issues', []))}个",
                intent="关注此类问题是否在后续章节中重复出现",
                tags=[f"ch{chapter.order:03d}", review_type],
                phase="reviewing",
            )
        self._record_decision(
            agent_name="character", novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
            chapter_order=chapter.order, decision_type="arc_judgment",
            summary=f"第{chapter.order}章: 人物更新{len(char_changes)}条",
            intent="跟踪人物弧线推进——当前状态变化需在后续章节中延续",
            tags=[f"ch{chapter.order:03d}"] + [c.get("name", "") for c in char_changes[:3]],
            phase="complete",
        )
        self._record_decision(
            agent_name="canon", novel_id=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
            chapter_order=chapter.order, decision_type="canon_classification",
            summary=f"第{chapter.order}章: 新增{len(new_facts)}条Canon事实, "
                    f"{len(conflicts)}个冲突",
            intent="冲突分类理由——标记需后续章节解决的Canon问题",
            tags=[f"ch{chapter.order:03d}", f"facts:{len(new_facts)}"],
            phase="complete",
        )

        # G3修复: 人物/Canon 持久化到 GBrain (DESIGN_DOC §19.3 Phase B)
        try:
            from brains.gbrain_wrapper import brain_write_page, brain_path_for
            import json
            bp = brain_path_for("novel-unknown")
            for change in char_changes:
                brain_write_page(bp, "character", change.get("id", ""),
                    change.get("name", ""),
                    json.dumps(change, ensure_ascii=False))
            for fact in new_facts:
                brain_write_page(bp, "canon", fact.get("id", ""),
                    fact.get("fact", "")[:80],
                    json.dumps(fact, ensure_ascii=False))
        except Exception as e:
            yield {"agent": "orchestrator",
                   "text": f"⚠️ GBrain 写入失败 — 人物/Canon 未持久化: {str(e)[:80]}"}

        # ── Canon冲突处理 (D2修复) ──
        if conflicts:
            critical = [c for c in conflicts if c.get("severity") == "critical"]
            if critical:
                reason = "; ".join(c.get("description", "") for c in critical)
                if self._pipeline_state:
                    self._pipeline_state.block(ch_id, reason)
                chapter.status = "blocked"
                yield {"agent": "orchestrator",
                       "text": f">>> ⛔ 管线阻塞: Canon严重冲突 — {reason} <<<"}
                return  # 不标记COMPLETE, 等待用户裁决

            # minor冲突 → COMPLETE_WITH_WARNINGS
            if self._pipeline_state:
                self._pipeline_state.record_checkpoint(ch_id, "canon_done", {
                    "conflicts": conflicts,
                    "resolved": False,
                })
                self._pipeline_state.transition(ch_id, "COMPLETE_WITH_WARNINGS")
            chapter.status = "complete_with_warnings"
            yield {"agent": "orchestrator",
                   "text": f">>> ⚠️ 第{chapter.order}章完成(有警告) — {len(conflicts)}个minor冲突 <<<"}
            # P2-11: 自动版本快照（警告路径）
            _auto_save_version(chapter, ch_id)
            return

        if self._pipeline_state:
            self._pipeline_state.record_checkpoint(ch_id, "canon_done")
            self._pipeline_state.transition(ch_id, "COMPLETE")

        chapter.status = "complete"

        # 命名Agent — 为完成的章节生成标题
        try:
            from agents.naming_agent import NamingAgent
            naming = NamingAgent(self.config)
            ch_name = await naming.generate_chapter_name(
                chapter_content=chapter.content[:500],
                chapter_order=chapter.order,
                plot_title=plot.title if plot else "",
            )
            ch_title = ch_name.get("primary", f"第{chapter.order}章") if isinstance(ch_name, dict) else str(ch_name)
            chapter.title = ch_title
            yield {"agent": "naming", "text": f"> 章节命名: {ch_title}",
                   "emoji": "🏷️"}
        except Exception:
            pass

        # 学习闭环: 从审核反馈中提炼可迁移的写作原则 → 写入经验本
        try:
            review_notes = getattr(self, '_last_review_issues', [])
            if review_notes and _experience_injector:
                principle = await _extract_writing_principle(
                    review_issues=review_notes,
                    chapter_excerpt=chapter.content[:800] if chapter.content else "",
                    chapter_order=chapter.order,
                    config=self.config,
                )
                if principle:
                    _init_experience_books()
                    if "writer" in _experience_books:
                        entry = ExperienceEntry(
                            type="技法修正",
                            insight=principle,
                            confidence=5,
                            status="unverified",
                            tags=["写作技法", f"ch{chapter.order:03d}"],
                        )
                        _experience_books["writer"].add_entry(entry)
                        yield {"agent": "orchestrator",
                               "text": f"> 学习: 提炼了1条写作原则"}
        except Exception:
            pass

        # 正面提炼 + 对比学习: 从本章中学习
        try:
            if chapter.content and len(chapter.content) > 500:
                excerpt = chapter.content[:1000]
                # 正面提炼
                positive_principle = await _extract_positive_principle(
                    chapter_excerpt=excerpt,
                    chapter_order=chapter.order,
                    config=self.config,
                )
                if positive_principle:
                    _init_experience_books()
                    if "writer" in _experience_books:
                        entry = ExperienceEntry(
                            type="技法发现",
                            insight=positive_principle,
                            confidence=6,
                            status="unverified",
                            tags=["写作技法", "正面", f"ch{chapter.order:03d}"],
                            discovered_in=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
                        )
                        _experience_books["writer"].add_entry(entry)
                        yield {"agent": "orchestrator",
                               "text": f"> 学习: 记录了1条本章好技法"}

                # 对比学习: 生成"你写的 vs 更好的写法"
                contrast = await _generate_contrast_example(
                    chapter_excerpt=excerpt,
                    chapter_order=chapter.order,
                    config=self.config,
                )
                if contrast and contrast.get("rewrite"):
                    _init_experience_books()
                    if "writer" in _experience_books:
                        safe = contrast.get("safe_pattern", "")
                        rewrite = contrast.get("rewrite", "")
                        why = contrast.get("why_better", "")
                        insight = (
                            f"安全模式: {safe}\n"
                            f"改进写法: {rewrite}\n"
                            f"改进原因: {why}"
                        )
                        entry = ExperienceEntry(
                            type="对比学习",
                            insight=insight,
                            confidence=7,
                            status="unverified",
                            tags=["对比学习", "改写", f"ch{chapter.order:03d}"],
                            discovered_in=chapter.novel_id if hasattr(chapter, 'novel_id') else "",
                        )
                        _experience_books["writer"].add_entry(entry)
                        yield {"agent": "orchestrator",
                               "text": f"> 学习: 生成了1条对比示例"}
        except Exception:
            pass

        # P2-11: 自动版本快照（完成路径）
        _auto_save_version(chapter, ch_id)

        yield {"agent": "orchestrator", "text": f">>> 第{chapter.order}章 完成 ✓ <<<"}

    async def _run_review_loop(self, chapter, context_pkg, writer, ch_id, max_rounds):
        """Review + revision loop. Supports auto-upgrade quick→full.

        D2/D6修复: quick审核发现问题 → 自动升级为full审核。

        返回: (actual_rounds_completed, accumulated_issues)
        """
        from agents.reviewer_agent import ReviewerAgent

        reviewer = ReviewerAgent(self.config)
        reviewer_cfg = self.pool.get_config("reviewer")
        actual_max = max_rounds
        all_issues = []

        for round_num in range(1, actual_max + 1):
            report = await reviewer.review(chapter, context_pkg)
            n_issues = len(report.issues)
            all_issues.extend(report.issues)

            cp_name = f"review_r{round_num}_done"
            snapshot = {
                "review_report": {
                    "overall_score": report.overall_score,
                    "issues": report.issues,
                },
                "revised_content_path": f"content/chapter-{chapter.order:03d}.md",
            }
            if self._pipeline_state:
                self._pipeline_state.record_checkpoint(ch_id, cp_name, snapshot)

            # ── 审核自动升级 (D6修复) ──
            has_major = any(
                i.get("severity") in ("critical", "major")
                for i in report.issues
            )
            if has_major and actual_max < MAX_REVISION_ROUNDS:
                actual_max = MAX_REVISION_ROUNDS
                if self._pipeline_state:
                    self._pipeline_state.set_review_type(ch_id, "full")
                yield {"agent": "reviewer",
                       "text": "> ⚠️ 检测到严重问题, 自动升级为完整审核"}

            if not report.needs_revision:
                self._last_review_rounds = round_num
                self._last_review_issues = all_issues
                return

            if round_num < actual_max:
                revised = await writer.revise(chapter, report)
                chapter.content = revised
                chapter.word_count = len(revised)

        self._last_review_rounds = actual_max
        self._last_review_issues = all_issues

    # ── P1-03: Durable Execution 恢复 (§7.4.5) ──

    async def resume_chapter(
        self,
        novel_id: str,
        chapter_order: int,
        state_mgr,
        chapter,
        plot,
        event,
    ):
        """从 PipelineStateManager 检查点恢复章节生成。

        读取检查点确定当前阶段和进度，跳过已完成阶段，
        从最近的检查点继续执行。

        适用场景:
          - 服务器崩溃后重启
          - WebSocket 断线重连检测到进行中的管线
          - 用户手动"继续"被阻塞的章节
        """
        ch_id = f"{novel_id}-{chapter_order:03d}"
        status = state_mgr.get_status(ch_id)
        checkpoints = state_mgr.get_checkpoints(ch_id)

        logger.info(f"resume_chapter {ch_id}: status={status}, checkpoints={len(checkpoints)}")

        # ❌ 不可恢复状态
        if status in ("IDLE", "COMPLETE", "COMPLETE_WITH_WARNINGS"):
            logger.warning(f"resume_chapter: {ch_id} 状态={status}, 无需恢复")
            return

        # checkpoints 是 dict: {"context_ready": {...}, "writing_done": {...}}
        completed_checkpoints = set(checkpoints.keys()) if isinstance(checkpoints, dict) else set()
        # 同时提取 checkpoint data (按名称索引)
        checkpoint_data = checkpoints if isinstance(checkpoints, dict) else {}
        logger.info(f"completed checkpoints: {completed_checkpoints}")

        from agents.context_agent import ContextAgent
        from agents.writer_agent import WriterAgent
        from agents.reviewer_agent import ReviewerAgent
        from agents.character_agent import CharacterAgent
        from agents.canon_agent import CanonAgent
        import asyncio

        # Phase 0: Context (skip if context_ready exists)
        if "context_ready" not in completed_checkpoints:
            yield {"agent": "system", "text": "> [恢复] Phase 0: 重新加载上下文..."}
            context_agent = ContextAgent(self.config)
            context_agent.sync_from_gbrain(novel_id)
            context_pkg = await context_agent.get_context(novel_id, chapter_order, chapter.title)
            if self._pipeline_state:
                self._pipeline_state.transition(ch_id, "CONTEXT_READY")
                self._pipeline_state.record_checkpoint(ch_id, "context_ready")
        else:
            context_agent = ContextAgent(self.config)
            context_agent.sync_from_gbrain(novel_id)
            context_pkg = await context_agent.get_context(novel_id, chapter_order, chapter.title)
            yield {"agent": "system", "text": f"> [恢复] Phase 0: 上下文已就绪 ✓"}

        # Phase 1: Plan (skip if plan_done exists)
        writer = WriterAgent(self.config)
        if "plan_done" not in completed_checkpoints:
            yield {"agent": "system", "text": "> [恢复] Phase 1: 重新规划章节..."}
            review_type = determine_review_type(
                chapter_order=chapter_order,
                event_type=event.event_type if event else "normal",
                is_key_event=bool(event and event.is_key),
                skeleton_word_count=0,
            )
            plans = await writer.plan(chapter, context_pkg, review_type)
            chapter.outline = plans
            if self._pipeline_state:
                self._pipeline_state.record_checkpoint(ch_id, "plan_done", {
                    "review_type": review_type,
                    "is_key_event": bool(event and event.is_key),
                })
                self._pipeline_state.set_review_type(ch_id, review_type)
        else:
            plan_cp = checkpoint_data.get("plan_done", {})
            review_type = plan_cp.get("data", {}).get("review_type", "full") if plan_cp else "full"
            yield {"agent": "system", "text": f"> [恢复] Phase 1: 章节规划已就绪 ✓ (review_type={review_type})"}

        # Phase 2: Writing (skip if writing_done exists)
        if "writing_done" not in completed_checkpoints:
            yield {"agent": "system", "text": "> [恢复] Phase 2: 重新写作..."}
            if self._pipeline_state:
                self._pipeline_state.transition(ch_id, "WRITING")
            async for token in writer.write_chapter(chapter, context_pkg, review_type):
                if isinstance(token, dict) and token.get("type") == "chapter_token":
                    yield {"type": "chapter_token", "text": token.get("text", "")}
            if self._pipeline_state:
                self._pipeline_state.record_checkpoint(ch_id, "writing_done", {
                    "word_count": chapter.word_count,
                    "content_preview": chapter.content[:200] if chapter.content else "",
                })
            yield {"agent": "writer", "text": f"> [恢复] 第{chapter.order}章写作完成, {chapter.word_count}字 ✓"}
        else:
            write_cp = checkpoint_data.get("writing_done", {})
            word_count = write_cp.get("data", {}).get("word_count", 0) if write_cp else 0
            yield {"agent": "system", "text": f"> [恢复] Phase 2: 写作已完成 ✓ ({word_count}字)"}

        # Phase 3+4: Review + Character/Canon (DAG parallel)
        if "review_done" not in completed_checkpoints or "char_done" not in completed_checkpoints:
            yield {"agent": "system", "text": "> [恢复] Phase 3+4: 审核与人物/Canon提取 (DAG并行)..."}

            reviewer = ReviewerAgent(self.config)
            char_agent = CharacterAgent(self.config)
            canon_agent = CanonAgent(self.config)

            max_rounds = MAX_REVISION_ROUNDS if review_type == "full" else 1

            # Phase 4 任务
            char_canon_still_needed = True
            char_canon_task = None
            if "char_done" in completed_checkpoints:
                char_canon_still_needed = False
                yield {"agent": "system", "text": "> [恢复] 人物/Canon 已提取 ✓"}
                char_changes, new_facts = [], []
            else:
                char_task = asyncio.create_task(char_agent.extract_and_update(chapter))
                canon_task = asyncio.create_task(canon_agent.extract_facts(chapter, plot, event))
                char_canon_task = asyncio.gather(char_task, canon_task)

            # Phase 3: review loop
            if review_type == "skip":
                yield {"agent": "reviewer", "text": "> 跳过审核 (🟢 动作章)"}
            else:
                if "review_r1_done" in completed_checkpoints:
                    yield {"agent": "system", "text": "> [恢复] 审核已完成 ✓"}
                else:
                    async for rev_ev in self._run_review_loop(chapter, context_pkg, writer, ch_id, max_rounds):
                        yield rev_ev
                    if self._pipeline_state:
                        self._pipeline_state.record_checkpoint(ch_id, "review_done")

            # 等待 Phase 4
            if char_canon_still_needed and char_canon_task:
                try:
                    char_changes, new_facts = await char_canon_task
                    if self._pipeline_state:
                        self._pipeline_state.record_checkpoint(ch_id, "char_done", {
                            "char_changes": len(char_changes),
                            "new_facts": len(new_facts),
                        })
                except Exception as e:
                    yield {"agent": "orchestrator",
                           "text": f"> ⚠️ [恢复] 人物/Canon提取异常: {e}"}
                    char_changes, new_facts = [], []

            # Canon conflict check
            conflicts = await canon_agent.check_conflicts(new_facts)
            if conflicts:
                critical = [c for c in conflicts if c.get("severity") == "critical"]
                if critical:
                    reason = "; ".join(c.get("description", "") for c in critical)
                    if self._pipeline_state:
                        self._pipeline_state.block(ch_id, reason)
                    chapter.status = "blocked"
                    yield {"agent": "orchestrator",
                           "text": f">>> ⛔ 管线阻塞: Canon严重冲突 — {reason} <<<"}
                    return
                if self._pipeline_state:
                    self._pipeline_state.transition(ch_id, "COMPLETE_WITH_WARNINGS")
                chapter.status = "complete_with_warnings"
                yield {"agent": "orchestrator",
                       "text": f">>> ⚠️ 第{chapter.order}章完成(有警告) — {len(conflicts)}个minor冲突 <<<"}
                return

            # Mark COMPLETE
            if self._pipeline_state:
                self._pipeline_state.transition(ch_id, "COMPLETE")
                self._pipeline_state.record_checkpoint(ch_id, "complete")
            chapter.status = "complete"
            yield {"agent": "orchestrator",
                   "text": f">>> ✅ 第{chapter.order}章恢复完成 <<<"}

    async def maybe_resume_pipeline(
        self,
        novel_id: str,
        state_mgr,
        get_chapter_fn,
    ):
        """启动时检查是否有未完成的管线章节并恢复执行。

        如果找到进行中（非 COMPLETE/IDLE/BLOCKED）的章节，
        自动从检查点恢复。

        配合 §7.4.5 Durable Execution，实现崩溃恢复。
        """
        if not state_mgr:
            return

        chapters = state_mgr.list_pending(novel_id)
        if not chapters:
            logger.info(f"maybe_resume: novel {novel_id} 无待恢复章节")
            return

        logger.info(f"maybe_resume: novel {novel_id} 找到 {len(chapters)} 个待恢复章节")

        for ch_id in chapters:
            status = state_mgr.get_status(ch_id)
            if status in ("IDLE", "COMPLETE", "COMPLETE_WITH_WARNINGS"):
                continue

            chapter_order = int(ch_id.split("-")[-1]) if "-" in ch_id else 0
            chapter = get_chapter_fn(chapter_order)
            if not chapter:
                logger.warning(f"maybe_resume: {ch_id} 找不到章节对象, 跳过")
                continue

            logger.info(f"maybe_resume: 恢复 {ch_id} (status={status})")
            yield {"type": "pipeline_resume", "chapter_order": chapter_order, "status": status}

            async for event in self.resume_chapter(
                novel_id, chapter_order, state_mgr, chapter,
                plot=None, event=None,
            ):
                yield event
