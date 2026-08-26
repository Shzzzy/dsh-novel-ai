"""Agent Communication Protocol (ACP) — 标准化 Agent 间通信层

Protocol spec:
- AgentMessage: Pydantic model for all inter-agent messages
- MessageType: Enum defining the protocol message types
- AgentRegistry: Singleton registry for agent discovery
- AgentBus: Async message bus with routing, timeout, retry, and fallback

Usage:
    bus = AgentBus()
    bus.register("writer", writer_handler)
    
    # fire-and-forget
    await bus.send(AgentMessage(...))
    
    # request-response with timeout
    reply = await bus.request(AgentMessage(...), timeout_s=30.0)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("novel-ai.acp")


# ── Message Types ──

class MessageType(str, Enum):
    """ACP message types"""
    # Lifecycle
    REGISTER = "register"
    REGISTER_ACK = "register_ack"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"

    # Task
    TASK_ASSIGN = "task_assign"
    TASK_RESULT = "task_result"
    TASK_ERROR = "task_error"
    TASK_PROGRESS = "task_progress"

    # Agent-to-agent
    REQUEST = "request"       # agent A asks agent B for info
    RESPONSE = "response"     # agent B replies
    NOTIFY = "notify"         # one-way notification
    LOG = "log"               # structured log entry


@dataclass
class AgentMessage:
    """Standard inter-agent message.

    Fields:
        msg_id: Unique message ID (auto-generated)
        msg_type: MessageType enum value
        sender: Sender agent name (e.g. "orchestrator")
        receiver: Receiver agent name (e.g. "writer") or "*" for broadcast
        task_id: Correlation ID — links task_assign ↔ task_result pairs
        payload: Arbitrary task data (dict)
        timestamp: ISO-8601 timestamp
        correlation_id: Links to previous message in chain
    """
    msg_type: MessageType
    sender: str
    receiver: str
    task_id: str = ""
    payload: dict = field(default_factory=dict)
    msg_id: str = ""
    timestamp: str = ""
    correlation_id: str = ""

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            from datetime import datetime, timezone
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type.value,
            "sender": self.sender,
            "receiver": self.receiver,
            "task_id": self.task_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentMessage":
        return cls(
            msg_id=d.get("msg_id", ""),
            msg_type=MessageType(d["msg_type"]),
            sender=d.get("sender", ""),
            receiver=d.get("receiver", ""),
            task_id=d.get("task_id", ""),
            payload=d.get("payload", {}),
            timestamp=d.get("timestamp", ""),
            correlation_id=d.get("correlation_id", ""),
        )


# ── Agent Handler ──

AgentHandler = Callable[[AgentMessage], Coroutine[Any, Any, AgentMessage]]


@dataclass
class AgentRecord:
    """Registered agent metadata."""
    name: str
    handler: AgentHandler
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    is_healthy: bool = True

    def __post_init__(self):
        if not self.registered_at:
            import time
            self.registered_at = time.time()
            self.last_heartbeat = self.registered_at


# ── Agent Registry ──

class AgentRegistry:
    """Singleton registry for agent discovery.

    Agents register with their name and handler function.
    The bus routes messages to the correct handler.
    """

    def __init__(self):
        self._agents: dict[str, AgentRecord] = {}
        self._lock = asyncio.Lock()

    async def register(self, name: str, handler: AgentHandler) -> AgentRecord:
        """Register an agent. Returns the record."""
        async with self._lock:
            record = AgentRecord(name=name, handler=handler)
            self._agents[name] = record
            logger.debug("Agent registered: %s", name)
            return record

    async def unregister(self, name: str) -> bool:
        """Remove an agent. Returns True if it existed."""
        async with self._lock:
            if name in self._agents:
                del self._agents[name]
                logger.debug("Agent unregistered: %s", name)
                return True
            return False

    async def get(self, name: str) -> Optional[AgentRecord]:
        """Get agent record by name."""
        return self._agents.get(name)

    async def list_all(self) -> list[str]:
        """List all registered agent names."""
        return list(self._agents.keys())

    async def healthy_count(self) -> int:
        """Count of healthy agents."""
        return sum(1 for r in self._agents.values() if r.is_healthy)

    async def is_registered(self, name: str) -> bool:
        return name in self._agents


# ── Agent Bus ──

class AgentBus:
    """Async message bus for agent communication.

    Features:
    - Message routing by receiver name (point-to-point) or broadcast ("*")
    - Request-response with timeout (correlated by task_id)
    - Fire-and-forget send
    - Heartbeat health tracking
    - Timeout and retry for task_assign messages
    """

    def __init__(self, registry: Optional[AgentRegistry] = None):
        self.registry = registry or AgentRegistry()
        # Pending requests: task_id → Future
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_lock = asyncio.Lock()

        # Default timeouts
        self.default_timeout_s = 60.0
        self.heartbeat_timeout_s = 10.0
        self.max_retries = 2

    # ── Registration ──

    async def register(self, name: str, handler: AgentHandler) -> AgentRecord:
        return await self.registry.register(name, handler)

    async def unregister(self, name: str) -> bool:
        return await self.registry.unregister(name)

    # ── Send / Route ──

    async def send(self, msg: AgentMessage) -> None:
        """Fire-and-forget: route message to receiver. No reply expected."""
        await self._route(msg)

    async def request(
        self, msg: AgentMessage, timeout_s: Optional[float] = None
    ) -> AgentMessage:
        """Send and wait for reply. Timeout raises TimeoutError.

        The receiver must reply with a message whose correlation_id matches
        this message's msg_id (or task_id).
        """
        if not msg.task_id:
            msg.task_id = msg.msg_id

        timeout = timeout_s if timeout_s is not None else self.default_timeout_s
        waiter: asyncio.Future = asyncio.get_event_loop().create_future()

        async with self._pending_lock:
            self._pending[msg.task_id] = waiter

        try:
            # Send the message
            await self._route(msg)

            # Wait for reply
            reply = await asyncio.wait_for(waiter, timeout=timeout)
            return reply
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"AgentBus request timed out after {timeout:.1f}s: "
                f"{msg.sender} → {msg.receiver} (task={msg.task_id})"
            )
        finally:
            async with self._pending_lock:
                self._pending.pop(msg.task_id, None)

    async def request_with_retry(
        self, msg: AgentMessage,
        timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> AgentMessage:
        """Request with retry on timeout or error."""
        retries = max_retries if max_retries is not None else self.max_retries
        timeout = timeout_s if timeout_s is not None else self.default_timeout_s

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                # Use a fresh msg_id for each attempt but keep the task_id
                msg.msg_id = str(uuid.uuid4())[:8]
                return await self.request(msg, timeout_s=timeout)
            except (TimeoutError, Exception) as e:
                last_error = e
                if attempt < retries:
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        "ACP retry %d/%d for %s → %s after %.1fs: %s",
                        attempt + 1, retries, msg.sender, msg.receiver, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        raise last_error or Exception("request_with_retry failed")

    async def broadcast(self, msg: AgentMessage) -> list[AgentMessage]:
        """Send to all registered agents. Returns list of reply futures."""
        msg.receiver = "*"
        names = await self.registry.list_all()
        results = []
        for name in names:
            m = AgentMessage(
                msg_type=msg.msg_type,
                sender=msg.sender,
                receiver=name,
                task_id=msg.task_id,
                payload=dict(msg.payload),
            )
            # Send without waiting for replies
            await self._route(m)
        return results

    async def resolve_reply(self, reply: AgentMessage) -> None:
        """Deliver a reply to a pending request future."""
        # Match by task_id (primary) or correlation_id (fallback)
        async with self._pending_lock:
            waiter = self._pending.get(reply.task_id) or self._pending.get(reply.correlation_id)
        if waiter and not waiter.done():
            waiter.set_result(reply)

    # ── Heartbeat ──

    async def heartbeat(self, agent_name: str) -> dict:
        """Check if agent is alive. Returns {alive, latency_ms}."""
        record = await self.registry.get(agent_name)
        if not record:
            return {"alive": False, "reason": "not_registered"}

        msg = AgentMessage(
            msg_type=MessageType.HEARTBEAT,
            sender="agent_bus",
            receiver=agent_name,
        )

        try:
            reply = await self.request(msg, timeout_s=self.heartbeat_timeout_s)
            if reply.msg_type == MessageType.HEARTBEAT_ACK:
                import time
                record.last_heartbeat = time.time()
                record.is_healthy = True
                return {"alive": True, "latency_ms": 0}
            return {"alive": False, "reason": "unexpected_reply"}
        except TimeoutError:
            record.is_healthy = False
            return {"alive": False, "reason": "timeout"}

    async def start_heartbeat_loop(
        self, agent_name: str, interval_s: float = 30.0
    ) -> asyncio.Task:
        """Start periodic heartbeat check for an agent. Returns cancelable task."""
        async def _loop():
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.heartbeat(agent_name)
                except Exception:
                    pass

        task = asyncio.create_task(_loop())
        return task

    # ── Internal ──

    async def _route(self, msg: AgentMessage) -> None:
        """Route message to target agent handler."""
        if msg.receiver == "*":
            names = await self.registry.list_all()
            for name in names:
                agent = await self.registry.get(name)
                if agent:
                    try:
                        await agent.handler(msg)
                    except Exception:
                        logger.exception("ACP route error: %s → %s", msg.sender, name)
            return

        agent = await self.registry.get(msg.receiver)
        if not agent:
            logger.warning("ACP: receiver not found: %s (from %s)", msg.receiver, msg.sender)
            return

        try:
            response = await agent.handler(msg)
            # Auto-resolve: if handler returned a reply, deliver it to waiter
            if response is not None:
                await self.resolve_reply(response)
        except Exception:
            logger.exception(
                "ACP handler error: %s → %s (task=%s)", msg.sender, msg.receiver, msg.task_id
            )
            # Send task_error reply if this was a task_assign
            if msg.msg_type == MessageType.TASK_ASSIGN and msg.task_id:
                error_reply = AgentMessage(
                    msg_type=MessageType.TASK_ERROR,
                    sender=msg.receiver,
                    receiver=msg.sender,
                    task_id=msg.task_id,
                    correlation_id=msg.msg_id,
                    payload={"error": "handler_exception"},
                )
                await self.resolve_reply(error_reply)
