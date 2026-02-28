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
        "./plugins",  # 安裝目錄的 plugins（讓 LLM 容易寫入）
        "./skills",   # 安裝目錄的 skills
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


class SlackConfig(BaseModel):
    """Slack adapter configuration."""

    enabled: bool = False
    bot_token_env: str = "KURO_SLACK_BOT_TOKEN"
    app_token_env: str = "KURO_SLACK_APP_TOKEN"  # For Socket Mode (no public URL needed)
    allowed_user_ids: list[str] = Field(default_factory=list)  # Slack user IDs are strings
    allowed_channel_ids: list[str] = Field(default_factory=list)
    max_message_length: int = 4000
    approval_timeout: int = 60

    def get_bot_token(self) -> str | None:
        return os.environ.get(self.bot_token_env)

    def get_app_token(self) -> str | None:
        return os.environ.get(self.app_token_env)


class LineConfig(BaseModel):
    """LINE Messaging API adapter configuration."""

    enabled: bool = False
    channel_secret_env: str = "KURO_LINE_CHANNEL_SECRET"
    channel_access_token_env: str = "KURO_LINE_ACCESS_TOKEN"
    webhook_port: int = 8443
    allowed_user_ids: list[str] = Field(default_factory=list)
    max_message_length: int = 5000
    approval_timeout: int = 60

    def get_channel_secret(self) -> str | None:
        return os.environ.get(self.channel_secret_env)

    def get_access_token(self) -> str | None:
        return os.environ.get(self.channel_access_token_env)


class EmailConfig(BaseModel):
    """Email adapter configuration (IMAP receive + SMTP send)."""

    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_env: str = "KURO_EMAIL_ADDRESS"
    password_env: str = "KURO_EMAIL_PASSWORD"
    allowed_senders: list[str] = Field(default_factory=list)  # Empty = allow all
    check_interval: int = 30  # seconds between IMAP IDLE reconnects
    max_message_length: int = 50000
    approval_timeout: int = 300  # Email is slower, 5 minutes

    def get_email(self) -> str | None:
        return os.environ.get(self.email_env)

    def get_password(self) -> str | None:
        return os.environ.get(self.password_env)


