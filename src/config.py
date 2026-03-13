"""Configuration management for Kuro assistant.

Loads settings from YAML config file with Pydantic validation.
Config file location: ~/.kuro/config.yaml
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from src.openai_catalog import OPENAI_OFFICIAL_MODELS


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
            known_models=list(OPENAI_OFFICIAL_MODELS),
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

    # Danger mode: bypass approval + sandbox restrictions.
    # Intended only for isolated environments.
    full_access_mode: bool = False
    auto_approve_levels: list[str] = Field(default_factory=lambda: ["low"])
    # Hard cap: tool risk above this level is denied without prompting.
    max_risk_level: str = "critical"
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
    complexity_tier: str = "moderate"  # Capability tier: trivial|simple|moderate|complex|expert
    # Phase 1 enhancements
    max_depth: int = 3  # Max recursive sub-agent depth (0 = no sub-agents)
    inherit_context: bool = False  # Inject parent conversation summary into sub-agent
    output_schema: dict | None = None  # JSON Schema for structured output (None = plain text)


class MemoryModeConfig(BaseModel):
    """Memory configuration for a Primary Agent instance."""

    mode: str = "independent"  # "independent" | "shared" | "linked"
    # For "linked": share specific memory tiers with named agents
    linked_agents: list[str] = Field(default_factory=list)
    link_tiers: list[str] = Field(default_factory=lambda: ["longterm"])


class BotBindingConfig(BaseModel):
    """Bot binding: which adapter this agent instance is attached to."""

    adapter_type: str = ""  # "discord" | "telegram" | "slack" | "line" | "email" | ""
    bot_token_env: str = ""  # Env var for a SEPARATE bot token
    overrides: dict[str, Any] = Field(default_factory=dict)


class InvocationConfig(BaseModel):
    """Who can invoke this agent instance."""

    allow_web_ui: bool = True
    allow_main_agent: bool = True
    allow_agents: list[str] = Field(default_factory=list)


class InstanceSecurityConfig(BaseModel):
    """Per-instance security overrides (empty = inherit from main config)."""

    # Approval: which risk levels auto-approve (e.g., ["low", "medium"])
    auto_approve_levels: list[str] = Field(default_factory=list)
    # Hard cap override: "" = inherit from main, else "low|medium|high|critical"
    max_risk_level: str = ""
    # Sandbox: allowed directories (empty = inherit from main)
    allowed_directories: list[str] = Field(default_factory=list)
    # Sandbox: blocked shell commands (empty = inherit from main)
    blocked_commands: list[str] = Field(default_factory=list)
    # Max shell execution time in seconds (0 = inherit from main)
    max_execution_time: int = 0


class InstanceFeatureOverrides(BaseModel):
    """Per-instance feature overrides (None = inherit from main config)."""

    # Context compression
    context_compression_enabled: bool | None = None
    context_compression_summarize_model: str | None = None
    context_compression_trigger_threshold: float | None = None

    # Memory and learning lifecycle
    memory_lifecycle_enabled: bool | None = None
    learning_enabled: bool | None = None

    # Code feedback loop
    code_feedback_enabled: bool | None = None

    # Vision/image analysis mode
    vision_image_analysis_mode: str | None = None  # auto | always | disabled

    # Task complexity system
    task_complexity_enabled: bool | None = None


class AgentInstanceConfig(BaseModel):
    """Configuration for a Primary Agent instance (full AI persona)."""

    id: str
    name: str
    enabled: bool = True
    # LLM settings (None = inherit from main config)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    system_prompt: str | None = None
    # Personality
    personality_mode: str = "independent"  # "independent" | "shared"
    # Tool configuration
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    max_tool_rounds: int = 10
    # Per-instance security (empty = inherit from main config)
    security: InstanceSecurityConfig = Field(default_factory=InstanceSecurityConfig)
    # Per-instance feature overrides (None fields inherit from main)
    feature_overrides: InstanceFeatureOverrides = Field(
        default_factory=InstanceFeatureOverrides
    )
    # Memory
    memory: MemoryModeConfig = Field(default_factory=MemoryModeConfig)
    # Bot binding
    bot_binding: BotBindingConfig = Field(default_factory=BotBindingConfig)
    # Invocation control
    invocation: InvocationConfig = Field(default_factory=InvocationConfig)
    # This Primary Agent's own Sub-Agent pool
    sub_agents: list[AgentDefinitionConfig] = Field(default_factory=list)


class AgentsConfig(BaseModel):
    """Unified multi-agent system configuration.

    Combines sub-agents (lightweight task executors) and Primary Agent instances
    (full AI personas with own memory/personality/bot binding) in one section.
    """

    enabled: bool = True
    max_concurrent_agents: int = 5
    default_max_tool_rounds: int = 5
    default_max_depth: int = 3
    allow_dynamic_creation: bool = True

    # Main agent's sub-agent pool (replaces old "predefined")
    sub_agents: list[AgentDefinitionConfig] = Field(default_factory=list)

    # Primary Agent instances (each with own memory/personality/sub-agents)
    instances: list[AgentInstanceConfig] = Field(default_factory=list)

    # Backward compat: old "predefined" auto-migrates to "sub_agents"
    predefined: list[AgentDefinitionConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _migrate_predefined(self):
        """Backward compat: merge predefined into sub_agents."""
        if self.predefined and not self.sub_agents:
            self.sub_agents = list(self.predefined)
            self.predefined = []
        return self


# === Phase 2: Agent Teams Configuration ===


class TeamRoleConfig(BaseModel):
    """Configuration for a role within an Agent Team."""

    name: str  # Role name, e.g. "researcher", "analyst"
    agent_name: str  # Corresponding AgentDefinition name
    responsibility: str = ""  # What this role does
    receives_from: list[str] = Field(default_factory=list)
    sends_to: list[str] = Field(default_factory=list)


class TeamDefinitionConfig(BaseModel):
    """Configuration for a predefined Agent Team."""

    name: str
    description: str = ""
    roles: list[TeamRoleConfig] = Field(default_factory=list)
    coordinator_model: str = ""  # Model for the team coordinator (empty = default)
    max_rounds: int = 5  # Maximum coordination rounds
    timeout_seconds: int = 300  # Overall team execution timeout


class TeamsConfig(BaseModel):
    """Agent Teams system configuration."""

    enabled: bool = True
    max_concurrent_teams: int = 2  # Max teams that can run simultaneously
    predefined: list[TeamDefinitionConfig] = Field(default_factory=list)


# === Phase 3: Agent-to-Agent (A2A) Configuration ===


class A2AConfig(BaseModel):
    """Agent-to-Agent cross-instance communication configuration."""

    enabled: bool = False  # Disabled by default — opt-in
    auth_token_env: str = "KURO_A2A_TOKEN"  # Env var for authentication
    known_peers: list[str] = Field(default_factory=list)  # Known peer endpoints
    auto_discover: bool = False  # Auto-discover peers on local network
    max_remote_agents: int = 10  # Max registered remote agents
    request_timeout: int = 120  # Timeout for remote requests (seconds)


class ContextCompressionConfig(BaseModel):
    """Context compression settings — auto-summarize old messages when context fills up."""

    enabled: bool = True
    trigger_threshold: float = 0.6  # Trigger compression at 60% of token budget
    keep_recent_turns: int = 10  # Keep the last N user+assistant turns verbatim
    summarize_model: str = "gemini/gemini-2.0-flash"  # Cheap fast model for summarization
    extract_facts: bool = True  # Auto-extract key facts into long-term memory
    max_summary_tokens: int = 600  # Max tokens for compressed summary
    token_budget: int = 100000  # Total token budget for context window


class ExecutionGuardConfig(BaseModel):
    """Guard rails to prevent runaway tool loops and risky bulk operations.

    Notes:
    - A value of 0 means "no hard limit" for that counter.
    - Duplicate tool calls are counted by (tool_name + normalized args) per task.
    """

    enabled: bool = True
    max_tool_calls_per_task: int = 0
    max_shell_calls_per_task: int = 0
    max_destructive_shell_ops_per_task: int = 3
    max_download_ops_per_task: int = 3
    max_repeat_tool_call: int = 1
    require_confirm_for_bulk_shell: bool = True
    bulk_shell_score_threshold: int = 4
    require_plan_for_high_risk: bool = True
    plan_model: str = ""  # Empty = use current active model
    plan_max_tokens: int = 280


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


class VisionConfig(BaseModel):
    """Vision and image analysis configuration.

    Controls how Kuro handles image/screenshot content when the LLM model
    does not support vision (multimodal) inputs.

    Modes:
      - "auto"     : (default) Only activate OCR/SVG conversion when the model
                      cannot handle images. Vision models receive raw images.
      - "always"   : Always run OCR/SVG analysis. Vision models get both the
                      raw image AND the text analysis. Text-only models get
                      the text analysis only.
      - "disabled" : Never convert images. Vision models receive raw images;
                      text-only models simply skip image content.
    """

    image_analysis_mode: str = "auto"  # auto | always | disabled

    # Output format for the image analysis
    fallback_format: str = "text"  # text | svg
    # Detail level for text output
    fallback_detail_level: str = "standard"  # brief | standard | detailed

    # Spatial grid size (NxN) for layout description
    grid_size: int = 4
    # Maximum UI elements to include in output (keeps token cost manageable)
    max_elements: int = 50

    # Explicit model capability overrides (model names like "ollama/qwen3:32b")
    vision_models: list[str] = Field(default_factory=list)
    text_only_models: list[str] = Field(default_factory=list)


class DiagnosticsConfig(BaseModel):
    """Self-diagnostics and auto-repair configuration.

    Controls the built-in diagnostic tools (debug_recent_errors,
    debug_session_info, debug_performance) and the self-repair system.

    The diagnostic tools let the LLM introspect Kuro's internal state
    when things go wrong — query recent errors, inspect session health,
    and profile performance.

    The self-repair tool (diagnose_and_repair) can be triggered manually
    or automatically when errors occur. It runs a diagnostic scan and
    suggests / applies fixes using a configurable model.
    """

    enabled: bool = True
    # Auto-trigger diagnostics when tool execution errors occur
    auto_diagnose_on_error: bool = True
    # Number of consecutive errors before auto-triggering repair
    error_threshold: int = 3

    # --- Model for self-repair ---
    # "main" = use the same model as the main agent
    # Or specify a custom model: "gemini/gemini-3-flash", "anthropic/claude-opus-4.6", etc.
    repair_model: str = "main"

    # --- Agent integration ---
    # Include diagnostic tools in sub-agents and agent instances
    include_in_agents: bool = True
    # Only include for agents whose model matches the main model
    # (False = include for ALL agents regardless of model)
    only_matching_model: bool = False

    # --- Which diagnostic tools to enable ---
    enabled_tools: list[str] = Field(default_factory=lambda: [
        "debug_recent_errors",
        "debug_session_info",
        "debug_performance",
        "diagnose_and_repair",
    ])


class TracingConfig(BaseModel):
    """LangSmith tracing configuration.

    Enable observability for all LLM calls with trace visualization,
    token counting, latency tracking, and cost estimation.

    Requires: pip install langsmith (or: poetry install -E tracing)

    Setup:
      1. Get API key from https://smith.langchain.com
      2. Set environment variables:
           LANGCHAIN_TRACING_V2=true
           LANGCHAIN_API_KEY=lsv2_...
           LANGCHAIN_PROJECT=kuro   (optional, defaults to "kuro")
      3. Set tracing.enabled=true in config.yaml
    """

    enabled: bool = False
    project_name: str = "kuro"
    # Trace tags for filtering in LangSmith UI
    tags: list[str] = Field(default_factory=lambda: ["kuro"])
    # Log tool calls as child spans
    trace_tools: bool = True
    # Log memory operations as child spans
    trace_memory: bool = False


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

    # --- Heuristic scoring weights (dimension → weight, sum should be ~1.0) ---
    dimension_weights: dict[str, float] = Field(default_factory=lambda: {
        "token_length": 0.08,
        "reasoning_markers": 0.18,
        "domain_count": 0.13,
        "step_indicators": 0.12,
        "code_complexity": 0.13,
        "context_dependency": 0.08,
        "constraint_count": 0.08,
        "ambiguity": 0.05,
        "external_tool_need": 0.15,
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


class DelegationComplexityConfig(BaseModel):
    """Complexity-based routing for main-agent task delegation to sub-agents."""

    enabled: bool = False
    # If true, delegate_to_agent defaults to complexity routing unless caller overrides.
    default_use_complexity: bool = False
    # Allow selecting a sub-agent automatically when agent_name is not provided
    # or when the named agent's tier is insufficient.
    allow_auto_select: bool = True
    # If true, reject/fallback when named sub-agent tier is below required tier.
    # If false, named agent can still run even when under-tier.
    enforce_min_tier: bool = True

    # Independent tier boundaries for delegation routing.
    # Uses same scoring engine as task_complexity, but thresholds are configurable separately.
    tier_boundaries: dict[str, float] = Field(default_factory=lambda: {
        "trivial": 0.15,
        "simple": 0.35,
        "moderate": 0.60,
        "complex": 0.85,
        # "expert" = above 0.85
    })
    # Preferred model per delegation tier. These are optional hints used when
    # auto-selecting a sub-agent; the system will prefer sub-agents on the
    # configured model when available.
    tier_models: dict[str, str] = Field(default_factory=lambda: {
        "trivial": "",
        "simple": "",
        "moderate": "",
        "complex": "",
    })


class KuroConfig(BaseModel):
    """Root configuration for Kuro assistant."""

    models: ModelsConfig = Field(default_factory=ModelsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    execution_guard: ExecutionGuardConfig = Field(default_factory=ExecutionGuardConfig)
    action_log: ActionLogConfig = Field(default_factory=ActionLogConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    a2a: A2AConfig = Field(default_factory=A2AConfig)
    context_compression: ContextCompressionConfig = Field(default_factory=ContextCompressionConfig)
    memory_lifecycle: MemoryLifecycleConfig = Field(default_factory=MemoryLifecycleConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    code_feedback: CodeFeedbackConfig = Field(default_factory=CodeFeedbackConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    task_complexity: TaskComplexityConfig = Field(default_factory=TaskComplexityConfig)
    delegation_complexity: DelegationComplexityConfig = Field(default_factory=DelegationComplexityConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)

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
        "- NEVER call the same tool with the same arguments more than once.\n"
        "- If a tool returns an error, explain the error to the user instead of retrying blindly.\n"
        "- When you have all the information needed, respond directly WITHOUT calling more tools.\n\n"
        "## Result Reporting — MANDATORY\n"
        "- After EVERY task completion, you MUST report the concrete results to the user.\n"
        "- NEVER respond with just 'Done', 'OK', 'Completed', or similar vague confirmations.\n"
        "- Always include WHAT was done and WHAT the outcome was (e.g., specific data, "
        "file paths, command output, analysis results).\n"
        "- When relaying a sub-agent's result, include the agent's actual findings — "
        "do NOT just say 'the agent completed the task'.\n"
        "- If a tool returned data, summarize the key information in your response.\n"
        "- If multiple tools were called, summarize ALL results, not just the last one.\n\n"
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
