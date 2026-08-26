"""Settings Manager — persistent LLM config store.

Replaces localStorage-only approach with backend-persisted settings.
Stores in ~/.novel-ai/shared/settings.json (migrate to SQLite later per §18.3).

DESIGN_DOC §12.3: LLM Provider 配置 + API Key 管理 + 自测连接
DESIGN_DOC §4.1: ProviderPool tiered model selection per agent role
DESIGN_DOC §18.3: migrate from JSON → SQLite shared.db

Security: API keys are stored plaintext for now. Per §18.3, they should
be encrypted at rest in the SQLite migration phase.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("novel-ai.settings")

# ── Default settings file path ──
SETTINGS_DIR = Path(os.environ.get("NOVELAI_SETTINGS_DIR", Path.home() / ".novel-ai" / "shared"))
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# ── Supported provider presets ──
PROVIDER_PRESETS = {
    "deepseek": {
        "name": "DeepSeek",
        "models": ["deepseek-chat", "deepseek-reasoner", "deepseek-chat-v3"],
        "default_model": "deepseek-chat",
        "api_base": "https://api.deepseek.com/v1",
        "context_window": 128_000,
    },
    "openai": {
        "name": "OpenAI",
        "models": ["gpt-4o", "gpt-4o-mini", "o3-mini", "o4-mini"],
        "default_model": "gpt-4o",
        "api_base": "https://api.openai.com/v1",
        "context_window": 128_000,
    },
    "anthropic": {
        "name": "Anthropic",
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-3-5-20250514"],
        "default_model": "claude-sonnet-4-20250514",
        "api_base": "https://api.anthropic.com",
        "context_window": 200_000,
    },
    "openrouter": {
        "name": "OpenRouter",
        "models": ["openai/gpt-4o", "anthropic/claude-sonnet-4", "google/gemini-2.5-pro"],
        "default_model": "openai/gpt-4o",
        "api_base": "https://openrouter.ai/api/v1",
        "context_window": 128_000,
    },
    "siliconflow": {
        "name": "硅基流动",
        "models": ["deepseek-ai/DeepSeek-R1", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "default_model": "deepseek-ai/DeepSeek-V3",
        "api_base": "https://api.siliconflow.cn/v1",
        "context_window": 128_000,
    },
    "ollama": {
        "name": "Ollama (本地)",
        "models": ["llama3", "mistral", "qwen2.5"],
        "default_model": "llama3",
        "api_base": "http://localhost:11434/v1",
        "context_window": 128_000,
    },
    "custom": {
        "name": "自定义",
        "models": [],
        "default_model": "",
        "api_base": "",
        "context_window": 128_000,
    },
}

# ── Agent roles ──
AGENT_ROLES = ["writer", "reviewer", "context", "character", "canon", "style", "skeleton"]

# ── Role recommendations ──
ROLE_RECOMMENDATIONS = {
    "writer":     {"label": "写作 Agent",   "desc": "起草章节正文，需要高质量模型", "suggested": "deepseek"},
    "reviewer":   {"label": "审稿 Agent",   "desc": "一致性检查,可用廉价模型",       "suggested": "deepseek"},
    "context":    {"label": "上下文 Agent", "desc": "伏笔状态/人物位置收集",          "suggested": "deepseek"},
    "character":  {"label": "人物 Agent",   "desc": "人物行为一致性检测",              "suggested": "deepseek"},
    "canon":      {"label": "Canon Agent",  "desc": "事实冲突检测",                      "suggested": "deepseek"},
    "style":      {"label": "文风 Agent",   "desc": "文风优化,可按需使用",              "suggested": "deepseek"},
    "skeleton":   {"label": "骨架 Agent",   "desc": "骨架生成和问答",                       "suggested": "deepseek"},
}


@dataclass
class LLMConfig:
    """Per-agent LLM configuration."""
    provider: str = "deepseek"       # provider key in PROVIDER_PRESETS
    api_key: str = ""
    api_base: Optional[str] = None   # override default API base
    model: str = "deepseek-chat"
    max_tokens: int = 6000
    temperature: float = 0.85
    top_p: float = 0.95
    extra_headers: dict = field(default_factory=dict)

    def mask_key(self) -> str:
        """Return masked API key for frontend display."""
        key = self.api_key
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * (len(key) - 8) + key[-4:]


@dataclass
class UserPrefs:
    """Non-LLM user preferences."""
    theme: str = "dark-study"
    font_size: int = 100      # percentage, 80-150
    language: str = "zh-CN"
    auto_save_interval: int = 120  # seconds


@dataclass
class Settings:
    """Top-level settings object."""
    llm_configs: dict[str, LLMConfig] = field(default_factory=dict)
    user_prefs: UserPrefs = field(default_factory=UserPrefs)
    version: int = 1

    @classmethod
    def create_default(cls) -> "Settings":
        """Create default settings with empty keys."""
        settings = cls()
        for role in AGENT_ROLES:
            settings.llm_configs[role] = LLMConfig()
        return settings


class SettingsManager:
    """Manages persistent settings storage and retrieval.

    Thread-safe for reads; writes are atomic via temp-file+rename.
    """

    def __init__(self, filepath: Optional[Path] = None):
        self._filepath = filepath or SETTINGS_FILE
        self._cache: Optional[Settings] = None

    def _ensure_dir(self):
        self._filepath.parent.mkdir(parents=True, exist_ok=True)

    def load(self, force: bool = False) -> Settings:
        """Load settings from disk. Caches in memory."""
        if self._cache is not None and not force:
            return self._cache

        self._ensure_dir()
        if self._filepath.exists():
            try:
                raw = json.loads(self._filepath.read_text(encoding="utf-8"))
                settings = self._dict_to_settings(raw)
                self._cache = settings
                return settings
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Settings file corrupted, using defaults: %s", e)

        settings = Settings.create_default()
        self._cache = settings
        # Don't auto-save; only save when user explicitly configures
        return settings

    def save(self, settings: Settings):
        """Atomically save settings to disk."""
        self._ensure_dir()
        data = self._settings_to_dict(settings)
        data["version"] = 1
        data["updated_at"] = time.time()

        tmp = self._filepath.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(self._filepath)
        self._cache = settings

    def get_llm_config(self, role: str) -> Optional[LLMConfig]:
        """Get LLM config for a specific agent role."""
        settings = self.load()
        return settings.llm_configs.get(role)

    def update_llm_config(self, role: str, updates: dict) -> LLMConfig:
        """Update LLM config for one role. Returns updated config."""
        settings = self.load()
        if role not in settings.llm_configs:
            settings.llm_configs[role] = LLMConfig()

        cfg = settings.llm_configs[role]
        for key, value in updates.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

        self.save(settings)
        return cfg

    def update_all_llm(self, base: dict, roles: Optional[list[str]] = None):
        """Batch update LLM configs. roles=None means all roles."""
        settings = self.load()
        targets = roles or AGENT_ROLES
        for role in targets:
            if role not in settings.llm_configs:
                settings.llm_configs[role] = LLMConfig()
            cfg = settings.llm_configs[role]
            for key, value in base.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        self.save(settings)

    def get_all_configs_safe(self) -> dict:
        """Get all LLM configs with masked API keys for frontend."""
        settings = self.load()
        result = {}
        for role, cfg in settings.llm_configs.items():
            result[role] = {
                "provider": cfg.provider,
                "api_key": cfg.mask_key(),
                "api_key_set": bool(cfg.api_key),
                "api_base": cfg.api_base,
                "model": cfg.model,
                "max_tokens": cfg.max_tokens,
                "temperature": cfg.temperature,
                "top_p": cfg.top_p,
            }
        return result

    def get_user_prefs(self) -> UserPrefs:
        return self.load().user_prefs

    def update_user_prefs(self, updates: dict):
        settings = self.load()
        for key, value in updates.items():
            if hasattr(settings.user_prefs, key):
                setattr(settings.user_prefs, key, value)
        self.save(settings)

    def to_provider_pool(self) -> "ProviderPool":
        """Build a ProviderPool from stored settings."""
        from agents.provider_pool import ProviderConfig, ProviderPool

        pool = ProviderPool()
        for role, cfg in self.load().llm_configs.items():
            provider_name = f"{cfg.provider}-{role}"
            # Skip if already added same provider config
            if provider_name not in pool._providers:
                preset = PROVIDER_PRESETS.get(cfg.provider, {})
                pool.add_provider(provider_name, ProviderConfig(
                    provider=cfg.provider,
                    model=cfg.model,
                    api_key=cfg.api_key,
                    api_base=cfg.api_base or preset.get("api_base", ""),
                    max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    context_window=preset.get("context_window", 128_000),
                ))
            pool.assign_role(role, provider_name)
        return pool

    # ── Serialization helpers ──

    def _settings_to_dict(self, settings: Settings) -> dict:
        return {
            "llm_configs": {
                role: asdict(cfg)
                for role, cfg in settings.llm_configs.items()
            },
            "user_prefs": asdict(settings.user_prefs),
        }

    def _dict_to_settings(self, data: dict) -> Settings:
        settings = Settings()
        if "llm_configs" in data:
            for role, cfg_data in data["llm_configs"].items():
                settings.llm_configs[role] = LLMConfig(**cfg_data)
        # Fill in missing roles with defaults
        for role in AGENT_ROLES:
            if role not in settings.llm_configs:
                settings.llm_configs[role] = LLMConfig()

        if "user_prefs" in data:
            settings.user_prefs = UserPrefs(**data["user_prefs"])
        return settings

    @classmethod
    async def test_connection(cls, cfg: LLMConfig) -> dict:
        """Test a connection to the configured LLM endpoint.

        Returns: {"ok": True, "latency_ms": 123} or {"ok": False, "error": "..."}
        """
        import httpx

        provider = cfg.provider
        preset = PROVIDER_PRESETS.get(provider, {})
        base_url = (cfg.api_base or preset.get("api_base", "")).rstrip("/")

        if not base_url:
            return {"ok": False, "error": "未配置 API Base URL"}

        if not cfg.api_key:
            return {"ok": False, "error": "未配置 API Key"}

        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://novelai.local"
            headers["X-Title"] = "NovelAI Writing Studio"

        # Use models list endpoint for a cheap validation call
        models_url = f"{base_url}/models"
        t0 = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(models_url, headers=headers)
                latency = (time.monotonic() - t0) * 1000

                if resp.status_code == 200:
                    return {"ok": True, "latency_ms": round(latency, 1)}
                elif resp.status_code == 401 or resp.status_code == 403:
                    return {"ok": False, "error": f"认证失败 (HTTP {resp.status_code})，请检查 API Key"}
                elif resp.status_code == 404:
                    # Some providers don't have /models — try a chat completion ping
                    return await cls._test_with_chat_ping(cfg, base_url, headers)
                else:
                    return {"ok": False, "error": f"连接失败 (HTTP {resp.status_code})"}
        except httpx.ConnectError:
            return {"ok": False, "error": f"无法连接到 {base_url}，请检查 API Base URL"}
        except httpx.TimeoutException:
            return {"ok": False, "error": "连接超时 (15s)，请检查网络或 API Base URL"}
        except Exception as e:
            logger.exception("Connection test error")
            return {"ok": False, "error": f"连接异常: {str(e)[:200]}"}

    @classmethod
    async def _test_with_chat_ping(cls, cfg: LLMConfig, base_url: str, headers: dict) -> dict:
        """Fallback: test with a minimal chat completion request."""
        import httpx

        chat_url = f"{base_url}/chat/completions"
        t0 = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    chat_url,
                    json={
                        "model": cfg.model or "gpt-4o",
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                    headers=headers,
                )
                latency = (time.monotonic() - t0) * 1000

                if resp.status_code in (200, 201):
                    return {"ok": True, "latency_ms": round(latency, 1)}
                elif resp.status_code == 401:
                    return {"ok": False, "error": "认证失败: API Key 无效"}
                elif resp.status_code == 404:
                    return {"ok": False, "error": f"模型 '{cfg.model}' 不可用或 API 路径错误"}
                else:
                    body = resp.text[:300]
                    return {"ok": False, "error": f"请求被拒 (HTTP {resp.status_code}): {body}"}
        except Exception:
            return {"ok": False, "error": "无法完成连接测试"}


# ── Singleton ──
_settings_mgr: Optional[SettingsManager] = None


def get_settings() -> SettingsManager:
    """Get or create the global settings manager."""
    global _settings_mgr
    if _settings_mgr is None:
        _settings_mgr = SettingsManager()
    return _settings_mgr
