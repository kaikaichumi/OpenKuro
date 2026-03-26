"""Model router: unified LLM interface via LiteLLM.

Supports cloud providers (Anthropic, OpenAI, Google) and local models (Ollama, llama.cpp)
with automatic fallback chain.
"""

from __future__ import annotations

import base64
import contextvars
import json
import os
import re
from contextlib import contextmanager
from urllib.parse import urlparse
from typing import Any, AsyncIterator

import time as _time

import aiohttp
import litellm
import structlog

from src.config import KuroConfig
from src.core.security.secret_broker import SecretBroker
from src.core.types import ModelResponse, ToolCall
from src.openai_catalog import (
    OPENAI_OFFICIAL_MODELS,
    is_openai_compatible_local_base_url,
    normalize_codex_oauth_model_id,
    normalize_openai_model,
)

logger = structlog.get_logger()

_OPENAI_COMPATIBLE_PROVIDER_ALIASES: set[str] = {
    "qwen",
    "openai-compatible",
    "llama.cpp",
    "llamacpp",
    "vllm",
    "lmstudio",
    "local",
}

# Suppress LiteLLM's verbose logging
litellm.suppress_debug_info = True

_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_CODEX_ORIGINATOR_DEFAULT = "codex_cli_rs"
_CODEX_DEFAULT_INSTRUCTIONS = (
    "You are Codex, a software engineering assistant running in a local user workspace."
)


# ---------------------------------------------------------------------------
# Context overflow detection
# ---------------------------------------------------------------------------

# Patterns that indicate a context-window overflow from various providers
_CONTEXT_OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"exceeds?\s+(the\s+)?(available\s+)?context\s+size", re.I),
    re.compile(r"maximum\s+context\s+length", re.I),
    re.compile(r"context[_ ]?window", re.I),
    re.compile(r"token\s+limit\s+exceeded", re.I),
    re.compile(r"request\s+too\s+large", re.I),
    re.compile(r"input.*too\s+long", re.I),
    re.compile(r"exceeds?\s+.*\btoken", re.I),
    re.compile(r"reduce\s+(the\s+)?(length|size)\s+of\s+(the\s+)?messages?", re.I),
]

# Extract numeric token counts from error messages
_TOKEN_COUNT_RE = re.compile(
    r"(?:request|input|prompt)\s*\(?\s*(\d[\d,]*)\s*tokens?\s*\)?", re.I
)
_TOKEN_LIMIT_RE = re.compile(
    r"(?:context|limit|maximum|max|available)\s*(?:size|length|window)?\s*(?:of|is|:)?\s*\(?\s*(\d[\d,]*)\s*tokens?\s*\)?",
    re.I,
)


def _is_context_overflow(error_str: str) -> bool:
    """Return True if the error message indicates a context-window overflow."""
    return any(p.search(error_str) for p in _CONTEXT_OVERFLOW_PATTERNS)


def _parse_overflow_tokens(error_str: str) -> tuple[int | None, int | None]:
    """Try to extract (request_tokens, limit_tokens) from an overflow error."""
    req = _TOKEN_COUNT_RE.search(error_str)
    lim = _TOKEN_LIMIT_RE.search(error_str)
    req_val = int(req.group(1).replace(",", "")) if req else None
    lim_val = int(lim.group(1).replace(",", "")) if lim else None
    return req_val, lim_val


# ---------------------------------------------------------------------------
# Vision capability detection
# ---------------------------------------------------------------------------

# Known vision-capable model families
_VISION_CAPABLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"claude-(sonnet|opus|haiku)", re.I),
    re.compile(r"gpt-4o", re.I),
    re.compile(r"gpt-5", re.I),
    re.compile(r"gemini", re.I),
    re.compile(r"llava", re.I),
    re.compile(r"minicpm-v", re.I),
    re.compile(r"qwen3\.5", re.I),
    re.compile(r"qwen.*vl", re.I),
    re.compile(r"internvl", re.I),
    re.compile(r"cogvlm", re.I),
    re.compile(r"phi-3.*vision", re.I),
]

# Known text-only model families (higher priority than vision patterns)
_TEXT_ONLY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"glm-4(?!v)", re.I),
    re.compile(r"qwen3(?!\.5)(?!.*vl)", re.I),
    re.compile(r"deepseek", re.I),
    re.compile(r"mistral(?!.*pixtral)", re.I),
    re.compile(r"llama3\.\d", re.I),
    re.compile(r"codestral", re.I),
    re.compile(r"phi-3(?!.*vision)", re.I),
    re.compile(r"command-r", re.I),
    re.compile(r"mixtral", re.I),
]

# Patterns matching errors when a model does not support image input
_VISION_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"image\s+input\s+(is\s+)?not\s+supported", re.I),
    re.compile(r"does\s+not\s+support\s+(image|vision|multimodal)", re.I),
    re.compile(r"cannot\s+process\s+image", re.I),
    re.compile(r"invalid.*content.*type.*image", re.I),
    re.compile(r"provide\s+the\s+mmproj", re.I),
]


def _is_vision_error(error_str: str) -> bool:
    """Return True if the error indicates the model cannot handle images."""
    return any(p.search(error_str) for p in _VISION_ERROR_PATTERNS)


class VisionNotSupportedError(Exception):
    """Raised when a model cannot handle image/vision inputs.

    The engine should catch this, convert images to OCR text, and retry.
    """

    def __init__(self, message: str, model: str | None = None) -> None:
        super().__init__(message)
        self.model = model


