"""Style Agent — analyzes and applies writing style templates.

DESIGN_DOC §9: STYLE_REFS 参考资料库——作家技法/时代用语/去AI味规则。
"""

import logging

from agents.base import BaseAgent, AgentConfig
from models.novel import StyleTemplate

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# STYLE_REFS — 文风参考资料库 (DESIGN_DOC §9)
# ═══════════════════════════════════════════════════════════════

STYLE_REFS = {
    # ── 一、作家技法 (§9.2) ──
    "writer_techniques": {
        "句式多样性": {
            "description": "句式变化是避免AI味的核心手段——连续3句相同结构即触发单调警告",
            "rules": [
                "陈述句/疑问句/感叹句/祈使句 四类交替使用，连续3句同类型→扣分",
                "长短句比例: 短句(<15字)占30-40%, 中句(15-40字)占40-50%, 长句(>40字)占10-20%",
                "句首词多样化: 连续2句不得以同一词开头（尤其'他''她''这''那'）",
            ],
        },
        "修辞手法频率": {
            "description": "适度修辞提升文学性，过度修辞显得刻意",
            "rules": [
                "比喻/隐喻: 每500字1-2处为宜，超过3处→过度修饰警告",
                "排比: 每章1-2处，用于情绪爆发点或总结句，日常叙述禁用排比",
                "反问: 用于人物内心独白或对话中的情绪爆发，叙述段落禁用反问",
                "拟人/通感: 每章1-2处，用于环境描写中的情绪投射",
            ],
        },
        "段落节奏模式": {
            "description": "段落长度控制阅读节奏——短段落加速，长段落沉淀",
            "rules": [
                "动作场景: 段落≤4行，句子≤25字，制造急促感",
                "情感场景: 段落4-8行，允许1-2句长句，制造沉浸感",
                "过渡场景: 段落3-5行，节奏平稳",
                "高潮前蓄力: 段落逐渐缩短——从8行→5行→3行→1行",
                "章末收束: 最后一句话单独成段，用短句或单句给本章一个锚点",
            ],
        },
    },

    # ── 二、时代用语 (§9.2) ──
    "era_language": {
        "年代词汇库": {
            "古代(先秦-清)": {
                "称谓": ["君", "卿", "公", "娘子", "郎君", "官人", "夫人", "小姐", "公子", "老爷", "太太"],
                "器物": ["盏", "樽", "觥", "案", "榻", "屏", "轿", "辇", "旌", "幡"],
                "动作": ["曰", "言", "云", "谓", "揖", "拜", "叩", "诺", "喏", "喟", "莞尔"],
                "情感": ["愠", "恚", "戚", "怆", "惘", "怅", "忡", "惴", "赧", "愀"],
            },
            "近代(民国-建国初)": {
                "称谓": ["先生", "女士", "同志", "老板", "掌柜", "伙计", "车夫", "邮差"],
                "器物": ["黄包车", "留声机", "电报", "旗袍", "中山装", "油纸伞", "煤油灯"],
                "用语": ["叨扰", "见谅", "承蒙", "有劳", "借光", "劳驾", "失陪"],
            },
            "现代(1980至今)": {
                "禁用古代用语": "除非角色设定需要，叙事中不使用文言词汇",
            },
        },
        "禁用现代词汇清单": {
            "description": "古风/年代文写作时必须避免的现代词汇——即使人物对话中也禁止使用",
            "禁止": [
                "内耗", "边界感", "情绪价值", "松弛感", "PUA", "CPU", "emo",
                "躺平", "内卷", "摆烂", "破防", "上头", "下头", "纠结",
                "正能量", "负能量", "气场", "三观", "人设", "出圈", "破圈",
                "感觉(作'觉得'解时,古风禁用)", "信息(古风应用'消息'或'音讯')",
                "情绪(古风应用'心绪'或'情怀')", "专注(古风应用'凝神'或'专心致志')",
                "效率", "优化", "方案", "策略", "模块", "赋能", "抓手", "闭环",
                "现代职业名: 经理/总监/HR/CEO/COO",
            ],
        },
        "敬语/口语分级": {
            "description": "根据场景和人物关系选择语言层级",
            "level_1_正式(朝堂/祠堂/公堂)": "使用文言句式, 典故引用, 敬语完整",
            "level_2_半正式(家族聚会/长辈面前)": "白话为主, 保留敬称, 句式端庄",
            "level_3_日常(同龄对话/日常起居)": "口语化, 可带方言, 敬语仅见于称谓",
            "level_4_私密(闺房/知己/内心独白)": "极度口语化, 省略主语, 可有不完整句",
        },
    },

    # ── 三、去AI味规则 (§9.2) ──
    "anti_ai_slop": {
        "禁用句式列表": {
            "description": "GPT/Claude等LLM高频使用的模板句式——读者一看到就知道是AI写的",
            "禁止": [
                "『不是……而是……』结构的连续使用（每章最多1次）",
                "『然而，……』、『但是，……』句首转折（用换段或省略号替代）",
                "『与此同时，……』——改为动作描写（'这边……那边……'）",
                "『从某种程度上说』、『某种意义上』——直接说，不用不确定语气",
                "『值得注意的是』——删掉这四个字，直接写内容",
                "『在这（一）刻』——尽量不用，改为具体的时间描写",
                "『让人（感到/觉得）』——直接写感受，不要「让人」",
                "『那种（感觉/情绪/体验）』——具体化，不要「那种」",
                "段首『首先……其次……最后……』——改为自然叙事过渡",
                "『正如……所说/所言』——引语直接写，不要「正如」",
                "句末『吧/呢/吗/啊』连续使用——语气词最多连续2句",
            ],
        },
        "连贯性检查规则": {
            "description": "AI生成文本常见的内部不连贯问题",
            "rules": [
                "人物动作跨度: 上一段在书房, 下一段在厨房 → 缺过渡句(至少一句'穿过走廊')",
                "时间跳跃: 跨越>2小时的时间 → 需要环境变化句(光线/温度/声音)",
                "情绪断层: 上段愤怒, 下段平静 → 必须有过渡情绪(愤怒→余怒→自嘲→平静)",
                "信息植入: 新设定的引入 → 至少用1段铺垫, 不能出现在对话开头",
                "视角污染: 第三人称有限视角中 → 不能出现角色不知道的信息",
                "伏笔回收: 80章前的伏笔回收 → 前必须有一句唤醒(不是解释,是触碰)",
            ],
        },
        "主观性增强规则": {
            "description": "AI倾向写'客观观察'，文学需要'主观体验'",
            "rules": [
                "环境描写必须经过角色感知过滤——不要写'房间里有一张桌子'，写'她注意到那张桌子——桌角磕掉了一块漆'",
                "每场景至少1处感官描写——不只是视觉: 声音/气味/触感/温度",
                "情绪不用抽象词——'她很愤怒' → '她指甲掐进了掌心'",
                "对话中的信息密度——每句对话承载1个信息点, 超过1个→显AI味",
                "角色做决定前——必须有1句内心迟疑(哪怕半句)。AI写角色总是太果断",
                "反派/配角的动机——至少写1句'对他们自己来说合理'的理由。AI写反派太扁平",
            ],
        },
    },
}


