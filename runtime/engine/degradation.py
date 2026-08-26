"""管线降级策略 — DESIGN_DOC §19.9

定义4种降级场景的处理方式:
1. LLM API 不可用 → 管线保持当前状态, 保存检查点, 通知前端
2. 审核第3轮未通过 → 保留第2轮版本, COMPLETE_WITH_WARNINGS
3. Canon 冲突不可自动解决 → 标记冲突, 提请用户裁决 (BLOCKED)
4. 上下文预加载过期 → 增量 diff 刷新 (对用户透明)
"""

from dataclasses import dataclass, field


@dataclass
class DegradationDecision:
    """降级决策——编排器在异常时使用此结构决定管线行为"""
    action: str  # "retry" | "degrade" | "block" | "continue"
    reason: str
    user_message: str = ""
    checkpoint_before: bool = True


def handle_llm_unavailable(attempt: int, max_retries: int = 3) -> DegradationDecision:
    """LLM API 不可用时的降级决策。

    attempt < max_retries: 自动重试
    attempt >= max_retries: 保存进度, 暂停管线
    """
    if attempt < max_retries:
        return DegradationDecision(
            action="retry",
            reason=f"API 调用失败，第{attempt+1}次重试",
            user_message="",
            checkpoint_before=False,
        )
    return DegradationDecision(
        action="degrade",
        reason=f"API 连接中断(已重试{max_retries}次)，进度已保存",
        user_message="API 连接中断，进度已保存。恢复后将继续。",
        checkpoint_before=True,
    )


def handle_review_exhausted(round_num: int, max_rounds: int,
                            best_score: float) -> DegradationDecision:
    """审核轮次耗尽时的降级决策。

    保留评分最高的版本, 标记 COMPLETE_WITH_WARNINGS。
    """
    return DegradationDecision(
        action="degrade",
        reason=f"审核{max_rounds}轮后未通过, 保留第{round_num}轮版本(评分{best_score}/10)",
        user_message=f"审核评分为 {best_score}/10。可手动修订或接受。",
        checkpoint_before=True,
    )


def handle_canon_conflict(conflicts: list[dict]) -> DegradationDecision:
    """Canon 冲突不可自动解决时的决策。

    critical 冲突: 阻塞管线, 等待用户裁决
    minor 冲突: 标记 COMPLETE_WITH_WARNINGS
    """
    critical = [c for c in conflicts if c.get("severity") == "critical"]
    if critical:
        desc = "; ".join(c.get("description", "") for c in critical)
        return DegradationDecision(
            action="block",
            reason=f"Canon 严重冲突: {desc}",
            user_message=f"发现 {len(critical)} 处事实冲突，请确认。",
            checkpoint_before=True,
        )
    return DegradationDecision(
        action="degrade",
        reason=f"Canon {len(conflicts)}个minor冲突, 标记COMPLETE_WITH_WARNINGS",
        user_message="",
        checkpoint_before=False,
    )
