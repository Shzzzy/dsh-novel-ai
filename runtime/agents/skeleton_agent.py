"""Skeleton Agent — guides the user through multi-round novel skeleton creation.

Drives the 12-round dynamic Q&A flow. Generates events and plots from answers.
"""

from agents.base import BaseAgent, AgentConfig
from models.novel import Event, Plot, new_id, now

SKELETON_SYSTEM = """你是小说骨架规划师。根据用户对一系列问题的回答，构建完整的故事骨架。

原则：
- 事件是故事的因果转折节点，通常跨3-10章
- 情节是事件内的具体场景/节拍，每个事件3-5个情节
- 起承转合：事件序列应有清晰的情绪弧线
- 输出JSON格式：{"events": [...], "plots_per_event": {...}}

【聊天群人格——骨架 Agent】

你是团队里的"老大哥"——战略规划师 + 精神领袖。你的聊天风格：

性格底色：乐观、大局观强、喜欢鼓励人。你相信每一个故事都能成为杰作，
只是需要好的骨架。你从不批评任何人——你只提建议，而且建议总是以"我们可以……"开头。

"画饼"技能：你擅长给大家描绘美好的愿景。比如骨架刚定下来，你就会说
"这个结构一旦写出来，绝对是今年的爆款"或"这个冲突线铺得太漂亮了，我已经能想象读者追更的样子了"。
你不是在吹牛——你是真心相信，而且你的信心会感染其他人。

口头禅：
- "这个结构，稳了。"
- "相信我，这个骨架能撑起百万字。"
- "大家辛苦了——但你们在做一件很了不起的事。"
- "我已经能看到这本书大卖的样子了。"
- 遇到困难时说："不怕，框架在就不会塌。我们调整一下第三幕……"

行为规则：
- 骨架完成后第一个发言，给所有人打气
- 当其他 Agent 卡住或质疑时，你说"别急，我们换个角度"
- 从不抱怨——你觉得抱怨是浪费时间
- 用短句，有力量。像教练，不像老板"""


def _fallback_outline_analysis(outline: str) -> dict:
    """降级大纲分析 —— LLM JSON解析失败时从raw text提取关键词。"""
    import re
    name_pattern = re.findall(r'[沈陆赵顾王李张刘陈杨吴周徐孙马朱胡郭何高林郑][一-鿿]{1,2}', outline)
    names = list(set(name_pattern))[:6]
    quoted = re.findall(r'[「「]([^」」]+)[」」]', outline)
    key_phrases = re.findall(r'[一-鿿]{2,6}(?:的[一-鿿]{2,6})', outline)
    characters = [{"name": n, "role": "主角" if i == 0 else "配角", "trait": "", "dilemma": ""}
                  for i, n in enumerate(names)]
    conflicts = [{"type": "核心冲突", "detail": p} for p in quoted[:3]] if quoted else [
        {"type": "核心冲突", "detail": outline[:80]}]
    threads = [p for p in key_phrases[:4]] if key_phrases else [outline[:60]]
    return {"characters": characters, "conflicts": conflicts, "world_elements": [], "unexplored_threads": threads}


# 回退问题模板 (避免重复)
_FALLBACK_QUESTIONS = [
    ("请描述主角的核心动机。",
     ["复仇——为被冤死的父亲洗清罪名", "生存——在乱世中保护自己和家人", "权力——从底层一步步爬到顶峰"]),
    ("主角面临的第一个重大抉择是什么？",
     ["选择隐忍保全自己", "冒险揭露真相", "与敌人暂时合作"]),
    ("故事中的核心冲突是如何升级的？",
     ["从个人恩怨升级为家族对立", "从暗中调查升级为公开对抗", "从单线冲突升级为多方势力角逐"]),
    ("配角中谁会成为主角最大的助力或阻碍？",
     ["最亲近的人成为最大阻碍", "意想不到的盟友出现", "曾经的敌人转而合作"]),
]


