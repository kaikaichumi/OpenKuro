"""Kuro - Personal AI Assistant.

Entry point for the application.
Usage:
    python -m src.main                          # Start CLI mode
    python -m src.main --init                   # Initialize default config
    python -m src.main --web                    # Start Web GUI (localhost:7860)
    python -m src.main --telegram               # Start Telegram adapter
    python -m src.main --discord                # Start Discord adapter
    python -m src.main --adapters               # Start all enabled adapters
    python -m src.main --encrypt-prompt         # Encrypt system prompt (interactive)
    python -m src.main --encrypt-prompt --prompt-file my_prompt.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

import structlog

# Suppress Windows asyncio pipe cleanup warnings
if sys.platform == "win32":
    warnings.filterwarnings("ignore", category=ResourceWarning, message="unclosed.*transport")
    warnings.filterwarnings("ignore", category=ResourceWarning, message="unclosed.*pipe")

from src.config import KuroConfig, get_kuro_home, load_config, save_default_config
from src.core.action_log import ActionLogger
from src.core.engine import ApprovalCallback, Engine
from src.core.memory.manager import MemoryManager
from src.core.model_router import ModelRouter
from src.core.security.audit import AuditLog
from src.core.tool_system import ToolSystem
from src.tools.memory_tools.search import set_memory_manager

logger = structlog.get_logger()


def setup_logging() -> None:
    """Configure structured logging."""
    log_dir = get_kuro_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def ensure_kuro_home() -> Path:
    """Ensure the ~/.kuro directory structure exists."""
    home = get_kuro_home()
    dirs = [
        home,
        home / "logs",
        home / "action_logs",
        home / "memory",
        home / "memory" / "facts",
        home / "skills",
        home / "plugins",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Create default MEMORY.md if not exists
    memory_file = home / "memory" / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text(
            "# Kuro Memory\n\n"
            "This file stores facts and preferences that Kuro will remember.\n"
            "You can edit this file directly.\n\n"
            "## User Preferences\n\n"
            "## Facts\n\n",
            encoding="utf-8",
        )

    return home


def _load_env() -> None:
    """Load .env files from project root and ~/.kuro/."""
    env_file = Path(".env")
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

    kuro_env = get_kuro_home() / ".env"
    if kuro_env.exists():
        from dotenv import load_dotenv
        load_dotenv(kuro_env)


def build_engine(
    config: KuroConfig,
    approval_callback: ApprovalCallback | None = None,
) -> Engine:
    """Build the core Engine with all components.

    This is the shared factory used by both CLI and adapter modes.
    """
    _load_env()

    # Core components
    model_router = ModelRouter(config)
    tool_system = ToolSystem()
    action_logger = ActionLogger(config.action_log)
    audit_log = AuditLog()
    memory_manager = MemoryManager()

    # Wire memory tools to the memory manager
    set_memory_manager(memory_manager)

    # Discover built-in tools
    tool_system.discover_tools()

    # Load external plugins
    plugin_loader = None
    if config.plugins.enabled:
        from src.core.plugin_loader import PluginLoader

        plugin_loader = PluginLoader(config.plugins)
        count = plugin_loader.load_plugins(tool_system.registry)
        if count:
            logger.info("loaded_plugin_tools", count=count)

    # Initialize skills manager
    from src.core.skills import SkillsManager

    skills_manager = SkillsManager(config.skills)
    if config.skills.enabled:
        skills_manager.discover_skills()
        for name in config.skills.auto_activate:
            skills_manager.activate(name)

    # Build engine
    engine = Engine(
        config=config,
        model_router=model_router,
        tool_system=tool_system,
        action_logger=action_logger,
        approval_callback=approval_callback or ApprovalCallback(),
        audit_log=audit_log,
        memory_manager=memory_manager,
        skills_manager=skills_manager,
    )

    # Initialize agent manager (needs engine's approval infrastructure)
    if config.agents.enabled:
        from src.core.agents import AgentManager

        agent_manager = AgentManager(
            config=config,
            model_router=model_router,
            tool_system=tool_system,
            approval_policy=engine.approval_policy,
            approval_callback=engine.approval_cb,
            audit_log=audit_log,
        )
        engine.agent_manager = agent_manager

    # Initialize task scheduler
    from src.core.scheduler import TaskScheduler
    from src.tools.scheduler import (
        ScheduleAddTool,
        ScheduleDisableTool,
        ScheduleEnableTool,
        ScheduleListTool,
        ScheduleRemoveTool,
    )

    scheduler = TaskScheduler()

    # Set up executor function for scheduler
    async def scheduler_executor(tool_name: str, params: dict) -> str:
        """Execute a tool for the scheduler."""
        try:
            result = await engine.execute_tool(tool_name, params)
            return result
        except Exception as e:
            logger.error("scheduler_execution_failed", tool=tool_name, error=str(e))
            return f"Error: {str(e)}"

    scheduler.set_executor(scheduler_executor)

    # Register scheduler tools
    tool_system.registry.register(ScheduleAddTool(scheduler))
    tool_system.registry.register(ScheduleListTool(scheduler))
    tool_system.registry.register(ScheduleRemoveTool(scheduler))
    tool_system.registry.register(ScheduleEnableTool(scheduler))
    tool_system.registry.register(ScheduleDisableTool(scheduler))

    # Store scheduler in engine for access
    engine.scheduler = scheduler

    return engine


def build_app(config: KuroConfig) -> tuple[Engine, "CLI"]:
    """Build the CLI application (backward-compatible)."""
    from src.ui.cli import CLI, CLIApprovalCallback

    engine = build_engine(config)
    approval = CLIApprovalCallback(approval_policy=engine.approval_policy)
    engine.approval_cb = approval
    cli = CLI(engine=engine, config=config)
    return engine, cli


async def async_main(config: KuroConfig) -> None:
    """Async entry point for CLI mode."""
    _, cli = build_app(config)
    await cli.run()


async def async_web_main(config: KuroConfig) -> None:
    """Async entry point for Web GUI mode."""
    from src.ui.web_server import WebServer

    engine = build_engine(config)
    server = WebServer(engine, config)
    host = config.web_ui.host
    port = config.web_ui.port
    print(f"Kuro Web GUI: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    await server.run()


async def async_adapter_main(
    config: KuroConfig,
    adapter_names: list[str] | None = None,
) -> None:
    """Async entry point for adapter mode.

    Starts the specified adapters and runs until interrupted.
    """
    from src.adapters.manager import AdapterManager

    engine = build_engine(config)
    manager = AdapterManager.from_config(engine, config, adapters=adapter_names)

    if not manager.adapter_names:
        print("No adapters configured. Check your config.yaml or specify --telegram.")
        return

    # Start all adapters
    await manager.start_all()

    # Start the task scheduler
    if hasattr(engine, 'scheduler'):
        await engine.scheduler.start()
        print("Task scheduler started")

    print(f"Kuro adapters running: {', '.join(manager.adapter_names)}")
    print("Press Ctrl+C to stop.")

    # Keep running until interrupted
    stop_event = asyncio.Event()

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # Stop scheduler
        if hasattr(engine, 'scheduler'):
            await engine.scheduler.stop()

        await manager.stop_all()
        # Give async tasks time to clean up (fixes Windows pipe warnings)
        await asyncio.sleep(0.25)


def _handle_encrypt_prompt(prompt_file: str | None = None) -> None:
    """Handle the --encrypt-prompt CLI command.

    Reads a plaintext prompt from a file or stdin, encrypts it,
    and saves to ~/.kuro/system_prompt.enc.
    """
    from src.core.security.prompt_protector import PromptProtector

    protector = PromptProtector()

    if prompt_file:
        # Read from file
        path = Path(prompt_file)
        if not path.exists():
            print(f"Error: File not found: {prompt_file}")
            sys.exit(1)
        plaintext = path.read_text(encoding="utf-8").strip()
        print(f"Read prompt from: {prompt_file} ({len(plaintext)} chars)")
    else:
        # Interactive: read from stdin
        print("Enter the system prompt (end with Ctrl+D / Ctrl+Z on Windows):")
        print("-" * 40)
        try:
            lines = []
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        plaintext = "\n".join(lines).strip()

    if not plaintext:
        print("Error: Empty prompt. Aborting.")
        sys.exit(1)

    preview = plaintext[:80] + ("..." if len(plaintext) > 80 else "")
    print(f"\nPrompt length: {len(plaintext)} characters")
    print(f"Preview: {preview}")

    try:
        confirm = input("\nEncrypt and save? [Y/n] > ").strip().lower()
    except EOFError:
        confirm = "y"

    if confirm not in ("", "y", "yes"):
        print("Aborted.")
        return

    enc_path = protector.encrypt_prompt(plaintext)
    print(f"\nEncrypted prompt saved to: {enc_path}")
    print("This prompt will be used on next startup.")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Kuro - Personal AI Assistant",
        prog="kuro",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize default configuration",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: ~/.kuro/config.yaml)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the Web GUI (default: http://127.0.0.1:7860)",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Start the Telegram adapter",
    )
    parser.add_argument(
        "--discord",
        action="store_true",
        help="Start the Discord adapter",
    )
    parser.add_argument(
        "--adapters",
        action="store_true",
        help="Start all enabled adapters (from config)",
    )
    parser.add_argument(
        "--encrypt-prompt",
        action="store_true",
        help="Encrypt a system prompt for secure storage",
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help="Path to a plaintext prompt file (used with --encrypt-prompt)",
    )
    args = parser.parse_args()

    # Setup
    setup_logging()
    ensure_kuro_home()

    if args.init:
        config_path = save_default_config(
            Path(args.config) if args.config else None
        )
        print(f"Default config saved to: {config_path}")
        return

    if args.encrypt_prompt:
        _handle_encrypt_prompt(args.prompt_file)
        return

    # Load config
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    # Determine run mode
    try:
        if args.web:
            # Web GUI mode
            asyncio.run(async_web_main(config))
        elif args.telegram:
            # Telegram-only mode
            asyncio.run(async_adapter_main(config, ["telegram"]))
        elif args.discord:
            # Discord-only mode
            asyncio.run(async_adapter_main(config, ["discord"]))
        elif args.adapters:
            # All enabled adapters mode
            asyncio.run(async_adapter_main(config, adapter_names=None))
        else:
            # Default: CLI mode
            asyncio.run(async_main(config))
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
