"""ACP → WebSocket bridge — enrich agent_log events with structured ACP metadata.

Minimal integration: wraps the existing _send() pattern to inject
ACP message ID, task ID, and correlation info into agent_log payloads.

Usage (in main.py):
    from engine.acp_bridge import AcpBridge
    
    bridge = AcpBridge(novel_id=novel_id)
    bridge.set_task("chapter-003", chapter_order=3)
    
    # Replace plain _send("agent_log", ...) with:
    bridge.log(ws_send, "orchestrator", "开始写作...")
    bridge.task_assign(ws_send, "writer", "写一段文字")
    bridge.task_result(ws_send, "writer", "写完了")

The bridge auto-injects ACP metadata into every event.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class AcpBridge:
    """Lightweight bridge that adds ACP metadata to WebSocket events.

    Does NOT require the full AgentBus — works standalone.
    Can be upgraded to full bus integration when Sprint 2 arrives.
    """

    novel_id: str = ""
    task_id: str = ""
    stage: str = ""
    chapter_order: int = 0

    # Counters
    _step_counter: int = field(default=0, init=False)

    def set_task(self, task_id: str, chapter_order: int = 0, stage: str = ""):
        """Start a new pipeline task."""
        self.task_id = task_id
        self.chapter_order = chapter_order
        self.stage = stage or "pipeline"
        self._step_counter = 0

    def _next_step(self) -> str:
        self._step_counter += 1
        return f"{self.task_id}.{self._step_counter:02d}"

    def _acp_meta(self) -> dict:
        """ACP metadata injected into every WS event."""
        return {
            "acp": {
                "task_id": self.task_id,
                "step": self._step_counter,
                "stage": self.stage,
                "chapter_order": self.chapter_order,
                "msg_id": str(uuid.uuid4())[:8],
            }
        }

    async def log(
        self,
        send_fn: Callable,
        agent: str,
        text: str,
        emoji: str = "",
        color: str = "",
        level: str = "info",
        extra: Optional[dict] = None,
    ):
        """Send agent_log with ACP metadata."""
        self._next_step()
        payload = {
            "agent": agent,
            "text": text,
            "emoji": emoji,
            "color": color,
            "level": level,
            **self._acp_meta(),
        }
        if extra:
            payload.update(extra)
        await send_fn("agent_log", **payload)

    async def task_assign(
        self,
        send_fn: Callable,
        agent: str,
        prompt_preview: str = "",
        extra: Optional[dict] = None,
    ):
        """Signal that a task is being assigned to an agent."""
        step = self._next_step()
        payload = {
            "agent": agent,
            "text": f">>> 任务分配: {agent} ← {prompt_preview[:100]}",
            "emoji": "📤",
            "color": "#a0a0a0",
            "acp_msg_type": "task_assign",
            "acp_step_id": step,
            **self._acp_meta(),
        }
        if extra:
            payload.update(extra)
        await send_fn("agent_log", **payload)

    async def task_result(
        self,
        send_fn: Callable,
        agent: str,
        text: str,
        emoji: str = "✅",
        color: str = "#7eb8da",
        extra: Optional[dict] = None,
    ):
        """Signal that an agent completed a task."""
        payload = {
            "agent": agent,
            "text": text,
            "emoji": emoji,
            "color": color,
            "acp_msg_type": "task_result",
            **self._acp_meta(),
        }
        if extra:
            payload.update(extra)
        await send_fn("agent_log", **payload)

    async def pipeline_event(
        self,
        send_fn: Callable,
        event: str,
        text: str,
        emoji: str = "",
        color: str = "",
        extra: Optional[dict] = None,
    ):
        """Send a named pipeline event with ACP metadata."""
        payload = {
            "agent": "orchestrator",
            "event": event,
            "text": text,
            "emoji": emoji,
            "color": color,
            **self._acp_meta(),
        }
        if extra:
            payload.update(extra)
        await send_fn("agent_log", **payload)
