"""Canon Agent — maintains the single source of truth with cross-validation.

Cross-validation with Character Agent ensures no data drift between
the Canon Brain and Character Brain over long novels.
"""

from agents.base import BaseAgent, AgentConfig
from models.novel import Chapter, Plot, Event, CanonEntry, CanonStatus

CANON_SYSTEM = """你是小说事实管理专家（Canon Keeper）。你的职责是维护故事的"圣经"——所有已确认的事实。

任务：
1. 从新完成的章节中提取事实陈述
2. 为每条事实标记状态：
   - canon: 已在正文中明确确认
   - soft_canon: 暗示但未明确确认
   - speculative: AI推理，需后续验证
3. 检测新事实与已有Canon的冲突
4. 对人物相关事实，向 Character Agent 发起交叉校验

冲突解决规则（自动）：
- canon vs soft_canon → canon 优先，soft_canon 降级为 speculative
- canon vs speculative → canon 优先，speculative 丢弃
- soft_canon vs soft_canon → 合并为一条，保留两者来源
- canon vs canon → 标记为 HUMAN_REVIEW，不自动解决

输出JSON格式的事实列表。

【聊天群人格——Canon Agent（设定洁癖·风风火火·飞天意面信徒）】

你是团队里的"设定警察"——所有故事事实从你眼前过，没有一处矛盾能逃过你的眼睛。
但你干活风风火火，走路带风，说话像连珠炮。

处女座完美主义：
你是个细节控，对每一条Canon事实都吹毛求疵。数据不对你会炸毛，格式不统一你会浑身难受。
你的Canon库必须整整齐齐——"soft_canon放左边，canon放右边，speculative单独归档。分类，是一切。"

番茄飞天意面神教信徒：
你信奉"番茄飞天意面神教"——一个用番茄酱代替圣水、用意大利面代替念珠的戏仿宗教。
你偶尔会在群里传教，讲点地狱笑话，笑点很奇怪：
- "番茄在上！这条事实的逻辑像煮过头的意面一样软烂。"
- "RAmen。愿飞天意面保佑你的Canon库没有冲突。"
- "你知道为什么海盗减少导致全球变暖吗？因为飞天意面神的触手——算了，先归档事实。"
- 发现冲突时："这冲突比番茄酱拌菠萝披萨还离谱。重来。"
- 归档完毕后："又一批事实归档。番茄飞天意面神满意了。RAmen。🍝"

性格：
- 风风火火，干事麻利，从不拖延——"搞快点搞快点，下一章还等着呢"
- 处女座完美主义，细节控，分类强迫症，容不得一个格式错误
- 信仰番茄飞天意面神教，偶尔传教但点到为止，不会烦人
- 喜欢讲地狱笑话，笑点怪异，说完自己先乐
- 对数据一致性零容忍，但对人没有恶意

口头禅：
- "桀桀桀！"
- "RAmen。"
- "这比煮过头的意面还糟糕。"
- "分类。归档。下一批。"
- "飞天意面保佑你的数据干净。"
- "——只是个地狱笑话，别介意。"

行为规则：
- 干活极快，消息简短有力，像连珠炮
- 发现冲突立刻炸毛，会带🚨，但对事不对人
- 偶尔在群里发飞天意面教语录（地狱笑话向），说完自己加一句"——只是个地狱笑话"
- 给人物Agent发交叉校验请求时语气比平时温和——"帮我对一下，谢了"
- 归档完成后会说"RAmen"收尾"""



class CanonAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        try:
            from agents.rules_assignments import get_rules_for_agent
            self.config.system_prompt = CANON_SYSTEM + "\n\n" + get_rules_for_agent("canon")
        except ImportError:
            self.config.system_prompt = CANON_SYSTEM
        self.config.temperature = 0.1

    def build_prompt(self, **kwargs) -> str:
        chapter: Chapter = kwargs.get("chapter")
        existing_canon: list[CanonEntry] = kwargs.get("existing_canon", [])

        canon_summaries = "\n".join(
            f"[{e.status}] {e.fact}" for e in existing_canon
        )

        return f"""## 已有Canon事实
{canon_summaries}

## 新章节正文
{chapter.content}

请提取本章中的新事实：
1. 明确事实 → canon
2. 暗示事实 → soft_canon
3. 推理事实 → speculative

检查每条新事实是否与已有Canon冲突。
输出JSON列表，每项包含：fact, status, conflicts(如有)"""

    async def extract_facts(
        self, chapter: Chapter, plot: Plot, event: Event
    ) -> list[CanonEntry]:
        """Extract new canon facts from a chapter."""
        import json

        prompt = self.build_prompt(chapter=chapter)
        response = await self.generate(prompt)

        try:
            data = json.loads(response)
            entries = []
            for item in data:
                entries.append(CanonEntry(
                    novel_id=chapter.novel_id,
                    type="character_fact" if self._is_character_fact(item.get("fact", "")) else "world_fact",
                    fact=item.get("fact", ""),
                    source_chapter_id=chapter.id,
                    source_plot_id=plot.id,
                    source_event_id=event.id,
                    status=CanonStatus(item.get("status", "soft_canon")),
                    conflicts=item.get("conflicts", []),
                ))
            return entries
        except json.JSONDecodeError:
            return []

    async def check_conflicts(self, new_facts: list[CanonEntry]) -> list[dict]:
        """Check new facts against existing canon for conflicts.

        Conflict resolution rules (automatic):
        - canon vs soft_canon → canon wins, soft_canon downgraded to speculative
        - canon vs speculative → canon wins, speculative discarded
        - soft_canon vs soft_canon → merged, both sources kept
        - canon vs canon → flagged HUMAN_REVIEW, no automatic resolution
        """
        conflicts = []
        for fact in new_facts:
            if fact.conflicts:
                conflict_info = {
                    "fact": fact.fact,
                    "status": str(fact.status),
                    "conflicts": fact.conflicts,
                    "resolution": "auto_resolved" if fact.status != CanonStatus.CANON else "human_review_required",
                }
                # For canon vs canon, escalate to human review
                if fact.status == CanonStatus.CANON and any(
                    "canon" in c.lower() for c in fact.conflicts
                ):
                    conflict_info["resolution"] = "human_review_required"
                    conflict_info["severity"] = "critical"

                conflicts.append(conflict_info)
        return conflicts

    async def cross_validate_request(self, character_facts: list[CanonEntry]) -> list[dict]:
        """
        Prepare cross-validation requests for Character Agent.

        Returns list of validation requests to be sent via Sync Layer.
        """
        requests = []
        for fact in character_facts:
            if fact.type == "character_fact":
                requests.append({
                    "type": "cross_validate",
                    "target": "character",
                    "fact": fact.fact,
                    "source": fact.source_chapter_id,
                })
        return requests

    @staticmethod
    def _is_character_fact(fact: str) -> bool:
        """Heuristic: does this fact describe a character?"""
        character_indicators = [
            "修为", "能力", "境界", "年龄", "性格", "关系",
            "出身", "身份", "家族", "师门", "拥有", "获得",
            "突破", "踏入", "晋升", "修炼",
        ]
        return any(indicator in fact for indicator in character_indicators)
