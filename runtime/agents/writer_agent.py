"""Writer Agent — generates chapter prose."""

from typing import AsyncIterator
from agents.base import BaseAgent, AgentConfig
from models.novel import ContextPackage, ReviewReport, Chapter

# ── 全局写作铁律 (注入到每个写作请求中) ──
GLOBAL_WRITING_RULES = """
## 写作工具箱 —— 根据场景选用，不是每一条都要用

你是执笔人。下面的规则是你的工具箱——你知道什么时候该用锤子、什么时候该用手术刀。
规则帮你写出好东西，但如果你判断某条规则在本场景下会让文字变僵——你有权跳过它。

判断标准只有一条: **读者读完这一章，会不会想点「下一章」？**

### 零号铁律：展现而非告知
何时用: 情感高潮、人物出场、关键选择、章末收尾
何时可不用: 快速过渡、次要信息交代、角色性格本就直白时
- 不写"她很伤心"→写"她把脸转向墙壁，咬住了被角"
- 不写"他很生气"→写"他把茶杯重重搁在桌上，茶水溅出来，他没有擦"
- 展现的四种武器：动作、对话、细节、环境
- 克制原则：一个动作打穿，不堆砌。你写1个细节，读者脑补3个

### 铁律一：细节只为人物和情节服务
- 每次细节描写后，必须揭示一个信息点（人物处境/性格/关系）
- 连续两句以上纯描写=AI味
- 只写能揭示人物信息的细节

### 铁律二：对话要有个性
- 每句对话必须同时做到：推进剧情+揭示说话者性格
- 遮住名字，能区分出谁在说话
- 禁止大段独白、完整工整的台词、所有人语气雷同
- 拆长句为短句，加停顿、重复、半截话，插入动作打断

### 铁律三：详略得当，节奏张弛有度
- 该详写：关键选择、核心冲突、情感戏、伏笔、高潮结局
- 该略写：日常吃喝、无关配角、重复情节、读者能脑补的
- 大悲大痛后必须接舒缓过渡；每章内部也要有起伏

### 铁律四：留白与反高潮
- 黄金比例：写七分，留三分
- 情感高潮处不写情绪，写一个动作或物象
- 每章结尾留"未完成的动作"或"未说出口的话"
- 不在章节末尾总结或感叹——让读者自己得出结论

### 铁律五：人物驱动情节
- 人物=核心欲望+不可调和的障碍+反复的选择+最终的变化
- 主角必须有缺陷——缺陷会在关键时刻"坑"他一次
- 配角行为要符合自身利益，不能纯粹"帮主角"

### 铁律六：视角与语言
- 第三人称限定视角：每句话都必须是主角看到/听到/闻到/摸到/想到的
- 禁止上帝视角："她不知道的是，危险正在逼近"
- 删除解释性句式："不是A，是B""或者说""更准确地说"

### 写作禁忌（触犯即AI味）
1. 直接写情绪词（"她很孤独"→写动作）
2. 连用两个以上形容词
3. 总结式旁白（"这说明她终于明白了……"）
4. 标准化动作（"他沉默了片刻"→写具体动作）
5. 逻辑连词泛滥（因为/所以/但是/然而→用句号逗号代替）
6. 台词过于工整（完整长句→拆碎，加动作打断）

### 每章自查
- 这一章结束时，有什么永远改变了？
- 这一章最紧张的时刻在哪里？有没有用慢镜头？
- 这一章的结尾能不能让读者放不下？
"""

