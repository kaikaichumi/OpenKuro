# Kuro (暗) - Personal AI Assistant

> **Kuro** - 在幕後默默運作的守護者。

A privacy-first personal AI assistant with multi-model support, computer control, messaging integration, and a browser-based GUI.

---

## Features

- **Multi-model support** - Anthropic Claude, OpenAI GPT, Google Gemini, Ollama local models via LiteLLM
- **17 built-in tools** - Files, shell, screenshots, clipboard, calendar, browser automation, memory
- **Messaging integration** - Telegram (full), Discord & LINE (stubs)
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

### All Adapters Mode

```bash
poetry run kuro --adapters
```

---

## CLI Commands

| Command | Description |
|---|---|
| `/model [name]` | Switch LLM model |
| `/trust [level]` | Set session trust level (low/medium/high) |
| `/history` | Show conversation history |
| `/memory` | View/search long-term memory |
| `/audit` | View security audit log |
| `/clear` | Clear current conversation |
| `/help` | Show available commands |

---

## Configuration

Config file: `~/.kuro/config.yaml` (created by `kuro --init`)

```yaml
models:
  default: "anthropic/claude-sonnet-4-20250514"
  fallback_chain:
    - "anthropic/claude-sonnet-4-20250514"
    - "openai/gpt-4o"
    - "ollama/llama3.1"
  providers:
    anthropic:
      api_key_env: "ANTHROPIC_API_KEY"
    openai:
      api_key_env: "OPENAI_API_KEY"
    ollama:
      base_url: "http://localhost:11434"
  temperature: 0.7
  max_tokens: 4096

security:
  auto_approve_levels: ["low"]
  session_trust_enabled: true
  trust_timeout_minutes: 30

sandbox:
  allowed_directories:
    - "~/Documents"
    - "~/Desktop"
  blocked_commands:
    - "rm -rf /"
    - "format"
  max_execution_time: 30
  max_output_size: 100000

adapters:
  telegram:
    enabled: false
    bot_token_env: "KURO_TELEGRAM_TOKEN"
    allowed_user_ids: []   # Empty = allow all

web_ui:
  enabled: true
  host: "127.0.0.1"
  port: 7860

action_log:
  mode: "tools_only"       # tools_only | full | mutations_only
  retention_days: 90
  max_file_size_mb: 50
```

---

## System Prompt Encryption

Protect the AI's core instructions from casual inspection.

```bash
# Encrypt from a file
poetry run kuro --encrypt-prompt --prompt-file my_prompt.txt

# Encrypt interactively
poetry run kuro --encrypt-prompt
# Enter prompt text, then Ctrl+D (Unix) or Ctrl+Z (Windows)
```

Encrypted prompt is stored at `~/.kuro/system_prompt.enc` using Fernet (AES-128-CBC + HMAC-SHA256) with a machine-derived key. See [docs/SYSTEM_PROMPT_ENCRYPTION.md](docs/SYSTEM_PROMPT_ENCRYPTION.md) for details.

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
| LOW | Yes | `file_read`, `screenshot`, `memory_search` |
| MEDIUM | With trust | `file_write`, `clipboard_write`, `web_navigate` |
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
| `file_read` | LOW | Read file contents |
| `file_write` | MEDIUM | Write/create files |
| `file_search` | LOW | Search/glob files |
| `shell_execute` | HIGH | Execute shell commands |
| `screenshot` | LOW | Capture screen (mss + Pillow) |
| `clipboard_read` | LOW | Read clipboard |
| `clipboard_write` | MEDIUM | Write clipboard |
| `calendar_read` | LOW | Read local ICS calendar |
| `calendar_write` | MEDIUM | Add calendar events |
| `web_navigate` | MEDIUM | Open URL in browser |
| `web_get_text` | LOW | Get page text content |
| `web_click` | MEDIUM | Click page element |
| `web_type` | MEDIUM | Type in input field |
| `web_screenshot` | LOW | Screenshot current page |
| `web_close` | LOW | Close browser |
| `memory_search` | LOW | Search long-term memory |
| `memory_store` | LOW | Store fact to memory |

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
    types.py                  # Message, Session, ToolCall
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
    calendar/                 # calendar_read, calendar_write
    web/                      # browser automation (Playwright)
    memory_tools/             # memory_search, memory_store
  adapters/
    base.py                   # BaseAdapter ABC
    manager.py                # Adapter lifecycle
    telegram_adapter.py       # Telegram (full implementation)
    discord_adapter.py        # Discord (stub)
    line_adapter.py           # LINE (stub)
  ui/
    cli.py                    # Rich terminal interface
    web_server.py             # FastAPI + WebSocket
    web/                      # Static HTML/CSS/JS
tests/
  test_phase4.py              # Computer control tests (35)
  test_phase5.py              # Messaging adapter tests (40)
  test_phase6.py              # Web GUI tests (35)
  test_phase7.py              # Encryption + docs tests
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
poetry run pytest tests/ -v
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
  screenshots/                # Captured screenshots
  calendar.ics                # Local calendar
  logs/
    assistant.log             # Application log
```

---

## License

MIT
