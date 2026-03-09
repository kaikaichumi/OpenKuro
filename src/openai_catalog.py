"""OpenAI model catalog helpers shared across adapters/UI/router."""

from __future__ import annotations

from urllib.parse import urlparse

# Official cloud model catalog shown in UI/Discord regardless of user config.
# This list can be extended at runtime with OPENAI_CODEX_OAUTH_MODELS for OAuth.
OPENAI_OFFICIAL_MODELS: list[str] = [
    "openai/gpt-5.4",
    "openai/gpt-5.4-pro",
    "openai/gpt-5.3-codex",
    "openai/gpt-5.3-chat-latest",
    "openai/gpt-5.2-pro",
    "openai/gpt-5.2-codex",
    "openai/gpt-5.2-chat-latest",
    "openai/gpt-5.2",
    "openai/gpt-5-pro",
    "openai/gpt-5.1-codex-max",
    "openai/gpt-5.1-codex",
    "openai/gpt-5.1-codex-mini",
    "openai/gpt-5.1-chat-latest",
    "openai/gpt-5.1",
    "openai/gpt-5-chat-latest",
    "openai/gpt-5-codex",
    "openai/gpt-5",
    "openai/codex-mini-latest",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]

# Models that are known to work with ChatGPT-account Codex OAuth backend.
OPENAI_CODEX_OAUTH_MODELS: list[str] = [
    "openai/gpt-5.4",
    "openai/gpt-5.4-pro",
    "openai/gpt-5.3-codex",
    "openai/gpt-5.2-pro",
    "openai/gpt-5.2-codex",
    "openai/gpt-5.2",
    "openai/gpt-5.1",
    "openai/gpt-5.1-codex-max",
    "openai/gpt-5.1-codex",
    "openai/gpt-5.1-codex-mini",
    "openai/gpt-5-pro",
    "openai/gpt-5-codex",
    "openai/gpt-5",
    "openai/codex-mini-latest",
]

_OPENAI_CODEX_OAUTH_MODEL_ID_SET = {
    m.split("/", 1)[1].strip().lower() for m in OPENAI_CODEX_OAUTH_MODELS
}

_OPENAI_CODEX_OAUTH_ALIASES: dict[str, str] = {
    "gpt-5.3-chat-latest": "gpt-5.3-codex",
    "gpt-5.2-chat-latest": "gpt-5.2",
    "gpt-5.1-chat-latest": "gpt-5.1",
    "gpt-5-chat-latest": "gpt-5",
    "gpt-5.2-codex-low": "gpt-5.2-codex",
    "gpt-5.2-codex-medium": "gpt-5.2-codex",
    "gpt-5.2-codex-high": "gpt-5.2-codex",
    "gpt-5.2-codex-xhigh": "gpt-5.2-codex",
    "gpt-5.2-low": "gpt-5.2",
    "gpt-5.2-medium": "gpt-5.2",
    "gpt-5.2-high": "gpt-5.2",
    "gpt-5.2-xhigh": "gpt-5.2",
    "gpt-5.1-codex-max-low": "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-medium": "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-high": "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-xhigh": "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini-medium": "gpt-5.1-codex-mini",
    "gpt-5.1-codex-mini-high": "gpt-5.1-codex-mini",
    "gpt-5-codex-mini": "codex-mini-latest",
    "codex-mini-latest": "gpt-5.1-codex-mini",
}


def normalize_openai_model(model: str) -> str:
    value = (model or "").strip()
    if not value:
        return ""
    if value.startswith("openai/"):
        return value
    return f"openai/{value}"


def is_openai_compatible_local_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
        host = (parsed.netloc or "").split("@")[-1].split(":")[0].strip().lower()
    except Exception:
        return False
    if not host:
        return False
    return host not in {"api.openai.com", "openai.com"}


def is_codex_oauth_model_supported(model: str) -> bool:
    normalized = normalize_openai_model(model)
    if not normalized.startswith("openai/"):
        return False
    model_id = normalized.split("/", 1)[1].strip().lower()
    if not model_id or model_id.endswith(".gguf"):
        return False
    mapped = _OPENAI_CODEX_OAUTH_ALIASES.get(model_id, model_id)
    if mapped in _OPENAI_CODEX_OAUTH_MODEL_ID_SET:
        return True
    if model_id.endswith("chat-latest"):
        return False
    return model_id.startswith("gpt-5.") or model_id.startswith("gpt-5-") or model_id.startswith("codex-")


def normalize_codex_oauth_model_id(model_id: str) -> str:
    m = (model_id or "").strip().lower()
    if not m:
        return "gpt-5.4"
    mapped = _OPENAI_CODEX_OAUTH_ALIASES.get(m, m)
    if mapped in _OPENAI_CODEX_OAUTH_MODEL_ID_SET:
        return mapped
    if m.startswith("gpt-5.") and not m.endswith("chat-latest"):
        return m
    if m.startswith("gpt-5-") and not m.endswith("chat-latest"):
        return m
    if m.startswith("codex-"):
        return m

    if "gpt-5.4-pro" in m:
        return "gpt-5.4-pro"
    if "gpt-5.4" in m:
        return "gpt-5.4"
    if "gpt-5.3-codex" in m or "gpt-5.3-chat-latest" in m:
        return "gpt-5.3-codex"
    if "gpt-5.2-codex" in m:
        return "gpt-5.2-codex"
    if "gpt-5.2" in m:
        return "gpt-5.2"
    if "gpt-5.1-codex-max" in m or "codex-max" in m:
        return "gpt-5.1-codex-max"
    if "gpt-5.1-codex-mini" in m or "codex-mini" in m:
        return "gpt-5.1-codex-mini"
    if "gpt-5.1-codex" in m:
        return "gpt-5.1-codex"
    if "gpt-5-pro" in m:
        return "gpt-5-pro"
    if "gpt-5-codex" in m:
        return "gpt-5-codex"
    if "gpt-5" in m:
        return "gpt-5"
    return "gpt-5.4"
