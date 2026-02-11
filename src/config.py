"""Configuration management for Kuro assistant.

Loads settings from YAML config file with Pydantic validation.
Config file location: ~/.kuro/config.yaml
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# === Default paths ===

def get_kuro_home() -> Path:
    """Get the Kuro data directory (~/.kuro)."""
    return Path(os.environ.get("KURO_HOME", Path.home() / ".kuro"))


# === Configuration Models ===


class ProviderConfig(BaseModel):
    """Configuration for a single LLM provider."""

    api_key_env: str | None = None  # Environment variable name for API key
    api_key: str | None = None  # Direct API key (not recommended)
    base_url: str | None = None  # Custom base URL (e.g., for Ollama)

    def get_api_key(self) -> str | None:
        """Resolve API key from env var or direct value."""
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return self.api_key


class ModelsConfig(BaseModel):
    """LLM model configuration."""

    default: str = "anthropic/claude-sonnet-4-20250514"
    fallback_chain: list[str] = Field(default_factory=lambda: [
        "anthropic/claude-sonnet-4-20250514",
        "openai/gpt-4o",
        "ollama/llama3.1",
    ])
    providers: dict[str, ProviderConfig] = Field(default_factory=lambda: {
        "anthropic": ProviderConfig(api_key_env="ANTHROPIC_API_KEY"),
        "openai": ProviderConfig(api_key_env="OPENAI_API_KEY"),
        "google": ProviderConfig(api_key_env="GOOGLE_API_KEY"),
        "ollama": ProviderConfig(base_url="http://localhost:11434"),
    })
    temperature: float = 0.7
    max_tokens: int = 4096
    stream: bool = True


class SecurityConfig(BaseModel):
    """Security layer configuration."""

    auto_approve_levels: list[str] = Field(default_factory=lambda: ["low"])
    require_approval_for: list[str] = Field(default_factory=lambda: [
        "shell_execute", "send_message",
    ])
    disabled_tools: list[str] = Field(default_factory=list)  # Completely disabled tools
    session_trust_enabled: bool = True
    trust_timeout_minutes: int = 30


class SandboxConfig(BaseModel):
    """Execution sandbox configuration."""

    allowed_directories: list[str] = Field(default_factory=lambda: [
        "~/Documents",
        "~/Desktop",
    ])
    blocked_commands: list[str] = Field(default_factory=lambda: [
        "rm -rf /",
        "format",
        "del /f /s /q C:\\",
        "reg delete",
        "rmdir /s /q C:\\",
    ])
    max_execution_time: int = 30  # seconds
    max_output_size: int = 100_000  # bytes


class ActionLogConfig(BaseModel):
    """Action log (operation history) configuration."""

    mode: str = "tools_only"  # tools_only | full | mutations_only
    retention_days: int = 90
    max_file_size_mb: int = 50
    include_full_result: bool = False


class TelegramConfig(BaseModel):
    """Telegram adapter configuration."""

    enabled: bool = False
    bot_token_env: str = "KURO_TELEGRAM_TOKEN"  # Environment variable name for bot token
    allowed_user_ids: list[int] = Field(default_factory=list)  # Empty = allow all (personal use)
    max_message_length: int = 4096
    approval_timeout: int = 60  # seconds to wait for approval response

    def get_bot_token(self) -> str | None:
        """Resolve bot token from environment variable."""
        return os.environ.get(self.bot_token_env)


class AdaptersConfig(BaseModel):
    """Messaging adapter configuration."""

    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: dict[str, Any] = Field(default_factory=dict)
    line: dict[str, Any] = Field(default_factory=dict)


class WebUIConfig(BaseModel):
    """Web GUI configuration."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7860


class KuroConfig(BaseModel):
    """Root configuration for Kuro assistant."""

    models: ModelsConfig = Field(default_factory=ModelsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    action_log: ActionLogConfig = Field(default_factory=ActionLogConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)

    # Core prompt: encrypted, always present as the first SYSTEM message.
    # Loaded from ~/.kuro/system_prompt.enc at startup. Not user-editable via config.
    core_prompt: str = ""

    # User-configurable system prompt (supplement to core prompt)
    system_prompt: str = (
        "You are Kuro, a personal AI assistant. You are helpful, concise, and "
        "security-conscious. You have access to tools for file operations, shell "
        "commands, screenshots, calendar, and web browsing. Always explain what "
        "you are about to do before using a tool. Respond in the user's language."
    )


# === Config Loading ===


def load_config(config_path: Path | None = None) -> KuroConfig:
    """Load configuration from YAML file.

    Falls back to defaults if the config file doesn't exist.
    Attempts to load an encrypted system prompt if available.
    """
    if config_path is None:
        config_path = get_kuro_home() / "config.yaml"

    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        config = KuroConfig(**raw)
    else:
        config = KuroConfig()

    # Load encrypted core prompt (always-present base layer, separate from system_prompt)
    try:
        from src.core.security.prompt_protector import load_core_prompt

        config.core_prompt = load_core_prompt()
    except ImportError:
        pass  # cryptography not installed — core_prompt stays empty

    return config


def save_default_config(config_path: Path | None = None) -> Path:
    """Save the default configuration to a YAML file.

    Creates parent directories if needed. Returns the path.
    """
    if config_path is None:
        config_path = get_kuro_home() / "config.yaml"

    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = KuroConfig()
    data = config.model_dump()

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return config_path
