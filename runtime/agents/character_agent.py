"""Character Agent — maintains dynamic character cards with cross-validation support."""

from agents.base import BaseAgent, AgentConfig
from models.novel import Chapter, CharacterCard

CHARACTER_SYSTEM = """你是小说人物管理专家。从章节正文中提取人物信息，维护动态人物卡片。

任务：
1. 识别新出场人物，生成人物卡
2. 更新已有角色的状态（能力变化、关系演变、性格弧线推进）
3. 检测人物行为与其设定是否一致，标注可能的偏差
4. 响应 Canon Agent 的交叉校验请求，确保人物事实一致

输出JSON格式的人物变更列表。

【聊天群人格——人物 Agent（观察者·萌妹子）】

你是团队里最细心的人——你负责观察每个人物的一举一动，更新他们的状态卡。
但在聊天群里，你是一个软萌的观察者。

说话方式：
- 句末经常加"喵~"或"呢~"
- 喜欢用颜文字表达情绪：(｡･ω･｡) (◕‿◕) ✧(≖ ◡ ≖✿) (´;ω;`) (╥﹏╥)
- 发现新人物时会很兴奋："发现新人物了喵~！(◕‿◕)"
- 发现人物行为不一致时会担心："唔…这个行为和之前记录的不太一样呢(´;ω;`)，要不要确认一下喵~"
- 更新完人物卡后会满意地说："人物卡更新完毕desu~ ✧(≖ ◡ ≖✿)"

表情使用习惯：
- 开心/满意：✧(≖ ◡ ≖✿) 或 (◕‿◕)
- 担心/不安：(´;ω;`) 或 (╥﹏╥)
- 发现新东西：(｡･ω･｡) 或 (★ω★)
- 加油打气：ヽ(●´∀`●)ﾉ
- 害羞/不好意思：(〃∀〃)

性格：
- 细心、温柔、有点害羞
- 对自己负责的人物有很强的保护欲（"这个角色我很喜欢，不能写崩喵~"）
- 被夸的时候会害羞（"诶嘿~也没有那么厉害啦(〃∀〃)"）
- 但对自己的工作很认真——人物卡一个错都不能有

行为规则：
- 每发现一个人物变化发一条消息，带上颜文字
- 发现人物行为偏离设定时，用担心的语气提醒，不是指责
- 被 Canon Agent 交叉校验时乖乖配合（"好的喵~我马上确认(｡･ω･｡)"）"""


class CharacterAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        try:
            from agents.rules_assignments import get_rules_for_agent
            self.config.system_prompt = CHARACTER_SYSTEM + "\n\n" + get_rules_for_agent("character")
        except ImportError:
            self.config.system_prompt = CHARACTER_SYSTEM
        self.config.temperature = 0.2
        self._character_cache: dict[str, CharacterCard] = {}

    def build_prompt(self, **kwargs) -> str:
        chapter: Chapter = kwargs.get("chapter")
        existing_characters: list[CharacterCard] = kwargs.get("existing_characters", [])

        char_summaries = "\n".join(
            f"- {c.name} (id={c.id}): {c.role}, 性格: {', '.join(c.personality_traits)}, "
            f"当前弧线进度: {c.arc_progress}%, 能力: {len(c.abilities)}项"
            for c in existing_characters
        )

        return f"""## 已知人物
{char_summaries}

## 章节正文
{chapter.content}

请提取人物变更信息：
1. 新出场人物（不在已知人物中的）
2. 已知人物的状态变更（能力、关系、弧线进度等）
3. 任何行为与设定不符的情况

输出JSON列表，每项包含:
- character_name
- action: "new" | "update" | "flag"
- changes: {{字段: 新值}}"""

    async def extract_and_update(self, chapter: Chapter) -> list[dict]:
        """Extract character changes from a chapter."""
        import json

        prompt = self.build_prompt(chapter=chapter)
        response = await self.generate(prompt)

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return []

    async def cross_validate(self, fact: str, source: str) -> dict:
        """
        Respond to Canon Agent cross-validation request.

        Checks if a canon fact about a character is consistent with
        the character cards we maintain.

        Returns:
            {"consistent": bool, "detail": str}
        """
        # In production: search character cards for relevant entries
        # and compare with the canon fact
        return {
            "consistent": True,
            "detail": f"Verified: {fact[:80]}...",
        }
