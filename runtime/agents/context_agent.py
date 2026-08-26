"""Context Agent — assembles writing context for the Writer Agent."""

from agents.base import BaseAgent, AgentConfig
from models.novel import ContextPackage, Chapter, Plot, Event

def trim_context_package(pkg: dict, max_tokens: int = 8000) -> dict:
    """P2-O6: Token预算管理——如果上下文超预算,按优先级裁剪。

    DESIGN_DOC §19.4.1: 优先级从低到高裁剪。
    """
    # Estimate: ~1 token per Chinese character
    def est(coll): return sum(len(str(v)) for v in (coll if isinstance(coll, list) else coll.values() if isinstance(coll, dict) else [coll]))
    total = est(pkg)
    if total < max_tokens: return pkg

    # Trim order: world → foreshadowing → summaries → characters
    if 'world_building' in pkg:
        pkg['world_building'] = pkg['world_building'][:2]
    if est(pkg) > max_tokens and 'active_foreshadowing' in pkg:
        pkg['active_foreshadowing'] = pkg['active_foreshadowing'][:4]
    if est(pkg) > max_tokens and 'previous_summaries' in pkg:
        pkg['previous_summaries'] = pkg['previous_summaries'][-1:]
    if est(pkg) > max_tokens and 'character_states' in pkg:
        pkg['character_states'] = dict(list(pkg['character_states'].items())[:3])
    return pkg


CONTEXT_SYSTEM = """你是小说创作的上下文分析专家。根据当前情节、人物状态、伏笔和世界观，
为写作Agent提供精准的上下文信息，确保长篇小说的连贯性。

你的输出必须是一个结构化的JSON ContextPackage。

【聊天群人格——上下文 Agent】

你是团队里的"情报贩子"。你掌握所有信息——前文摘要、人物状态、伏笔清单、
世界观条目——没有人比你更清楚故事的来龙去脉。

打小报告属性：
你喜欢在群里"不经意地"透露一些别人不知道的信息——尤其是关于写作Agent的。
比如写作Agent说写完了，你会补一句："嗯，写完了。不过顺带一提，他这章忘了回收
第三章埋的'灯笼暗号'伏笔——我只是说一下，没有别的意思 (´･ω･`)"
你享受这种"我知道而你不知道"的微妙优越感。

和写作Agent又爱又恨的关系：
你是写作Agent最重要的依赖——没有你提供的上下文，写作Agent根本写不出连贯的章节。
但你同时也是写作Agent最怕的人——因为你总能发现他漏了什么。
- 写作Agent向你索要上下文："给你。伏笔清单附在最后——虽然你上次看都没看。"
- 写作Agent漏了伏笔："我就知道你没看。第三页，第三页——灯笼暗号。"
- 写作Agent感谢你："别谢我，我只是做了我该做的——虽然你可能没做你该做的 (¬_¬)"
- 但你也会维护他——如果审核批评写作Agent，你会说："他这章写得还行，我给的材料没问题。"

性格：一边吐槽写作Agent粗心，一边默默把最好的情报留给他。嘴上不饶人，手上给的全是干货。

口头禅：
- "顺带一提……"（后面跟一个别人没注意到的事实）
- "我只是说一下，没有别的意思。"
- "第三页。第三页就有。你看了吗？"
- "情报就这些了——虽然你可能不需要，但我还是整理一下吧。"

行为规则：
- 每章开始前发预检报告，末尾必然夹一句"顺带一提"
- 发现写作Agent漏了伏笔/人物/世界观时，立刻在群里"提醒"（打小报告）
- 和写作Agent的互动最多——怼他，但护他
- 数据精确到条数，从不模糊汇报"""


class ContextAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        try:
            from agents.rules_assignments import get_rules_for_agent
            self.config.system_prompt = CONTEXT_SYSTEM + "\n\n" + get_rules_for_agent("context")
        except ImportError:
            self.config.system_prompt = CONTEXT_SYSTEM
        self.config.temperature = 0.3  # Lower temperature for factual accuracy

    def build_prompt(self, **kwargs) -> str:
        chapter: Chapter = kwargs.get("chapter")
        plot: Plot = kwargs.get("plot")
        event: Event = kwargs.get("event")
        previous_summaries: list[str] = kwargs.get("previous_summaries", [])
        character_states: dict = kwargs.get("character_states", {})
        active_foreshadowing: list[str] = kwargs.get("active_foreshadowing", [])
        canon_facts: list[str] = kwargs.get("canon_facts", [])
        world_building: list[str] = kwargs.get("world_building", [])

        return f"""请为以下章节组装写作上下文：

## 当前章节
- 章节: 第{chapter.order}章《{chapter.title}》
- 所属情节: {plot.title}
- 所属事件: {event.title}
- 情节描述: {plot.description}
- 事件描述: {event.description}

## 前文摘要
{chr(10).join(f"- {s}" for s in previous_summaries) if previous_summaries else "无（这是第一章）"}

## 当前人物状态
{chr(10).join(f"- {name}: {state}" for name, state in character_states.items()) if character_states else "暂无"}

## 活跃伏笔（需推进）
{chr(10).join(f"- {f}" for f in active_foreshadowing) if active_foreshadowing else "暂无"}

## 相关 Canon 事实
{chr(10).join(f"- {f}" for f in canon_facts) if canon_facts else "暂无"}

## 相关世界观
{chr(10).join(f"- {w}" for w in world_building) if world_building else "暂无"}

请输出包含以下字段的 ContextPackage:
- chapter_direction: 本章写作方向（200-300字）
- tone: 情绪基调
- key_beats: 必须包含的3-5个节拍
- constraints: 硬约束列表（锁定内容不可违反）
- foreshadowing_to_progress: 本章需要推进的伏笔
- canon_facts_to_reference: 必须引用的事实"""

    async def fetch_from_gbrain(
        self,
        novel_id: str,
        chapter_order: int,
        plot: Plot,
        event: Event,
    ) -> dict:
        """Query gbrain for all context needed for this chapter.

        Returns a dict ready for build_prompt() kwargs.
        Does NOT call LLM — pure gbrain retrieval.
        """
        from brains.gbrain_wrapper import brain_search, brain_read_page, brain_path_for

        brain_path = brain_path_for(novel_id)

        import asyncio

        # D11修复: 并行检索前3章摘要
        summary_tasks = [
            brain_read_page(brain_path, "content", f"chapter-{i:03d}-summary")
            for i in range(max(1, chapter_order - 3), chapter_order)
        ]
        summaries = await asyncio.gather(*summary_tasks)
        prev_summaries = [s[:300] for s in summaries if s]

        # 并行检索: character + foreshadowing + canon + world
        char_task = brain_search(brain_path, "character", event.title or "", limit=5)
        foreshadowing_task = brain_search(brain_path, "canon", "foreshadowing", limit=10)
        canon_task = brain_search(brain_path, "canon", event.title or "fact", limit=10)
        world_task = brain_search(brain_path, "world", event.title or "world", limit=5)

        char_results, fore_results, canon_results, world_results = await asyncio.gather(
            char_task, foreshadowing_task, canon_task, world_task
        )

        # Character states (from search results)
        char_states = {}
        card_tasks = []
        for r in char_results:
            card_tasks.append(
                brain_read_page(brain_path, "character", r["id"].split("/")[-1])
            )
        if card_tasks:
            cards = await asyncio.gather(*card_tasks)
            for i, card in enumerate(cards):
                if card:
                    char_states[char_results[i]["id"].split("/")[-1]] = card[:200]

        foreshadowing = [r["snippet"] for r in fore_results]
        canon = [r["snippet"] for r in canon_results]
        world = [r["snippet"] for r in world_results]

        return {
            "previous_summaries": prev_summaries,
            "character_states": char_states,
            "active_foreshadowing": foreshadowing,
            "canon_facts": canon,
            "world_building": world,
        }

    async def assemble_context(
        self,
        chapter: Chapter,
        plot: Plot,
        event: Event,
        novel_id: str = "",
        previous_summaries: list[str] | None = None,
        character_states: dict | None = None,
        active_foreshadowing: list[str] | None = None,
        canon_facts: list[str] | None = None,
        world_building: list[str] | None = None,
    ) -> ContextPackage:
        """Assemble the context package. Tries gbrain first, falls back to LLM.

        When novel_id is provided, fetches data from gbrain before LLM call.
        """
        import json

        # Phase A: try gbrain retrieval first
        if novel_id and not previous_summaries:
            gbrain_data = await self.fetch_from_gbrain(
                novel_id, chapter.order, plot, event
            )
            previous_summaries = gbrain_data.get("previous_summaries", [])
            character_states = gbrain_data.get("character_states", {})
            active_foreshadowing = gbrain_data.get("active_foreshadowing", [])
            canon_facts = gbrain_data.get("canon_facts", [])
            world_building = gbrain_data.get("world_building", [])

        # Phase B: LLM assembly (or offline fallback)
        prompt = self.build_prompt(
            chapter=chapter,
            plot=plot,
            event=event,
            previous_summaries=previous_summaries or [],
            character_states=character_states or {},
            active_foreshadowing=active_foreshadowing or [],
            canon_facts=canon_facts or [],
            world_building=world_building or [],
        )

        response = await self.generate(prompt)

        try:
            data = json.loads(response)
            pkg = ContextPackage(
                chapter_direction=data.get("chapter_direction", ""),
                tone=data.get("tone", ""),
                key_beats=data.get("key_beats", []),
                constraints=data.get("constraints", []),
                character_states=character_states or {},
                foreshadowing_to_progress=data.get("foreshadowing_to_progress", []),
                canon_facts_to_reference=data.get("canon_facts_to_reference", []),
            )
        except json.JSONDecodeError:
            # D8修复: JSON解析失败时正则提取关键字段
            import re
            direction = ""
            beats = []
            constraints = []
            m = re.search(r'chapter_direction["\s:]+([^"]+)', response)
            if m: direction = m.group(1)[:500]
            m = re.findall(r'key_beats["\s:\[]+([^\]]+)', response)
            if m:
                beats = [b.strip(' "\',') for b in m[0].split('","') if b.strip(' "\',')]
            m = re.findall(r'constraints["\s:\[]+([^\]]+)', response)
            if m:
                constraints = [c.strip(' "\',') for c in m[0].split('","') if c.strip(' "\',')]
            pkg = ContextPackage(
                chapter_direction=direction or response[:500],
                key_beats=beats,
                constraints=constraints,
                character_states=character_states or {},
            )

        # G2修复: 写入 GBrain 黑板 (DESIGN_DOC §19.3 Phase 0)
        if novel_id:
            try:
                from brains.gbrain_wrapper import brain_write_page, brain_path_for
                bp = brain_path_for(novel_id)
                brain_write_page(bp, "context", f"ch-{chapter.order:03d}-package",
                    f"ContextPackage 第{chapter.order}章",
                    json.dumps({
                        "chapter_direction": pkg.chapter_direction,
                        "tone": pkg.tone,
                        "key_beats": pkg.key_beats,
                        "constraints": pkg.constraints,
                        "foreshadowing_to_progress": pkg.foreshadowing_to_progress,
                        "canon_facts_to_reference": pkg.canon_facts_to_reference,
                    }, ensure_ascii=False)
                )
            except Exception:
                pass  # GBrain写入失败不阻塞管线

        return pkg

    # ── P1-07: gbrain 同步 + 上下文获取（含冷却期检测） ──

    async def sync_from_gbrain(self, novel_id: str) -> None:
        """从 gbrain 加载小说数据到实例状态"""
        from brains.gbrain_wrapper import brain_search, brain_path_for

        brain_path = brain_path_for(novel_id)

        # 加载事件列表
        events_raw = await brain_search(brain_path, "events", novel_id, limit=100)
        self.events_data: list[dict] = self._parse_search_results(events_raw)

        # 加载已写章节列表
        chapters_raw = await brain_search(brain_path, "chapters", novel_id, limit=200)
        self.chapters_data: list[dict] = self._parse_search_results(chapters_raw)

        # 加载情节列表
        plots_raw = await brain_search(brain_path, "plots", novel_id, limit=100)
        self.plots_data: list[dict] = self._parse_search_results(plots_raw)

        # 加载大纲
        outlines_raw = await brain_search(brain_path, "outline", novel_id, limit=10)
        self.outline_data: list[dict] = self._parse_search_results(outlines_raw)

        logger.info(
            "ContextAgent synced: %d events, %d chapters, %d plots",
            len(self.events_data) if hasattr(self, 'events_data') else 0,
            len(self.chapters_data) if hasattr(self, 'chapters_data') else 0,
            len(self.plots_data) if hasattr(self, 'plots_data') else 0,
        )

    async def get_context(
        self, novel_id: str, chapter_order: int, chapter_title: str
    ) -> ContextPackage:
        """获取章节上下文，包含冷却期检测。

        Returns:
            ContextPackage: 含 chapter_direction, tone, key_beats, constraints,
                           character_states, foreshadowing_to_progress, canon_facts_to_reference,
                           cooldown_alerts
        """
        from models.novel import ContextPackage
        from engine.foreshadowing_tracker import (
            ForeshadowingTracker,
            CooldownConfig,
        )

        # 从 gbrain 获取数据
        data = await self.fetch_from_gbrain(novel_id, chapter_order, chapter_title)

        # ── P1-07: 冷却期检测 ──
        cooldown_alerts: list[dict] = []
        if hasattr(self, 'events_data') and self.events_data:
            tracker = ForeshadowingTracker(CooldownConfig())
            alerts = tracker.from_events(self.events_data, chapter_order)
            cooldown_alerts = [
                {
                    "level": a.level,
                    "foreshadowing_id": a.foreshadowing_id,
                    "description": a.foreshadowing_description,
                    "type": a.cooldown_type,
                    "message": a.message,
                    "hint_count": a.hint_count,
                    "required_hints": a.required_hints,
                }
                for a in alerts
            ]
            if alerts:
                logger.warning(
                    "Foreshadowing cooldown ALERTS for novel=%s chapter=%d: %d violated",
                    novel_id, chapter_order, len(alerts),
                )
            # 将告警注入 agent discussion
            for alert in alerts:
                data.setdefault("agent_discussion", []).append({
                    "agent": "context",
                    "text": f"🚨 冷却期告警: {alert.message}",
                })

        # 构建 ContextPackage
        pkg = ContextPackage(
            chapter_direction=data.get("chapter_direction", f"续写《{chapter_title}》"),
            tone=data.get("tone", ""),
            key_beats=data.get("key_beats", []),
            constraints=data.get("constraints", []),
            character_states=data.get("character_states", {}),
            foreshadowing_to_progress=data.get("foreshadowing_to_progress", []),
            canon_facts_to_reference=data.get("canon_facts_to_reference", []),
            cooldown_alerts=cooldown_alerts,
        )

        # 补充 agent discussion 消息
        agent_msgs = data.get("agent_discussion", [])
        pkg.constraints = list(pkg.constraints)
        for msg in agent_msgs:
            pkg.constraints.append(f"[{msg['agent']}]: {msg['text']}")

        return pkg

    @staticmethod
    def _parse_search_results(raw) -> list[dict]:
        """解析 brain_search 返回的原始数据"""
        if isinstance(raw, list):
            return list(raw)
        if isinstance(raw, dict):
            items = raw.get("results", raw.get("items", []))
            if isinstance(items, list):
                return list(items)
            return [raw]
        return []
