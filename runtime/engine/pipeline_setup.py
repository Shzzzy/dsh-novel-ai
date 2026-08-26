"""Pipeline Bootstrap — 一键初始化管线所需全部组件

提供 PipelineContext: 将 PipelineStateManager, AcpBridge,
PipelineStateAcpConnector, OrchestratorPipeline 打包为单一上下文,
在 WebSocket handler 中注入 _send 函数即可开箱使用。

用法:
    ctx = PipelineContext(novel_id="novel-001", brain_path="/tmp/gbrain")
    ctx.set_send_fn(_send)  # _send 来自 WebSocket handler

    # 管线操作
    await ctx.pipe.context_ready("ch-001")
    await ctx.pipe.writing_start("ch-001")
    ...

    # 崩溃恢复
    action = ctx.state.recover("ch-001")
"""

from typing import Callable, Optional

from engine.pipeline_state import PipelineStateManager
from engine.acp_bridge import AcpBridge
from engine.pipeline_state_integration import (
    PipelineStateAcpConnector,
    OrchestratorPipeline,
)


class PipelineContext:
    """管线上下文 — 打包所有管线相关组件。"""

    def __init__(self, novel_id: str, brain_path: str = ""):
        self.novel_id = novel_id
        self.brain_path = brain_path

        # 状态管理器
        self.state = PipelineStateManager(brain_path=brain_path)

        # ACP 桥接
        self.bridge = AcpBridge(novel_id=novel_id)

        # 状态变更 → WS 事件
        self.connector = PipelineStateAcpConnector(self.state, self.bridge)
        self.state.set_on_state_change(self.connector.on_state_change)

        # 编排器编排器管线操作
        self.pipe = OrchestratorPipeline(self.state, self.bridge)

    def set_send_fn(self, send_fn: Callable):
        """注入 WebSocket send 函数。"""
        self.connector.set_send_fn(send_fn)
        self.pipe.set_send_fn(send_fn)

    def set_task(self, task_id: str, chapter_order: int = 1, stage: str = "IDLE"):
        """设置当前管线任务上下文。"""
        self.bridge.set_task(task_id, chapter_order=chapter_order, stage=stage)

    # ── 便捷属性 ──

    @property
    def task_id(self) -> str:
        return self.bridge.task_id

    def get_status(self, chapter_id: str) -> str:
        return self.state.get_status(chapter_id)

    def is_chapter_unlocked(self, chapter_id: str) -> bool:
        return self.state.is_chapter_unlocked(chapter_id)

    def recover(self, chapter_id: str) -> dict:
        return self.state.recover(chapter_id)
