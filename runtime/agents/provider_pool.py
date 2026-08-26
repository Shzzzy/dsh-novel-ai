"""Provider Pool — maps agent roles to LLM provider configurations.

Replaces the single AgentConfig with a multi-provider pool, enabling:
- Writer Agent → high-quality model (DeepSeek/Claude)
- Reviewer Agent → cheaper model for consistency checks
- Context/Character/Canon/Style agents → fast model for extraction tasks
"""

from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    """Configuration for one LLM provider."""
    provider: str         # "deepseek" | "openai" | "anthropic"
    model: str            # "deepseek-chat" | "claude-sonnet-4-20250514" | ...
    api_key: str = ""
    api_base: str = ""
    max_tokens: int = 6000
    temperature: float = 0.85
    # Per-model limits
    context_window: int = 128_000


@dataclass
class AgentRoleConfig:
    """Which provider an agent role uses."""
    agent_role: str       # "writer" | "reviewer" | "context" | "character" | "canon" | "style" | "skeleton"
    provider_name: str    # which provider in the pool to use
    extra_params: dict = field(default_factory=dict)


class ProviderPool:
    """Manages multiple LLM providers for different agent roles.

    Usage:
        pool = ProviderPool()
        pool.add_provider("deepseek", ProviderConfig(...))
        pool.add_provider("deepseek-cheap", ProviderConfig(...))
        pool.assign_role("writer", "deepseek")
        pool.assign_role("reviewer", "deepseek-cheap")

        writer_cfg = pool.get_config("writer")
        reviewer_cfg = pool.get_config("reviewer")
    """

    def __init__(self):
        self._providers: dict[str, ProviderConfig] = {}
        self._roles: dict[str, AgentRoleConfig] = {}

    def add_provider(self, name: str, config: ProviderConfig):
        self._providers[name] = config

    def assign_role(self, agent_role: str, provider_name: str, **extra):
        if provider_name not in self._providers:
            raise ValueError(f"Unknown provider: {provider_name}")
        self._roles[agent_role] = AgentRoleConfig(
            agent_role=agent_role,
            provider_name=provider_name,
            extra_params=extra,
        )

    def get_config(self, agent_role: str) -> ProviderConfig:
        """Get the provider config for an agent role."""
        role = self._roles.get(agent_role)
        if not role:
            # Fallback: use the first provider in the pool
            if self._providers:
                return next(iter(self._providers.values()))
            raise ValueError(f"No provider assigned for role '{agent_role}'"
                             " and no fallback available")
        return self._providers[role.provider_name]

    def role_uses_provider(self, agent_role: str, provider_name: str) -> bool:
        role = self._roles.get(agent_role)
        return role is not None and role.provider_name == provider_name

    def get_model(self, agent_role: str = "writer", task_type: str = "") -> str:
        """Get the model name for an agent role (convenience wrapper).

        Args:
            agent_role: "writer", "reviewer", "context", "character", "canon", etc.
            task_type: Optional hint for model selection (e.g., "write_chapter" → writer,
                       "review_chapter" → reviewer).

        Returns:
            Model name string (e.g., "deepseek-chat").
        """
        # Map task_type to agent_role if agent_role is ambiguous
        role = agent_role
        if task_type and task_type.startswith("review"):
            role = "reviewer"
        elif task_type and task_type.startswith("write"):
            role = "writer"
        try:
            return self.get_config(role).model
        except ValueError:
            if self._providers:
                return next(iter(self._providers.values())).model
            return "deepseek-chat"

    def available(self) -> list[str]:
        """Return list of available provider names."""
        return list(self._providers.keys())

    def override(self, model: str):
        """Override all provider models with a specific model name."""
        for cfg in self._providers.values():
            cfg.model = model

    @classmethod
    def default(cls, api_key: str = "") -> "ProviderPool":
        """Create a default pool with DeepSeek for all roles."""
        pool = cls()
        pool.add_provider("deepseek", ProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            api_key=api_key,
            api_base="https://api.deepseek.com/v1",
            max_tokens=6000,
            temperature=0.85,
        ))
        for role in ["writer", "reviewer", "context", "character", "canon", "style", "skeleton"]:
            pool.assign_role(role, "deepseek")
        return pool

    @classmethod
    def tiered_default(cls, api_key: str = "") -> "ProviderPool":
        """Create a tiered pool: Writer=DeepSeek, Reviewer+others=DeepSeek.

        Future: Reviewer → cheaper model, Writer → Claude Opus.
        """
        pool = cls()
        pool.add_provider("deepseek", ProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            api_key=api_key,
            api_base="https://api.deepseek.com/v1",
            max_tokens=6000,
            temperature=0.85,
        ))
        for role in ["writer", "reviewer", "context", "character", "canon", "style", "skeleton"]:
            pool.assign_role(role, "deepseek")
        return pool
