"""Base agent class with LLM provider abstraction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, AsyncIterator
import json
import asyncio
import logging

logger = logging.getLogger("novel-ai")

# 可重试的HTTP状态码
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0  # 指数退避基数(秒)


@dataclass
class AgentConfig:
    """Configuration for an agent."""
    name: str
    model: str = "deepseek-chat"
    provider: str = "deepseek"  # deepseek, anthropic, openai
    api_key: str = ""
    api_base: str = ""
    max_tokens: int = 8192  # 长篇写作: 2000-4500字 ≈ 3000-7000 tokens, 上浮20%余量
    temperature: float = 0.85
    system_prompt: str = ""


class BaseAgent(ABC):
    """Abstract base for all novel-writing agents."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self._history: list[dict] = []
        self._offline = not config.api_key

    def is_offline(self) -> bool:
        return self._offline

    @abstractmethod
    def build_prompt(self, **kwargs) -> str:
        """Build the user prompt for this agent's task."""
        ...

    async def generate(self, prompt: str, system: str | None = None) -> str:
        """Call the LLM and return the full response text."""
        if self._offline:
            return self._offline_response(prompt)
        system_prompt = system or self.config.system_prompt
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return await self._call_provider(messages)

    async def generate_stream(self, prompt: str, system: str | None = None) -> AsyncIterator[str]:
        """Call the LLM and yield tokens as they arrive."""
        if self._offline:
            text = self._offline_response(prompt)
            for word in text.split():
                yield word + " "
                import asyncio
                await asyncio.sleep(0.02)
            return
        system_prompt = system or self.config.system_prompt
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        async for token in self._call_provider_stream(messages):
            yield token

    def _offline_response(self, prompt: str) -> str:
        return "[OFFLINE MODE] LLM未连接。请在设置中配置API Key。"

    async def _call_provider(self, messages: list[dict]) -> str:
        import httpx, asyncio, logging
        logger = logging.getLogger("novel-ai")
        base = self.config.api_base or "https://api.deepseek.com/v1"
        url = f"{base}/chat/completions"
        MAX_RETRIES = 3; BASE_DELAY = 1.0; RETRYABLE_STATUS = {429, 502, 503, 504}
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    }, json={
                        "model": self.config.model,
                        "messages": messages,
                        "max_tokens": self.config.max_tokens,
                        "temperature": self.config.temperature,
                    })
                    data = resp.json()
                    if resp.status_code == 200:
                        return data["choices"][0]["message"]["content"]
                    if resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500:
                        last_error = Exception(f"API error {resp.status_code}")
                        if attempt < MAX_RETRIES:
                            delay = BASE_DELAY * (2 ** attempt)
                            logger.warning(f"LLM retry {attempt+1}/{MAX_RETRIES+1}")
                            await asyncio.sleep(delay)
                            continue
                    raise last_error or Exception(f"API error {resp.status_code}")
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BASE_DELAY * (2 ** attempt))
                    continue
                raise
        raise last_error or Exception("LLM call failed")

    async def _call_provider_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        import httpx
        base = self.config.api_base or "https://api.deepseek.com/v1"
        url = f"{base}/chat/completions"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream("POST", url, headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    }, json={
                        "model": self.config.model,
                        "messages": messages,
                        "max_tokens": self.config.max_tokens,
                        "temperature": self.config.temperature,
                        "stream": True,
                    }) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                chunk = line[6:]
                                if chunk == "[DONE]":
                                    return
                                try:
                                    import json
                                    data = json.loads(chunk)
                                    delta = data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        yield delta["content"]
                                except Exception:
                                    pass
                return
            except Exception:
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(1.0 * (2 ** attempt))
                    continue
                raise


class SimpleAgent(BaseAgent):
    """最小化具体Agent——用于不需要 build_prompt 的简单LLM调用。"""
    def build_prompt(self, **kwargs) -> str:
        return kwargs.get("prompt", "")

    async def _call_provider(self, messages: list[dict]) -> str:
        """Default provider call using OpenAI-compatible API with retry."""
        import httpx

        base = self.config.api_base or "https://api.deepseek.com/v1"
        url = f"{base}/chat/completions"
        last_error = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {self.config.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.config.model,
                            "messages": messages,
                            "max_tokens": self.config.max_tokens,
                            "temperature": self.config.temperature,
                        },
                    )

                # 鉴权/参数错误 → 不重试
                if resp.status_code in (401, 403, 400, 404):
                    resp.raise_for_status()

                data = resp.json()

                # API返回错误 → 可重试或直接失败
                if resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500:
                    last_error = Exception(
                        f"API error {resp.status_code}: {data.get('error', {}).get('message', '')}"
                    )
                    if attempt < MAX_RETRIES:
                        delay = BASE_DELAY * (2 ** attempt)
                        logger.warning(
                            f"LLM call failed (attempt {attempt+1}/{MAX_RETRIES+1}), "
                            f"retrying in {delay:.1f}s: {last_error}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise last_error

                return data["choices"][0]["message"]["content"]

            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Network error (attempt {attempt+1}), retrying: {e}")
                    await asyncio.sleep(delay)
                    continue
                raise

        raise last_error or Exception("LLM call failed")

    async def _call_provider_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Default streaming provider call. Retries on connection setup failure.

        Stream中断时回退到非流式调用以获取完整响应。
        """
        import httpx

        base = self.config.api_base or "https://api.deepseek.com/v1"
        url = f"{base}/chat/completions"
        parse_errors = 0

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream(
                        "POST", url,
                        headers={
                            "Authorization": f"Bearer {self.config.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.config.model,
                            "messages": messages,
                            "max_tokens": self.config.max_tokens,
                            "temperature": self.config.temperature,
                            "stream": True,
                        },
                    ) as resp:
                        # 鉴权错误不重试
                        if resp.status_code in (401, 403, 400):
                            resp.raise_for_status()

                        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                            delay = BASE_DELAY * (2 ** attempt)
                            await asyncio.sleep(delay)
                            continue

                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        yield content
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    parse_errors += 1
                                    if parse_errors > 10:
                                        logger.warning(
                                            f"Stream parse errors exceeded threshold ({parse_errors})"
                                        )
                                    continue
                return  # Stream succeeded, exit retry loop

            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError) as e:
                if attempt < MAX_RETRIES:
                    delay = BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Stream connection failed, retrying: {e}")
                    await asyncio.sleep(delay)
                    continue
                # 最后一次失败: 回退到非流式
                logger.warning("Stream failed after retries, falling back to non-stream")
                text = await self._call_provider(messages)
                for word in text.split():
                    yield word + " "
                return
