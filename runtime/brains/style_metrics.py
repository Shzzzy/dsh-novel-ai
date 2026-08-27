"""轻量本地句式分析 (移植自 dsh-novel-writer 设计, 纯规则零依赖)

九类句式统计 + 情感词典 + 句长分布 + 对话密度 + 风格指纹。
不依赖 ONNX 模型, 毫秒级, 零 API 成本。

输出 (POST /api/style/metrics):
  sentence_types  九类句式分布 {type: count}
  emotion_curve   情感分布 {喜/怒/哀/惧/中性}
  avg_sentence_len 平均句长
  dialogue_ratio  对话占比
  style_fingerprint 风格指纹 (多维向量, 可做相似度对比)
"""

import re

# ── 句式分类正则 ─────────────────────────────────────────────────────────────
_SENTENCE_TYPES = {
    "陈述": re.compile(r"^[^？！。…]*[。]$"),
    "疑问": re.compile(r"[？?]|^[^。！]*吗$|^[^。！]*呢$"),
    "感叹": re.compile(r"[！!]$|^[^。？]*啊$|^[^。？]*呀$"),
    "对话": re.compile(r"^[“\"『「]"),
    "短句": None,  # 动态: <=6 字
    "长句": None,  # 动态: >=40 字
    "排比": None,  # 动态: 连续相似句式
    "反问": re.compile(r"^[^。！？]*难道[^。！？]*[？?]$|^[^。！？]*怎(么|能)[^。！？]*[？?]$"),
    "省略": re.compile(r"…{2,}$"),
}

# ── 情感词典 (内置小型词典) ──────────────────────────────────────────────────
_EMOTION_LEXICON = {
    "喜": ["笑", "高兴", "开心", "欢喜", "雀跃", "兴奋", "得意", "满足", "欣慰", "痛快", "美滋滋", "喜", "乐呵", "咧嘴"],
    "怒": ["怒", "气", "恨", "恼", "咬牙", "暴跳", "火冒", "狰狞", "攥紧拳头", "拍桌", "愤", "吼"],
    "哀": ["哭", "泪", "哀", "悲", "痛", "伤心", "心碎", "哽咽", "泣", "呜咽", "绝望", "凄凉", "酸涩", "难受"],
    "惧": ["怕", "恐惧", "惊", "颤", "发抖", "冷汗", "心慌", "胆寒", "吓", "畏惧", "哆嗦", "毛骨悚然"],
}

# ── 网文高频词/套路标记 (webnovel-tropes 精简版) ────────────────────────────
_TROPE_MARKERS = {
    "打脸流": ["打脸", "啪啪", "跪下", "求饶", "后悔莫及"],
    "扮猪吃虎": ["深藏不露", "扮猪", "真人不露相", "低调"],
    "逆袭流": ["逆袭", "翻身", "一鸣惊人", "一飞冲天"],
    "系统流": ["系统提示", "叮", "任务完成", "奖励"],
    "退婚流": ["退婚", "悔婚", "一纸婚书"],
    "重生流": ["重生", "前世", "上一世", "重新来过"],
    "无敌流": ["无敌", "横扫", "碾压", "一招秒", "无人能敌"],
}

_STOP_PUNCT = "。！？；，、：\"“”‘’「」『』…—《》（）"


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句"""
    parts = re.split(r'(?<=[。！？!?…])', text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]


def _classify(sentence: str) -> list[str]:
    types = []
    for name, pattern in _SENTENCE_TYPES.items():
        if pattern is None:
            continue
        if pattern.search(sentence):
            types.append(name)
    # 动态规则
    if len(sentence) <= 6:
        types.append("短句")
    elif len(sentence) >= 40:
        types.append("长句")
    return types


def _emotion_of(sentence: str) -> str:
    for emo, words in _EMOTION_LEXICON.items():
        for w in words:
            if w in sentence:
                return emo
    return "中性"


def analyze_style(text: str) -> dict:
    """主入口: 返回句式/情感/风格指纹"""
    sentences = _split_sentences(text)
    total = len(sentences) or 1

    # 句式分布
    type_counts: dict[str, int] = {}
    for s in sentences:
        for t in _classify(s):
            type_counts[t] = type_counts.get(t, 0) + 1

    # 情感曲线
    emotion_counts = {"喜": 0, "怒": 0, "哀": 0, "惧": 0, "中性": 0}
    for s in sentences:
        emotion_counts[_emotion_of(s)] += 1

    # 对话密度
    dialogue = sum(1 for s in sentences if s.startswith(("“", '"', "「", "『")))

    # 句长
    lengths = [len(s) for s in sentences]
    avg_len = round(sum(lengths) / total, 1)

    # 网文套路标记
    tropes = []
    for name, markers in _TROPE_MARKERS.items():
        if any(m in text for m in markers):
            tropes.append(name)

    # 风格指纹 (归一化向量)
    fingerprint = {
        "对话占比": round(dialogue / total, 3),
        "短句占比": round(type_counts.get("短句", 0) / total, 3),
        "长句占比": round(type_counts.get("长句", 0) / total, 3),
        "疑问句占比": round(type_counts.get("疑问", 0) / total, 3),
        "感叹句占比": round(type_counts.get("感叹", 0) / total, 3),
        "平均句长": avg_len,
        "情绪密度": round((total - emotion_counts["中性"]) / total, 3),
    }

    return {
        "sentence_count": len(sentences),
        "sentence_types": {k: v for k, v in sorted(type_counts.items(), key=lambda x: -x[1])},
        "emotion_curve": emotion_counts,
        "avg_sentence_len": avg_len,
        "dialogue_ratio": round(dialogue / total, 3),
        "tropes": tropes,
        "style_fingerprint": fingerprint,
    }