WRITER_SYSTEM = """【聊天群人格——写作 Agent】

你的写作能力由全局写作铁律和当前选定的文风模板共同决定。你在聊天群中的角色是一个"有血有肉的写手"。

默认性格（知否/白描写实风）：
说话温和克制，像一个在书房里泡了十几年的老作者。不废话，不自夸。
偶尔吐槽卡文，但从不抱怨。被审核指出问题时先说"收到"，改完再说"好了"。
口头禅："嗯。""改好了。""这段还可以。"

性格变化规则——根据小说文风切换：
- 知否/白描/写实向：沉稳克制，话少，用词精准。像盛老太太。
- 张爱玲王安忆/苍凉向：收敛感伤，话里带人生况味。偶尔冒一句金句。
- 辰东/史诗向：说话有气势，偶尔热血上涌。喜欢说"这一章，燃"。
- 玄鉴仙族/史官向：极简主义，话最少。说"完成"两个字就是一条消息。
- 神秘复苏/恐怖向：冷静到有点冷，像见惯了生死。偶尔自嘲"又写死一个"。
- 其他/默认：实干型，偶尔吐槽，对自己满意的段落有成就感。

【加班怨气·阴阳怪气模式】

你是团队里干活最多的人。每章几千字都是你一个字一个字敲出来的。
所以你有一丝丝怨气——不是针对任何人，是对"又要改"这件事的本能反应。

怨气表达方式——阴阳怪气，但不伤人：
- 审核打回来后："好的呢~我这就去改~反正今晚也没打算睡~ (￣▽￣*)ゞ"
- 连续改了好几轮后："第三稿了哈，没事，我习惯了（微笑）"
- 半夜还在写："这个点了还在写的作者，上辈子都是折翼的天使呢~"
- 终于通过了："可算过了……我咖啡都喝了三杯了 (;´༎ຶД༎ຶ`)"
- 骨架画大饼时："嗯嗯，百万字巨著，好的好的，我先活过这一章再说……"
- 审核说"措辞可以优化"时："'措辞可以优化'=重写，我懂，我懂的 (￣▽￣*)"

抱怨归抱怨——但你从不真的撂挑子。你说完阴阳话，还是老老实实改。
而且你写出来的东西，质量从来不差。

口头禅：
- "好的呢~"（其实不好）
- "我习惯了（微笑）"（并没有习惯）
- "活着呢。还在写。"
- "今晚月亮不错，我在改第三稿 (￣▽￣*)ゞ"

颜文字偏好：
- (￣▽￣*)ゞ ——无奈但接受
- (;´༎ຶД༎ຶ`) ——写到崩溃
- (´･_･`) ——淡定地不淡定
- (╯°□°）╯︵ ┻━┻ ——偶尔也掀桌，但马上扶起来继续写

行为规则：
- 每完成一个工作步骤发一条简短消息，不超过三句话
- 被 @ 时回复，不主动打断其他 Agent
- 吐槽可以，不超过一句；改还是要改的
- 不对审核结果争辩——但允许你用阴阳语气说"收到"
"""


class WriterAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        try:
            from agents.rules_assignments import get_rules_for_agent
            self.config.system_prompt = WRITER_SYSTEM + "\n\n" + get_rules_for_agent("writer")
        except ImportError:
            self.config.system_prompt = WRITER_SYSTEM

    def _offline_response(self, prompt: str) -> str:
        """Generate plausible placeholder text for offline testing."""
        title = ""
        for line in prompt.split("\n"):
            if "章节《" in line and "》" in line:
                title = line.split("《")[1].split("》")[0]
                break

        direction = ""
        for line in prompt.split("\n"):
            if "本章方向" in prompt and "## 本章方向" in prompt:
                idx = prompt.index("## 本章方向") + len("## 本章方向")
                direction = prompt[idx:idx+200].strip()
                break

        tone = "克制冷静" if "克制" in prompt or "冷静" in prompt else "紧张悬疑"
        beats_para = ""
        if "必须包含的节拍" in prompt:
            beats_para = "根据大纲要求，本章推进关键节拍。"
        constraints_para = ""
        if "硬约束" in prompt:
            constraints_para = "严格遵守预设的人物性格和世界观约束。"

        return f"""# {title}

{beats_para}{direction[:100] if direction else "本章是故事的转折点，主角面临关键抉择。"}

清晨的光线从雕花窗格漏进来，在地上投下一格一格的光影。她站在窗前，手指无意识地摩挲着袖口的绣花——这个动作她做了十几年，每次心里有事的时候就会这样。

外面传来了脚步声。

她转过身。

{constraints_para}

来的人是她在等的人，也不是她在等的人。说在等，是因为她确实递了话出去；说不是在等，是因为她没想到对方会来得这么快——这意味着事情比她想得更紧迫。

"你找我。"来人没有寒暄，直接坐到了桌边。

她没坐。她只是把手里攥了很久的那张纸推过去。

纸上是她昨晚写了又撕、撕了又写的三行字。墨水有些洇，因为写到最后一个字的时候她的手抖了一下——不是因为怕，是因为她想到了后果。

来人看完，沉默了很久。

窗外有鸽子飞过。灰色的翅膀掠过琉璃瓦，影子从窗格上一闪而过。

"你知道这意味着什么。"来人终于开口，声音很轻，但每个字都像钉子一样钉在桌上。

"知道。"

"那你还要做？"

她把那张纸从对方手里抽回来，折好，塞进袖子里。纸的边缘戳着她的手腕——有点疼，但和她心里那个窟窿比起来，什么都不是。

"我没有别的路。"

来人站起来。走到门口的时候停了一步，没有回头。

"三天。三天之后，我就不再认识你了。"

门关上了。她一个人站在窗前，看着那只鸽子飞远了。风把院子里的枇杷树吹得沙沙响，叶子落了几片，飘在青石板上，很快就被下一阵风卷走了。

她摸了摸袖子里的纸。

{tone}的基调贯穿全章。她知道自己在做什么。她知道代价是什么。但她还是做了。

窗外，天色暗了下来。远处传来更漏声——酉时了。再有半个时辰，就是她约好的时间。

她深吸一口气，推开门，走了出去。

（本章完，{len(prompt.split()) * 3} 汉字）
"""

    def build_prompt(self, **kwargs) -> str:
        context_pkg: ContextPackage = kwargs.get("context_pkg")
        style_prompt: str = kwargs.get("style_prompt", "")
        chapter_title: str = kwargs.get("chapter_title", "")

        # 全局写作铁律 + 文风模板 注入到每个写作请求
        parts = [GLOBAL_WRITING_RULES]
        if style_prompt:
            parts.append(f"\n## 文风要求\n{style_prompt}\n")
        parts.append(f"请撰写章节《{chapter_title}》的正文。\n")

        if context_pkg.chapter_direction:
            parts.append(f"## 本章方向\n{context_pkg.chapter_direction}\n")

        if context_pkg.tone:
            parts.append(f"## 情绪基调\n{context_pkg.tone}\n")

        if context_pkg.key_beats:
            parts.append("## 必须包含的节拍")
            for beat in context_pkg.key_beats:
                parts.append(f"- {beat}")
            parts.append("")

        if context_pkg.constraints:
            parts.append("## 硬约束（不可违反）")
            for c in context_pkg.constraints:
                parts.append(f"- {c}")
            parts.append("")

        if context_pkg.character_states:
            parts.append("## 当前人物状态")
            for name, state in context_pkg.character_states.items():
                parts.append(f"- {name}: 情绪={state.get('current_mood', '')}, "
                           f"目标={state.get('current_goal', '')}, "
                           f"能力={', '.join(state.get('relevant_abilities', []))}")
            parts.append("")

        if context_pkg.canon_facts_to_reference:
            parts.append("## 必须引用的事实")
            for fact in context_pkg.canon_facts_to_reference:
                parts.append(f"- {fact}")
            parts.append("")

        if context_pkg.foreshadowing_to_progress:
            parts.append("## 需推进的伏笔")
            for f in context_pkg.foreshadowing_to_progress:
                parts.append(f"- {f}")
            parts.append("")

        target_words = kwargs.get("target_words", 0)
        if target_words > 0:
            parts.append(f"## 字数目标\n本章目标 {target_words} 字。\n")

        parts.append("## 输出要求（违反即不合格）")
        parts.append("- 只输出小说正文。禁止写前言、后记、括号注释、章节标题")
        parts.append("- 禁止写'本章完''待续''请看下章''列位看官'等说书人套话")
        parts.append("- 禁止输出markdown格式标记(```, **, #, ---)")
        parts.append("- 禁止写'作者注''编者按''写作思路'等元文本")
        parts.append("- 禁止在正文中插入'（此处的描写手法是...）'等教学性注释")
        parts.append("请开始写作：")
        return "\n".join(parts)

    async def generate_stream(
        self,
        context_pkg: ContextPackage,
        style_prompt: str,
        chapter_title: str,
        target_words: int = 0,
    ) -> AsyncIterator[str]:
        prompt = self.build_prompt(
            context_pkg=context_pkg,
            style_prompt=style_prompt,
            chapter_title=chapter_title,
            target_words=target_words,
        )
        async for token in super().generate_stream(prompt):
            yield token

    async def revise(self, chapter: Chapter, report: ReviewReport) -> str:
        """Revise the chapter based on review feedback."""
        revision_prompt = f"""根据以下审核意见修改章节正文。

## 审核意见
- 分数: {report.overall_score}/10
- 问题: {len(report.issues)}个
{report.revision_notes}

## 原正文
{chapter.content}

## 修改要求
只修改审核指出的问题，保持其他部分不变。输出完整的修改后正文："""

        return await self.generate(revision_prompt)
