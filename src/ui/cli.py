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
- `/model <name>` - Switch model (e.g., `/model ollama/llama3.1`)
- `/model` - Show current model
- `/trust <level>` - Set session trust level (low/medium/high)
- `/history` - Show conversation history
- `/clear` - Clear conversation history
- `/quit` or `/exit` - Exit Kuro
"""


class CLIApprovalCallback(ApprovalCallback):
    """CLI-based approval that prompts the user in the terminal."""

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

        elif cmd == "/model":
            if arg:
                self.current_model = arg
                console.print(f"[green]Model switched to: {arg}[/green]")
            else:
                current = self.current_model or self.config.models.default
                console.print(f"[blue]Current model: {current}[/blue]")

        elif cmd == "/trust":
            if arg in ("low", "medium", "high", "critical"):
                self.session.trust_level = arg
                console.print(f"[green]Session trust level set to: {arg.upper()}[/green]")
            else:
                console.print(
                    f"[blue]Current trust level: {self.session.trust_level.upper()}[/blue]\n"
                    f"[dim]Usage: /trust low|medium|high|critical[/dim]"
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
