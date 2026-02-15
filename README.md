# Kuro (暗) - Personal AI Assistant

> **Kuro** - 在幕後默默運作的守護者。

A privacy-first personal AI assistant with multi-agent architecture, multi-model support, computer control, messaging integration, and a browser-based GUI.

---

## Features

- **Multi-Agent System** - Delegate tasks to sub-agents with different models (local/cloud)
- **Multi-model support** - Anthropic Claude, OpenAI GPT, Google Gemini, Ollama local models via LiteLLM
- **20 built-in tools** - Files, shell, screenshots, clipboard, calendar, browser automation, memory, time, agent delegation
- **Skills + Plugins** - On-demand SKILL.md instructions, external Python tool plugins
- **Messaging integration** - Telegram, Discord (full), LINE (stub)
- **Web GUI** - Dark-themed browser interface at `localhost:7860` with WebSocket streaming
- **CLI** - Rich terminal with markdown rendering, streaming, slash commands
- **5-layer security** - Approval, sandbox, credentials, audit, sanitizer
- **3-tier memory** - Working memory, conversation history (SQLite), long-term RAG (ChromaDB)
- **System prompt encryption** - Protect AI guidance from casual reading
- **Zero-token action logging** - JSONL operation history without LLM cost

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

Browser-based chat with dark theme, streaming, approval modals, settings panel, and audit log viewer.

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
  predefined:
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
| `/skill <name>` | Activate/deactivate a skill (toggle) |
| `/plugins` | List loaded plugins and their tools |
| `/trust [level]` | Set session trust level (low/medium/high) |
| `/history` | Show conversation history |
| `/clear` | Clear current conversation |
| `/help` | Show available commands |

---

## Multi-Agent System

### Overview

Kuro can spawn sub-agents that run with different models, enabling efficient task delegation:

- **Local models** for simple, fast tasks (Ollama)
- **Cloud models** for complex reasoning (Claude, GPT-4)
- **Specialized models** for specific domains (coding, research)

### Creating Agents

**Interactive (CLI):**

```bash
> /agent create
  Agent name: researcher

  Available models:
    Gemini:
      1. gemini/gemini-3-flash
      2. gemini/gemini-3-pro
    Anthropic:
      3. anthropic/claude-opus-4.6
      4. anthropic/claude-sonnet-4.5
    OpenAI:
      5. openai/gpt-5.3-codex
      6. openai/gpt-5.2
    Ollama:
      7. ollama/qwen3:32b
      8. ollama/qwen3-coder

  Model (number or name): 1
  System prompt (Enter to skip): You are a web research specialist.
  Allowed tools (comma-separated, Enter for all): web_navigate, web_get_text, memory_store

  Agent 'researcher' created with model gemini/gemini-3-flash
```

**Config (YAML):**

```yaml
agents:
  enabled: true
  predefined:
    - name: fast
      model: ollama/qwen3:32b
      max_tool_rounds: 3

    - name: researcher
      model: gemini/gemini-3-flash
      allowed_tools: [web_navigate, web_get_text, memory_store]
```

### Delegation Flow

**Manual delegation:**

```bash
> /agent run fast Summarize the last 3 commits in git log
```

**LLM-driven delegation:**

The main agent can autonomously delegate via the `delegate_to_agent` tool:

```
User: "Research the latest Rust async runtime benchmarks and summarize"

Main Agent:
  → Calls delegate_to_agent(agent_name="researcher", task="Find benchmarks...")
  → Researcher agent (Gemini Flash) uses web tools, searches, extracts data
  → Returns summary to main agent
  → Main agent synthesizes final response
```

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
  predefined: []

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

### MEMORY.md

Manually editable file at `~/.kuro/memory/MEMORY.md`. Kuro reads this on every conversation for persistent preferences and facts.

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
| **Screen** |||
| `screenshot` | LOW | Capture screen (mss + Pillow) |
| `clipboard_read` | LOW | Read clipboard |
| `clipboard_write` | MEDIUM | Write clipboard |
| **Calendar & Time** |||
| `calendar_read` | LOW | Read local ICS calendar |
| `calendar_write` | MEDIUM | Add calendar events |
| `get_time` | LOW | Get current date, time, timezone |
| **Web** |||
| `web_navigate` | MEDIUM | Open URL in browser |
| `web_get_text` | LOW | Get page text content |
| `web_click` | MEDIUM | Click page element |
| `web_type` | MEDIUM | Type in input field |
| `web_screenshot` | LOW | Screenshot current page |
| `web_close` | LOW | Close browser |
| **Memory** |||
| `memory_search` | LOW | Search long-term memory |
| `memory_store` | LOW | Store fact to memory |
| **Agents** |||
| `delegate_to_agent` | MEDIUM | Delegate task to sub-agent |
| `list_agents` | LOW | List available agents |

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
    types.py                  # Message, Session, ToolCall, AgentDefinition
    agents.py                 # AgentRunner, AgentManager (multi-agent)
    skills.py                 # SkillsManager
    plugin_loader.py          # PluginLoader
    security/
      approval.py             # Risk-based approval policy
      sandbox.py              # Execution sandbox
      credentials.py          # OS keychain
      audit.py                # HMAC audit log
      sanitizer.py            # Input sanitization
      prompt_protector.py     # System prompt encryption
    memory/
      manager.py              # Context builder
      working.py              # Sliding window
      history.py              # SQLite persistence
      longterm.py             # ChromaDB + MEMORY.md
  tools/
    base.py                   # BaseTool ABC + RiskLevel
    filesystem/               # file_read, file_write, file_search
    shell/                    # shell_execute
    screen/                   # screenshot, clipboard
    calendar/                 # calendar_read, calendar_write, get_time
    web/                      # browser automation (Playwright)
    memory_tools/             # memory_search, memory_store
    agents/                   # delegate_to_agent, list_agents
  adapters/
    base.py                   # BaseAdapter ABC
    manager.py                # Adapter lifecycle
    telegram_adapter.py       # Telegram (full)
    discord_adapter.py        # Discord (full)
    line_adapter.py           # LINE (stub)
  ui/
    cli.py                    # Rich terminal interface
    web_server.py             # FastAPI + WebSocket
    web/                      # Static HTML/CSS/JS
tests/
  test_phase4.py              # Computer control tests
  test_phase5.py              # Messaging adapter tests
  test_phase7.py              # Encryption + tool restriction tests
  test_discord.py             # Discord adapter tests
  test_skills.py              # Skills + Plugins tests (45)
  test_agents.py              # Multi-agent tests (35)
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
# All tests (301 total)
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
  system_prompt.enc           # Encrypted system prompt (optional)
  audit.db                    # Security audit log
  history.db                  # Conversation history
  action_logs/                # JSONL operation logs (daily rotation)
  memory/
    MEMORY.md                 # Editable preferences/facts
    facts/                    # Knowledge files
    vector_store/             # ChromaDB data
  skills/                     # User-created SKILL.md files
  plugins/                    # User-created Python tools
  screenshots/                # Captured screenshots
  calendar.ics                # Local calendar
  logs/
    assistant.log             # Application log
```

---

## License

MIT