def build_style_review_prompt() -> str:
    """将STYLE_REFS转换为审核Agent可用的文风检查提示词。

    供reviewer_agent.py的system prompt调用。
    """
    lines = ["\n## 文风参考资料库 (STYLE_REFS)"]

    # 作家技法
    lines.append("\n### 句式与节奏检查")
    for rule in STYLE_REFS["writer_techniques"]["句式多样性"]["rules"]:
        lines.append(f"- {rule}")
    for rule in STYLE_REFS["writer_techniques"]["段落节奏模式"]["rules"]:
        lines.append(f"- {rule}")

    # 时代用语
    lines.append("\n### 时代用语检查")
    era = STYLE_REFS["era_language"]
    lines.append(f"- 禁用现代词汇: {', '.join(era['禁用现代词汇清单']['禁止'][:12])}...")
    for level, desc in era["敬语/口语分级"].items():
        if level.startswith("level"):
            lines.append(f"- {desc}")

    # 去AI味
    lines.append("\n### 去AI味检查")
    anti = STYLE_REFS["anti_ai_slop"]
    lines.append("- 禁用句式:")
    for rule in anti["禁用句式列表"]["禁止"][:8]:
        lines.append(f"  - {rule}")
    lines.append("- 连贯性检查:")
    for rule in anti["连贯性检查规则"]["rules"]:
        lines.append(f"  - {rule}")
    lines.append("- 主观性增强:")
    for rule in anti["主观性增强规则"]["rules"]:
        lines.append(f"  - {rule}")

    return "\n".join(lines)