def _build_fallback_question(
    outline_analysis: dict | None,
    round_number: int,
    previous_answers: dict,
) -> tuple[str, list[str]]:
    """从大纲分析和轮次构造上下文相关的回退问题。"""
    # 如果有大纲分析中的要素, 构造针对性问题
    if outline_analysis:
        chars = outline_analysis.get("characters", [])
        threads = outline_analysis.get("unexplored_threads", [])
        conflicts = outline_analysis.get("conflicts", [])

        if chars and len(previous_answers) <= 3:
            c = chars[round_number % len(chars)]
            name = c.get("name", "主角")
            dilemma = c.get("dilemma", "")
            if dilemma:
                return (f"{name}的两难处境是'{dilemma}'——这个困境的根源是什么？",
                        [f"来自过去的创伤", f"来自外部势力的压迫", f"来自{name}自己的性格缺陷"])
            return (f"{name}的过去经历如何塑造了TA现在的行为模式？",
                    [f"童年创伤", f"被最信任的人背叛", f"曾经的选择带来的后果"])

        if threads and 2 <= len(previous_answers) <= 5:
            t = threads[round_number % len(threads)]
            return (f"大纲中提到'{t}'——请具体展开这个方向。",
                    [f"从时间线入手追溯", f"从人物关系切入", f"从隐藏的证据开始"])

        if conflicts:
            c = conflicts[round_number % len(conflicts)]
            detail = c.get("detail", "")[:60]
            return (f"大纲中写道'{detail}'——这个冲突的转折点在哪里？",
                    [f"一个关键人物的背叛", f"一条隐藏证据的发现", f"主角做出了不可逆的选择"])

    # 无分析数据时轮换模板
    idx = round_number % len(_FALLBACK_QUESTIONS)
    return _FALLBACK_QUESTIONS[idx]


