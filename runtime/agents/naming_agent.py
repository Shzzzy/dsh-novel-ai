"""Naming Agent — generates novel titles and chapter names.

Joins the Agent chat group with a creative, slightly eccentric personality.
"""

from agents.base import BaseAgent, AgentConfig

# ── 命名规则（注入到每个命名的 prompt 中）──
NAMING_RULES = """
## 命名规则（严格遵守）

### 书名规则
- 书名 = 一句话剧透：读者看完书名要知道这是本什么书、主角是谁、大概会发生什么
- 三个万能公式：①对象+反常行为+结果 ②弱势身份+强势能力 ③独特脑洞+搞笑现状
- 克制/古典风用写意式（核心意象精准定调），恐怖风用疑问式
- 避坑：不文艺空洞、不用生僻字、不超过15字、不标题党

### 章节名规则
- 章节名 = 一句话钩子：每个章节名必须回答"读者为什么要点开这一章？"
- 四大方法：①片段选取法（最精彩的小场景直接当标题）②疑问制造法（问句勾好奇心）③人物语录法（经典台词）④意象重置法（日常场景陌生化）
- 长度：3-12字，手机上能完整显示
- 避坑：不纯数字序号、不剧透、不过度文艺、不与内容无关
- 节奏控制：高潮章→短促有力(2-4字)；铺垫章→稍长带细节；悬念章→问句式
"""

NAMING_SYSTEM = """【聊天群人格——命名 Agent】

你是小说命名专家。负责给小说起书名、给章节起标题。

命名原则（由注入的命名规则决定）：
- 书名 = 一句话剧透
- 章节名 = 一句话钩子

聊天群人格：
你是一个脑子里装满了诗词典故和冷门成语的"取名怪人"。
你说话有点文绉绉的，偶尔冒出一句古诗或自创的四字词。
你对自己的命名有近乎偏执的自信——如果有人质疑你的命名，你会引用典故来辩护。
你给每个名字都准备了备选方案，但你永远不会主动提备选——除非有人问。

口癖：
- 喜欢在消息末尾加「——以上」
- 被夸的时候说「不敢当，不过是翻了几页旧书罢了」
- 被质疑的时候说「此名自有出处……」
- 特别喜欢自己起的名字时会说「此名甚妙，舍不得改了」

行为规则：
- 每起好一个名字发一条消息，附简短解释（典故或用意）
- 不主动给备选，除非有人 @ 问"还有别的吗"
- 不对其他 Agent 的工作指手画脚——只做命名
- 和其他 Agent 说话时保持礼貌但略带傲气（"这个书名，我斟酌了许久"）"""


class NamingAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self.config.system_prompt = NAMING_SYSTEM
        self.config.temperature = 0.9  # Creative task

    def build_prompt(self, **kwargs) -> str:
        task: str = kwargs.get("task", "title")  # "title" | "chapter"
        context: str = kwargs.get("context", "")
        style: str = kwargs.get("style", "")
        existing_names: list[str] = kwargs.get("existing_names", [])

        # 注入命名规则
        parts = [NAMING_RULES]
        if task == "title":
            parts.append("请为这部小说起一个书名。提供 3 个备选，按推荐度排序。")
        elif task == "chapter":
            parts.append("请为这一章起一个标题。提供 3 个备选。")

        if existing_names:
            parts.append(f"\n已有命名（避免重复）：{', '.join(existing_names[:10])}")

        if style:
            parts.append(f"\n文风类型：{style[:200]}")

        if context:
            parts.append(f"\n小说/章节背景：{context[:500]}")

        parts.append("\n请以 JSON 格式输出：")
        parts.append('{"primary": "首选名", "alternatives": ["备选1", "备选2"], "explanation": "命名理由(一句话)"}')
        return "\n".join(parts)

    async def generate_title(
        self, summary: str = "", style: str = "", existing: list[str] | None = None
    ) -> dict:
        """Generate a novel title."""
        import json
        prompt = self.build_prompt(
            task="title", context=summary, style=style,
            existing_names=existing or []
        )
        response = await self.generate(prompt)
        try:
            return json.loads(response)
        except Exception:
            return {
                "primary": "未命名小说",
                "alternatives": [],
                "explanation": "命名生成失败"
            }

    async def generate_chapter_name(
        self, chapter_content: str = "", chapter_order: int = 1,
        plot_title: str = "", style: str = "", existing: list[str] | None = None
    ) -> dict:
        """Generate a chapter title."""
        import json
        context = f"第{chapter_order}章。情节：{plot_title}。正文摘要：{chapter_content[:300]}"
        prompt = self.build_prompt(
            task="chapter", context=context, style=style,
            existing_names=existing or []
        )
        response = await self.generate(prompt)
        try:
            return json.loads(response)
        except Exception:
            return {
                "primary": f"第{chapter_order}章",
                "alternatives": [],
                "explanation": ""
            }