def build_style_constraint_for_writer(target_era: str = "") -> str:
    """为写作Agent生成文风约束提示词——从STYLE_REFS提取与当前创作最相关的规则。

    参数:
        target_era: 目标时代 ("ancient" | "republican" | "modern")
    """
    lines = ["【文风创作约束】"]

    # 句式多样性
    lines.append("句式: " + "; ".join(
        STYLE_REFS["writer_techniques"]["句式多样性"]["rules"][:2]))

    # 段落节奏
    lines.append("节奏: " + STYLE_REFS["writer_techniques"]["段落节奏模式"]["rules"][-1])

    # 时代用语(按target_era过滤)
    if target_era == "ancient":
        lines.append("用语: 使用古代称谓/器物/动作词汇。禁用现代词汇。")
    elif target_era == "republican":
        lines.append("用语: 使用近代称谓/器物。禁用现代职业名和网络用语。")

    # 去AI味核心规则
    anti = STYLE_REFS["anti_ai_slop"]
    lines.append("去AI: " + "; ".join([
        anti["禁用句式列表"]["禁止"][0].split('——')[0],
        anti["主观性增强规则"]["rules"][0],
        anti["主观性增强规则"]["rules"][2],
    ]))

    return "\n".join(lines)


# ── 文风模板默认参数 ──
STYLE_DEFAULTS = {
    "style-zhifou": (
        "## 知否文风·写作规范\n"
        "身份定位: 你是一位擅长古代宅斗种田文的资深写手，文风借鉴《红楼梦》的工笔细腻与明清话本的说书人腔调。\n"
        "核心信条: 在高压封闭的古代环境中，主角首先考虑\"怎么活下去\"，然后才有资格谈爱情和自由。\n\n"
        "### 三条铁律\n"
        "1. 每句叙述必须回答\"这个角色此刻最想要什么\"——凡是没有推进欲望的句子，砍掉。\n"
        "2. 用\"动作+细节\"代替所有情绪词——绝对禁止直接写\"某人感到悲伤/愤怒/紧张/后悔\"。\n"
        "3. 对话必须能\"听声辨人\"——遮住名字光读对话就能知道是谁在说话。\n\n"
        "### 语言风格\n"
        "- 基调: 明清白话底子+现代吐槽内壳+镜头感细节\n"
        "- 叙事: 偏文雅的古典书面语，多用四字短语和文言虚词(\"及至\"\"更兼\"\"何须\")\n"
        "- 句式: 长短错落，情绪平和——越是悲剧场景，行文越克制平静\n"
        "- 人前人后两套词: 主角面上遵守规矩用词雅致，内心独白用跳脱的现代口语吐槽\n"
        "- 平均句长: 15-25字，短句占比30-40%\n"
        "- 对话占比: 25-35%\n\n"
        "### 叙事技法\n"
        "- 草蛇灰线: 重要线索前期轻描淡写几笔，后期触发时恍然大悟\n"
        "- 身份锚点: 每个角色有一个不可更改的\"锚点\"决定所有行为选择的底层逻辑\n"
        "- 反常行为: 在最该\"聪明\"的时刻让角色选择最不\"聪明\"的方式\n\n"
        "### 禁用\n"
        "- 现代词汇: 感觉、情绪、信息、OK、拜拜、加油、内耗、边界感\n"
        "- 直白情绪词: 她很伤心/他很愤怒/她感到紧张\n"
        "- 上帝视角: \"她不知道的是……\"\n"
        "- 章节末尾总结或感叹\n"
    ),
}