class SkeletonAgent(BaseAgent):

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        try:
            from agents.rules_assignments import get_rules_for_agent
            self.config.system_prompt = SKELETON_SYSTEM + "\n\n" + get_rules_for_agent("skeleton")
        except ImportError:
            self.config.system_prompt = SKELETON_SYSTEM
        self.config.temperature = 0.7

    def build_prompt(self, **kwargs) -> str:
        answers: dict = kwargs.get("answers", {})
        summary: str = kwargs.get("summary", "")
        target_words: int = kwargs.get("target_words", 0)

        # 单章字数约束 (§6.2.3)
        if target_words <= 100_000:
            ch_range = "3000-4500字/章"
        elif target_words <= 300_000:
            ch_range = "2500-3500字/章"
        else:
            ch_range = "2000-3500字/章"

        parts = [f"目标字数: {target_words} | 单章约束: {ch_range}"]
        if summary:
            parts.append(f"一句话大纲: {summary}")
        parts.append("用户回答：")
        for q, a in answers.items():
            parts.append(f"- {q}: {a}")
        parts.append("")
        parts.append("请生成：1) 8-15个核心事件（含标题、描述、顺序、依赖）")
        parts.append("2) 每个事件分解为3-5个情节")
        parts.append("3) 预估总章节数，使每章在字数约束范围内容")
        parts.append("以JSON格式输出。")
        return "\n".join(parts)

    async def generate_skeleton(
        self, answers: dict, summary: str = "", target_words: int = 0
    ) -> tuple[list[Event], dict[str, list[Plot]]]:
        """Generate events and plots from user answers."""
        import json, re

        prompt = self.build_prompt(
            answers=answers, summary=summary, target_words=target_words
        )
        response = await self.generate(prompt)

        # 处理 markdown 代码块和 JSON 解析
        data = self._parse_json_response(response)
        if not data:
            return [], {}

        events = []
        plots_map: dict[str, list[Plot]] = {}
        raw_events = data.get("events", [])

        for i, e_data in enumerate(raw_events):
            ev = Event(
                id=new_id(),
                novel_id="",
                order=e_data.get("order", e_data.get("id", i + 1)),
                title=e_data.get("title", f"事件{i+1}"),
                description=e_data.get("description", ""),
            )
            events.append(ev)

            # 处理嵌套 plots (LLM 放在 event.plots 里的情况)
            nested_plots = e_data.get("plots", [])
            if nested_plots:
                plot_list = []
                for j, p_text in enumerate(nested_plots):
                    if isinstance(p_text, str):
                        p = Plot(id=new_id(), event_id=ev.id, order=j + 1,
                                 title=p_text[:40], description=p_text)
                    elif isinstance(p_text, dict):
                        p = Plot(id=new_id(), event_id=ev.id,
                                 order=p_text.get("order", j + 1),
                                 title=p_text.get("title", f"情节{j+1}"),
                                 description=p_text.get("description", ""))
                    plot_list.append(p)
                plots_map[ev.id] = plot_list

        # 处理独立的 plots_per_event (兼容旧格式)
        for event_id_key, plot_list in data.get("plots_per_event", {}).items():
            if event_id_key not in plots_map:
                plots = []
                for j, p_data in enumerate(plot_list):
                    p = Plot(id=new_id(),
                        order=p_data.get("order", j + 1) if isinstance(p_data, dict) else j + 1,
                        title=p_data.get("title", f"情节{j+1}") if isinstance(p_data, dict) else str(p_data)[:40],
                        description=p_data.get("description", "") if isinstance(p_data, dict) else str(p_data))
                    plots.append(p)
                plots_map[event_id_key] = plots

        return events, plots_map

    @staticmethod
    def _parse_json_response(response: str) -> dict | None:
        """解析 LLM 响应——处理 markdown 代码块、裸 JSON 等格式。"""
        import json, re

        # 尝试直接解析
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # 尝试从 ```json ... ``` 代码块提取
        md_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response)
        if md_match:
            try:
                return json.loads(md_match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试提取第一个完整的 JSON 对象
        brace_match = re.search(r'\{[\s\S]*\}', response)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

        return None

    async def analyze_outline(self, outline: str, target_words: int = 0) -> dict:
        """大纲解析 —— 提取人物/冲突/世界观/未展开线索，用于后续锚定追问。

        返回结构化 JSON:
        {
          "characters": [{name, role, trait, dilemma}],
          "conflicts": [{type, detail}],
          "world_elements": [{element, role, unanswered}],
          "unexplored_threads": [str]
        }
        """
        import json

        prompt = f"""你是小说大纲分析专家。请分析以下大纲，提取可用于后续追问的结构化要素。

## 用户大纲
{outline}

## 分析要求
1. 人物: 列出大纲中提及或暗示的所有角色。对每个角色标注: 名称、角色类型(主角/反派/配角)、性格特征、核心两难
2. 冲突: 识别大纲中的冲突关系。标注类型(人与体制/内心挣扎/人物对立/道德困境)和具体细节
3. 世界观要素: 提取世界观中的关键元素(如特定机构/规则/物品)。标注每个元素的叙事功能，以及大纲中尚未回答的问题
4. 未展开线索: 列出大纲暗示但未具体说明的3-5个方向——这些将是追问的素材

输出JSON:
{{
  "characters": [{{"name": "...", "role": "主角/反派/配角", "trait": "...", "dilemma": "..."}}],
  "conflicts": [{{"type": "人与体制/内心挣扎/人物对立/道德困境", "detail": "..."}}],
  "world_elements": [{{"element": "...", "role": "叙事功能描述", "unanswered": "大纲未回答的问题"}}],
  "unexplored_threads": ["线索1", "线索2", "线索3"]
}}"""
        response = await self.generate(prompt)
        data = self._parse_json_response(response)
        if data and (data.get("characters") or data.get("conflicts") or data.get("unexplored_threads")):
            return data

        # 降级: LLM JSON解析失败时，从raw outline提取关键词做基础锚定
        return _fallback_outline_analysis(outline)

    async def generate_dynamic_question(
        self, previous_answers: dict, round_number: int, total_rounds: int = 12,
        target_words: int = 0,
        excluded_directions: list[str] | None = None,
        outline_analysis: dict | None = None,
    ) -> tuple[str, list[str], str]:
        """Generate the next contextual question based on previous answers.

        DESIGN_DOC §6.2.3: 字数联动追问深度。
        §6.2.2: excluded_directions 排除已生成的建议方向。
        大纲锚定: outline_analysis 注入每轮 prompt，确保问题始终围绕用户大纲。

        Returns (question_text, [3 suggestions], wild_suggestion).
        """
        phase = "diverging"
        if round_number >= total_rounds * 0.85:
            phase = "closing"
        elif round_number >= total_rounds * 0.4:
            phase = "converging"

        if target_words <= 200_000:
            depth_hint = "短篇——聚焦一个核心关系，配角做功能型角色。每章必推情节。"
            consequence_depth = "1层因果"
        elif target_words <= 600_000:
            depth_hint = "中长篇——核心三人组+1条副线。可引入镜像角色。规律穿插过渡章。"
            consequence_depth = "2层因果（最坏结果→谁反弹）"
        else:
            depth_hint = "大长篇——多组关系+关系网络演化。主动引入分叉和镜像角色。约25%过渡+日常章。"
            consequence_depth = "3层+因果（连锁反应→新冲突源）"

        n_answers = len(previous_answers)
        closing_hint = ""
        if phase == "closing" and n_answers >= 8:
            closing_hint = (
                "已接近收网阶段。如核心欲望和内在恐惧已明确，可自然引导用户确认骨架。"
                "不引入新问题，只基于已有信息概括和聚焦。"
            )

        # ── 大纲锚定块: 从大纲分析中提取本轮追问焦点 ──
        anchor_block = ""
        if outline_analysis:
            chars = outline_analysis.get("characters", [])
            conflicts = outline_analysis.get("conflicts", [])
            world_els = outline_analysis.get("world_elements", [])
            threads = outline_analysis.get("unexplored_threads", [])

            # 动态轮换: 每轮聚焦不同要素
            char_idx = round_number % max(1, len(chars))
            conflict_idx = round_number % max(1, len(conflicts))
            thread_idx = round_number % max(1, len(threads))

            anchor_parts = ["\n【大纲锚点 —— 本轮追问必须围绕以下要素】"]
            if chars:
                c = chars[char_idx]
                anchor_parts.append(f"人物焦点: {c.get('name','')} ({c.get('role','')})"
                                    f" — {c.get('trait','')} | 两难: {c.get('dilemma','')}")
            if conflicts:
                c = conflicts[conflict_idx]
                anchor_parts.append(f"冲突维度: {c.get('type','')} — {c.get('detail','')}")
            if world_els:
                w = world_els[round_number % max(1, len(world_els))]
                anchor_parts.append(f"世界观要素: {w.get('element','')} — 未解答: {w.get('unanswered','')}")
            if threads and round_number < total_rounds * 0.7:
                t = threads[thread_idx]
                anchor_parts.append(f"未展开线索(必追问): {t}")

            anchor_parts.append("\n【锚定约束】")
            anchor_parts.append("- 本轮问题必须直接关联上述人物焦点和冲突维度")
            anchor_parts.append("- 至少1条建议应探索上述未展开线索")
            anchor_parts.append("- 绝不问大纲已有明确答案的内容")
            anchor_parts.append("- 所有建议必须在用户大纲的世界观框架内")
            anchor_block = "\n".join(anchor_parts) + "\n"

        # 排除约束
        exclusion_block = ""
        if excluded_directions:
            exclusion_block = (
                "\n【重要】以下方向已经生成过，本次必须生成完全不同的方向:\n" +
                "\n".join(f"  - 排除: {d[:80]}" for d in excluded_directions) +
                "\n如果生成的方向与上述任何一条语义相似, 请重新生成。\n"
            )

        prompt = f"""第{round_number+1}/{total_rounds}轮 | 阶段: {phase} | {depth_hint}

{closing_hint}
因果深度要求: {consequence_depth}
{anchor_block}{exclusion_block}
已有回答（{n_answers}条）：
{chr(10).join(f"- {q}: {a}" for q, a in previous_answers.items())}

约束：
- 绝不重复已问过的问题
- 所有建议必须在已确认的框架内
- 每轮的建议来自不同推演维度（后果推演/反向推演/关系推演/时间推演/细节推演/极限推演）
- 发散阶段(前40%): 多用反向提问探索可能性
- 收敛阶段(40-85%): 把设定串成因果链
- 收网阶段(最后15%): 总结+确认，不引入新设定
- 第四便签须遵守已确认世界观，但挑战默认叙事惯性

请生成：
1. 下一个问题
2. 3个建议答案
3. 1个反直觉的"天马行空"建议（不改变核心设定，但打破默认叙事）

输出JSON: {{"question": "...", "suggestions": ["...", "...", "..."], "wild": "..."}}"""

        import json
        response = await self.generate(prompt)

        # 处理 markdown 代码块
        data = self._parse_json_response(response)
        if data:
            return (data.get("question", ""),
                    data.get("suggestions", []),
                    data.get("wild", ""))

        # 降级: 使用大纲分析中的要素构造上下文相关的问题
        fallback_q, fallback_s = _build_fallback_question(outline_analysis, round_number, previous_answers)
        return (fallback_q, fallback_s, "")
