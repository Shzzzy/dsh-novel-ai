"""ACP Agent Setup — 初始化 8 Agent 的 ACP 通信层

Wire up: orchestrator, skeleton, context, writer, reviewer, character, canon, style
on a shared AgentBus, with optional heartbeat monitoring.

Agents:
  1. orchestrator — 主编排器, coordinates all other agents
  2. skeleton     — 骨架 Agent, guides novel planning
  3. context      — 上下文 Agent, assembles writing context
  4. writer       — 写作 Agent, generates chapter prose
  5. reviewer     — 审核 Agent, quality checks
  6. character    — 人物 Agent, character consistency
  7. canon        — Canon Agent, single source of truth
  8. style        — 文风 Agent, style constraints

Usage:
    from engine.acp_setup import AcpSetup
    
    setup = AcpSetup(config)
    await setup.bootstrap()
    
    # Use via bus:
    reply = await setup.bus.request(make_task_assign(
        "orchestrator", "writer", "写一段文字", task_id="w-1",
    ))
    
    # Access wrapped agents:
    setup.context.execute("query")
    setup.writer.execute("prompt")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from agents.base import AgentConfig
from agents.context_agent import ContextAgent
from agents.writer_agent import WriterAgent
from agents.reviewer_agent import ReviewerAgent
from agents.character_agent import CharacterAgent
from agents.canon_agent import CanonAgent
from agents.style_agent import StyleAgent
from agents.skeleton_agent import SkeletonAgent
from agents.orchestrator import Orchestrator
from sync.sync_layer import SyncLayer

from engine.acp import AgentBus, AgentMessage, MessageType
from engine.acp_agent import ACPAgent

logger = logging.getLogger("novel-ai.acp-setup")

# ── Agent names (the "8 Agents" in ACP spec) ──
AC_ORCHESTRATOR = "orchestrator"
AC_SKELETON = "skeleton"
AC_CONTEXT = "context"
AC_WRITER = "writer"
AC_REVIEWER = "reviewer"
AC_CHARACTER = "character"
AC_CANON = "canon"
AC_STYLE = "style"

ALL_AGENTS = [AC_ORCHESTRATOR, AC_SKELETON, AC_CONTEXT, AC_WRITER,
              AC_REVIEWER, AC_CHARACTER, AC_CANON, AC_STYLE]


class AcpSetup:
    """Creates and wires all 8 agents with ACP.

    Two modes:
    1. With real BaseAgent subclasses (production) — pass config
    2. With mock agents (testing) — pass mock instances

    After bootstrap(), all agents are registered on self.bus.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.bus = AgentBus()

        # Raw agents (populated by bootstrap)
        self.raw_orchestrator: Optional[Orchestrator] = None
        self.raw_skeleton: Optional[SkeletonAgent] = None
        self.raw_context: Optional[ContextAgent] = None
        self.raw_writer: Optional[WriterAgent] = None
        self.raw_reviewer: Optional[ReviewerAgent] = None
        self.raw_character: Optional[CharacterAgent] = None
        self.raw_canon: Optional[CanonAgent] = None
        self.raw_style: Optional[StyleAgent] = None

        # ACP-wrapped agents (populated by bootstrap)
        self.orchestrator: Optional[ACPAgent] = None
        self.skeleton: Optional[ACPAgent] = None
        self.context: Optional[ACPAgent] = None
        self.writer: Optional[ACPAgent] = None
        self.reviewer: Optional[ACPAgent] = None
        self.character: Optional[ACPAgent] = None
        self.canon: Optional[ACPAgent] = None
        self.style: Optional[ACPAgent] = None

        self._heartbeat_tasks: list[asyncio.Task] = []
        self._booted = False

    async def bootstrap(self, heartbeat_interval_s: float = 30.0) -> "AcpSetup":
        """Create and register all 8 agents. Returns self for chaining."""
        # ── Create raw agents ──
        sync_layer = SyncLayer()
        self.raw_orchestrator = Orchestrator(self.config, sync_layer)
        self.raw_skeleton = SkeletonAgent(self.config)
        self.raw_context = ContextAgent(self.config)
        self.raw_writer = WriterAgent(self.config)
        self.raw_reviewer = ReviewerAgent(self.config)
        self.raw_character = CharacterAgent(self.config)
        self.raw_canon = CanonAgent(self.config)
        self.raw_style = StyleAgent(self.config)

        # ── Wrap in ACPAgent ──
        self.orchestrator = ACPAgent(self.raw_orchestrator, AC_ORCHESTRATOR, self.bus)
        self.skeleton = ACPAgent(self.raw_skeleton, AC_SKELETON, self.bus)
        self.context = ACPAgent(self.raw_context, AC_CONTEXT, self.bus)
        self.writer = ACPAgent(self.raw_writer, AC_WRITER, self.bus)
        self.reviewer = ACPAgent(self.raw_reviewer, AC_REVIEWER, self.bus)
        self.character = ACPAgent(self.raw_character, AC_CHARACTER, self.bus)
        self.canon = ACPAgent(self.raw_canon, AC_CANON, self.bus)
        self.style = ACPAgent(self.raw_style, AC_STYLE, self.bus)

        # ── Register all 8 on bus ──
        await asyncio.gather(
            self.orchestrator.register(),
            self.skeleton.register(),
            self.context.register(),
            self.writer.register(),
            self.reviewer.register(),
            self.character.register(),
            self.canon.register(),
            self.style.register(),
        )

        # ── Health check: verify all 8 agents respond ──
        for agent_name in ALL_AGENTS:
            result = await self.bus.heartbeat(agent_name)
            if not result["alive"]:
                logger.warning("Agent %s health check failed: %s",
                               agent_name, result.get("reason", "unknown"))

        # ── Start heartbeat loop (optional) ──
        if heartbeat_interval_s > 0:
            for agent_name in ALL_AGENTS:
                task = await self.bus.start_heartbeat_loop(
                    agent_name, interval_s=heartbeat_interval_s
                )
                self._heartbeat_tasks.append(task)

        self._booted = True
        logger.info(
            "ACP bootstrap complete: %d agents registered, %d healthy on bus",
            len(ALL_AGENTS),
            await self.bus.registry.healthy_count(),
        )
        return self

    async def shutdown(self):
        """Cancel heartbeat tasks and unregister agents."""
        for task in self._heartbeat_tasks:
            task.cancel()
        self._heartbeat_tasks = []

        for agent in [self.orchestrator, self.skeleton,
                      self.context, self.writer, self.reviewer,
                      self.character, self.canon, self.style]:
            if agent:
                try:
                    await agent.unregister()
                except Exception:
                    pass

        self._booted = False

    async def health_report(self) -> dict:
        """Return health status for all agents."""
        report = {}
        for name in ALL_AGENTS:
            result = await self.bus.heartbeat(name)
            report[name] = result
        return report

    @property
    def is_booted(self) -> bool:
        return self._booted

    # ── Agent lookup ──

    def get(self, name: str) -> Optional[ACPAgent]:
        """Get ACPAgent by name."""
        mapping = {
            AC_ORCHESTRATOR: self.orchestrator,
            AC_SKELETON: self.skeleton,
            AC_CONTEXT: self.context,
            AC_WRITER: self.writer,
            AC_REVIEWER: self.reviewer,
            AC_CHARACTER: self.character,
            AC_CANON: self.canon,
            AC_STYLE: self.style,
        }
        return mapping.get(name)

    def get_raw(self, name: str):
        """Get raw (unwrapped) BaseAgent by name."""
        mapping = {
            AC_ORCHESTRATOR: self.raw_orchestrator,
            AC_SKELETON: self.raw_skeleton,
            AC_CONTEXT: self.raw_context,
            AC_WRITER: self.raw_writer,
            AC_REVIEWER: self.raw_reviewer,
            AC_CHARACTER: self.raw_character,
            AC_CANON: self.raw_canon,
            AC_STYLE: self.raw_style,
        }
        return mapping.get(name)