class ContextOverflowError(Exception):
    """Raised when the request exceeds the model's context window.

    Callers should catch this, compress the context, and retry.
    """

    def __init__(
        self,
        message: str,
        token_count: int | None = None,
        limit: int | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.token_count = token_count
        self.limit = limit
        self.model = model


class ModelRouter:
    """Routes LLM requests through LiteLLM with fallback support."""

    def __init__(self, config: KuroConfig) -> None:
        self.config = config
        self._text_only_cache: set[str] = set()  # runtime cache from vision errors
        self._provider_api_keys_ctx: contextvars.ContextVar[dict[str, str] | None] = (
            contextvars.ContextVar("provider_api_keys_ctx", default=None)
        )
        self._provider_auth_ctx: contextvars.ContextVar[dict[str, dict[str, Any]] | None] = (
            contextvars.ContextVar("provider_auth_ctx", default=None)
        )
        self.secret_broker = SecretBroker(
            getattr(self.config, "secret_broker", None),
            self.config.models.providers,
        )
        self._setup_provider_keys()
        self._sync_gateway_proxy_env()

    @staticmethod
    def _local_no_proxy_defaults() -> list[str]:
        """Default NO_PROXY patterns to keep local/runtime traffic direct."""
        return [
            "localhost",
            "127.0.0.1",
            "::1",
            "*.local",
            "10.*",
            "192.168.*",
            "172.16.*",
            "172.17.*",
            "172.18.*",
            "172.19.*",
            "172.20.*",
            "172.21.*",
            "172.22.*",
            "172.23.*",
            "172.24.*",
            "172.25.*",
            "172.26.*",
            "172.27.*",
            "172.28.*",
            "172.29.*",
            "172.30.*",
            "172.31.*",
        ]

    def _sync_gateway_proxy_env(self) -> None:
        """Apply egress gateway proxy settings to process HTTP proxy env vars.

        This allows provider HTTP stacks (including LiteLLM/httpx/aiohttp) to
        route through the same gateway without invasive per-provider patches.
        """
        egress = getattr(self.config, "egress_policy", None)
        if egress is None:
            return

        gateway_enabled = bool(getattr(egress, "gateway_enabled", False))
        gateway_mode = str(getattr(egress, "gateway_mode", "enforce") or "enforce").strip().lower()
        proxy_url = str(getattr(egress, "gateway_proxy_url", "") or "").strip()

        # In shadow mode we intentionally do not apply proxy env routing.
        if not gateway_enabled or gateway_mode != "enforce" or not proxy_url:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                if os.environ.get(key):
                    os.environ.pop(key, None)
            return

        no_proxy_values: list[str] = []

        # Explicit gateway bypass domains from config.
        bypass = getattr(egress, "gateway_bypass_domains", []) or []
        for item in bypass:
            val = str(item or "").strip()
            if val:
                no_proxy_values.append(val)

        include_private = bool(getattr(egress, "gateway_include_private_network", False))
        if not include_private:
            no_proxy_values.extend(self._local_no_proxy_defaults())

        # Keep explicitly local provider base URLs direct to avoid routing loops.
        for provider_cfg in self.config.models.providers.values():
            base = str(getattr(provider_cfg, "base_url", "") or "").strip()
            if not base:
                continue
            try:
                host = (urlparse(base).hostname or "").strip()
            except Exception:
                host = ""
            if host:
                no_proxy_values.append(host)

        # Preserve any existing NO_PROXY values.
        existing = str(os.environ.get("NO_PROXY", "") or "").strip()
        if existing:
            no_proxy_values.extend([v.strip() for v in existing.split(",") if v.strip()])

        deduped: list[str] = []
        seen: set[str] = set()
        for raw in no_proxy_values:
            k = raw.lower()
            if not k or k in seen:
                continue
            seen.add(k)
            deduped.append(raw)

        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["ALL_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
        os.environ["all_proxy"] = proxy_url
        if deduped:
            joined = ",".join(deduped)
            os.environ["NO_PROXY"] = joined
            os.environ["no_proxy"] = joined

    def refresh_network_runtime(self) -> None:
        """Refresh network-related runtime settings after config changes."""
        self._sync_gateway_proxy_env()

    def refresh_secret_runtime(self) -> None:
        """Refresh provider secret broker runtime after config changes."""
        self.secret_broker = SecretBroker(
            getattr(self.config, "secret_broker", None),
            self.config.models.providers,
        )
        self._setup_provider_keys()

    def revoke_provider_secret(self, provider: str) -> dict[str, Any]:
        broker = getattr(self, "secret_broker", None)
        if broker is None or not bool(getattr(broker, "enabled", False)):
            return {"status": "error", "reason": "secret_broker_disabled"}
        return broker.revoke_provider(provider)

    def rotate_provider_secret(self, provider: str, new_secret: str | None) -> dict[str, Any]:
        broker = getattr(self, "secret_broker", None)
        if broker is None or not bool(getattr(broker, "enabled", False)):
            return {"status": "error", "reason": "secret_broker_disabled"}
        return broker.rotate_provider(provider, new_secret)

    def secret_broker_status(self) -> dict[str, Any]:
        broker = getattr(self, "secret_broker", None)
        if broker is None:
            return {"enabled": False, "reason": "not_initialized"}
        return broker.status()

    @contextmanager
    def provider_auth_override(
        self,
        provider: str,
        auth_context: dict[str, Any] | None,
    ):
        """Temporarily override provider auth context for current async context."""
        current = dict(self._provider_auth_ctx.get() or {})
        if auth_context:
            current[provider] = dict(auth_context)
        else:
            current.pop(provider, None)
        token = self._provider_auth_ctx.set(current or None)
        try:
            yield
        finally:
            self._provider_auth_ctx.reset(token)

    @contextmanager
    def provider_api_key_override(
        self,
        provider: str,
        api_key: str | None,
    ):
        """Temporarily override provider API key for the current async context."""
        current = dict(self._provider_api_keys_ctx.get() or {})
        if api_key:
            current[provider] = api_key
        else:
            current.pop(provider, None)
        token = self._provider_api_keys_ctx.set(current or None)
        auth_ctx = {"mode": "api_key", "api_key": api_key} if api_key else None
        try:
            with self.provider_auth_override(provider, auth_ctx):
                yield
        finally:
            self._provider_api_keys_ctx.reset(token)

    def _provider_api_key(self, provider: str) -> str | None:
        current = self._provider_api_keys_ctx.get() or {}
        key = current.get(provider)
        if key:
            return key
        auth = self._provider_auth(provider)
        if auth and auth.get("mode") == "api_key":
            raw = auth.get("api_key")
            if isinstance(raw, str):
                return raw.strip() or None
        broker = getattr(self, "secret_broker", None)
        if broker is not None and bool(getattr(broker, "enabled", False)):
            leased = broker.acquire_secret(
                provider,
                reason="model_request",
            )
            if leased:
                return leased
        return None

    def _provider_auth(self, provider: str) -> dict[str, Any] | None:
        current = self._provider_auth_ctx.get() or {}
        auth = current.get(provider)
        if isinstance(auth, dict):
            return auth
        return None

    def _setup_provider_keys(self) -> None:
        """Set up API keys from config into environment variables."""
        export_provider_env = True
        broker = getattr(self, "secret_broker", None)
        if broker is not None and bool(getattr(broker, "enabled", False)):
            export_provider_env = bool(getattr(broker, "export_provider_env", False))

        for provider_name, provider_cfg in self.config.models.providers.items():
            if provider_cfg.api_key_env and export_provider_env:
                key = provider_cfg.get_api_key()
                if key:
                    # LiteLLM reads keys from env vars
                    os.environ.setdefault(provider_cfg.api_key_env, key)

            if provider_cfg.base_url and provider_name == "ollama":
                os.environ.setdefault("OLLAMA_API_BASE", provider_cfg.base_url)

            # LiteLLM reads Gemini key from GEMINI_API_KEY
            if (
                provider_name == "gemini"
                and provider_cfg.api_key_env
                and export_provider_env
            ):
                key = provider_cfg.get_api_key()
                if key:
                    os.environ.setdefault("GEMINI_API_KEY", key)

    @staticmethod
    def _is_local_base_url(url: str | None) -> bool:
        """Return True if api base URL points to localhost/private loopback."""
        if not url:
            return False
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

    @staticmethod
    def _normalize_local_probe_base_url(url: str) -> str:
        """Normalize localhost probe URL for faster connection failure on Windows."""
        raw = (url or "").strip()
        if not raw:
            return raw
        # localhost can incur extra IPv6/IPv4 fallback latency on some systems.
        return re.sub(
            r"^(https?://)localhost(?=[:/]|$)",
            r"\g<1>127.0.0.1",
            raw,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _split_model_name(model_name: str) -> tuple[str, str]:
        """Split `provider/model_id` into `(provider, model_id)`."""
        value = (model_name or "").strip()
        if "/" not in value:
            return "", value
        provider, model_id = value.split("/", 1)
        return provider, model_id

    @staticmethod
    def _with_provider_prefix(provider: str, model_id: str) -> str:
        """Normalize a model id to the target provider prefix."""
        target = (provider or "").strip()
        raw = (model_id or "").strip()
        if not target or not raw:
            return ""

        if raw.startswith(f"{target}/"):
            return raw

        base = raw
        if "/" in raw:
            first, rest = raw.split("/", 1)
            if first == target:
                return raw
            if first in {
                "openai",
                "ollama",
                "llama",
                "anthropic",
                "gemini",
                "qwen",
                "openai-compatible",
                "llama.cpp",
                "llamacpp",
                "vllm",
                "lmstudio",
                "local",
            }:
                base = rest

        if target == "openai":
            return normalize_openai_model(base)
        return f"{target}/{base}"

    @staticmethod
    def _is_openai_compatible_provider_alias(provider: str) -> bool:
        return (provider or "").strip().lower() in _OPENAI_COMPATIBLE_PROVIDER_ALIASES

    def _resolve_provider_config(self, provider: str) -> tuple[str, Any | None]:
        """Resolve provider config, including aliases for local OpenAI-compatible runtimes."""
        providers = self.config.models.providers
        direct = providers.get(provider)
        if direct is not None:
            return provider, direct

        if not self._is_openai_compatible_provider_alias(provider):
            return provider, None

        # Backward compatibility: if alias provider (e.g. qwen/...) has no
        # dedicated config entry, reuse local openai-compatible settings.
        for fallback_name in ("llama", "openai"):
            fallback_cfg = providers.get(fallback_name)
            if fallback_cfg is None:
                continue
            base_url = str(getattr(fallback_cfg, "base_url", "") or "").strip()
            if not base_url:
                continue
            if fallback_name == "openai" and not is_openai_compatible_local_base_url(
                base_url
            ):
                continue
            return fallback_name, fallback_cfg

        return provider, None

    def _to_litellm_model(self, model_name: str) -> str:
        """Translate user-facing provider aliases to LiteLLM-compatible model ids."""
        provider, model_id = self._split_model_name(model_name)
        if provider == "llama" or self._is_openai_compatible_provider_alias(provider):
            normalized = self._with_provider_prefix("openai", model_id)
            return normalized or normalize_openai_model(model_id)
        return model_name

    def _resolve_response_model(
        self,
        requested_model: str,
        response_model: Any,
    ) -> str:
        """Resolve provider-returned model id to normalized `provider/model` form."""
        requested = str(requested_model or "").strip()
        raw = str(response_model or "").strip()
        if not raw:
            return requested

        provider, _ = self._split_model_name(requested)
        if provider:
            normalized = self._with_provider_prefix(provider, raw)
            if normalized:
                return normalized

        return raw

    def _should_disable_tools_for_model(
        self,
        model_name: str,
        provider_cfg: Any | None,
    ) -> bool:
        """Heuristic guard for strict local templates that break with tool schema."""
        provider, model_id = self._split_model_name(model_name)
        model_lower = model_id.lower()
        # Important: detect LLaMA family from the model id only.
        # Provider names like "ollama"/"llama" should not auto-disable tools
        # for non-LLaMA models such as Qwen or DeepSeek.
        if "llama" not in model_lower:
            return False
        if provider in {"ollama", "llama"}:
            return True
        if provider == "openai" and provider_cfg and self._is_local_base_url(
            provider_cfg.base_url
        ):
            return True
        return False

    @staticmethod
    def _sanitize_strict_function_turns(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Sanitize tool-call history for strict providers (e.g., Gemini).

        Gemini requires each assistant function-call turn to be followed
        immediately by function-response/tool turns. If context compression
        or truncation leaves orphan entries, requests fail with 400.
        """
        sanitized: list[dict[str, Any]] = []
        dropped_assistant_calls = 0
        dropped_tool_turns = 0
        i = 0

        while i < len(messages):
            msg = messages[i]
            if not isinstance(msg, dict):
                i += 1
                continue

            role = str(msg.get("role", "")).strip().lower()
            tool_calls = msg.get("tool_calls")
            has_tool_calls = (
                role == "assistant"
                and isinstance(tool_calls, list)
                and bool(tool_calls)
            )

            if has_tool_calls:
                expected_ids: list[str] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    raw_id = tc.get("id")
                    tc_id = str(raw_id).strip() if raw_id is not None else ""
                    if tc_id:
                        expected_ids.append(tc_id)

                j = i + 1
                tool_msgs: list[dict[str, Any]] = []
                while j < len(messages):
                    nxt = messages[j]
                    if not isinstance(nxt, dict):
                        break
                    nxt_role = str(nxt.get("role", "")).strip().lower()
                    if nxt_role != "tool":
                        break
                    tool_msgs.append(nxt)
                    j += 1

                valid_pair = bool(tool_msgs)
                if valid_pair and expected_ids:
                    seen_ids = {
                        str(tm.get("tool_call_id", "")).strip()
                        for tm in tool_msgs
                        if isinstance(tm, dict)
                    }
                    valid_pair = all(tc_id in seen_ids for tc_id in expected_ids)

                if valid_pair:
                    sanitized.append(msg)
                    sanitized.extend(tool_msgs)
                    i = j
                    continue

                # Degrade broken assistant tool-call turn into plain assistant text
                # when content exists; otherwise drop it.
                content = msg.get("content")
                keep_content = (
                    isinstance(content, str) and bool(content.strip())
                ) or (
                    isinstance(content, list) and bool(content)
                )
                if keep_content:
                    repaired = dict(msg)
                    repaired.pop("tool_calls", None)
                    sanitized.append(repaired)

                dropped_assistant_calls += 1
                dropped_tool_turns += len(tool_msgs)
                i = j if tool_msgs else i + 1
                continue

            if role == "tool":
                # Orphan tool response without a preceding function-call turn.
                dropped_tool_turns += 1
                i += 1
                continue

            sanitized.append(msg)
            i += 1

        return sanitized, dropped_assistant_calls, dropped_tool_turns

    @property
    def default_model(self) -> str:
        return self.config.models.default

    def supports_vision(self, model: str | None = None) -> bool:
        """Check whether a model supports image/vision inputs.

        Uses (in priority order):
        1. Runtime cache (models that failed with vision errors)
        2. Explicit config overrides (vision.vision_models / text_only_models)
        3. Static pattern matching on known model families
        4. Default: False (safe — assumes text-only)
        """
        target = model or self.default_model

        # 1. Runtime cache: we already know this model is text-only
        if target in self._text_only_cache:
            return False

        # 2. Config overrides
        vision_cfg = getattr(self.config, "vision", None)
        if vision_cfg:
            if target in vision_cfg.vision_models:
                return True
            if target in vision_cfg.text_only_models:
                return False

        # 3. Pattern matching (text-only has higher priority)
        for pattern in _TEXT_ONLY_PATTERNS:
            if pattern.search(target):
                return False
        for pattern in _VISION_CAPABLE_PATTERNS:
            if pattern.search(target):
                return True

        # 4. Default: assume text-only (safe fallback)
        return False

    @staticmethod
    def _extract_chatgpt_account_id_from_token(token: str | None) -> str | None:
        if not token:
            return None
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
            claims = json.loads(decoded)
            if not isinstance(claims, dict):
                return None
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                raw = auth_claim.get("chatgpt_account_id")
                if isinstance(raw, str):
                    return raw.strip() or None
            return None
        except Exception:
            return None

    @staticmethod
    def _is_codex_oauth_mode(provider: str, auth: dict[str, Any] | None) -> bool:
        if provider != "openai" or not isinstance(auth, dict):
            return False
        if str(auth.get("mode", "")).strip().lower() != "codex_oauth":
            return False
        token = auth.get("access_token")
        return isinstance(token, str) and bool(token.strip())

    def _build_codex_headers(self, auth: dict[str, Any]) -> dict[str, str]:
        access_token = str(auth.get("access_token", "")).strip()
        if not access_token:
            raise RuntimeError("Missing OpenAI OAuth access token")
        account_id = str(auth.get("account_id", "")).strip() or (
            self._extract_chatgpt_account_id_from_token(access_token) or ""
        )
        originator = str(auth.get("originator", "")).strip() or _CODEX_ORIGINATOR_DEFAULT
        headers = {
            "Authorization": f"Bearer {access_token}",
            "OpenAI-Beta": "responses=experimental",
            "originator": originator,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return headers

    @staticmethod
    def _stringify_message_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            return "\n".join(parts)
        if content is None:
            return ""
        return str(content)

    @staticmethod
    def _content_to_codex_parts(content: Any, *, role: str) -> list[dict[str, Any]]:
        text_type = "output_text" if role == "assistant" else "input_text"
        if isinstance(content, str):
            return [{"type": text_type, "text": content}]
        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                p_type = str(part.get("type", "")).strip().lower()
                if p_type in {"text", "input_text", "output_text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        parts.append({"type": text_type, "text": text})
                    continue
                if p_type in {"image_url", "input_image"}:
                    if role == "assistant":
                        # Assistant history in Responses API only supports output_text/refusal.
                        continue
                    raw_url = part.get("image_url")
                    if isinstance(raw_url, dict):
                        raw_url = raw_url.get("url")
                    if isinstance(raw_url, str) and raw_url:
                        parts.append({"type": "input_image", "image_url": raw_url})
            if parts:
                return parts
        if content is None:
            return []
        return [{"type": text_type, "text": str(content)}]

    def _messages_to_codex_input(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for msg in messages:
            role_raw = str(msg.get("role", "user")).strip().lower()
            role = role_raw
            if role == "system":
                role = "developer"
            if role not in {"developer", "assistant", "user"}:
                role = "user"

            if role_raw == "tool":
                tool_name = str(msg.get("name", "")).strip() or "tool"
                tool_output = self._stringify_message_content(msg.get("content"))
                text = f"[Tool {tool_name} output]"
                if tool_output:
                    text = f"{text}\n{tool_output}"
                parts = [{"type": "input_text", "text": text}]
            else:
                parts = self._content_to_codex_parts(msg.get("content"), role=role)

            if not parts:
                continue
            converted.append({
                "type": "message",
                "role": role,
                "content": parts,
            })
        return converted

    @staticmethod
    def _decode_sse_data_line(line: str) -> dict[str, Any] | None:
        trimmed = line.strip()
        if not trimmed.startswith("data:"):
            return None
        payload = trimmed[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except Exception:
            return None
        return event if isinstance(event, dict) else None

    @staticmethod
    def _extract_sse_delta(event: dict[str, Any]) -> str | None:
        event_type = str(event.get("type", "")).strip().lower()
        # `.done` often carries the finalized full text snapshot, which can
        # duplicate content already streamed via `.delta` chunks.
        if not event_type.endswith(".delta"):
            return None
        for key in ("delta", "text", "output_text"):
            raw = event.get(key)
            if isinstance(raw, str) and raw:
                return raw
        item = event.get("item")
        if isinstance(item, dict):
            raw = item.get("text")
            if isinstance(raw, str) and raw:
                return raw
        return None

    @staticmethod
    def _extract_text_from_response_obj(response_obj: dict[str, Any] | None) -> str:
        if not isinstance(response_obj, dict):
            return ""
        direct = response_obj.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct

        chunks: list[str] = []
        output = response_obj.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"output_text", "text"}:
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in {"output_text", "text"}:
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                chunks.append(text)
        return "".join(chunks).strip()

    @staticmethod
    def _extract_usage_from_response_obj(response_obj: dict[str, Any] | None) -> dict[str, int]:
        if not isinstance(response_obj, dict):
            return {}
        usage = response_obj.get("usage")
        if not isinstance(usage, dict):
            return {}

        def _as_int(v: Any) -> int:
            try:
                return int(v)
            except Exception:
                return 0

        prompt_tokens = _as_int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
        completion_tokens = _as_int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
        total_tokens = _as_int(usage.get("total_tokens", 0))
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            payload = raw.strip()
            if not payload:
                return {}
            try:
                parsed = json.loads(payload)
            except Exception:
                return {"_raw": raw}
            if isinstance(parsed, dict):
                return parsed
            return {"_value": parsed}
        return {}

    @staticmethod
    def _dedupe_tool_calls(tool_calls: list[ToolCall]) -> list[ToolCall]:
        deduped: list[ToolCall] = []
        seen: set[tuple[str, str, str]] = set()
        for tc in tool_calls:
            try:
                args_key = json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True)
            except Exception:
                args_key = str(tc.arguments)
            key = (str(tc.id or "").strip(), str(tc.name or "").strip(), args_key)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(tc)
        return deduped

    def _extract_tool_calls_from_output_items(
        self,
        output_items: list[Any] | None,
    ) -> list[ToolCall]:
        calls: list[ToolCall] = []
        if not isinstance(output_items, list):
            return calls
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).strip().lower()
            if item_type not in {"function_call", "tool_call"}:
                continue
            name = str(item.get("name") or item.get("tool_name") or "").strip()
            if not name:
                continue
            raw_id = item.get("call_id") or item.get("id")
            call_id = str(raw_id).strip() if raw_id is not None else ""
            if not call_id:
                call_id = f"call_{len(calls) + 1}_{name}"
            arguments = self._parse_tool_arguments(item.get("arguments"))
            calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        return calls

    def _extract_tool_calls_from_response_obj(
        self,
        response_obj: dict[str, Any] | None,
    ) -> list[ToolCall]:
        if not isinstance(response_obj, dict):
            return []

        calls: list[ToolCall] = []

        calls.extend(
            self._extract_tool_calls_from_output_items(response_obj.get("output"))
        )

        required_action = response_obj.get("required_action")
        if isinstance(required_action, dict):
            submit = required_action.get("submit_tool_outputs")
            if isinstance(submit, dict):
                raw_calls = submit.get("tool_calls")
                if isinstance(raw_calls, list):
                    for raw_tc in raw_calls:
                        if not isinstance(raw_tc, dict):
                            continue
                        fn = raw_tc.get("function")
                        if isinstance(fn, dict):
                            name = str(fn.get("name", "")).strip()
                            arguments = self._parse_tool_arguments(fn.get("arguments"))
                        else:
                            name = str(raw_tc.get("name", "")).strip()
                            arguments = self._parse_tool_arguments(
                                raw_tc.get("arguments")
                            )
                        if not name:
                            continue
                        raw_id = raw_tc.get("id") or raw_tc.get("call_id")
                        call_id = str(raw_id).strip() if raw_id is not None else ""
                        if not call_id:
                            call_id = f"call_{len(calls) + 1}_{name}"
                        calls.append(
                            ToolCall(id=call_id, name=name, arguments=arguments)
                        )

        return self._dedupe_tool_calls(calls)

    @staticmethod
    def _normalize_codex_tools(
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if not tools:
            return []
        normalized: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = str(tool.get("type", "")).strip().lower()
            if tool_type and tool_type != "function":
                continue
            function = tool.get("function")
            if isinstance(function, dict):
                name = str(function.get("name", "")).strip()
                description = str(function.get("description", "")).strip()
                parameters = function.get("parameters")
            else:
                name = str(tool.get("name", "")).strip()
                description = str(tool.get("description", "")).strip()
                parameters = tool.get("parameters")
            if not name:
                continue
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            else:
                # Copy before normalization so we don't mutate original tool defs.
                parameters = dict(parameters)

            schema_type = str(parameters.get("type", "")).strip().lower()
            if not schema_type:
                schema_type = "object"
                parameters["type"] = "object"

            # Codex OAuth tool schema validation is stricter and rejects object
            # schemas without explicit properties. Normalize no-arg tools to a
            # valid optional placeholder field.
            if schema_type == "object":
                props = parameters.get("properties")
                if not isinstance(props, dict):
                    props = {}
                if not props:
                    props = {
                        "_noop": {
                            "type": "string",
                            "description": "No parameters required. Leave empty.",
                        }
                    }
                parameters["properties"] = props
                required = parameters.get("required")
                if not isinstance(required, list):
                    parameters["required"] = []
            codex_tool: dict[str, Any] = {
                "type": "function",
                "name": name,
                "parameters": parameters,
            }
            if description:
                codex_tool["description"] = description
            normalized.append(codex_tool)
        return normalized

    def _build_codex_payload(
        self,
        *,
        model_name: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        model_id_raw = model_name.split("/")[-1] if "/" in model_name else model_name
        model_id = self._normalize_codex_model_id(model_id_raw)
        instructions = (
            os.environ.get("OPENAI_CODEX_INSTRUCTIONS", "").strip()
            or _CODEX_DEFAULT_INSTRUCTIONS
        )
        payload: dict[str, Any] = {
            "model": model_id,
            "input": self._messages_to_codex_input(messages),
            "instructions": instructions,
            "store": False,
            "stream": True,
            "text": {"verbosity": "medium"},
            "reasoning": {"summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
        codex_tools = self._normalize_codex_tools(tools)
        if codex_tools:
            payload["tools"] = codex_tools
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _normalize_codex_model_id(model_id: str) -> str:
        return normalize_codex_oauth_model_id(model_id)

    async def _complete_codex_oauth(
        self,
        *,
        model_name: str,
        messages: list[dict[str, Any]],
        auth: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        payload = self._build_codex_payload(
            model_name=model_name,
            messages=messages,
            tools=tools,
        )
        headers = self._build_codex_headers(auth)
        timeout = aiohttp.ClientTimeout(total=240, sock_read=240)

        deltas: list[str] = []
        event_tool_calls: list[ToolCall] = []
        final_response: dict[str, Any] | None = None
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(_CODEX_RESPONSES_URL, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Codex OAuth request failed ({resp.status}): {body[:320]}"
                    )

                buffer = ""
                async for raw in resp.content.iter_any():
                    if not raw:
                        continue
                    buffer += raw.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        event = self._decode_sse_data_line(line)
                        if not event:
                            continue
                        event_type = str(event.get("type", "")).strip().lower()
                        if event_type in {"response.completed", "response.done"}:
                            response_obj = event.get("response")
                            if isinstance(response_obj, dict):
                                final_response = response_obj
                            continue
                        item = event.get("item")
                        if isinstance(item, dict):
                            event_tool_calls.extend(
                                self._extract_tool_calls_from_output_items([item])
                            )
                        delta = self._extract_sse_delta(event)
                        if delta:
                            deltas.append(delta)

                if buffer:
                    event = self._decode_sse_data_line(buffer)
                    if event:
                        response_obj = event.get("response")
                        if isinstance(response_obj, dict):
                            final_response = response_obj
                        item = event.get("item")
                        if isinstance(item, dict):
                            event_tool_calls.extend(
                                self._extract_tool_calls_from_output_items([item])
                            )
                        delta = self._extract_sse_delta(event)
                        if delta:
                            deltas.append(delta)

        content = "".join(deltas).strip()
        if not content:
            content = self._extract_text_from_response_obj(final_response)
        tool_calls = self._extract_tool_calls_from_response_obj(final_response)
        if not tool_calls and event_tool_calls:
            tool_calls = self._dedupe_tool_calls(event_tool_calls)
        usage = self._extract_usage_from_response_obj(final_response)
        response_model = None
        if isinstance(final_response, dict):
            response_model = final_response.get("model")
        resolved_model = self._resolve_response_model(model_name, response_model)
        return ModelResponse(
            content=content,
            tool_calls=tool_calls or None,
            model=resolved_model or model_name,
            usage=usage,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def _stream_codex_oauth(
        self,
        *,
        model_name: str,
        messages: list[dict[str, Any]],
        auth: dict[str, Any],
    ) -> AsyncIterator[str]:
        payload = self._build_codex_payload(model_name=model_name, messages=messages)
        headers = self._build_codex_headers(auth)
        timeout = aiohttp.ClientTimeout(total=240, sock_read=240)

        had_delta = False
        final_response: dict[str, Any] | None = None
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(_CODEX_RESPONSES_URL, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Codex OAuth stream failed ({resp.status}): {body[:320]}"
                    )

                buffer = ""
                async for raw in resp.content.iter_any():
                    if not raw:
                        continue
                    buffer += raw.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        event = self._decode_sse_data_line(line)
                        if not event:
                            continue
                        event_type = str(event.get("type", "")).strip().lower()
                        if event_type in {"response.completed", "response.done"}:
                            response_obj = event.get("response")
                            if isinstance(response_obj, dict):
                                final_response = response_obj
                            continue
                        delta = self._extract_sse_delta(event)
                        if delta:
                            had_delta = True
                            yield delta

                if buffer:
                    event = self._decode_sse_data_line(buffer)
                    if event:
                        response_obj = event.get("response")
                        if isinstance(response_obj, dict):
                            final_response = response_obj
                        delta = self._extract_sse_delta(event)
                        if delta:
                            had_delta = True
                            yield delta

        if not had_delta:
            fallback_text = self._extract_text_from_response_obj(final_response)
            if fallback_text:
                yield fallback_text

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
        target_provider = target_model.split("/")[0] if "/" in target_model else ""
        target_auth = self._provider_auth(target_provider)
        target_is_codex_oauth = self._is_codex_oauth_mode(target_provider, target_auth)
        if not target_is_codex_oauth:
            for fallback in self.config.models.fallback_chain:
                if fallback != target_model and fallback not in models_to_try:
                    models_to_try.append(fallback)

        last_error: Exception | None = None

        for model_name in models_to_try:
            provider = model_name.split("/")[0] if "/" in model_name else ""
            provider_cfg_name, provider_cfg = self._resolve_provider_config(provider)
            provider_auth = self._provider_auth(provider)
            if provider_cfg_name != provider and provider_cfg is not None:
                logger.debug(
                    "provider_alias_resolved",
                    provider=provider,
                    resolved_provider=provider_cfg_name,
                    model=model_name,
                )
            try:
                if self._is_codex_oauth_mode(provider, provider_auth):
                    logger.info(
                        "model_request",
                        requested_model=target_model,
                        attempt_model=model_name,
                        codex_model=self._normalize_codex_model_id(
                            model_name.split("/", 1)[1] if "/" in model_name else model_name
                        ),
                        provider=provider,
                        mode="codex_oauth",
                    )
                    _t0 = _time.monotonic()
                    parsed = await self._complete_codex_oauth(
                        model_name=model_name,
                        messages=messages,
                        auth=provider_auth or {},
                        tools=tools,
                    )
                    _latency = (_time.monotonic() - _t0) * 1000

                    actual_model = str(parsed.model or model_name)
                    logger.info(
                        "model_response",
                        requested_model=target_model,
                        attempt_model=model_name,
                        actual_model=actual_model,
                        provider=provider,
                        mode="codex_oauth",
                    )
                    if actual_model != model_name:
                        logger.warning(
                            "model_alias_mismatch",
                            requested_model=target_model,
                            attempt_model=model_name,
                            actual_model=actual_model,
                            provider=provider,
                        )

                    try:
                        from src.core.tracing import trace_llm_call
                        trace_llm_call(
                            model=model_name,
                            messages=messages,
                            response=parsed,
                            usage=parsed.usage,
                            latency_ms=_latency,
                            tags=getattr(self.config, "tracing", None)
                            and self.config.tracing.tags
                            or [],
                        )
                    except Exception:
                        pass

                    return parsed

                kwargs: dict[str, Any] = {
                    "model": self._to_litellm_model(model_name),
                    "temperature": temp,
                    "max_tokens": max_tok,
                    "stream": False,  # Non-streaming for complete()
                }
                prepared_messages = messages
                if provider == "gemini":
                    (
                        prepared_messages,
                        dropped_calls,
                        dropped_tools,
                    ) = self._sanitize_strict_function_turns(messages)
                    if dropped_calls or dropped_tools:
                        logger.warning(
                            "gemini_function_turn_sanitized",
                            dropped_assistant_calls=dropped_calls,
                            dropped_tool_turns=dropped_tools,
                            before=len(messages),
                            after=len(prepared_messages),
                        )
                kwargs["messages"] = prepared_messages

                # Get provider-specific base_url
                if provider_cfg and provider_cfg.base_url and provider != "ollama":
                    kwargs["api_base"] = provider_cfg.base_url
                override_key = self._provider_api_key(provider)
                if not override_key and provider_cfg_name != provider:
                    override_key = self._provider_api_key(provider_cfg_name)
                if override_key:
                    kwargs["api_key"] = override_key

                if tools:
                    if self._should_disable_tools_for_model(model_name, provider_cfg):
                        logger.warning(
                            "tools_disabled_for_strict_template",
                            model=model_name,
                            reason="local_llama_template",
                        )
                    else:
                        kwargs["tools"] = tools
                        kwargs["tool_choice"] = "auto"

                logger.info(
                    "model_request",
                    requested_model=target_model,
                    attempt_model=model_name,
                    provider=provider,
                    mode="api",
                )
                _t0 = _time.monotonic()
                response = await litellm.acompletion(**kwargs)
                _latency = (_time.monotonic() - _t0) * 1000

                parsed = self._parse_response(response, model_name)

                actual_model = str(parsed.model or model_name)
                logger.info(
                    "model_response",
                    requested_model=target_model,
                    attempt_model=model_name,
                    actual_model=actual_model,
                    provider=provider,
                    mode="api",
                )
                if actual_model != model_name:
                    logger.warning(
                        "model_alias_mismatch",
                        requested_model=target_model,
                        attempt_model=model_name,
                        actual_model=actual_model,
                        provider=provider,
                    )

                # Trace LLM call to LangSmith (non-blocking, never raises)
                try:
                    from src.core.tracing import trace_llm_call
                    trace_llm_call(
                        model=model_name,
                        messages=messages,
                        response=parsed,
                        usage=parsed.usage,
                        latency_ms=_latency,
                        tags=getattr(self.config, "tracing", None)
                        and self.config.tracing.tags
                        or [],
                    )
                except Exception:
                    pass

                return parsed

            except Exception as e:
                if self._is_codex_oauth_mode(provider, provider_auth):
                    raise RuntimeError(
                        f"Codex OAuth request failed for model '{model_name}': {e}"
                    ) from e
                error_str = str(e)
                if "Assistant response prefill is incompatible with enable_thinking" in error_str:
                    try:
                        retry_kwargs = dict(kwargs)
                        extra_body = dict(retry_kwargs.get("extra_body", {}))
                        extra_body["enable_thinking"] = False
                        retry_kwargs["extra_body"] = extra_body
                        logger.warning("retry_disable_thinking", model=model_name)
                        response = await litellm.acompletion(**retry_kwargs)
                        parsed = self._parse_response(response, model_name)
                        actual_model = str(parsed.model or model_name)
                        logger.info(
                            "model_response",
                            requested_model=target_model,
                            attempt_model=model_name,
                            actual_model=actual_model,
                            provider=provider,
                            mode="api",
                        )
                        if actual_model != model_name:
                            logger.warning(
                                "model_alias_mismatch",
                                requested_model=target_model,
                                attempt_model=model_name,
                                actual_model=actual_model,
                                provider=provider,
                            )
                        return parsed
                    except Exception as retry_err:
                        e = retry_err
                        error_str = str(retry_err)

                # Context overflow: don't try fallback models — the caller
                # should compress and retry with the *same* model.
                if _is_context_overflow(error_str):
                    req_tokens, lim_tokens = _parse_overflow_tokens(error_str)
                    logger.warning(
                        "context_overflow",
                        model=model_name,
                        request_tokens=req_tokens,
                        limit_tokens=lim_tokens,
                    )
                    raise ContextOverflowError(
                        error_str,
                        token_count=req_tokens,
                        limit=lim_tokens,
                        model=model_name,
                    ) from e

                # Vision error: no point trying other models in the chain
                # — they will likely fail the same way.
                if _is_vision_error(error_str):
                    # Missing mmproj is often a runtime setup issue (fixable
                    # without changing model identity), so avoid hard-caching
                    # text-only mode in that specific case.
                    if "mmproj" not in error_str.lower():
                        self._text_only_cache.add(model_name)
                    logger.warning(
                        "vision_not_supported",
                        model=model_name,
                        error=error_str,
                    )
                    raise VisionNotSupportedError(
                        error_str, model=model_name
                    ) from e

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
            "model": self._to_litellm_model(target_model),
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
        }

        provider = target_model.split("/")[0] if "/" in target_model else ""
        provider_cfg_name, provider_cfg = self._resolve_provider_config(provider)
        provider_auth = self._provider_auth(provider)
        if self._is_codex_oauth_mode(provider, provider_auth):
            if tools:
                logger.debug(
                    "codex_oauth_tools_ignored_stream",
                    model=target_model,
                )
            logger.info(
                "model_stream_request",
                requested_model=target_model,
                attempt_model=target_model,
                codex_model=self._normalize_codex_model_id(
                    target_model.split("/", 1)[1] if "/" in target_model else target_model
                ),
                provider=provider,
                mode="codex_oauth",
            )
            async for delta in self._stream_codex_oauth(
                model_name=target_model,
                messages=messages,
                auth=provider_auth or {},
            ):
                yield delta
            return

        # Don't stream if tools are present (need full response for tool calls)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if provider_cfg and provider_cfg.base_url and provider != "ollama":
            kwargs["api_base"] = provider_cfg.base_url
        override_key = self._provider_api_key(provider)
        if not override_key and provider_cfg_name != provider:
            override_key = self._provider_api_key(provider_cfg_name)
        if override_key:
            kwargs["api_key"] = override_key

        if tools and self._should_disable_tools_for_model(target_model, provider_cfg):
            kwargs.pop("tools", None)
            kwargs.pop("tool_choice", None)
            logger.warning(
                "tools_disabled_for_strict_template_stream",
                model=target_model,
                reason="local_llama_template",
            )

        logger.info(
            "model_stream_request",
            requested_model=target_model,
            attempt_model=target_model,
            provider=provider,
            mode="api",
        )
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

        response_model = getattr(response, "model", None)
        resolved_model = self._resolve_response_model(model_name, response_model)

        return ModelResponse(
            content=message.content,
            tool_calls=tool_calls,
            model=resolved_model or model_name,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", ""),
        )

    async def list_models_grouped(self) -> dict[str, list[str]]:
        """List available models grouped by provider.

        Returns a dict like {"gemini": ["gemini/gemini-2.5-flash", ...], "ollama": [...]}.
        Cloud providers show known models only when credentials are available.
        Local providers (Ollama, OpenAI-compatible) are dynamically queried and
        can still show local known models without cloud API keys.
        """
        groups: dict[str, list[str]] = {}

        for name, cfg in self.config.models.providers.items():
            provider_models: list[str] = []
            openai_compatible_models: list[str] = []
            has_api_key = bool((cfg.get_api_key() or "").strip())
            has_codex_oauth = self._is_codex_oauth_mode(
                name, self._provider_auth(name)
            )
            is_local_provider = (
                name == "ollama"
                or name == "llama"
                or (name == "openai" and is_openai_compatible_local_base_url(cfg.base_url))
            )

            # --- Dynamic discovery for local providers ---
            if name == "ollama":
                try:
                    import httpx

                    base = (cfg.base_url or "http://localhost:11434").rstrip("/")
                    probe_base = self._normalize_local_probe_base_url(base)
                    timeout: httpx.Timeout | float
                    if self._is_local_base_url(base):
                        timeout = httpx.Timeout(connect=0.6, read=1.0, write=1.0, pool=0.6)
                    else:
                        timeout = 5
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.get(f"{probe_base}/api/tags")
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
                    probe_base = self._normalize_local_probe_base_url(base)
                    probe_url = (
                        f"{probe_base}/models"
                        if probe_base.endswith("/v1")
                        else f"{probe_base}/v1/models"
                    )
                    timeout: httpx.Timeout | float
                    if self._is_local_base_url(base):
                        timeout = httpx.Timeout(connect=0.6, read=1.0, write=1.0, pool=0.6)
                    else:
                        timeout = 5
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.get(probe_url)
                        if resp.status_code == 200:
                            data = resp.json()
                            for m in data.get("data", []):
                                model_id = m.get("id", "")
                                if model_id:
                                    if name == "openai" and is_openai_compatible_local_base_url(
                                        cfg.base_url
                                    ):
                                        discovered = normalize_openai_model(model_id)
                                        if discovered not in openai_compatible_models:
                                            openai_compatible_models.append(discovered)
                                    else:
                                        discovered = self._with_provider_prefix(name, model_id)
                                        if discovered and discovered not in provider_models:
                                            provider_models.append(discovered)
                except Exception:
                    pass

            # --- Static known_models from config ---
            # Cloud providers: only when API/auth is available.
            # Local providers: always allowed.
            if name == "openai":
                if has_api_key or has_codex_oauth:
                    for km in OPENAI_OFFICIAL_MODELS:
                        candidate = normalize_openai_model(km)
                        if candidate not in provider_models:
                            provider_models.append(candidate)
                for km in cfg.known_models:
                    candidate = normalize_openai_model(km)
                    if candidate.endswith(".gguf"):
                        if candidate not in openai_compatible_models:
                            openai_compatible_models.append(candidate)
                        continue
                    if not (has_api_key or has_codex_oauth):
                        continue
                    if candidate not in provider_models:
                        provider_models.append(candidate)
            elif cfg.known_models:
                if not (is_local_provider or has_api_key):
                    continue
                for km in cfg.known_models:
                    candidate = self._with_provider_prefix(name, km)
                    if candidate and candidate not in provider_models:
                        provider_models.append(candidate)

            if provider_models:
                groups[name] = provider_models
            if name == "openai" and openai_compatible_models:
                groups["openai-compatible"] = openai_compatible_models

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
