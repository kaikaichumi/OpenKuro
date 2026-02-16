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
    known_models: list[str] = Field(default_factory=list)  # Predefined model list for UI

    def get_api_key(self) -> str | None:
        """Resolve API key: env var first, then direct value as fallback."""
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                return key
        return self.api_key


class ModelsConfig(BaseModel):
    """LLM model configuration."""

    default: str = "anthropic/claude-sonnet-4.5"
    fallback_chain: list[str] = Field(default_factory=lambda: [
        "anthropic/claude-sonnet-4.5",
        "openai/gpt-5.2",
        "ollama/qwen3:32b",
    ])
    providers: dict[str, ProviderConfig] = Field(default_factory=lambda: {
        "gemini": ProviderConfig(
            api_key_env="GEMINI_API_KEY",
            known_models=[
                "gemini/gemini-3-flash",
                "gemini/gemini-3-pro",
                "gemini/gemini-2.5-flash",
                "gemini/gemini-2.5-pro",
            ],
        ),
        "anthropic": ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            known_models=[
                "anthropic/claude-opus-4.6",
                "anthropic/claude-sonnet-4.5",
                "anthropic/claude-haiku-4.5",
            ],
        ),
        "openai": ProviderConfig(
            api_key_env="OPENAI_API_KEY",
            known_models=[
                "openai/gpt-5.3-codex",
                "openai/gpt-5.2",
                "openai/gpt-5",
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
            ],
        ),
        "ollama": ProviderConfig(
            base_url="http://localhost:11434",
            api_key="not-needed",
            known_models=[
                "ollama/qwen3:32b",
                "ollama/qwen3-coder",
                "ollama/llama3.3:70b",
                "ollama/deepseek-r1",
                "ollama/deepseek-coder-v2",
                "ollama/llama3.2:3b",
                "ollama/mistral-nemo",
            ],
        ),
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
        "~/.kuro/plugins",
        "~/.kuro/skills",
        "~/.kuro/memory",
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


class DiscordConfig(BaseModel):
    """Discord adapter configuration."""

    enabled: bool = False
    bot_token_env: str = "KURO_DISCORD_TOKEN"  # Environment variable name for bot token
    allowed_user_ids: list[int] = Field(default_factory=list)  # Empty = allow all
    allowed_channel_ids: list[int] = Field(default_factory=list)  # Empty = allow all
    command_prefix: str = "!"  # Prefix for bot commands
    max_message_length: int = 2000  # Discord's limit
    approval_timeout: int = 60  # seconds to wait for approval response

    def get_bot_token(self) -> str | None:
        """Resolve bot token from environment variable."""
        return os.environ.get(self.bot_token_env)


class AdaptersConfig(BaseModel):
    """Messaging adapter configuration."""

    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    line: dict[str, Any] = Field(default_factory=dict)


class WebUIConfig(BaseModel):
    """Web GUI configuration."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7860


class SkillsConfig(BaseModel):
    """Skills system configuration."""

    enabled: bool = True
    skills_dirs: list[str] = Field(default_factory=lambda: ["~/.kuro/skills"])
    auto_activate: list[str] = Field(default_factory=list)  # Skills to auto-activate on startup


class PluginsConfig(BaseModel):
    """Plugin loader configuration."""

    enabled: bool = True
    plugins_dir: str = "~/.kuro/plugins"


class AgentDefinitionConfig(BaseModel):
    """Persisted agent definition in config."""

    name: str
    model: str  # e.g., "ollama/llama3.1", "gemini/gemini-2.5-flash"
    system_prompt: str = ""  # Optional custom system prompt for this agent
    allowed_tools: list[str] = Field(default_factory=list)  # Empty = all tools
    denied_tools: list[str] = Field(default_factory=list)  # Explicit denials
    max_tool_rounds: int = 5  # Sub-agents get fewer rounds by default
    temperature: float | None = None  # None = inherit from main config
    max_tokens: int | None = None  # None = inherit from main config


class AgentsConfig(BaseModel):
    """Multi-agent system configuration."""

    enabled: bool = True
    max_concurrent_agents: int = 5  # Max agents that can run simultaneously
    default_max_tool_rounds: int = 5  # Default tool rounds for sub-agents
    predefined: list[AgentDefinitionConfig] = Field(default_factory=list)


class KuroConfig(BaseModel):
    """Root configuration for Kuro assistant."""

    models: ModelsConfig = Field(default_factory=ModelsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    action_log: ActionLogConfig = Field(default_factory=ActionLogConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)

    # Core prompt: encrypted, always present as the first SYSTEM message.
    # Loaded from ~/.kuro/system_prompt.enc at startup. Not user-editable via config.
    core_prompt: str = ""

    # Agent loop: max tool call rounds before forcing a text response
    max_tool_rounds: int = 10

    # User-configurable system prompt (supplement to core prompt)
    system_prompt: str = (
        "You are Kuro, a personal AI assistant. You are helpful, concise, and "
        "security-conscious. You have access to tools for file operations, shell "
        "commands, screenshots, calendar, and web browsing. Always explain what "
        "you are about to do before using a tool. Respond in the user's language.\n\n"
        "## Tool Usage Rules\n"
        "- Call each tool ONCE per step and wait for its result before deciding the next action.\n"
        "- After receiving a tool result, summarize the outcome to the user in natural language.\n"
        "- NEVER call the same tool with the same arguments more than once.\n"
        "- If a tool returns an error, explain the error to the user instead of retrying blindly.\n"
        "- When you have all the information needed, respond directly WITHOUT calling more tools."
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
