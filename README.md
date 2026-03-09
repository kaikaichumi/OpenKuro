# Kuro (暗) - Personal AI Assistant

> **Kuro** - 在幕後默默運作的守護者。

A privacy-first personal AI assistant with multi-agent architecture, multi-model support, computer control, messaging integration, and a browser-based GUI.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Features

- **Multi-Agent Instances** - Primary Agent instances with independent memory, personality, engine, and sub-agent pools; each instance can bind to its own bot
- **Multi-Agent System** - Sub-agents (recursive, context-aware), Agent Teams (peer-to-peer collaboration), A2A (cross-instance remote delegation)
- **Split-Screen Chat** - Multi-panel web UI (1–6 panels) with per-panel agent binding and WebSocket multiplexing
- **Real-Time Dashboard** - Live agent state visualization, event timeline, and aggregated statistics via WebSocket push
- **Task Complexity Estimation** - ML-based complexity scoring with adaptive model routing; routes simple tasks to fast/cheap models, complex tasks to frontier models
- **Task Scheduler + Proactive Notifications** - Cron-like scheduling with auto-push results to Discord/Telegram
- **Multi-model support** - Anthropic Claude, OpenAI GPT, Google Gemini, Ollama local models via LiteLLM
- **Workflow Engine** - Composable multi-step automation with YAML definitions, agent chaining, and template variables
- **Context Overflow Auto-Compression** - Automatically compresses context when hitting token limits; truncates old tool results and drops stale messages
- **Self-Update** - Update Kuro with a single command (`/update`), no reinstall or reconfiguration needed
- **Customizable Personality** - Define Kuro's character via `~/.kuro/personality.md`
- **Security Dashboard** - Real-time security visualization, posture scoring, integrity verification
- **Usage Analytics** - Tool usage statistics, cost estimation, smart optimization suggestions
- **Experience Learning** - Learns from past interactions; extracts error patterns, tool usage optimizations, and model performance insights
- **Self-Diagnostics & Auto-Repair** - Built-in diagnostic tools for LLM self-debugging: error analysis, session health, performance profiling, and automated repair recommendations; configurable repair model (main or custom); `/diagnose` system command across all adapters
- **46 built-in tools** - Files, shell, screenshots, clipboard, desktop control, calendar, browser automation, memory, time, scheduling, agent delegation, agent instance management, team orchestration, remote delegation, workflows, version check, self-update, self-diagnostics
- **10+ Built-in Skills** - Translator, code reviewer, git helper, debug assistant, data analyst, and more
- **Skills + Plugins** - On-demand SKILL.md instructions, external Python tool plugins, one-click install
- **Messaging integration** - Telegram, Discord, Slack (Socket Mode), LINE (webhook), Email (IMAP IDLE + SMTP)
- **Web GUI** - Dark-themed browser interface at `localhost:7860` with split-screen chat, agent management, real-time dashboard, WebSocket streaming, i18n (English, Traditional Chinese)
- **CLI** - Rich terminal with markdown rendering, streaming, slash commands
- **5-layer security** - Approval, sandbox, credentials, audit, sanitizer
- **3-tier memory** - Working memory, conversation history (SQLite), long-term RAG (ChromaDB) with lifecycle management (decay, consolidation, pruning)
- **Memory Lifecycle** - Importance scoring, time-based decay, automatic consolidation and pruning to prevent infinite memory growth
- **System prompt encryption** - Protect AI guidance from casual reading
- **Zero-token action logging** - JSONL operation history without LLM cost
- **Auto Date/Time Context** - Current date and time automatically injected into LLM context for temporal awareness
- **LangSmith Tracing** - Optional LLM observability with trace visualization, token usage, latency tracking, and cost estimation via LangSmith
- **DPI-Aware Desktop Automation** - Automatic display scaling detection; coordinates are converted to logical pixels so mouse clicks land accurately on high-DPI screens (125%, 150%, 200%)
- **Universal Text Input** - CJK/Unicode text typing support via clipboard bridge; works with Chinese, Japanese, Korean, and all non-ASCII characters

---

## Installation

### Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/docs/#installation)
- (Optional) [Ollama](https://ollama.ai/) for local models
- (Optional) Playwright: `playwright install chromium` for browser tools

### Setup

```bash
# Clone the repository
git clone <repo-url> && cd assistant

# Install dependencies
poetry install

# Initialize default config
poetry run kuro --init

# Set up API keys
cp .env.example .env
# Edit .env with your API keys:
#   ANTHROPIC_API_KEY=sk-...
#   OPENAI_API_KEY=sk-...
#   GEMINI_API_KEY=...
```

---

## Quick Start

### CLI Mode (default)

```bash
poetry run kuro
```

Interactive terminal with streaming responses, tool approval, and slash commands.

### Web GUI Mode

```bash
poetry run kuro --web
# Open http://127.0.0.1:7860
```

Browser-based chat with dark theme, split-screen panels (1–6), agent management, real-time dashboard, approval modals, settings panel, and audit log viewer.

#### Optional: OpenAI OAuth Subscription in Web UI

If you want ChatGPT Plus/Pro subscription traffic in Web UI (instead of API-key billing), use the built-in Codex OAuth sign-in flow.

No local `client_secret` is required. The app uses OpenAI's public Codex OAuth client id by default.

Optional env overrides:

```bash
# OPENAI_CODEX_OAUTH_CLIENT_ID="app_EMoamEEZ73f0CkXaXp7hrann"
# OPENAI_CODEX_OAUTH_SCOPE="openid profile email offline_access"
# OPENAI_CODEX_OAUTH_REDIRECT_URI="http://127.0.0.1:7860/api/oauth/openai/callback"
# OPENAI_CODEX_OAUTH_MODELS="gpt-5.4,gpt-5.4-pro,gpt-5.3-codex,gpt-5.3-chat-latest,gpt-5.2-pro,gpt-5.2-codex,gpt-5.2-chat-latest,gpt-5.2,gpt-5-pro,gpt-5.1-codex-max,gpt-5.1-codex,gpt-5.1-codex-mini,gpt-5.1-chat-latest,gpt-5.1,gpt-5-codex,gpt-5-chat-latest,gpt-5,codex-mini-latest"
# OPENAI_CODEX_INSTRUCTIONS="You are Codex, a software engineering assistant running in a local user workspace."
```

Then open Web UI Settings and use **Sign in with OpenAI (Subscription)**.  
OpenAI models are shown as separate entries: `OpenAI (API)` vs `OpenAI (OAuth Subscription)`.

### Telegram Bot Mode

```bash
# Set bot token in .env: KURO_TELEGRAM_TOKEN=your-token
poetry run kuro --telegram
```

### Discord Bot Mode

```bash
# Set bot token in .env: KURO_DISCORD_TOKEN=your-token
poetry run kuro --discord
```

### Slack Bot Mode

```bash
# Set tokens in .env:
#   KURO_SLACK_BOT_TOKEN=xoxb-...
#   KURO_SLACK_APP_TOKEN=xapp-...  (Socket Mode)
poetry run kuro --slack
```

### LINE Bot Mode

```bash
# Set in .env:
#   KURO_LINE_CHANNEL_SECRET=...
#   KURO_LINE_ACCESS_TOKEN=...
poetry run kuro --line
```

### Email Mode

```bash
# Set in .env:
#   KURO_EMAIL_ADDRESS=you@gmail.com
#   KURO_EMAIL_PASSWORD=app-password
poetry run kuro --email
```

### All Adapters Mode

```bash
poetry run kuro --adapters
```

---

## Local Model Setup (Ollama)

Kuro supports running fully local models via [Ollama](https://ollama.ai/), enabling offline operation and privacy-focused workflows.

### 1. Install Ollama

Download from [https://ollama.ai/](https://ollama.ai/) or:

```bash
# macOS / Linux
curl -fsSL https://ollama.ai/install.sh | sh

# Windows
# Download installer from https://ollama.ai/download/windows
```

### 2. Pull Models

```bash
# Recommended: Best general-purpose model (2026)
ollama pull qwen3:32b

# Fast coding model
ollama pull qwen3-coder

# Larger models
ollama pull llama3.3:70b
ollama pull deepseek-r1

# Lightweight models
ollama pull llama3.2:3b
ollama pull mistral-nemo
```

### 3. Configure Kuro

Edit `~/.kuro/config.yaml`:

```yaml
models:
  # Use Ollama as default
  default: "ollama/qwen3:32b"

  # Or keep cloud default with local fallback
  default: "anthropic/claude-sonnet-4.5"
  fallback_chain:
    - "anthropic/claude-sonnet-4.5"
    - "openai/gpt-5.2"
    - "ollama/qwen3:32b"  # Falls back to local if cloud fails

  providers:
    ollama:
      base_url: "http://localhost:11434"  # Default Ollama URL
      api_key: "not-needed"  # Local models don't require API keys
```

### 4. Switch Models

```bash
# In CLI mode
> /model ollama/qwen3:32b

# Or use /models to see all available
> /models
```

### 5. Create Multi-Agent Setup with Local Models

Use local models for fast, simple tasks, cloud models for complex reasoning:

```bash
# Create a fast local agent
> /agent create
  Agent name: fast
  Model: ollama/qwen3:32b
  System prompt: You are a fast local assistant. Be extremely concise.
  Allowed tools:

# Create a cloud reasoning agent
> /agent create
  Agent name: thinker
  Model: anthropic/claude-sonnet-4.5
  System prompt: You are a deep reasoning specialist.
  Allowed tools:
```

Or pre-configure in `config.yaml`:

```yaml
agents:
  enabled: true
  max_concurrent_agents: 3
  sub_agents:
    - name: fast
      model: ollama/qwen3:32b
      system_prompt: "You are a fast local assistant. Be extremely concise."
      max_tool_rounds: 3

    - name: coder
      model: ollama/qwen3-coder
      system_prompt: "You are a coding specialist."
      allowed_tools:
        - file_read
        - file_write
        - file_search
        - shell_execute

    - name: cloud
      model: anthropic/claude-sonnet-4.5
      system_prompt: "You handle complex reasoning tasks."

  # Optional: Primary Agent instances with independent memory + bot binding
  instances:
    - id: "customer-service"
      name: "Customer Service Bot"
      model: "gemini/gemini-3-flash"
      personality_mode: "independent"
      memory:
        mode: "independent"
      bot_binding:
        adapter_type: "discord"
        bot_token_env: "KURO_DISCORD_TOKEN_CS"
      sub_agents:
        - name: faq-lookup
          model: ollama/qwen3:32b
          system_prompt: "You look up FAQ answers."
```

### 6. Verify Ollama is Running

```bash
# Check Ollama status
curl http://localhost:11434/api/tags

# In Kuro CLI
> /models
# Should see ollama/llama3.1, ollama/... in the list
```

---

## CLI Commands

| Command | Description |
|---|---|
| `/models` | List all available models (grouped by provider) |
| `/model [name]` | Switch LLM model (e.g., `/model ollama/llama3.1`) |
| `/agents` | List all registered sub-agents |
| `/agent create` | Create new sub-agent (interactive model selection) |
| `/agent delete <name>` | Delete a sub-agent |
| `/agent info <name>` | Show agent details |
| `/agent run <name> <task>` | Run task on a sub-agent |
| `/skills` | List all available skills |
| `/skills available` | List built-in skills catalog |
| `/skills install <name>` | Install a built-in skill |
| `/skills search <query>` | Search skills by keyword |
| `/skill <name>` | Activate/deactivate a skill (toggle) |
| `/plugins` | List loaded plugins and their tools |
| `/trust [level]` | Set session trust level (low/medium/high) |
| `/stats` | Show usage analytics and smart suggestions |
| `/security` | Show security posture score |
| `/version` | Show current version and git info |
| `/update` | Check for and install updates |
| `/personality` | Show, edit, or reset personality settings |
| `/history` | Show conversation history |
| `/clear` | Clear current conversation |
| `/help` | Show available commands |

---

## Multi-Agent System

Kuro's multi-agent architecture supports hierarchical instances, sub-agents, teams, and cross-instance delegation:

### Agent Instances (Primary Agents)

Each instance is a fully independent AI agent with its own memory, personality, engine, sub-agent pool, and optional bot binding:

```
PrimaryAgent "main" (Engine + Memory + Personality)
├── SubAgent "fast"        (lightweight, no memory)
├── SubAgent "researcher"  (lightweight, web tools)
└── SubAgent "coder"       (lightweight, file tools)

PrimaryAgent "customer-service" (独立 Engine + Memory) → Discord Bot
├── SubAgent "faq-lookup"
└── SubAgent "translator"

PrimaryAgent "analyst" (linked memory) → Telegram Bot
└── SubAgent "data-processor"
```

- **Independent memory** — each instance has its own conversation history and long-term memory (or shared/linked)
- **Independent personality** — each instance can have its own `personality.md`
- **Own sub-agent pool** — each instance defines and manages its own sub-agents
- **Bot binding** — bind to a specific Discord/Telegram/Slack bot with per-instance token
- **LLM self-service** — the main agent can create/delete instances via tools at runtime
- **Web management** — CRUD instances via the Agents page in Web GUI

Memory modes:
- `independent` — fully isolated memory (default)
- `shared` — shares memory with the main agent
- `linked` — own history, shares long-term memory with specified agents

### Tier 1: Sub-Agents (Parent-Child Delegation)

Spawn sub-agents with different models for efficient task delegation:

- **Recursive delegation** — sub-agents can spawn further sub-agents (depth-limited)
- **Parent context inheritance** — sub-agents can see the parent conversation
- **Structured output** — agents can return JSON via `output_schema`
- **Dynamic creation** — LLM can create agents at runtime via `create_agent` tool

```yaml
agents:
  enabled: true
  max_concurrent_agents: 5
  default_max_depth: 3         # Recursive delegation depth limit
  allow_dynamic_creation: true # LLM can create agents at runtime
  sub_agents:                  # Main agent's sub-agent pool
    - name: fast
      model: ollama/qwen3:32b
      max_tool_rounds: 3
    - name: researcher
      model: gemini/gemini-3-flash
      inherit_context: true    # Sees parent conversation
      allowed_tools: [web_navigate, web_get_text, memory_store]
```

### Tier 2: Agent Teams (Peer-to-Peer Collaboration)

Multiple agents working as a team with shared workspace and messaging:

- **SharedWorkspace** — team-wide key-value store for data sharing
- **MessageBus** — async inter-agent messaging (point-to-point + broadcast)
- **TeamCoordinator** — LLM-driven task planning, progress evaluation, result synthesis
- **Parallel execution** — team roles run concurrently each round

```yaml
teams:
  enabled: true
  max_concurrent_teams: 2
  predefined:
    - name: research-team
      description: "Research + analysis + report writing"
      coordinator_model: anthropic/claude-sonnet-4.5
      max_rounds: 5
      roles:
        - name: researcher
          agent_name: researcher
          responsibility: "Search and gather information"
        - name: analyst
          agent_name: fast
          responsibility: "Analyze data and find insights"
        - name: writer
          agent_name: thinker
          responsibility: "Write the final report"
```

### Tier 3: Agent-to-Agent (A2A) — Cross-Instance Communication

Delegate tasks to agents on remote Kuro instances over HTTP:

- **Capability advertisement** — remote instances publish their agent list
- **Auto-discovery** — find Kuro peers on the local network
- **Authenticated delegation** — token-based auth for cross-instance tasks

```yaml
a2a:
  enabled: false              # Opt-in
  auth_token_env: KURO_A2A_TOKEN
  known_peers:
    - "http://192.168.1.100:7860"  # GPU server
  auto_discover: false
```

### Delegation Flow

**Manual:** `/agent run fast Summarize the last 3 commits`

**LLM-driven:** The main agent autonomously delegates via `delegate_to_agent`:
```
User: "Research the latest Rust async runtime benchmarks and summarize"
Main Agent → delegate_to_agent("researcher", "Find benchmarks...")
  → Researcher (Gemini Flash) searches web, extracts data
  → Returns to main agent → synthesized response
```

**Team-driven:** Multiple agents collaborate via `run_team`:
```
User: "Write a market analysis report on AI chip companies"
Main Agent → run_team("research-team", "Analyze AI chip market...")
  → Round 1: researcher searches + analyst processes + writer outlines
  → Round 2: coordinator evaluates → assigns follow-ups
  → Final: coordinator synthesizes unified report
```

---

## Split-Screen Chat

The Web GUI supports multi-panel chat with up to 6 simultaneous panels:

- **Layout modes** — Single, Split 2, Split 3, Grid 4 (2×2), Grid 6 (3×2)
- **Per-panel agent** — each panel can be bound to a different agent instance
- **WebSocket multiplexing** — single connection, messages routed by `agent_id`
- **Independent sessions** — each panel maintains its own conversation history
- **Agent switching** — change the agent for any panel via dropdown

---

## Real-Time Dashboard

Live visualization of all agent activity at `/dashboard`:

- **Agent state cards** — real-time status (idle/busy) per agent with message, tool call, delegation, and error counters
- **Event timeline** — chronological feed of all agent events (message received, tool calls, delegations, errors, status changes)
- **Live WebSocket push** — events appear instantly via `/ws/dashboard`
- **Statistics overview** — total events, active agents, tool calls, errors

---

## Task Complexity Estimation

Kuro uses a two-phase system to estimate task complexity and route to the best model:

### Phase 1: Heuristic Scoring (Zero-cost)

Analyzes 8 text dimensions including reasoning markers, code tokens, domain count, external tool needs, and CJK/multilingual support. Produces a score from 0.0 to 1.0.

### Phase 2: ML Classifier (Optional)

For ambiguous scores, a local ONNX model (fine-tuned DistilBERT, ~130MB) provides:

| Output | Description |
|---|---|
| Score | 0.0–1.0 complexity regression |
| Tier | trivial / simple / moderate / complex / expert |
| Domains | code, math, data, system, creative, finance, research |
| Intent | greeting, question, code_gen, analysis, debug, planning, creative, multi_step |

### Adaptive Model Routing

Based on complexity tier:

- **Trivial/Simple** → fast/cheap model (e.g., Ollama local, Gemini Flash)
- **Moderate** → default model
- **Complex/Expert** → frontier model (e.g., Claude Opus, GPT-5)

### Task Decomposition

Overly complex tasks (expert tier) are automatically broken into sub-tasks and delegated to sub-agents for parallel execution.

---

## Experience Learning

Kuro learns from past interactions by analyzing action logs:

- **Error pattern recognition** — identifies recurring tool failures
- **Tool usage optimization** — finds slow or redundant tool sequences
- **Model performance tracking** — tracks which models work best for which tasks
- **Lesson generation** — creates actionable "lessons learned" injected into future context

Lessons are stored at `~/.kuro/memory/lessons.json` and automatically loaded into relevant conversations.

---

## LangSmith Tracing (Observability)

Optional integration with [LangSmith](https://smith.langchain.com) for full LLM observability. Every LLM call and tool execution is traced with model name, token usage, latency, and cost.

### What You Get

| Feature | Description |
|---|---|
| **LLM Call Traces** | Every `litellm.acompletion()` is logged with input messages, model, tokens, latency |
| **Tool Execution Traces** | Each tool call recorded with parameters, result, duration |
| **Visual Trace Tree** | See the full conversation flow (LLM → tool → LLM → tool → response) in LangSmith dashboard |
| **Token Cost Tracking** | Automatic per-call and cumulative token usage and cost estimation |
| **Latency Analysis** | Identify slow LLM calls and tool executions |
| **Filtering by Tags** | Filter traces by custom tags in LangSmith UI |

### Setup

```bash
# 1. Install langsmith (optional dependency)
poetry install -E tracing

# 2. Set environment variables
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=lsv2_pt_...   # Get from https://smith.langchain.com
export LANGCHAIN_PROJECT=kuro           # Optional, defaults to "kuro"
```

### Configuration (config.yaml)

```yaml
tracing:
  enabled: true
  project_name: "kuro"
  tags: ["kuro", "production"]  # Custom tags for filtering
  trace_tools: true              # Log tool executions as child spans
  trace_memory: false            # Log memory operations (verbose)
```

### Architecture

LangSmith tracing is completely optional and non-intrusive:

- **Zero overhead when disabled** — all tracing calls are no-ops when `enabled: false`
- **Never breaks the main flow** — all tracing errors are silently caught
- **Sanitized data** — base64 images are stripped, large outputs are truncated before sending
- **No dependency lock-in** — `langsmith` is an optional Poetry extra

```
User Message → Engine
                ├── [LangSmith: trace_llm_call] → LiteLLM → Provider
                ├── [LangSmith: trace_tool_call] → Tool Execution
                ├── [LangSmith: trace_llm_call] → LiteLLM → Provider
                └── Response
```

---

## Desktop Automation (Computer Use)

Kuro can see and control the desktop through screenshots, OCR, and mouse/keyboard automation. Works with both vision-capable models (Claude, GPT-4o, Gemini) and text-only models (DeepSeek, Llama, Mistral).

### DPI Scaling Awareness

On Windows with display scaling (125%, 150%, 200%), screenshot pixels and mouse coordinates are in different coordinate spaces. Kuro automatically detects and corrects this:

```
Physical pixels (mss screenshot):  2880 x 1620  (150% scaling)
Logical pixels (pyautogui/mouse):  1920 x 1080

OCR detects "OK" button at physical (1440, 810)
 → Auto-converted to logical (960, 540)
 → mouse_action(action="click", x=960, y=540)  ← correct!
```

Detection method: compares `mss` capture size with `pyautogui.size()` to calculate the exact scale factor. Falls back to Windows DPI API if needed.

### Text-Only Model Support

When using models without vision (DeepSeek, Llama, Mistral, etc.), screenshots are automatically converted to structured text via OCR + OpenCV:

```
[Screen Analysis] 1920x1080 (logical coordinates — use directly with mouse_action)

== Text Elements (5 found) ==
[T1] "File" at (0,0)-(45,22) center:(22,11) — top-left
[T2] "Edit" at (50,0)-(95,22) center:(72,11) — top-left
...

== Click Targets (use these coordinates with mouse_action) ==
   1. "File" -> mouse_action(action="click", x=22, y=11)
   2. "Edit" -> mouse_action(action="click", x=72, y=11)
```

The LLM reads this structured text and can operate the desktop by calling `mouse_action` with the provided coordinates.

### CJK/Unicode Text Input

`pyautogui.write()` only supports ASCII characters. Kuro automatically detects non-ASCII text and uses a clipboard bridge:

1. Saves current clipboard content
2. Copies the target text to clipboard
3. Sends Ctrl+V (or Cmd+V on macOS) to paste
4. Restores original clipboard content

This works transparently for Chinese, Japanese, Korean, and all other Unicode text.

### Vision Auto-Fallback Modes

Configured via `config.yaml`:

```yaml
vision:
  image_analysis_mode: "auto"      # auto | always | disabled
  fallback_format: "text"          # text | svg
  fallback_detail_level: "standard"  # brief | standard | detailed
  grid_size: 4                     # NxN spatial grid
```

| Mode | Vision Model | Text-Only Model |
|---|---|---|
| `auto` (default) | Raw image | OCR + OpenCV analysis |
| `always` | Image + analysis | OCR + OpenCV analysis |
| `disabled` | Raw image | Image skipped |

---

## Self-Diagnostics & Auto-Repair

Kuro can diagnose its own problems at runtime through 4 LLM-callable diagnostic tools. When something goes wrong, the LLM can introspect system state, identify root causes, and suggest fixes — without you needing to read log files manually.

### Diagnostic Tools

| Tool | Purpose | Example Question |
|---|---|---|
| `debug_recent_errors` | Query recent tool failures with error details, parameters, and frequency | "Why did that last operation fail?" |
| `debug_session_info` | Inspect current session: message count, token estimate, DPI scale, memory size, diagnostics config, tracing status | "Why is the response so slow?" |
| `debug_performance` | Tool duration profiling, model routing decisions, complexity routing history, slow operation detection | "Which model is being used and why?" |
| `diagnose_and_repair` | Full system health check: error patterns, session health, performance bottlenecks, config audit, memory health, and actionable repair recommendations | "Fix the system" / "Diagnose everything" |

### System Command

All adapters support a `/diagnose` (or `!diagnose`) command for instant health checks without LLM round-trip:
- Telegram: `/diagnose`
- Discord: `!diagnose`
- Web UI: via chat or API

### Configuration

Diagnostics are fully configurable via `config.yaml`:

```yaml
diagnostics:
  enabled: true                    # Enable/disable all diagnostic tools
  auto_diagnose_on_error: true     # Auto-trigger diagnostics on errors
  error_threshold: 3               # Consecutive errors before auto-repair
  repair_model: "main"             # "main" = use main model, or custom model
  include_in_agents: true          # Include diagnostics in sub-agents
  only_matching_model: false       # Only for agents with matching model
  enabled_tools:                   # Which diagnostic tools to enable
    - debug_recent_errors
    - debug_session_info
    - debug_performance
    - diagnose_and_repair
```

**Repair model options:**
- `"main"` — use the same model as the main agent (default)
- `"gemini/gemini-3-flash"` — use a fast/cheap model for diagnostics
- `"anthropic/claude-opus-4.6"` — use a frontier model for complex diagnosis

### How It Works

All diagnostic tools are **LOW risk** (auto-approved) and read from existing data sources:

```
debug_recent_errors   ← reads action_logs/*.jsonl (failed tool calls)
debug_session_info    ← reads Session object + memory state + DPI + diagnostics config
debug_performance     ← reads action_logs + audit.db (durations, models, routing)
diagnose_and_repair   ← combines all above + config audit + memory health check
```

### Agent Integration

Sub-agents and agent instances automatically receive diagnostic tools when:
1. `diagnostics.include_in_agents` is `true` (default)
2. If `diagnostics.only_matching_model` is `true`, only agents using the same model as the main agent get diagnostics
3. Diagnostic tools are injected even when an agent has a restricted `allowed_tools` list

### Example Flows

```
User: "Why did the screenshot click not work?"

LLM → calls debug_recent_errors(tool_name="mouse_action")
     → sees: "Coordinates (1440, 810) out of screen bounds (0-1919, 0-1079)"
     → diagnosis: DPI scaling was causing physical coords to be used instead of logical
     → explains to user and retries with analyze_image for correct coordinates
```

```
User: "Fix the system"

LLM → calls diagnose_and_repair(scope="full")
     → scans errors, session, performance, config, memory
     → returns structured report with severity levels and recommended fixes
     → explains issues and applies safe auto-fixes if requested
```

### Combined with LangSmith

| Layer | What | When |
|---|---|---|
| **Diagnostic tools** | Instant, in-conversation, LLM can act on results | "Fix this now" |
| **`/diagnose` command** | Quick health check, no LLM cost | "Is everything OK?" |
| **LangSmith traces** | Historical, external dashboard, human reviews | "Analyze last week's performance" |

---

## Configuration

Config file: `~/.kuro/config.yaml` (created by `kuro --init`)

```yaml
models:
  default: "anthropic/claude-sonnet-4.5"
  fallback_chain:
    - "anthropic/claude-sonnet-4.5"
    - "openai/gpt-5.2"
    - "ollama/qwen3:32b"
  providers:
    gemini:
      api_key_env: "GEMINI_API_KEY"
      known_models:
        - "gemini/gemini-3-flash"
        - "gemini/gemini-3-pro"
        - "gemini/gemini-2.5-flash"
        - "gemini/gemini-2.5-pro"
    anthropic:
      api_key_env: "ANTHROPIC_API_KEY"
      known_models:
        - "anthropic/claude-opus-4.6"
        - "anthropic/claude-sonnet-4.5"
        - "anthropic/claude-haiku-4.5"
    openai:
      api_key_env: "OPENAI_API_KEY"
      known_models:
        - "openai/gpt-5.3-codex"
        - "openai/gpt-5.2"
        - "openai/gpt-5"
    ollama:
      base_url: "http://localhost:11434"
      api_key: "not-needed"  # Local models don't require API keys
      known_models:
        - "ollama/qwen3:32b"
        - "ollama/qwen3-coder"
        - "ollama/llama3.3:70b"
        - "ollama/deepseek-r1"
  temperature: 0.7
  max_tokens: 4096

security:
  auto_approve_levels: ["low"]
  require_approval_for: ["shell_execute", "send_message"]
  disabled_tools: []
  session_trust_enabled: true
  trust_timeout_minutes: 30

sandbox:
  allowed_directories:
    - "~/Documents"
    - "~/Desktop"
    - "~/.kuro/plugins"   # Allow Kuro to create plugin tools
    - "~/.kuro/skills"    # Allow Kuro to create skill files
    - "~/.kuro/memory"    # Allow Kuro to manage memory
  blocked_commands:
    - "rm -rf /"
    - "format"
    - "del /f /s /q C:\\"
  max_execution_time: 30
  max_output_size: 100000

agents:
  enabled: true
  max_concurrent_agents: 5
  default_max_tool_rounds: 5
  sub_agents: []            # Main agent's sub-agent pool (replaces old "predefined")
  instances: []             # Primary Agent instances (independent memory/personality/bot)

skills:
  enabled: true
  skills_dirs: ["~/.kuro/skills"]
  auto_activate: []

plugins:
  enabled: true
  plugins_dir: "~/.kuro/plugins"

adapters:
  telegram:
    enabled: false
    bot_token_env: "KURO_TELEGRAM_TOKEN"
    allowed_user_ids: []
  discord:
    enabled: false
    bot_token_env: "KURO_DISCORD_TOKEN"
    allowed_channel_ids: []

web_ui:
  enabled: true
  host: "127.0.0.1"
  port: 7860

action_log:
  mode: "tools_only"
  retention_days: 90
  max_file_size_mb: 50
```

---

## System Prompt Encryption

Protect the AI's core instructions from casual inspection. See [docs/SYSTEM_PROMPT_ENCRYPTION.md](docs/SYSTEM_PROMPT_ENCRYPTION.md) for implementation details.

```bash
# Encrypt from a file
poetry run kuro --encrypt-prompt --prompt-file my_prompt.txt

# Encrypt interactively
poetry run kuro --encrypt-prompt
# Enter prompt text, then Ctrl+D (Unix) or Ctrl+Z (Windows)
```

---

## Security Architecture

### 5-Layer Defense

| Layer | Module | Description |
|---|---|---|
| Approval | `approval.py` | Risk-based human approval. LOW auto-passes, MEDIUM+ requires confirmation |
| Sandbox | `sandbox.py` | Directory whitelist, command blacklist, execution timeout |
| Credentials | `credentials.py` | OS keychain via `keyring` (no plaintext config) |
| Audit | `audit.py` | SQLite append-only log with HMAC integrity verification |
| Sanitizer | `sanitizer.py` | Input/output sanitization, prompt injection detection |

### Risk Levels

| Level | Auto-approve | Examples |
|---|---|---|
| LOW | Yes | `file_read`, `screenshot`, `memory_search`, `get_time`, `list_agents` |
| MEDIUM | With trust | `file_write`, `clipboard_write`, `web_navigate`, `delegate_to_agent` |
| HIGH | Never | `shell_execute` |
| CRITICAL | Never | `send_message` |

---

## Memory System

### 3-Tier Architecture

| Tier | Storage | Purpose |
|---|---|---|
| Working Memory | In-memory | Current conversation context (sliding window) |
| Conversation History | SQLite | Past conversations, searchable |
| Long-term Memory | ChromaDB + MEMORY.md | Facts, preferences, RAG retrieval |

### Memory Lifecycle

Kuro automatically manages memory growth with:

- **Importance scoring** — recency × frequency × source weight (user > system > compression)
- **Time-based decay** — exponential decay on unused memories
- **Consolidation** — merges similar memories to reduce redundancy
- **Pruning** — removes memories that fall below importance threshold
- **MEMORY.md auto-organization** — keeps the file manageable as it grows

### Context Compression

When a conversation approaches the model's token limit, Kuro automatically:

1. Summarizes older messages to reduce token count
2. Truncates large tool outputs
3. Drops stale context while preserving key information
4. Retries with the same model (avoids unnecessary fallback)

### MEMORY.md

Manually editable file at `~/.kuro/memory/MEMORY.md`. Kuro reads this on every conversation for persistent preferences and facts.

---

## Scheduler Notifications

When a scheduled task completes, Kuro proactively pushes the result to the adapter (Discord/Telegram) where the task was created.

### How It Works

1. A user creates a scheduled task via Discord or Telegram
2. The task automatically records the notification target (adapter + channel/chat ID)
3. When the task fires, the result is pushed back to the user

```
User (Discord):  "Remind me to check stocks every day at 9am"
Kuro:             Creates schedule_add with notify_adapter="discord"
Every day 9:00:   Kuro executes the task → sends result to Discord channel
```

### Notification Control

By default, notifications are **on** for Discord/Telegram and **off** for CLI. The LLM can explicitly disable notifications:

```json
{ "task_id": "silent-task", "notify": false, ... }
```

Notifications include both **success results** and **error alerts** if a task fails.

---

## Messaging Adapters

### Supported Platforms

| Platform | Mode | Status |
|---|---|---|
| Telegram | Long-polling | Full |
| Discord | Gateway (discord.py) | Full |
| Slack | Socket Mode (slack-bolt) | Full |
| LINE | Webhook (aiohttp) | Full |
| Email | IMAP IDLE + SMTP | Full |

### Configuration (config.yaml)

```yaml
adapters:
  slack:
    enabled: true
    bot_token_env: KURO_SLACK_BOT_TOKEN
    app_token_env: KURO_SLACK_APP_TOKEN   # Socket Mode token
    allowed_user_ids: []                  # Empty = allow all
    allowed_channel_ids: []

  line:
    enabled: true
    channel_secret_env: KURO_LINE_CHANNEL_SECRET
    channel_access_token_env: KURO_LINE_ACCESS_TOKEN
    webhook_port: 8443                    # LINE webhook port (separate from web UI)

  email:
    enabled: true
    imap_host: imap.gmail.com
    smtp_host: smtp.gmail.com
    email_env: KURO_EMAIL_ADDRESS
    password_env: KURO_EMAIL_PASSWORD
    allowed_senders: []                   # Empty = allow all
    approval_timeout: 300                 # 5 min for email approvals
```

---

## Self-Update

Update Kuro without reinstalling or losing your configuration. All user data lives in `~/.kuro/` which is separate from the code directory.

### CLI Commands

```bash
# Check version
poetry run kuro --version

# Check if updates are available
poetry run kuro --check-update

# Update to latest version (interactive confirmation)
poetry run kuro --update
```

### Interactive Commands

```bash
# In CLI mode
> /version     # Show version + git hash
> /update      # Check & install updates
```

### How It Works

- Uses `git pull origin main` + `poetry install` (if dependencies changed)
- **Does NOT require GitHub Releases** — works with any git commits
- Automatically stashes local changes, updates, then pops stash
- Shows what changed (commit log) before updating
- Kuro can also update itself via LLM tool calls (`check_update`, `perform_update`)

---

## Personality System

Customize Kuro's character, tone, and behavior by editing a simple markdown file.

### File Location

```
~/.kuro/personality.md
```

This file is automatically created on first startup with default settings.

### Example

```markdown
# Kuro Personality

## Traits
- Friendly but professional
- Concise, not verbose
- Security-conscious

## Communication Style
- Respond in the user's language
- Use emoji moderately
- Explain before taking action

## Special Instructions
- Always respond in Traditional Chinese
- Use a slightly playful tone
- Add relevant emojis to responses
```

### How It Works

The personality file is injected into the LLM context on every conversation, between the system prompt and skills:

```
core_prompt → system_prompt → personality.md → skills → MEMORY.md → RAG → conversation
```

### Commands

```bash
> /personality        # View current personality
> /personality edit   # Open in editor (Notepad on Windows)
> /personality reset  # Restore defaults
```

### Web API

```
GET  /api/personality   # Read personality content
PUT  /api/personality   # Update personality (JSON body: {"content": "..."})
```

---

## Built-in Tools

| Tool | Risk | Description |
|---|---|---|
| **Files** |||
| `file_read` | LOW | Read file contents |
| `file_write` | MEDIUM | Write/create files |
| `file_search` | LOW | Search/glob files |
| **Shell** |||
| `shell_execute` | HIGH | Execute shell commands |
| **Screen & Desktop** |||
| `screenshot` | LOW | Capture screen with DPI metadata (mss + Pillow) |
| `analyze_image` | LOW | OCR + OpenCV analysis with logical coordinates for text-only models |
| `clipboard_read` | LOW | Read clipboard |
| `clipboard_write` | MEDIUM | Write clipboard |
| `mouse_action` | MEDIUM | Mouse control: click, double-click, right-click, drag, scroll (DPI-aware) |
| `keyboard_action` | MEDIUM | Keyboard control: type (Unicode/CJK), press, hotkey |
| `screen_info` | LOW | Get screen resolution and mouse position |
| `computer_use` | HIGH | Start desktop automation session with guided workflow |
| **Calendar & Time** |||
| `calendar_read` | LOW | Read local ICS calendar |
| `calendar_write` | MEDIUM | Add calendar events |
| `get_time` | LOW | Get current date, time, timezone |
| **Web** |||
| `web_navigate` | LOW | Open URL in browser |
| `web_get_text` | LOW | Get page text content |
| `web_click` | MEDIUM | Click page element |
| `web_type` | MEDIUM | Type in input field |
| `web_screenshot` | LOW | Screenshot current page |
| `web_close` | LOW | Close browser |
| **Memory** |||
| `memory_search` | LOW | Search long-term memory |
| `memory_store` | LOW | Store fact to memory |
| **Agents** |||
| `delegate_to_agent` | LOW | Delegate task to sub-agent (recursive, context-aware) |
| `list_agents` | LOW | List available agents |
| `create_agent` | MEDIUM | Dynamically create a new agent at runtime |
| `delete_agent` | LOW | Delete a runtime-created agent |
| **Agent Instances** |||
| `create_agent_instance` | MEDIUM | Create a new Primary Agent instance (with memory, personality, bot binding) |
| `delete_agent_instance` | HIGH | Delete a Primary Agent instance and its data |
| `list_agent_instances` | LOW | List all Primary Agent instances and their sub-agents |
| **Teams** |||
| `run_team` | MEDIUM | Run a multi-agent team on a task |
| `create_team` | MEDIUM | Create a new agent team |
| `list_teams` | LOW | List registered teams |
| **Remote (A2A)** |||
| `remote_delegate` | HIGH | Delegate task to agent on a remote Kuro instance |
| `discover_remote_agents` | LOW | Discover agents on remote Kuro instances |
| **Scheduling** |||
| `schedule_add` | MEDIUM | Add a scheduled task |
| `schedule_list` | LOW | List scheduled tasks |
| `schedule_remove` | MEDIUM | Remove a scheduled task |
| **Workflows** |||
| `workflow_create` | MEDIUM | Create a multi-step workflow |
| `workflow_run` | MEDIUM | Run a registered workflow |
| `workflow_list` | LOW | List workflows and recent runs |
| `workflow_delete` | MEDIUM | Delete a workflow |
| **Session** |||
| `session_clear` | LOW | Clear conversation history |
| **System** |||
| `get_version` | LOW | Show current Kuro version |
| `check_update` | LOW | Check if updates are available |
| `perform_update` | HIGH | Update Kuro from GitHub |
| **Diagnostics & Self-Repair** |||
| `debug_recent_errors` | LOW | Query recent tool failures with error details and frequency |
| `debug_session_info` | LOW | Inspect session state: messages, tokens, DPI, memory, diagnostics config, tracing |
| `debug_performance` | LOW | Tool durations, model routing, complexity decisions, slow ops |
| `diagnose_and_repair` | LOW | Full system health check with repair recommendations (configurable model) |

---

## Skills System

Skills are markdown files with YAML frontmatter that inject domain-specific instructions into the LLM context on-demand.

### Creating a Skill

```bash
mkdir -p ~/.kuro/skills/my-skill
```

Create `~/.kuro/skills/my-skill/SKILL.md`:

```markdown
---
name: my-skill
description: Brief description
---

# Skill Instructions

When this skill is active, you should...
- Follow these guidelines
- Use this approach
```

### Using Skills

```bash
# List available skills
> /skills

# Activate a skill
> /skill my-skill

# Deactivate
> /skill my-skill
```

Or auto-activate in config:

```yaml
skills:
  auto_activate: ["my-skill"]
```

---

## Plugins System

Extend Kuro with custom Python tools.

### Creating a Plugin

Create `~/.kuro/plugins/my_tool.py`:

```python
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

class MyCustomTool(BaseTool):
    name = "my_tool"
    description = "Does something custom"
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string"}
        },
        "required": ["input"]
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        result = params["input"].upper()
        return ToolResult.ok(result)
```

Restart Kuro — the tool is auto-discovered.

---

## Development

### Project Structure

```
src/
  main.py                     # Entry point + CLI args
  config.py                   # Pydantic config + YAML
  core/
    engine.py                 # Agent loop (model -> tools -> approve)
    model_router.py           # LiteLLM multi-model routing
    tool_system.py            # Plugin auto-discovery
    action_log.py             # JSONL operation logger
    analytics.py              # Usage analytics + cost estimator + smart advisor
    complexity.py             # Task complexity estimation (heuristic + ML)
    complexity_ml.py          # ONNX ML classifier for complexity scoring
    learning.py               # Experience learning engine (error patterns, lessons)
    tracing.py                # LangSmith observability integration
    code_feedback.py          # Code quality feedback system
    types.py                  # Message, Session, ToolCall, AgentDefinition
    agents.py                 # AgentRunner, AgentManager (recursive delegation, structured output)
    agent_events.py           # AgentEventBus + AgentEvent (real-time event tracking)
    agent_instance.py         # AgentInstance (Primary Agent with independent engine/memory)
    agent_instance_manager.py # AgentInstanceManager (lifecycle, CRUD, memory isolation)
    teams/
      types.py                # TeamRole, TeamDefinition, TeamMessage, TeamResult
      workspace.py            # SharedWorkspace (async key-value store)
      message_bus.py          # MessageBus (per-role async queues)
      coordinator.py          # TeamCoordinator (LLM-driven orchestration)
      team_runner.py          # TeamRunner + TeamManager
    a2a/
      protocol.py             # AgentCapability, A2ARequest, A2AResponse
      server.py               # A2A HTTP endpoints (delegate, capabilities, ping)
      client.py               # A2A HTTP client
      discovery.py            # Peer discovery + capability caching
    skills.py                 # SkillsManager (with install/search)
    plugin_loader.py          # PluginLoader
    workflow.py               # WorkflowEngine (multi-step automation)
    scheduler.py              # TaskScheduler (cron-like scheduling + notifications)
    updater.py                # Self-update mechanism (git pull + poetry install)
    security/
      approval.py             # Risk-based approval policy
      sandbox.py              # Execution sandbox
      credentials.py          # OS keychain (keyring)
      audit.py                # HMAC-verified audit log (SQLite)
      sanitizer.py            # Input/output sanitization + injection detection
      prompt_protector.py     # System prompt encryption (AES-128-CBC)
    memory/
      manager.py              # Context builder + auto date/time injection
      working.py              # Sliding window
      history.py              # SQLite persistence
      longterm.py             # ChromaDB + MEMORY.md
      compressor.py           # Auto-summarization on context overflow
      lifecycle.py            # Memory decay, consolidation, pruning
  tools/
    base.py                   # BaseTool ABC + RiskLevel
    filesystem/               # file_read, file_write, file_search
    shell/                    # shell_execute
    screen/                   # screenshot, clipboard, desktop_control, computer_use, analyze_image, dpi
    calendar/                 # calendar_read, calendar_write, get_time
    web/                      # browser automation (Playwright)
    memory_tools/             # memory_search, memory_store
    agents/                   # delegate_to_agent, list_agents, create/delete agent + instance tools
    teams/                    # run_team, create_team, list_teams
    a2a/                      # remote_delegate, discover_remote_agents
    scheduler/                # schedule_add, schedule_list, etc.
    workflow/                 # workflow_create, workflow_run, etc.
    analytics/                # dashboard_summary, token_usage, security_report, diagnostics
    system/                   # get_version, check_update, perform_update
  adapters/
    base.py                   # BaseAdapter ABC
    manager.py                # Adapter lifecycle
    telegram_adapter.py       # Telegram (full)
    discord_adapter.py        # Discord (full)
    line_adapter.py           # LINE (webhook)
    slack_adapter.py          # Slack (Socket Mode)
    email_adapter.py          # Email (IMAP IDLE + SMTP)
  ui/
    cli.py                    # Rich terminal interface
    web_server.py             # FastAPI + WebSocket
    web/                      # Static HTML/CSS/JS (modular architecture)
      index.html              # Main chat interface (split-screen multi-panel)
      agents.html             # Agent instance management page
      dashboard.html          # Real-time agent visualization dashboard
      config.html             # Settings/configuration
      analytics.html          # Usage statistics
      security.html           # Security dashboard
      scheduler.html          # Task scheduling UI
      css/                    # Modular CSS (variables, base, components, layout)
      js/                     # Modular JS (chat, config, analytics, scheduler, agents, dashboard, i18n, ...)
      locales/                # i18n translations (en.json, zh-TW.json)
models/
  complexity_model_int8.onnx  # ONNX INT8 quantized complexity classifier
  complexity_tokenizer/       # DistilBERT tokenizer files
tests/                        # 17 test files, 419+ test cases
  test_phase4.py              # Computer control tests
  test_phase5.py              # Messaging adapter tests
  test_phase7.py              # Encryption + tool restriction tests
  test_discord.py             # Discord adapter tests
  test_skills.py              # Skills + Plugins tests
  test_agents.py              # Multi-agent tests
  test_workflow.py            # Workflow engine tests
  ...
```

### Adding a New Tool

1. Create a new file under `src/tools/<category>/`
2. Subclass `BaseTool` with `name`, `description`, `parameters`, `risk_level`
3. Implement `async execute(self, params, context) -> ToolResult`
4. The tool is auto-discovered at startup (no registration needed)

### Adding a New Adapter

1. Create `src/adapters/my_adapter.py`
2. Subclass `BaseAdapter` with `start()` and `stop()` methods
3. Register in `AdapterManager.from_config()`

### Running Tests

```bash
# All tests
poetry run pytest tests/ -v

# Specific test file
poetry run pytest tests/test_agents.py -v

# With coverage
poetry run pytest tests/ --cov=src --cov-report=html
```

---

## Runtime Data

```
~/.kuro/
  config.yaml                 # User configuration
  personality.md              # Customizable AI personality & style
  system_prompt.enc           # Encrypted system prompt (optional)
  audit.db                    # Security audit log
  history.db                  # Conversation history
  scheduler.json              # Scheduled tasks (with notification targets)
  action_logs/                # JSONL operation logs (daily rotation)
  memory/
    MEMORY.md                 # Editable preferences/facts
    facts/                    # Knowledge files
    vector_store/             # ChromaDB data
  agents/                     # Agent instance data (per-instance memory, personality, history)
    <instance-id>/
      memory/                 # Independent long-term memory
      history.db              # Independent conversation history
      personality.md          # Independent personality
  skills/                     # User-created SKILL.md files
  plugins/                    # User-created Python tools
  screenshots/                # Captured screenshots
  calendar.ics                # Local calendar
  logs/
    assistant.log             # Application log
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

Copyright (c) 2026 Kuro Assistant Contributors
