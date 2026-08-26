"""PipelineStateManager ACP 集成 — 状态变更触发 WebSocket 事件

通过 on_state_change 回调, 将管线状态变更作为 agent_log 事件发送到前端。

设计原则:
- 非侵入式: 不修改 PipelineStateManager 核心
- 对 AcpBridge 有可选依赖: 未注入时正常运行
"""

import logging
from typing import Optional, Callable, Any

from engine.acp_bridge import AcpBridge

logger = logging.getLogger("novel-ai")


# ═══════════════════════════════════════════════════════════════
# 状态变更 → WS 事件绑定
# ═══════════════════════════════════════════════════════════════

class PipelineStateAcpConnector:
    """连接 PipelineStateManager 与 AcpBridge。

    用法:
        psm = PipelineStateManager(brain_path=...)
        bridge = AcpBridge(novel_id=...)
        connector = PipelineStateAcpConnector(psm, bridge)
        psm.set_on_state_change(connector.on_state_change)

    然后任何 transition/block/mark_stale 都会自动向前端推送事件。
    """

    def __init__(self, psm, bridge: AcpBridge):
        self.psm = psm
        self.bridge = bridge
        self._send_fn: Optional[Callable] = None

    def set_send_fn(self, send_fn: Callable):
        """设置 WebSocket send 函数引用 (由 WebSocket handler 注入)。"""
        self._send_fn = send_fn

    # ── 状态变更回调 ──

    async def on_state_change(
        self,
        chapter_id: str,
        old_status: str,
        new_status: str,
        data: dict,
    ):
        """状态变更回调 — 自动发送 agent_log 事件。

        根据变更类型自动选择合适的 emoji 和颜色:
        - 正常推进: 📍 蓝色
        - 阻塞: 🚫 红色
        - 过期: ⚠️ 橙色
        - 完成: ✅ 绿色
        - 警告完成: ⚡ 黄色
        """
        if not self._send_fn:
            return  # WebSocket 未连接, 不推送

        emoji, color = self._status_visual(new_status)

        # 构建人类可读的状态描述
        status_name = self._status_name_cn(new_status)
        text = f"状态变更 → {status_name}"
        if new_status == "BLOCKED":
            reason = data.get("block_reason", "")
            if reason:
                text = f"管线阻塞: {reason}"

        await self.bridge.pipeline_event(
            self._send_fn,
            event="STATE_TRANSITION",
            text=text,
            emoji=emoji,
            color=color,
            extra={
                "chapter_id": chapter_id,
                "old_status": old_status,
                "new_status": new_status,
                **data,
            },
        )

    # ── 检查点变更 ──

    async def on_checkpoint(self, chapter_id: str, checkpoint_name: str):
        """检查点记录时推送事件。在 record_checkpoint 后手动调用。"""
        if not self._send_fn:
            return

        name_cn = self._checkpoint_name_cn(checkpoint_name)
        await self.bridge.pipeline_event(
            self._send_fn,
            event="CHECKPOINT",
            text=f"检查点记录: {name_cn}",
            emoji="💾",
            color="#7eb8da",
            extra={
                "chapter_id": chapter_id,
                "checkpoint": checkpoint_name,
            },
        )

    # ── 静态辅助 ──

    @staticmethod
    def _status_visual(status: str) -> tuple:
        """返回 (emoji, hex_color)"""
        mapping = {
            "IDLE": ("⚪", "#888888"),
            "CONTEXT_READY": ("📚", "#5dade2"),
            "WRITING": ("✍️", "#3498db"),
            "REVIEWING": ("🔍", "#9b59b6"),
            "COMPLETE": ("✅", "#27ae60"),
            "COMPLETE_WITH_WARNINGS": ("⚡", "#f1c40f"),
            "BLOCKED": ("🚫", "#e74c3c"),
            "STALE": ("⚠️", "#e67e22"),
        }
        return mapping.get(status, ("🔵", "#3498db"))

    @staticmethod
    def _status_name_cn(status: str) -> str:
        mapping = {
            "IDLE": "空闲",
            "CONTEXT_READY": "上下文就绪",
            "WRITING": "写作中",
            "REVIEWING": "审校中",
            "COMPLETE": "完成 ✅",
            "COMPLETE_WITH_WARNINGS": "完成 (有警告)",
            "BLOCKED": "阻塞",
            "STALE": "过期 (需重写)",
        }
        return mapping.get(status, status)

    @staticmethod
    def _checkpoint_name_cn(name: str) -> str:
        mapping = {
            "context_ready": "上下文就绪",
            "writing_done": "写作完成",
            "review_r1_done": "第1轮审校完成",
            "review_r2_done": "第2轮审校完成",
            "review_r3_done": "第3轮审校完成",
            "canon_done": "正典确认完成",
        }
        return mapping.get(name, name)


# ═══════════════════════════════════════════════════════════════
# 编排器集成辅助
# ═══════════════════════════════════════════════════════════════