class AdaptersConfig(BaseModel):
    """Messaging adapter configuration."""

    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    line: LineConfig = Field(default_factory=LineConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


class WebUIConfig(BaseModel):
    """Web GUI configuration."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7860


class SkillsConfig(BaseModel):
    """Skills system configuration."""

    enabled: bool = True
    skills_dirs: list[str] = Field(default_factory=lambda: [
        "./skills",        # 安裝目錄（優先，LLM 容易寫入）
        "~/.kuro/skills",  # 使用者 home 目錄
    ])
    auto_activate: list[str] = Field(default_factory=list)  # Skills to auto-activate on startup


class PluginsConfig(BaseModel):
    """Plugin loader configuration."""

    enabled: bool = True
    plugins_dir: str = "./plugins"  # 改為安裝目錄（LLM 容易寫入）


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


class ContextCompressionConfig(BaseModel):
    """Context compression settings — auto-summarize old messages when context fills up."""

    enabled: bool = True
    trigger_threshold: float = 0.8  # Trigger compression at 80% of token budget
    keep_recent_turns: int = 10  # Keep the last N user+assistant turns verbatim
    summarize_model: str = "gemini/gemini-2.0-flash"  # Cheap fast model for summarization
    extract_facts: bool = True  # Auto-extract key facts into long-term memory
    max_summary_tokens: int = 600  # Max tokens for compressed summary
    token_budget: int = 100000  # Total token budget for context window


class MemoryLifecycleConfig(BaseModel):
    """Memory lifecycle management — prevent infinite memory growth."""

    enabled: bool = True
    decay_lambda: float = 0.01  # Exponential decay rate (half-life ~69 days)
    prune_threshold: float = 0.1  # Remove memories with importance below this
    consolidation_distance: float = 0.15  # Merge memories with cosine distance below this
    daily_maintenance_time: str = "03:00"  # Time to run daily maintenance (HH:MM)
    weekly_maintenance_day: int = 0  # Day of week for weekly consolidation (0=Monday)
    memory_md_max_lines: int = 200  # Auto-organize MEMORY.md when exceeding this
    pin_user_memories: bool = True  # User-stored memories get pinned (no decay)


class LearningConfig(BaseModel):
    """Experience learning — analyze action logs and learn from mistakes."""

    enabled: bool = True
    max_lessons: int = 20  # Maximum number of lessons to store
    inject_top_k: int = 5  # Inject top K relevant lessons into context
    error_threshold: int = 3  # Number of similar errors before creating a lesson
    analysis_time: str = "04:00"  # Time to run daily analysis (HH:MM)
    track_model_performance: bool = True  # Track which models perform best per task type


class CodeFeedbackConfig(BaseModel):
    """Code quality feedback loop — auto-check code after writing."""

    enabled: bool = False  # Disabled by default, opt-in
    lint_on_write: bool = True
    type_check_on_write: bool = False  # Slower, disabled by default
    test_on_write: bool = False  # Slower, disabled by default
    max_auto_fix_rounds: int = 3
    file_patterns: list[str] = Field(default_factory=lambda: ["*.py"])


class TaskComplexityConfig(BaseModel):
    """Task complexity estimation and adaptive model routing.

    Estimates task complexity from user messages and routes them to
    appropriate models. Can decompose overly complex tasks into sub-tasks.
    """

    enabled: bool = True

    # Trigger mode: "auto" | "manual" | "auto_silent"
    #   auto:        analyze every message, log result
    #   manual:      only via /complexity command or analyze_complexity tool
    #   auto_silent: analyze every message silently, only route
    trigger_mode: str = "auto"

    # --- Heuristic scoring weights (dimension → weight, sum should be 1.0) ---
    dimension_weights: dict[str, float] = Field(default_factory=lambda: {
        "token_length": 0.10,
        "reasoning_markers": 0.20,
        "domain_count": 0.15,
        "step_indicators": 0.15,
        "code_complexity": 0.15,
        "context_dependency": 0.10,
        "constraint_count": 0.10,
        "ambiguity": 0.05,
    })

    # --- LLM refinement for ambiguous scores ---
    llm_refinement: bool = True
    refinement_model: str = ""  # Empty = use context_compression.summarize_model
    ambiguity_low: float = 0.35   # Below → trust heuristic
    ambiguity_high: float = 0.65  # Above → trust heuristic

    # --- Model tier mapping (tier → model name, empty = auto-detect) ---
    fast_model: str = ""       # e.g., "gemini/gemini-2.5-flash"
    standard_model: str = ""   # e.g., "anthropic/claude-sonnet-4.5"
    frontier_model: str = ""   # e.g., "anthropic/claude-opus-4.6"

    # --- Tier boundaries (score thresholds) ---
    tier_boundaries: dict[str, float] = Field(default_factory=lambda: {
        "trivial": 0.15,
        "simple": 0.35,
        "moderate": 0.60,
        "complex": 0.85,
        # "expert" = above 0.85
    })

    # --- Decomposition ---
    decomposition_enabled: bool = True
    decomposition_threshold: float = 0.80  # Decompose tasks above this score
    max_subtasks: int = 5
    parallel_subtasks: bool = True  # Run independent sub-tasks in parallel

    # --- Local ML Model (fine-tuned DistilBERT ONNX classifier) ---
    ml_model_enabled: bool = False  # Disabled by default; enable after installing model
    ml_model_path: str = ""  # Path to ONNX model file (empty = <project>/models/complexity_model_int8.onnx)
    ml_tokenizer_path: str = ""  # Path to tokenizer dir (empty = <project>/models/complexity_tokenizer/)
    ml_estimation_mode: str = "hybrid"  # "ml_only" | "hybrid" | "ml_refine"
    #   ml_only:   Use ML model score exclusively (replaces heuristic)
    #   hybrid:    Blend ML + heuristic scores (0.6 ML + 0.4 heuristic)
    #   ml_refine: Use ML model only in ambiguous zone (replaces LLM refinement)

    # --- Feedback ---
    track_accuracy: bool = True  # Log predicted vs actual complexity for learning


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
    context_compression: ContextCompressionConfig = Field(default_factory=ContextCompressionConfig)
    memory_lifecycle: MemoryLifecycleConfig = Field(default_factory=MemoryLifecycleConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    code_feedback: CodeFeedbackConfig = Field(default_factory=CodeFeedbackConfig)
    task_complexity: TaskComplexityConfig = Field(default_factory=TaskComplexityConfig)

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
        "- When you have all the information needed, respond directly WITHOUT calling more tools.\n\n"
        "## Permission & Approval — IMPORTANT\n"
        "- NEVER refuse to call a tool based on perceived permission issues.\n"
        "- ALWAYS call the tool directly. The approval system will automatically prompt the user if needed.\n"
        "- You do NOT know whether a tool call will be approved or denied — only the system does.\n"
        "- If a tool is denied, the result will explicitly say 'Denied:' — only then acknowledge it.\n"
        "- NEVER generate phrases like 'permission denied', 'permission check failed', or "
        "'I cannot execute due to permissions' on your own. Let the system handle permissions."
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


def save_config(config: KuroConfig, config_path: Path | None = None) -> Path:
    """Save a configuration object to YAML file.

    Preserves the core_prompt field (not written to YAML since it's encrypted separately).
    Creates parent directories if needed. Returns the path.
    """
    if config_path is None:
        config_path = get_kuro_home() / "config.yaml"

    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(exclude={"core_prompt"})

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return config_path


def save_default_config(config_path: Path | None = None) -> Path:
    """Save the default configuration to a YAML file.

    Creates parent directories if needed. Returns the path.
    """
    return save_config(KuroConfig(), config_path)
