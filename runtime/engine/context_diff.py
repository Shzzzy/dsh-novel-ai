"""quick_diff() — 硬约束#3: 增量上下文刷新 (DESIGN_DOC §19.6)

预加载的上下文包在 Phase A 生成, 到 Phase B 写作时可能过期。
quick_diff 做字段级对比——修正"骨架预期"→"前一章实际"，零LLM调用。
"""


def quick_diff(preloaded_states: dict, actual_states: dict) -> dict:
    """字段级对比预加载状态 vs 实际人物状态。

    参数:
        preloaded_states: {name: {field: expected_value}}
        actual_states:   {name: {field: actual_value}}

    返回:
        {name: {field: {"expected": ..., "actual": ...}}}
        只包含变更的字段。未变更的不返回。

    示例:
        preloaded = {"景昭": {"location": "凉州关外", "status": "北征中"}}
        actual = {"景昭": {"location": "返京途中", "status": "北征中", "health": "左臂无法动弹"}}
        diff = quick_diff(preloaded, actual)
        → {"景昭": {"location": {"expected": "凉州关外", "actual": "返京途中"}}}
    """
    changes = {}
    for name, expected_fields in preloaded_states.items():
        actual_fields = actual_states.get(name, {})
        for field, expected_value in expected_fields.items():
            actual_value = actual_fields.get(field)
            if actual_value is not None and actual_value != expected_value:
                changes.setdefault(name, {})[field] = {
                    "expected": expected_value,
                    "actual": actual_value,
                }
    return changes


def apply_diff(context_package: dict, diff: dict) -> dict:
    """将 quick_diff 结果注入 ContextPackage, 替换过期的预加载值。

    修改 context_package 中的 character_states 字段。
    未变更的字段保持不变。
    """
    if not diff:
        return context_package

    char_states = context_package.get("character_states", {})
    for name, field_changes in diff.items():
        if name in char_states:
            for field, change in field_changes.items():
                char_states[name][field] = change["actual"]
    context_package["character_states"] = char_states
    return context_package
