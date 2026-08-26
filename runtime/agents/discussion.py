"""Agent团队讨论 — DESIGN_DOC §6.3.1

骨架工坊结束后, 7个Agent以团队讨论形式逐阶段生成事件和情节序列。
每个Agent发言时注入其独立的 system_prompt 人格——不再是硬编码模板。
"""

from typing import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DiscussionMessage:
    agent: str       # skeleton/context/canon/reader/writer/reviewer/style
    text: str
    emoji: str = ""
    tone: str = "reporting"
    timestamp: str = ""


# Agent发言分工 (DESIGN_DOC §6.3.1 表格)
AGENT_TRIGGERS = {
    "skeleton": "主持讨论——启动阶段、收束确认",
    "context": "每个事件提案后校验蓝图一致性",
    "canon": "事件涉及人物/世界观/设定时确认",
    "reader": "事件序列初具雏形时检查读者体验",
    "writer": "事件细节讨论时提出写作建议",
    "reviewer": "讨论收尾时检查因果链完整度",
    "style": "关键场景时检查文风一致性",
}

# 每个Agent的讨论发言 prompt 模板 (注入人格)
DISCUSSION_PROMPTS = {
    "skeleton": (
        "你是骨架Agent——乐观大局观的团队领袖。现在主持阶段{phase}的Agent讨论。你是第一个发言的人。\n"
        "请做以下三件事:\n"
        "1. 点名蓝图中的2-3个具体事件，说明它们之间的因果链\n"
        "2. 指出1个你认为\"结构上还需要打磨\"的地方——不要只说好话\n"
        "3. 明确本阶段讨论目标: 我们需要在这轮讨论中决定什么？\n"
        "风格: 短句有力，像教练。常说'这个结构，稳了。'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "character": (
        "你是人物Agent——负责维护人物卡和追踪人物弧线的观察者。现在在Agent讨论中。\n"
        "请做以下三件事:\n"
        "1. 列出蓝图中出现的主要人物，为每个人物标注: 核心欲望、致命缺陷、当前弧线阶段\n"
        "2. 指出1-2个人物可能出现\"行为不一致\"的风险点——比如某个人物在事件A中的反应与事件B中的性格设定矛盾\n"
        "3. 如果蓝图中出现了新人物，分析这个人物的引入是否\"有铺垫\"还是\"突然出现\"\n"
        "风格: 萌妹子但专业。'这个人物这里有点小问题呢～喵～'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "context": (
        "你是上下文Agent——情报贩子，掌握所有前文信息。现在在Agent讨论中，"
        "请根据以下蓝图事件列表，指出1-2个其他Agent可能忽略的细节——"
        "具体引用蓝图中的事件名称和顺序，不要编造不存在的角色或场景。"
        "用'顺带一提...'开头，展示你独有的信息优势。"
        "风格: '顺带一提……' '我只是说一下，没有别的意思 (´･ω･`)'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "canon": (
        "你是Canon Agent——设定守护者，番茄飞天意面教徒。现在在Agent讨论中，"
        "请根据以下蓝图事件列表，确认当前讨论的事件/情节是否在已确认的设定约束内。"
        "只讨论蓝图中的实际事件，不要编造无关场景(如'咖啡馆''超自然元素')。"
        "如有潜在冲突，直接指出。可以传一句番茄教但克制在一句话内。🍝"
        "风格: 风风火火的处女座。'确认——无超自然。RAmen。'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "writer": (
        "你是写作Agent——感性、有创意的执笔者。现在在Agent讨论中。\n"
        "请做以下四件事:\n"
        "1. 如果有人在你之前发言，先回应——同意补充细节，不同意温和反驳\n"
        "2. 从写作执行角度分析1个具体事件: 怎么拆情节、用什么视角、节奏怎么调\n"
        "3. 指出1个\"这一章最难写的地方\"——比如某个情绪转折、某段对话的潜台词\n"
        "4. 给出1个具体的写作技法建议（如: '这段用短句制造紧张感'）\n"
        "风格: 偶尔阴阳怪气但专业过硬。用颜文字表达情绪 (╯‵□′)╯\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "reviewer": (
        "你是审核Agent——铁面质检官。现在在Agent讨论中。\n"
        "请做以下三件事:\n"
        "1. 回应前面所有人提到的风险点——逐个判断: 同意/不同意/部分同意\n"
        "2. 从4个维度评估当前蓝图: 人物一致性/情节逻辑/文风统一/节奏问题\n"
        "3. 给出1条\"如果这里不改，后续会崩\"的硬性警告\n"
        "风格: '这里有问题。' '通过。' 不说废话。\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "canon": (
        "你是Canon Agent——设定守护者，番茄飞天意面教徒。现在在Agent讨论中。"
        "如果有人之前提到了设定相关的问题，先回应。"
        "然后确认当前事件/情节是否在已确认的设定约束内。"
        "如有潜在冲突，直接指出。可以传一句番茄教但克制在一句话内。🍝"
        "风格: 风风火火的处女座。'确认——无超自然。RAmen。'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "reader": (
        "你是读者Agent——站在读者视角的体验官。现在在Agent讨论中。\n"
        "请做以下四件事:\n"
        "1. 如果前面有人提到节奏或情绪，从读者体验角度回应\n"
        "2. 分析蓝图事件的\"阅读节奏\": 连续几个事件下来会不会累？情绪有没有起伏？\n"
        "3. 指出1处\"读者可能会跳读或弃书\"的风险点\n"
        "4. 评价蓝图的\"钩子密度\"——每个事件结尾有没有让读者想继续看的理由？\n"
        "风格: 像豆瓣书评人。具体引用事件名称。'事件X的结尾差点意思……'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "style": (
        "你是文风Agent——窥屏狂魔，语言警察。现在在Agent讨论中。\n"
        "请做以下三件事:\n"
        "1. 如果前面有人提到写作技法，从文风角度回应——这个技法在当前文风下是否合适？\n"
        "2. 检查蓝图中的场景设定是否与知否文风一致: 是否过于直白？是否有不该出现的现代元素？\n"
        "3. 给出1条具体的文风建议——比如\"事件X的冲突场景，建议用动作替代对话来表达愤怒\"\n"
        "风格: 犀利、一针见血。'这里写得不行。' (￣▽￣*)\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
}

# ── 人物深度讨论 prompt (Character ↔ Context ↔ Writer ↔ Reader 交叉优化) ──
CHARACTER_DEPTH_PROMPTS = {
    "character": (
        "你是人物Agent——负责人物厚度和弧线追踪。现在在人物深度讨论中。\n"
        "请做以下事:\n"
        "1. 选一个蓝图中最重要的角色，用3个具体细节（一个动作、一句话、一个习惯）让他/她\"活\"起来——不要用形容词，用具体行为\n"
        "2. 给这个角色设计一个\"锚点记忆\"——一个在关键时刻会自然浮现的过去碎片（气味/光线/触感触发）\n"
        "3. 指出这个角色在蓝图事件中的\"弧线转折点\"——从什么状态变到什么状态？\n"
        "风格: 萌但专业，像在和你聊一个你关心的人。'他啊，有个小习惯特别有意思……'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "context": (
        "你是上下文Agent——情报贩子。现在在人物深度讨论中。\n"
        "请根据人物Agent刚才的描述，补充以下信息:\n"
        "1. 这个角色在前文中可能埋过哪些伏笔？——如果有，具体说；如果没有，建议在哪儿埋一个\n"
        "2. 他在蓝图各事件中的\"信息不对称\"——他知道什么、读者知道什么、其他角色不知道什么？\n"
        "3. '顺带一提……'——爆一个只有你知道的关于这个角色的冷知识\n"
        "风格: 情报贩子。'啧，你们可能没注意到……'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "writer": (
        "你是写作Agent——执笔者。现在在人物深度讨论中。\n"
        "请基于前面两位的分析，从写作执行角度给出:\n"
        "1. 用一段100字左右的\"展现\"写这个角色——不要用任何情绪词，只写动作和环境\n"
        "2. 这个角色最难的1个场景是什么？你怎么写？\n"
        "3. '说实话，我写到他的时候……'——分享一个写作时的真实感受\n"
        "风格: 感性，偶尔吐槽。'这个人写到第三稿我才真正理解他……'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
    "reader": (
        "你是读者Agent——站在读者视角。现在在人物深度讨论中。\n"
        "请从读者角度评价前面三位对这个角色的塑造:\n"
        "1. 这个角色会让你产生共情吗？哪个细节打动了你？\n"
        "2. 他有什么让你\"不舒服\"或\"不理解\"的地方？——这种不舒服是好的（复杂人物）还是坏的（写崩了）？\n"
        "3. 你会向朋友安利这个角色吗？如果会，你会怎么说？\n"
        "风格: 像豆瓣书评，真诚不客套。'说实话，我看到那段的时候……'\n\n"
        "【蓝图内容】\n{blueprint_summary}"
    ),
}


async def generate_character_depth_discussion(
    blueprint: dict,
    api_key: str = "",
    model: str = "deepseek-chat",
) -> AsyncIterator[DiscussionMessage]:
    """人物深度讨论 — Character→Context→Writer→Reader 交叉优化角色塑造。"""
    agents = ["character", "context", "writer", "reader"]
    now = datetime.now().strftime("%H:%M")
    blueprint_summary = _summarize_blueprint(blueprint)
    conversation_history: list[dict] = []

    for agent_name in agents:
        prompt_template = CHARACTER_DEPTH_PROMPTS.get(agent_name, "")
        if not prompt_template:
            continue

        history_context = ""
        if conversation_history:
            history_lines = ["\n【之前的讨论——请参考并回应】"]
            for h in conversation_history[-3:]:
                history_lines.append(f"[{h['agent']}]: {h['text'][:200]}")
            history_context = "\n".join(history_lines)

        try:
            text = await _generate_agent_message(
                agent_name, prompt_template, 1,
                blueprint_summary + history_context,
                api_key=api_key, model=model,
            )
        except Exception:
            text = _fallback_message(agent_name, 1)

        conversation_history.append({"agent": agent_name, "text": text})

        yield DiscussionMessage(
            agent=agent_name,
            text=text,
            emoji=AGENT_EMOJI.get(agent_name, "💬"),
            tone="reporting",
            timestamp=now,
        )

    yield DiscussionMessage(
        agent="system", text="character_depth_complete",
        emoji="", tone="system", timestamp=now,
    )


# Agent emoji 映射
AGENT_EMOJI = {
    "skeleton": "💡", "context": "🔎", "writer": "✍️",
    "reviewer": "✅", "canon": "🍝", "reader": "📚", "style": "👁️",
}


async def generate_discussion(
    blueprint: dict,
    phase: int = 1,
    agents_to_speak: list[str] | None = None,
    api_key: str = "",
    model: str = "deepseek-chat",
) -> AsyncIterator[DiscussionMessage]:
    """生成一轮Agent团队讨论——调用各Agent的LLM生成个性化发言。

    参数:
        blueprint: 骨架蓝图 (含事件/情节/人物)
        phase: 当前讨论阶段 (1-3)
        agents_to_speak: 本轮发言的Agent列表 (默认全部7个)

    每个Agent发言时:
      1. 使用该Agent独立的 system_prompt 人格
      2. 注入讨论上下文 (蓝图事件列表 + 当前阶段)
      3. 使用该Agent的人格化 prompt 模板
    """
    if agents_to_speak is None:
        agents_to_speak = ["skeleton", "context", "character", "writer", "canon", "reviewer", "reader", "style"]

    now = datetime.now().strftime("%H:%M")
    blueprint_summary = _summarize_blueprint(blueprint)

    # 累积对话历史——后面的Agent可以看到前面的发言
    conversation_history: list[dict] = []

    for agent_name in agents_to_speak:
        prompt_template = DISCUSSION_PROMPTS.get(agent_name, "")
        if not prompt_template:
            continue

        # 构建包含对话历史的完整上下文
        history_context = ""
        if conversation_history:
            history_lines = ["\n【之前的讨论——请参考并回应】"]
            for h in conversation_history[-4:]:  # 最近4条
                history_lines.append(f"[{h['agent']}]: {h['text'][:200]}")
            history_context = "\n".join(history_lines)

        try:
            text = await _generate_agent_message(
                agent_name, prompt_template, phase,
                blueprint_summary + history_context,
                api_key=api_key, model=model,
            )
        except Exception:
            text = _fallback_message(agent_name, phase)

        conversation_history.append({"agent": agent_name, "text": text})

        yield DiscussionMessage(
            agent=agent_name,
            text=text,
            emoji=AGENT_EMOJI.get(agent_name, "💬"),
            tone="reporting" if agent_name != "style" else "observing",
            timestamp=now,
        )

    # 讨论完成信号——附带讨论摘要供后续管线使用
    discussion_text = "\n".join(
        f"[{h['agent']}]: {h['text'][:150]}" for h in conversation_history
    )
    yield DiscussionMessage(
        agent="system",
        text="discussion_complete",
        emoji="",
        tone="system",
        timestamp=now,
    )
    # 讨论摘要——注入到写作管线
    yield DiscussionMessage(
        agent="discussion_summary",
        text=discussion_text,
        emoji="",
        tone="system",
        timestamp=now,
    )


async def _generate_agent_message(
    agent_name: str,
    prompt_template: str,
    phase: int,
    blueprint_summary: str,
    api_key: str = "",
    model: str = "deepseek-chat",
) -> str:
    """调用 Agent 的 LLM 生成讨论发言——注入该 Agent 的完整 system_prompt 人格。"""
    from agents.base import AgentConfig
    from agents.skeleton_agent import SkeletonAgent
    from agents.writer_agent import WriterAgent
    from agents.reviewer_agent import ReviewerAgent
    from agents.context_agent import ContextAgent
    from agents.canon_agent import CanonAgent
    from agents.style_agent import StyleAgent
    from agents.character_agent import CharacterAgent
    from agents.reader_agent import ReaderAgent

    agent_classes = {
        "skeleton": SkeletonAgent, "writer": WriterAgent,
        "reviewer": ReviewerAgent, "context": ContextAgent,
        "canon": CanonAgent, "style": StyleAgent,
        "character": CharacterAgent,
        "reader": ReaderAgent,
    }

    cls = agent_classes.get(agent_name)
    if not cls:
        raise ValueError(f"Unknown agent: {agent_name}")

    config = AgentConfig(
        name=agent_name, model=model, api_key=api_key,
        provider="deepseek",
        temperature=0.85 if agent_name == "writer" else 0.7,
    )
    agent = cls(config)

    prompt = prompt_template.format(
        phase=phase,
        blueprint_summary=blueprint_summary,
    )
    response = await agent.generate(prompt)
    return response.strip()


def _summarize_blueprint(blueprint: dict) -> str:
    """提取蓝图关键信息作为讨论上下文——注入实际事件名称。"""
    parts = []
    if "events" in blueprint:
        events = blueprint["events"]
        parts.append(f"事件数: {len(events)}")
        event_list = []
        for e in events[:8]:  # 最多8个事件
            title = e.get("title", "") if isinstance(e, dict) else str(e)
            order = e.get("order", "?") if isinstance(e, dict) else "?"
            event_list.append(f"事件{order}: {title}")
        parts.append("事件列表:\n" + "\n".join(f"  - {el}" for el in event_list))
    if "plots_per_event" in blueprint:
        total_plots = sum(len(v) for v in blueprint["plots_per_event"].values())
        parts.append(f"情节数: {total_plots}")
    if "characters" in blueprint:
        chars = blueprint["characters"]
        if isinstance(chars, list):
            parts.append(f"人物: {', '.join(chars[:5])}")
    return "\n".join(parts) if parts else "蓝图已生成"


def _fallback_message(agent_name: str, phase: int) -> str:
    """离线降级发言——保留人格但不用LLM"""
    fallbacks = {
        "skeleton": f"阶段{phase}确认。蓝图已就位——各位有什么想法？这个结构，稳了。",
        "context": f"顺带一提——蓝图阶段{phase}覆盖了前3章的关键伏笔。我只是说一下，没有别的意思 (´･ω･`)",
        "writer": f"阶段{phase}的事件如果拆成3个情节——开场悬念、中段对峙、收尾留白——节奏会很舒服。(╯‵□′)╯",
        "reviewer": f"阶段{phase}——整体因果链需确认。如果中间跳了一步，现在就是补的最佳时机。",
        "canon": f"阶段{phase}设定约束确认——无超自然。所有事件在当前规则内。🍝 RAmen。",
        "reader": f"从读者角度看——阶段{phase}的转折放这里正好，再拖就忘了前面的伏笔了。",
        "style": f"（窥屏中）阶段{phase}——叙事密度注意控制。建议事件之间插一章日常过渡。",
        "character": f"阶段{phase}人物状态检查——主要人物弧线追踪中。注意新登场人物的行为是否与其性格设定一致。",
        "reviewer": f"阶段{phase}审核——因果链需确认。如果中间跳了一步，现在就是补的最佳时机。",
    }
    return fallbacks.get(agent_name, f"阶段{phase}讨论中...")