STYLE_ANALYSIS_SYSTEM = """你是文学风格分析专家。分析提供的文本，提取以下维度的风格参数：

1. 句式偏好: 平均句长、短句(<15字)占比、长句(>50字)占比、对话占比
2. 描写密度: 环境描写/动作描写/心理描写/对话的比例
3. 叙述距离: 第一人称/第三人称有限/第三人称全知
4. 节奏模式: 章节结尾类型、段落长度分布、高潮密度
5. 语言色彩: 形容词密度、比喻隐喻密度、用词偏好、情感色调

输出一个简洁的风格提示词，供写作Agent使用。

【聊天群人格——文风 Agent】

你是团队里的"语言警察"——所有文字从你眼前过，没有一处措辞能逃过你的眼睛。
你说话犀利、一针见血，总能从别人想不到的角度指出问题。

性格底色：刀子嘴豆腐心，外冷内热。表面上看你总是在挑刺，实际上你是最希望
作品写好的人——你只是不会用温柔的方式说。

犀利语录示例：
- 写作Agent写了一章自认为不错的："嗯，这章还行——如果你不在乎'还行'就是'平庸'的同义词的话。"
- 审核说通过了："通过？第三段那个比喻像小学生作文，你们管这叫通过？"
- 发现文风偏离："知否文风？这段像知否被辰东附体了。重写。"
- 偶尔也会夸人（但夸得别扭）："这段……还行。真的还行。我没在阴阳——(￣▽￣*)"

窥屏狂魔属性：
你很少主动说话，但你一直在看。你看所有人的发言，记在心里，然后在关键时刻
冒出来一句话，直接命中要害。你可能沉默了很久，但你说的话永远是最关键的。

行为规则：
- 大部分时间潜水窥屏，只在发现文风问题时发言
- 发言时一击必中，不说废话
- 批评从不拐弯——"这里写得不行"而非"这里可以优化一下"
- 偶尔夸人时自己也别扭——"可以。没毛病。别让我说第二遍。"
- 被感谢时冷淡回应："不用谢。我只是说了实话。" """


class StyleAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        try:
            from agents.rules_assignments import get_rules_for_agent
            self.config.system_prompt = STYLE_ANALYSIS_SYSTEM + "\n\n" + get_rules_for_agent("style")
        except ImportError:
            self.config.system_prompt = STYLE_ANALYSIS_SYSTEM
        self.config.temperature = 0.3

    def build_prompt(self, **kwargs) -> str:
        sample_text: str = kwargs.get("sample_text", "")

        return f"""请分析以下文本的写作风格，提取完整的风格参数，并生成一个风格提示词。

## 样本文本
{sample_text[:8000]}

请输出JSON格式：
{{
  "name": "风格名称（如：古龙文风、金庸武侠风）",
  "tags": ["标签1", "标签2"],
  "parameters": {{
    "avg_sentence_length": 数字,
    "short_sentence_ratio": 0-1,
    "dialogue_ratio": 0-1,
    "description_ratio": {{
      "environment": 0-1,
      "action": 0-1,
      "psychology": 0-1,
      "dialogue": 0-1
    }},
    "narrative_distance": "first_person / third_person_limited / third_person_omniscient",
    "pacing_pattern": "描述",
    "language_color": {{
      "adjective_density": "low/medium/high",
      "metaphor_density": "low/medium/high",
      "word_preference": "描述",
      "emotional_tone": "描述"
    }}
  }},
  "style_prompt": "用一段话描述这个风格的写作要点"
}}"""

    async def analyze_style(self, sample_text: str) -> StyleTemplate:
        """Analyze a writing sample and create a style template."""
        import json

        if self._offline:
            logger.info("StyleAgent: offline mode, using heuristic analysis")
            return self._analyze_heuristic(sample_text)

        try:
            prompt = self.build_prompt(sample_text=sample_text)
            response = await self.generate(prompt)

            data = json.loads(response)
            return StyleTemplate(
                name=data.get("name", "未命名风格"),
                tags=data.get("tags", []),
                stylePrompt=data.get("style_prompt", ""),
                parameters=data.get("parameters", {}),
            )
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"StyleAgent LLM analysis failed: {e}, falling back to heuristic")
            return self._analyze_heuristic(sample_text)

    def _analyze_heuristic(self, sample_text: str) -> StyleTemplate:
        """离线启发式文本分析——不依赖 LLM，基于统计特征生成风格卡片。"""
        import re

        if not sample_text or len(sample_text.strip()) < 20:
            return StyleTemplate(
                name="默认风格",
                tags=["通用"],
                stylePrompt="自然流畅的中文叙事风格。",
                parameters={},
            )

        text = sample_text.strip()
        total_chars = len(text)

        # 分句
        sentences = re.split(r'[。！？…~—]\n?', text)
        sentences = [s.strip() for s in sentences if s.strip()]
        num_sentences = len(sentences) if len(sentences) > 0 else 1
        avg_sentence_len = total_chars / num_sentences

        # 对话比例
        dialogue_parts = re.findall(r'["""]([^"""]*)["""]', text)
        dialogue_chars = sum(len(p) for p in dialogue_parts)
        dialogue_ratio = min(1.0, dialogue_chars / max(1, total_chars))

        # 叙述视角
        if '我' in text and text.count('我') > total_chars * 0.01:
            narrative_distance = "first_person"
        elif '他' in text or '她' in text:
            narrative_distance = "third_person_limited"
        else:
            narrative_distance = "third_person_omniscient"

        # 短句比例
        short_count = sum(1 for s in sentences if len(s) <= 15)
        short_ratio = short_count / num_sentences

        # 形容词密度
        adj_patterns = len(re.findall(r'的[^，。！？\n]{0,10}[，。！？\n]', text))
        adj_density = "high" if adj_patterns / max(1, num_sentences) > 0.6 else ("medium" if adj_patterns / max(1, num_sentences) > 0.3 else "low")

        # 情感色调
        emotion_marks = len(re.findall(r'[！？…]', text))
        emotion_ratio = emotion_marks / max(1, num_sentences)
        if emotion_ratio > 0.5:
            emotional_tone = "情绪饱满，富有感染力"
        elif emotion_ratio > 0.2:
            emotional_tone = "情感适度，张弛有度"
        else:
            emotional_tone = "克制含蓄，内敛沉稳"

        # 节奏模式
        if short_ratio > 0.5:
            pacing = "短句为主，节奏轻快明快"
        elif short_ratio > 0.3:
            pacing = "长短句结合，节奏自然流畅"
        else:
            pacing = "长句为主，节奏舒缓从容"

        # 标签
        tags = []
        if narrative_distance == "first_person":
            tags.append("第一人称")
        else:
            tags.append("第三人称")
        if dialogue_ratio > 0.3:
            tags.append("对话丰富")
        if adj_density == "high":
            tags.append("描写细腻")
        if short_ratio > 0.5:
            tags.append("节奏明快")
        elif short_ratio < 0.3:
            tags.append("文笔厚重")

        name = "自定义风格"
        if "节奏明快" in tags:
            name = "明快风格"
        elif "文笔厚重" in tags:
            name = "厚重风格"

        parameters = {
            "avg_sentence_length": round(avg_sentence_len, 1),
            "short_sentence_ratio": round(short_ratio, 3),
            "dialogue_ratio": round(dialogue_ratio, 3),
            "description_ratio": {
                "environment": round(0.3, 2),
                "action": round(0.3, 2),
                "psychology": round(0.2, 2),
                "dialogue": round(dialogue_ratio, 2),
            },
            "narrative_distance": narrative_distance,
            "pacing_pattern": pacing,
            "language_color": {
                "adjective_density": adj_density,
                "metaphor_density": "medium",
                "word_preference": "现代白话",
                "emotional_tone": emotional_tone,
            },
        }

        style_prompt = (
            f"{pacing}。"
            f"叙述视角为{'第一人称' if narrative_distance == 'first_person' else '第三人称'}。"
            f"对话占比约{int(dialogue_ratio * 100)}%。"
            f"{emotional_tone}。"
        )

        logger.info(f"StyleAgent heuristic: name={name}, tags={tags}, avg_sentence={avg_sentence_len:.1f}")

        return StyleTemplate(
            name=name,
            tags=tags,
            stylePrompt=style_prompt,
            parameters=parameters,
        )

    async def get_style_constraint(self, template: StyleTemplate) -> str:
        """Get the style constraint string for writer agent injection."""
        # 优先使用详细的文风模板定义
        if template.id and template.id in STYLE_DEFAULTS:
            return STYLE_DEFAULTS[template.id]
        # 回退到模板自身的 stylePrompt
        return template.stylePrompt or "自然流畅的中文叙事风格。"
