"""Rich CLI interface for Kuro assistant.

Features:
- Rich markdown rendering
- Streaming token output
- Slash commands (/model, /help, /quit, etc.)
- Human-in-the-loop approval prompts
"""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from src.config import KuroConfig, get_kuro_home
from src.core.engine import ApprovalCallback, Engine
from src.core.types import Session
from src.tools.base import RiskLevel

console = Console()


BANNER = r"""
 _  __
| |/ /  _   _  _ __  ___
| ' /  | | | || '__|/ _ \
| . \  | |_| || |  | (_) |
|_|\_\  \__,_||_|   \___/
"""

HELP_TEXT = """
**Slash Commands:**
- `/help` - Show this help message
- `/models` - List all available models (grouped by provider)
- `/model <name>` - Switch model (e.g., `/model gemini/gemini-2.5-flash`)
- `/model` - Show current model
- `/trust <level>` - Set session trust level (low/medium/high)
- `/skills` - List all available skills
- `/skill <name>` - Activate/deactivate a skill (toggle)
- `/plugins` - List loaded plugins and their tools
- `/agents` - List all registered sub-agents
- `/agent create` - Create a new sub-agent (interactive model selection)
- `/agent delete <name>` - Delete a sub-agent
- `/agent info <name>` - Show agent details
- `/agent run <name> <task>` - Run a task on a sub-agent
- `/history` - Show conversation history
- `/clear` - Clear conversation history
- `/quit` or `/exit` - Exit Kuro
"""


class CLIApprovalCallback(ApprovalCallback):
    """CLI-based approval that prompts the user in the terminal."""

    def __init__(self, approval_policy=None):
        self.approval_policy = approval_policy

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        """Show approval prompt and wait for user input."""
        risk_colors = {
            RiskLevel.LOW: "green",
            RiskLevel.MEDIUM: "yellow",
            RiskLevel.HIGH: "red",
            RiskLevel.CRITICAL: "bold red",
        }
        color = risk_colors.get(risk_level, "white")

        console.print()
        console.print(
            Panel(
                Text.from_markup(
                    f"[bold]Tool:[/bold] {tool_name}\n"
                    f"[bold]Risk:[/bold] [{color}]{risk_level.value.upper()}[/{color}]\n"
                    f"[bold]Params:[/bold] {_format_params(params)}"
                ),
                title="[bold yellow]⚡ Approval Required[/bold yellow]",
                border_style="yellow",
            )
        )

        # Use simple input for approval (prompt_toolkit not needed here)
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input("  Approve? [Y/n/trust] > ").strip().lower(),
            )
        except (EOFError, KeyboardInterrupt):
            return False

        if response in ("", "y", "yes"):
            return True
        if response == "trust":
            # Trust this risk level for the session
            session.trust_level = risk_level.value
            if self.approval_policy:
                self.approval_policy.elevate_session_trust(session.id, risk_level)
            console.print(
                f"  [green]✓ Trusted {risk_level.value.upper()} actions for this session[/green]"
            )
            return True
        return False


def _format_params(params: dict[str, Any]) -> str:
    """Format tool parameters for display."""
    if not params:
        return "{}"
    lines = []
    for k, v in params.items():
        val_str = str(v)
        if len(val_str) > 100:
            val_str = val_str[:100] + "..."
        lines.append(f"  {k}: {val_str}")
    return "\n" + "\n".join(lines)


