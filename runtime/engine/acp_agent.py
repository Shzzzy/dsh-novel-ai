"""ACPAgent — ACP-aware wrapper around BaseAgent.

Wraps any BaseAgent subclass with:
- Automatic registration on AgentBus
- Heartbeat responder
- task_assign → generate → task_result flow
- Structured error handling via task_error
- Progress reporting via task_progress

Usage:
    agent = ACPAgent(writer, "writer", bus)
    await agent.register()  # → sends REGISTER, handles HEARTBEAT, TASK_ASSIGN
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from agents.base import BaseAgent
from engine.acp import (
    AgentBus, AgentMessage, AgentRecord, MessageType,
)

logger = logging.getLogger("novel-ai.acp")


class ACPAgent:
    """ACP-wrapped agent that communicates via the message bus.

    Responsibilities:
    - Register on the bus with message handler
    - Respond to HEARTBEAT with HEARTBEAT_ACK
    - Handle TASK_ASSIGN → run generate() → reply TASK_RESULT
    - Handle REQUEST → run generate() → reply RESPONSE
    - Report errors via TASK_ERROR
    - Emit task_progress for long-running work
    """

    def __init__(
        self,
        agent: BaseAgent,
        name: str,
        bus: AgentBus,
        default_timeout_s: float = 120.0,
    ):
        self.agent = agent
        self.name = name
        self.bus = bus
        self.default_timeout_s = default_timeout_s
        self._record: Optional[AgentRecord] = None
        self._status: dict = {"state": "initialized", "tasks_handled": 0, "errors": 0}
        self._health: bool = True

    # ── Lifecycle ──

    async def register(self) -> AgentRecord:
        """Register with the bus. Must be called before any communication."""
        self._record = await self.bus.register(self.name, self._handle_message)
        self._status["state"] = "registered"
        logger.info("ACPAgent[%s] registered on bus", self.name)
        return self._record

    async def unregister(self) -> bool:
        """Remove from the bus."""
        self._status["state"] = "unregistered"
        self._record = None
        return await self.bus.unregister(self.name)

    @property
    def is_registered(self) -> bool:
        return self._record is not None

    @property
    def status(self) -> dict:
        return dict(self._status)

    # ── Direct invocation (bypasses bus, for orchestrator) ──

    async def execute(self, prompt: str, system: Optional[str] = None) -> str:
        """Run generate() directly — no bus routing. Returns result text."""
        self._status["tasks_handled"] += 1
        try:
            result = await self.agent.generate(prompt, system=system)
            return result
        except Exception as e:
            self._status["errors"] += 1
            raise

    async def execute_stream(
        self, prompt: str, system: Optional[str] = None
    ):
        """Run generate_stream() directly — yield tokens."""
        self._status["tasks_handled"] += 1
        try:
            async for token in self.agent.generate_stream(prompt, system=system):
                yield token
        except Exception as e:
            self._status["errors"] += 1
            raise

    # ── Message handler ──

    async def _handle_message(self, msg: AgentMessage) -> AgentMessage:
        """Main message dispatcher. Routes by msg_type."""
        try:
            if msg.msg_type == MessageType.HEARTBEAT:
                return await self._handle_heartbeat(msg)
            elif msg.msg_type == MessageType.TASK_ASSIGN:
                return await self._handle_task_assign(msg)
            elif msg.msg_type == MessageType.REQUEST:
                return await self._handle_request(msg)
            elif msg.msg_type == MessageType.NOTIFY:
                await self._handle_notify(msg)
                return AgentMessage(
                    msg_type=MessageType.HEARTBEAT_ACK,
                    sender=self.name,
                    receiver=msg.sender,
                    task_id=msg.task_id,
                    correlation_id=msg.msg_id,
                )
            else:
                # Unknown type — ack with heartbeat_ack as acknowledgement
                return AgentMessage(
                    msg_type=MessageType.HEARTBEAT_ACK,
                    sender=self.name,
                    receiver=msg.sender,
                    task_id=msg.task_id,
                    correlation_id=msg.msg_id,
                    payload={"echo_type": msg.msg_type.value},
                )
        except Exception as e:
            logger.exception("ACPAgent[%s] handler error for %s", self.name, msg.msg_type)
            self._status["errors"] += 1
            return AgentMessage(
                msg_type=MessageType.TASK_ERROR,
                sender=self.name,
                receiver=msg.sender,
                task_id=msg.task_id,
                correlation_id=msg.msg_id,
                payload={"error": str(e), "error_type": type(e).__name__},
            )

    # ── Message type handlers ──

    async def _handle_heartbeat(self, msg: AgentMessage) -> AgentMessage:
        """Respond to heartbeat."""
        return AgentMessage(
            msg_type=MessageType.HEARTBEAT_ACK,
            sender=self.name,
            receiver=msg.sender,
            task_id=msg.task_id,
            correlation_id=msg.msg_id,
            payload={"status": self._status},
        )

    async def _handle_task_assign(self, msg: AgentMessage) -> AgentMessage:
        """Execute task: build prompt → generate → return result."""
        self._status["tasks_handled"] += 1
        payload = msg.payload
        prompt = payload.get("prompt", "")
        system_prompt = payload.get("system_prompt", None)

        try:
            result = await self.agent.generate(prompt, system=system_prompt)
            return AgentMessage(
                msg_type=MessageType.TASK_RESULT,
                sender=self.name,
                receiver=msg.sender,
                task_id=msg.task_id,
                correlation_id=msg.msg_id,
                payload={"result": result, "agent": self.name},
            )
        except Exception as e:
            self._status["errors"] += 1
            return AgentMessage(
                msg_type=MessageType.TASK_ERROR,
                sender=self.name,
                receiver=msg.sender,
                task_id=msg.task_id,
                correlation_id=msg.msg_id,
                payload={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "agent": self.name,
                },
            )

    async def _handle_request(self, msg: AgentMessage) -> AgentMessage:
        """Handle info request from another agent."""
        payload = msg.payload
        prompt = payload.get("prompt", "")
        system_prompt = payload.get("system_prompt", None)

        try:
            result = await self.agent.generate(prompt, system=system_prompt)
            return AgentMessage(
                msg_type=MessageType.RESPONSE,
                sender=self.name,
                receiver=msg.sender,
                task_id=msg.task_id,
                correlation_id=msg.msg_id,
                payload={"result": result, "agent": self.name},
            )
        except Exception as e:
            return AgentMessage(
                msg_type=MessageType.TASK_ERROR,
                sender=self.name,
                receiver=msg.sender,
                task_id=msg.task_id,
                correlation_id=msg.msg_id,
                payload={"error": str(e), "agent": self.name},
            )

    async def _handle_notify(self, msg: AgentMessage) -> None:
        """Receive one-way notification (e.g., state update)."""
        event_type = msg.payload.get("event", "")
        logger.debug("ACPAgent[%s] notified: %s → %s",
                      self.name, msg.sender, event_type)


# ── Typed messages for specific agent tasks ──

def make_task_assign(
    sender: str,
    receiver: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    task_id: str = "",
    extra: Optional[dict] = None,
) -> AgentMessage:
    """Helper: create a TASK_ASSIGN message."""
    payload = {"prompt": prompt}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if extra:
        payload.update(extra)
    return AgentMessage(
        msg_type=MessageType.TASK_ASSIGN,
        sender=sender,
        receiver=receiver,
        task_id=task_id or "",
        payload=payload,
    )


def make_log(
    sender: str,
    text: str,
    level: str = "info",
    agent_type: str = "",
    color: str = "",
    extra: Optional[dict] = None,
) -> AgentMessage:
    """Helper: create a structured LOG message (for WebSocket agent_log)."""
    payload = {"text": text, "level": level}
    if agent_type:
        payload["agent"] = agent_type
    if color:
        payload["color"] = color
    if extra:
        payload.update(extra)
    return AgentMessage(
        msg_type=MessageType.LOG,
        sender=sender,
        receiver="*",
        payload=payload,
    )
