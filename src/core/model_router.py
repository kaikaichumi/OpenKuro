"""Model router: unified LLM interface via LiteLLM.

Supports cloud providers (Anthropic, OpenAI, Google) and local models (Ollama, llama.cpp)
with automatic fallback chain.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import litellm
import structlog

from src.config import KuroConfig
from src.core.types import ModelResponse, ToolCall

logger = structlog.get_logger()

# Suppress LiteLLM's verbose logging
litellm.suppress_debug_info = True


class ModelRouter:
    """Routes LLM requests through LiteLLM with fallback support."""

    def __init__(self, config: KuroConfig) -> None:
        self.config = config
        self._setup_provider_keys()

    def _setup_provider_keys(self) -> None:
        """Set up API keys from config into environment variables."""
        for provider_name, provider_cfg in self.config.models.providers.items():
            if provider_cfg.api_key_env:
                key = provider_cfg.get_api_key()
                if key:
                    # LiteLLM reads keys from env vars
                    os.environ.setdefault(provider_cfg.api_key_env, key)

            if provider_cfg.base_url and provider_name == "ollama":
                os.environ.setdefault("OLLAMA_API_BASE", provider_cfg.base_url)

            # LiteLLM reads Gemini key from GEMINI_API_KEY
            if provider_name == "gemini" and provider_cfg.api_key_env:
                key = provider_cfg.get_api_key()
                if key:
                    os.environ.setdefault("GEMINI_API_KEY", key)

    @property
    def default_model(self) -> str:
        return self.config.models.default

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """Send a completion request to the LLM.

        Tries the specified model first, then falls back through the chain.
        """
        target_model = model or self.default_model
        use_stream = stream if stream is not None else self.config.models.stream
        temp = temperature if temperature is not None else self.config.models.temperature
        max_tok = max_tokens if max_tokens is not None else self.config.models.max_tokens

        # Build the models to try (specified model + fallback chain)
        models_to_try = [target_model]
        for fallback in self.config.models.fallback_chain:
            if fallback != target_model and fallback not in models_to_try:
                models_to_try.append(fallback)

        last_error: Exception | None = None

        for model_name in models_to_try:
            try:
                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temp,
                    "max_tokens": max_tok,
                    "stream": False,  # Non-streaming for complete()
                }

                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                # Get provider-specific base_url
                provider = model_name.split("/")[0] if "/" in model_name else ""
                provider_cfg = self.config.models.providers.get(provider)
                if provider_cfg and provider_cfg.base_url and provider != "ollama":
                    kwargs["api_base"] = provider_cfg.base_url

                logger.debug("model_request", model=model_name)
                response = await litellm.acompletion(**kwargs)

                return self._parse_response(response, model_name)

            except Exception as e:
                last_error = e
                logger.warning(
                    "model_fallback",
                    model=model_name,
                    error=str(e),
                )
                continue

        raise RuntimeError(
            f"All models failed. Last error: {last_error}"
        ) from last_error

    async def stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream a completion response, yielding text chunks.

        Note: Streaming does not support tool calls. If tools are provided
        and the model wants to call a tool, use complete() instead.
        """
        target_model = model or self.default_model
        temp = temperature if temperature is not None else self.config.models.temperature
        max_tok = max_tokens if max_tokens is not None else self.config.models.max_tokens

        kwargs: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
        }

        # Don't stream if tools are present (need full response for tool calls)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        provider = target_model.split("/")[0] if "/" in target_model else ""
        provider_cfg = self.config.models.providers.get(provider)
        if provider_cfg and provider_cfg.base_url and provider != "ollama":
            kwargs["api_base"] = provider_cfg.base_url

        response = await litellm.acompletion(**kwargs)

        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def _parse_response(self, response: Any, model_name: str) -> ModelResponse:
        """Parse LiteLLM response into ModelResponse."""
        choice = response.choices[0] if response.choices else None
        if not choice:
            return ModelResponse(model=model_name)

        message = choice.message
        tool_calls = None

        if hasattr(message, "tool_calls") and message.tool_calls:
            tool_calls = [
                ToolCall.from_litellm(tc.model_dump() if hasattr(tc, "model_dump") else tc)
                for tc in message.tool_calls
            ]

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }

        return ModelResponse(
            content=message.content,
            tool_calls=tool_calls,
            model=model_name,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", ""),
        )

    async def list_models_grouped(self) -> dict[str, list[str]]:
        """List available models grouped by provider.

        Returns a dict like {"gemini": ["gemini/gemini-2.5-flash", ...], "ollama": [...]}.
        Cloud providers show their known_models if an API key is configured.
        Local providers (Ollama, OpenAI-compatible) are dynamically queried.
        """
        groups: dict[str, list[str]] = {}

        for name, cfg in self.config.models.providers.items():
            provider_models: list[str] = []

            # --- Dynamic discovery for local providers ---
            if name == "ollama":
                try:
                    import httpx

                    base = cfg.base_url or "http://localhost:11434"
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"{base}/api/tags", timeout=5)
                        if resp.status_code == 200:
                            data = resp.json()
                            for m in data.get("models", []):
                                provider_models.append(f"ollama/{m['name']}")
                except Exception:
                    pass

            elif cfg.base_url:
                # OpenAI-compatible local server (e.g. llama.cpp, vLLM)
                try:
                    import httpx

                    base = cfg.base_url.rstrip("/")
                    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(url, timeout=5)
                        if resp.status_code == 200:
                            data = resp.json()
                            for m in data.get("data", []):
                                model_id = m.get("id", "")
                                if model_id:
                                    provider_models.append(f"openai/{model_id}")
                except Exception:
                    pass

            # --- Static known_models for cloud providers (requires API key) ---
            if cfg.known_models and (cfg.get_api_key() or cfg.base_url):
                for km in cfg.known_models:
                    if km not in provider_models:
                        provider_models.append(km)

            if provider_models:
                groups[name] = provider_models

        return groups

    async def list_models(self) -> list[str]:
        """List available models as a flat list (for CLI and backward compat)."""
        groups = await self.list_models_grouped()
        models: list[str] = [self.default_model]
        for provider_models in groups.values():
            for m in provider_models:
                if m not in models:
                    models.append(m)
        return models