class CLI:
    """Interactive CLI for Kuro assistant."""

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        self.engine = engine
        self.config = config
        self.session = Session(adapter="cli")
        self.current_model: str | None = None

        # Setup prompt history
        history_dir = get_kuro_home() / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_dir / "cli_input.txt")),
        )

    async def run(self) -> None:
        """Main CLI loop."""
        self._print_banner()

        with patch_stdout():
            while True:
                try:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.prompt_session.prompt("\n> "),
                    )
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye![/dim]")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue

                # Handle slash commands
                if user_input.startswith("/"):
                    should_continue = await self._handle_command(user_input)
                    if not should_continue:
                        break
                    continue

                # Process regular message
                await self._process_message(user_input)

    async def _process_message(self, user_text: str) -> None:
        """Send a message to the engine and display the response."""
        try:
            console.print()

            # Use non-streaming for now (streaming will show tool calls properly)
            response = await self.engine.process_message(
                user_text,
                self.session,
                model=self.current_model,
            )

            # Render response as markdown
            console.print(
                Panel(
                    Markdown(response),
                    title="[bold blue]Kuro[/bold blue]",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")

    async def _handle_command(self, command: str) -> bool:
        """Handle a slash command. Returns False if should exit."""
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye![/dim]")
            return False

        elif cmd == "/help":
            console.print(Markdown(HELP_TEXT))

        elif cmd == "/models":
            groups = await self.engine.model.list_models_grouped()
            if not groups:
                console.print("[dim]No models available[/dim]")
            else:
                console.print("[bold]Available models:[/bold]")
                for provider, models in groups.items():
                    console.print(f"  [cyan]{provider.capitalize()}:[/cyan]")
                    for m in models:
                        marker = " [green](active)[/green]" if m == (self.current_model or self.config.models.default) else ""
                        console.print(f"    - {m}{marker}")

        elif cmd == "/model":
            if arg:
                self.current_model = arg
                console.print(f"[green]Model switched to: {arg}[/green]")
            else:
                current = self.current_model or self.config.models.default
                console.print(f"[blue]Current model: {current}[/blue]")

        elif cmd == "/trust":
            if arg in ("low", "medium", "high", "critical"):
                from src.tools.base import RiskLevel
                level_map = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM,
                             "high": RiskLevel.HIGH, "critical": RiskLevel.CRITICAL}
                self.session.trust_level = arg
                self.engine.approval_policy.elevate_session_trust(
                    self.session.id, level_map[arg])
                console.print(f"[green]Session trust level set to: {arg.upper()}[/green]")
            else:
                console.print(
                    f"[blue]Current trust level: {self.session.trust_level.upper()}[/blue]\n"
                    f"[dim]Usage: /trust low|medium|high|critical[/dim]"
                )

        elif cmd == "/skills":
            skills = self.engine.skills.list_skills() if self.engine.skills else []
            if not skills:
                console.print("[dim]No skills found. Place SKILL.md files in ~/.kuro/skills/<name>/[/dim]")
            else:
                active = self.engine.skills._active if self.engine.skills else set()
                for s in skills:
                    marker = "[green]\u25cf[/green]" if s.name in active else "[dim]\u25cb[/dim]"
                    console.print(f"  {marker} {s.name} \u2014 {s.description}")

        elif cmd == "/skill":
            if not arg:
                console.print("[yellow]Usage: /skill <name>[/yellow]")
            elif self.engine.skills:
                if arg in self.engine.skills._active:
                    self.engine.skills.deactivate(arg)
                    console.print(f"[dim]Deactivated skill: {arg}[/dim]")
                else:
                    if self.engine.skills.activate(arg):
                        console.print(f"[green]Activated skill: {arg}[/green]")
                    else:
                        console.print(f"[red]Skill not found: {arg}[/red]")
            else:
                console.print("[dim]Skills system not initialized[/dim]")

        elif cmd == "/plugins":
            tool_names = self.engine.tools.registry.get_names()
            # Show count of built-in vs total tools
            console.print(f"[bold]Loaded tools ({len(tool_names)}):[/bold]")
            for name in sorted(tool_names):
                console.print(f"  - {name}")

        elif cmd == "/agents":
            if not self.engine.agent_manager:
                console.print("[dim]Agent system not enabled[/dim]")
            else:
                agents = self.engine.agent_manager.list_definitions()
                if not agents:
                    console.print(
                        "[dim]No agents registered. Use /agent create to create one.[/dim]"
                    )
                else:
                    console.print("[bold]Registered agents:[/bold]")
                    running = self.engine.agent_manager._running
                    for defn in agents:
                        status = (
                            " [green](running)[/green]"
                            if defn.name in running
                            else ""
                        )
                        tools_info = ""
                        if defn.allowed_tools:
                            preview = ", ".join(defn.allowed_tools[:3])
                            if len(defn.allowed_tools) > 3:
                                preview += "..."
                            tools_info = f" tools=[{preview}]"
                        console.print(
                            f"  - [cyan]{defn.name}[/cyan]: {defn.model}"
                            f"{tools_info}{status} [dim]({defn.created_by})[/dim]"
                        )

        elif cmd == "/agent":
            parts_inner = arg.split(maxsplit=1)
            subcmd = parts_inner[0].lower() if parts_inner else ""
            subarg = parts_inner[1] if len(parts_inner) > 1 else ""

            if subcmd == "create":
                await self._create_agent_interactive()

            elif subcmd == "delete":
                if not subarg:
                    console.print("[yellow]Usage: /agent delete <name>[/yellow]")
                elif (
                    self.engine.agent_manager
                    and self.engine.agent_manager.unregister(subarg)
                ):
                    console.print(f"[green]Agent '{subarg}' deleted[/green]")
                else:
                    console.print(f"[red]Agent '{subarg}' not found[/red]")

            elif subcmd == "info":
                if not subarg:
                    console.print("[yellow]Usage: /agent info <name>[/yellow]")
                elif self.engine.agent_manager:
                    defn = self.engine.agent_manager.get_definition(subarg)
                    if defn:
                        console.print(
                            Panel(
                                Text.from_markup(
                                    f"[bold]Name:[/bold] {defn.name}\n"
                                    f"[bold]Model:[/bold] {defn.model}\n"
                                    f"[bold]System Prompt:[/bold] "
                                    f"{defn.system_prompt[:100] or '(default)'}\n"
                                    f"[bold]Allowed Tools:[/bold] "
                                    f"{', '.join(defn.allowed_tools) or 'all'}\n"
                                    f"[bold]Denied Tools:[/bold] "
                                    f"{', '.join(defn.denied_tools) or 'none'}\n"
                                    f"[bold]Max Tool Rounds:[/bold] "
                                    f"{defn.max_tool_rounds}\n"
                                    f"[bold]Created By:[/bold] {defn.created_by}"
                                ),
                                title=f"[bold cyan]Agent: {defn.name}[/bold cyan]",
                                border_style="cyan",
                            )
                        )
                    else:
                        console.print(f"[red]Agent '{subarg}' not found[/red]")

            elif subcmd == "run":
                # Parse: /agent run <name> <task>
                run_parts = subarg.split(maxsplit=1)
                if len(run_parts) < 2:
                    console.print(
                        "[yellow]Usage: /agent run <name> <task>[/yellow]"
                    )
                elif self.engine.agent_manager:
                    agent_name, task = run_parts
                    console.print(
                        f"[dim]Delegating to agent '{agent_name}'...[/dim]"
                    )
                    try:
                        result = await self.engine.agent_manager.run_agent(
                            agent_name, task
                        )
                        console.print(
                            Panel(
                                Markdown(result),
                                title=(
                                    f"[bold magenta]Agent: "
                                    f"{agent_name}[/bold magenta]"
                                ),
                                border_style="magenta",
                                padding=(1, 2),
                            )
                        )
                    except Exception as e:
                        console.print(f"[red]Agent error: {e}[/red]")
                else:
                    console.print("[dim]Agent system not enabled[/dim]")

            else:
                console.print(
                    "[yellow]Usage: /agent create|delete|info|run[/yellow]"
                )

        elif cmd == "/history":
            if not self.session.messages:
                console.print("[dim]No conversation history[/dim]")
            else:
                for msg in self.session.messages:
                    if msg.role.value == "system":
                        continue
                    role_color = {
                        "user": "green",
                        "assistant": "blue",
                        "tool": "yellow",
                    }.get(msg.role.value, "white")
                    preview = msg.content[:200] if msg.content else "(empty)"
                    console.print(f"[{role_color}]{msg.role.value}:[/{role_color}] {preview}")

        elif cmd == "/clear":
            self.session = Session(adapter="cli")
            console.print("[green]Conversation cleared[/green]")

        else:
            console.print(f"[red]Unknown command: {cmd}[/red]")
            console.print("[dim]Type /help for available commands[/dim]")

        return True

    async def _create_agent_interactive(self) -> None:
        """Interactive agent creation with model selection."""
        if not self.engine.agent_manager:
            console.print("[red]Agent system not enabled[/red]")
            return

        console.print("[bold]Create a new agent[/bold]")
        console.print()

        # 1. Get agent name
        try:
            name = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("  Agent name: ").strip()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]Cancelled[/dim]")
            return

        if not name:
            console.print("[red]Name is required[/red]")
            return

        if self.engine.agent_manager.get_definition(name):
            console.print(f"[red]Agent '{name}' already exists[/red]")
            return

        # 2. Show available models and ask which to use
        console.print()
        console.print("[bold]Available models:[/bold]")
        groups = await self.engine.model.list_models_grouped()
        model_list: list[str] = []
        for provider, models in groups.items():
            console.print(f"  [cyan]{provider.capitalize()}:[/cyan]")
            for m in models:
                model_list.append(m)
                console.print(f"    {len(model_list)}. {m}")

        console.print()
        try:
            model_input = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("  Model (number or name): ").strip()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]Cancelled[/dim]")
            return

        # Resolve model
        if model_input.isdigit():
            idx = int(model_input) - 1
            if 0 <= idx < len(model_list):
                model = model_list[idx]
            else:
                console.print("[red]Invalid model number[/red]")
                return
        else:
            model = model_input

        if not model:
            console.print("[red]Model is required[/red]")
            return

        # 3. Optional: custom system prompt
        try:
            sys_prompt = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input("  System prompt (Enter to skip): ").strip(),
            )
        except (EOFError, KeyboardInterrupt):
            sys_prompt = ""

        # 4. Optional: tool restrictions
        try:
            tools_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input(
                    "  Allowed tools (comma-separated, Enter for all): "
                ).strip(),
            )
        except (EOFError, KeyboardInterrupt):
            tools_input = ""

        allowed_tools = (
            [t.strip() for t in tools_input.split(",") if t.strip()]
            if tools_input
            else []
        )

        # 5. Create and register
        from src.core.types import AgentDefinition

        defn = AgentDefinition(
            name=name,
            model=model,
            system_prompt=sys_prompt,
            allowed_tools=allowed_tools,
            max_tool_rounds=self.config.agents.default_max_tool_rounds,
            created_by="user",
        )
        self.engine.agent_manager.register(defn)

        console.print(
            f"\n[green]Agent '{name}' created with model {model}[/green]"
        )

    def _print_banner(self) -> None:
        """Print the startup banner."""
        model = self.current_model or self.config.models.default
        console.print(
            Panel(
                Text.from_markup(
                    f"[bold cyan]{BANNER}[/bold cyan]\n"
                    f"  [dim]Model:[/dim] [bold]{model}[/bold]\n"
                    f"  [dim]Type /help for commands, /quit to exit[/dim]"
                ),
                border_style="cyan",
            )
        )