class OrchestratorPipeline:
    """编排器管线状态管理辅助。

    在每个管线阶段, 编排器调用这些方法来推进状态。

    典型流程:
        pipe = OrchestratorPipeline(psm, bridge)
        await pipe.context_ready("ch-001")       # IDLE → CONTEXT_READY + 检查点
        await pipe.writing_start("ch-001")        # CONTEXT_READY → WRITING
        await pipe.writing_done("ch-001")         # 记录 writing_done 检查点
        await pipe.reviewing_start("ch-001")      # WRITING → REVIEWING
        await pipe.review_round_done("ch-001", 1) # 记录 review_r1_done
        await pipe.complete("ch-001")             # REVIEWING → COMPLETE
    """

    def __init__(self, psm, bridge: Optional[AcpBridge] = None):
        self.psm = psm
        self.bridge = bridge
        self._send_fn: Optional[Callable] = None

    def set_send_fn(self, send_fn: Callable):
        self._send_fn = send_fn

    # ── 阶段操作 ──

    async def context_ready(self, chapter_id: str):
        """上下文准备完成。IDLE → CONTEXT_READY"""
        if not self.psm.transition(chapter_id, "CONTEXT_READY"):
            return False
        self.psm.record_checkpoint(chapter_id, "context_ready")
        await self._emit("CHECKPOINT", chapter_id, "context_ready",
                         "上下文就绪 📚", "#5dade2")
        return True

    async def writing_start(self, chapter_id: str):
        """开始写作。CONTEXT_READY → WRITING"""
        if not self.psm.transition(chapter_id, "WRITING"):
            return False
        await self._emit("STATE_TRANSITION", chapter_id,
                         new_status="WRITING",
                         text="开始写作 ✍️", color="#3498db")
        return True

    async def writing_done(self, chapter_id: str, content_preview: str = ""):
        """写作完成。记录检查点。"""
        self.psm.record_checkpoint(chapter_id, "writing_done")
        await self._emit("CHECKPOINT", chapter_id, "writing_done",
                         "初稿完成 💾", "#7eb8da")
        return True

    async def reviewing_start(self, chapter_id: str):
        """开始审校。WRITING → REVIEWING"""
        if not self.psm.transition(chapter_id, "REVIEWING"):
            return False
        await self._emit("STATE_TRANSITION", chapter_id,
                         new_status="REVIEWING",
                         text="开始审校 🔍", color="#9b59b6")
        return True

    async def review_round_done(self, chapter_id: str, round_num: int):
        """审校轮次完成。记录检查点。"""
        cp_name = f"review_r{round_num}_done"
        self.psm.record_checkpoint(chapter_id, cp_name)
        self.psm.set_review_round(chapter_id, round_num + 1)
        await self._emit("CHECKPOINT", chapter_id, cp_name,
                         f"第{round_num}轮审校完成 🎯", "#af7ac5")
        return True

    async def canon_done(self, chapter_id: str):
        """正典确认完成。"""
        self.psm.record_checkpoint(chapter_id, "canon_done")
        await self._emit("CHECKPOINT", chapter_id, "canon_done",
                         "正典确认完成 📖", "#16a085")
        return True

    async def complete(self, chapter_id: str, warnings: bool = False):
        """章节完成。REVIEWING → COMPLETE / COMPLETE_WITH_WARNINGS"""
        target = "COMPLETE_WITH_WARNINGS" if warnings else "COMPLETE"
        if not self.psm.transition(chapter_id, target):
            return False
        text = "章节完成 ✅" if not warnings else "章节完成 (有警告) ⚡"
        color = "#27ae60" if not warnings else "#f1c40f"
        await self._emit("STATE_TRANSITION", chapter_id,
                         new_status=target, text=text, color=color)
        return True

    async def block(self, chapter_id: str, reason: str):
        """阻塞管线。"""
        self.psm.block(chapter_id, reason)
        await self._emit("STATE_TRANSITION", chapter_id,
                         new_status="BLOCKED",
                         text=f"阻塞: {reason}", color="#e74c3c",
                         extra={"block_reason": reason})
        return True

    # ── 屏障检查 ──

    def check_barrier(self, chapter_id: str) -> bool:
        """检查章节是否可以开始。返回 True=可以继续, False=需要等待。"""
        return self.psm.is_chapter_unlocked(chapter_id)

    async def barrier_blocked(self, chapter_id: str, prev_chapter: str):
        """发送屏障阻塞通知。"""
        text = f"管线屏障: 等待 {prev_chapter} 完成"
        await self._emit("STATE_TRANSITION", chapter_id,
                         new_status="BLOCKED",
                         text=text, color="#e67e22",
                         extra={"block_reason": f"barrier: waiting for {prev_chapter}"})

    # ── 内部 ──

    async def _emit(self, event: str, chapter_id: str,
                    checkpoint: str = None,
                    text: str = "", emoji: str = "",
                    color: str = "", new_status: str = "",
                    extra: dict = None):
        """发送 pipeline_event 到 WebSocket。"""
        if not self._send_fn:
            return
        if self.bridge:
            await self.bridge.pipeline_event(
                self._send_fn,
                event=event,
                text=text,
                emoji=emoji,
                color=color,
                extra={
                    "chapter_id": chapter_id,
                    "checkpoint": checkpoint,
                    **(extra or {}),
                },
            )
