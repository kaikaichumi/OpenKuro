"""Shell execution tool: run commands with sandbox protection."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

_SUDO_RE = re.compile(r"(^|[;&|]\s*)sudo\b", re.IGNORECASE)
_NETWORK_RE = re.compile(
    r"\b(curl|wget|invoke-webrequest|iwr|httpie|nc|ncat|telnet|ssh|scp|sftp|ftp|ping|nmap|nslookup|dig)\b",
    re.IGNORECASE,
)
_WRITE_RE = re.compile(
    r"\b("
    r"rm|rmdir|del|erase|"
    r"move-item|mv|rename-item|ren|"
    r"copy-item|cp|tee|out-file|set-content|add-content|"
    r"touch|mkdir|new-item|truncate|sed\s+-i|chmod|chown|"
    r"pip(?:3)?\s+install|apt(?:-get)?\s+install|yum\s+install|dnf\s+install|"
    r"npm\s+(?:install|i)\b|pnpm\s+add|yarn\s+add"
    r")\b",
    re.IGNORECASE,
)
_SPAWN_RE = re.compile(
    r"\b(start-process|nohup|screen|tmux|start\s+\"|cmd\s+/c\s+start)\b",
    re.IGNORECASE,
)
_REDIRECT_RE = re.compile(r"(?:^|[^<])>{1,2}(?![&])|<<|2>|1>|&>", re.IGNORECASE)


def _is_within_allowed(path: str, allowed_dirs: list[str]) -> bool:
    """Best-effort path containment check (resolves symlinks if possible)."""
    if not allowed_dirs:
        return True
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        target = Path(path).expanduser()
    for root_raw in allowed_dirs:
        root_s = str(root_raw or "").strip()
        if not root_s:
            continue
        try:
            root = Path(root_s).expanduser().resolve()
        except Exception:
            root = Path(root_s).expanduser()
        with_context = False
        try:
            target.relative_to(root)
            with_context = True
        except Exception:
            with_context = False
        if with_context:
            return True
    return False


class ShellExecuteTool(BaseTool):
    """Execute a shell command and return its output."""

    name = "shell_execute"
    description = (
        "Execute a shell command and return its stdout/stderr output. "
        "On Windows this uses PowerShell, on Linux/macOS it uses bash. "
        "Commands are subject to sandbox restrictions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "working_directory": {
                "type": "string",
                "description": "Working directory for the command (default: user home)",
            },
        },
        "required": ["command"],
    }
    risk_level = RiskLevel.HIGH

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        command = params.get("command", "")
        working_dir = params.get("working_directory")
        isolated_cfg = getattr(getattr(context, "config", None), "isolated_runner", None)
        isolation_tier = str(getattr(context, "isolation_tier", "standard")).strip().lower()
        isolated_mode = bool(
            isolated_cfg is not None
            and bool(getattr(isolated_cfg, "enabled", False))
            and (
                isolation_tier in {"restricted", "sealed"}
                or isinstance(context.isolated_env, dict)
            )
        )

        if not command:
            return ToolResult.fail("Command is required")

        if (
            isolated_mode
            and bool(getattr(isolated_cfg, "disallow_working_directory_override", True))
            and str(working_dir or "").strip()
        ):
            return ToolResult.fail(
                "working_directory override is disabled in isolated runner mode"
            )

        # Never allow interactive privilege escalation from agent shell runs.
        # It can block on password prompts and stall the whole request.
        if sys.platform != "win32" and _SUDO_RE.search(command or ""):
            return ToolResult.fail(
                "sudo commands are not executed by the agent. "
                "Please run this command manually on the host terminal."
            )

        # Resolve working directory
        if working_dir:
            cwd = os.path.expanduser(working_dir)
        elif context.working_directory:
            cwd = context.working_directory
        else:
            cwd = os.path.expanduser("~")

        if not os.path.isdir(cwd):
            return ToolResult.fail(f"Working directory not found: {cwd}")

        hard_mode = bool(
            isolated_mode
            and isolated_cfg is not None
            and bool(getattr(isolated_cfg, "hard_mode", False))
        )
        hard_external_sandbox_prefix: list[str] = []
        hard_external_sandbox_required = False
        if hard_mode:
            allow_prefixes = [
                str(v).strip().lower()
                for v in (getattr(isolated_cfg, "hard_allow_command_prefixes", []) or [])
                if str(v).strip()
            ]
            normalized = str(command or "").strip().lower()
            if allow_prefixes:
                if not any(
                    normalized.startswith(prefix)
                    for prefix in allow_prefixes
                ):
                    return ToolResult.fail(
                        "isolated hard mode blocked command: not in allowlist prefixes"
                    )

            if bool(getattr(isolated_cfg, "hard_restrict_to_allowed_directories", True)):
                allowed_dirs = [str(v) for v in (context.allowed_directories or []) if str(v or "").strip()]
                if allowed_dirs and not _is_within_allowed(cwd, allowed_dirs):
                    return ToolResult.fail(
                        "isolated hard mode blocked command: working directory outside allowed_directories"
                    )

            if bool(getattr(isolated_cfg, "hard_block_network_commands", True)) and _NETWORK_RE.search(command):
                return ToolResult.fail(
                    "isolated hard mode blocked command: network-related command detected"
                )
            if bool(getattr(isolated_cfg, "hard_block_write_commands", True)) and _WRITE_RE.search(command):
                return ToolResult.fail(
                    "isolated hard mode blocked command: write/mutation command detected"
                )
            if bool(getattr(isolated_cfg, "hard_block_process_spawn", True)) and _SPAWN_RE.search(command):
                return ToolResult.fail(
                    "isolated hard mode blocked command: process-spawn pattern detected"
                )
            if bool(getattr(isolated_cfg, "hard_block_shell_redirects", True)) and _REDIRECT_RE.search(command):
                return ToolResult.fail(
                    "isolated hard mode blocked command: shell redirection detected"
                )

            hard_external_sandbox_prefix = [
                str(v).strip()
                for v in (getattr(isolated_cfg, "hard_external_sandbox_prefix", []) or [])
                if str(v).strip()
            ]
            hard_external_sandbox_required = bool(
                getattr(isolated_cfg, "hard_external_sandbox_required", False)
            )
            if hard_external_sandbox_required and not hard_external_sandbox_prefix:
                return ToolResult.fail(
                    "isolated hard mode requires external sandbox, but no hard_external_sandbox_prefix is configured"
                )
            if hard_external_sandbox_prefix:
                runner = hard_external_sandbox_prefix[0]
                if not shutil.which(runner):
                    return ToolResult.fail(
                        f"isolated hard mode external sandbox runner not found: {runner}"
                    )

        env_map = context.isolated_env if isinstance(context.isolated_env, dict) else None
        execution_backend = "native"

        try:
            if hard_external_sandbox_prefix:
                execution_backend = "external_sandbox"
                if sys.platform == "win32":
                    shell_cmd = [
                        *hard_external_sandbox_prefix,
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        command,
                    ]
                else:
                    shell_cmd = [
                        *hard_external_sandbox_prefix,
                        "bash",
                        "-lc",
                        command,
                    ]
                exec_kwargs: dict[str, Any] = {
                    "stdin": asyncio.subprocess.DEVNULL,
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "cwd": cwd,
                    "env": env_map,
                }
                if sys.platform == "win32":
                    exec_kwargs["creationflags"] = getattr(
                        asyncio,
                        "CREATE_NEW_PROCESS_GROUP",
                        0x00000200,
                    )
                proc = await asyncio.create_subprocess_exec(
                    *shell_cmd,
                    **exec_kwargs,
                )
            elif sys.platform == "win32":
                # Use PowerShell on Windows
                shell_cmd = ["powershell", "-NoProfile", "-Command", command]
                proc = await asyncio.create_subprocess_exec(
                    *shell_cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env_map,
                    creationflags=getattr(asyncio, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
                )
            else:
                # Use shell on Unix
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env_map,
                )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=context.max_execution_time,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult.fail(
                    f"Command timed out after {context.max_execution_time} seconds"
                )

            # Decode output
            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

            # Truncate if needed
            max_size = context.max_output_size
            if len(stdout_str) > max_size:
                stdout_str = stdout_str[:max_size] + "\n... (output truncated)"
            if len(stderr_str) > max_size // 4:
                stderr_str = stderr_str[: max_size // 4] + "\n... (stderr truncated)"

            # Build output
            output_parts = []
            if stdout_str:
                output_parts.append(stdout_str)
            if stderr_str:
                output_parts.append(f"[STDERR]\n{stderr_str}")

            output = "\n".join(output_parts) or "(no output)"
            exit_code = proc.returncode or 0

            if exit_code != 0:
                return ToolResult.ok(
                    f"[Exit code: {exit_code}]\n{output}",
                    exit_code=exit_code,
                    execution_backend=execution_backend,
                )

            return ToolResult.ok(output, exit_code=0, execution_backend=execution_backend)

        except FileNotFoundError:
            return ToolResult.fail(
                "Shell not found. Ensure PowerShell (Windows) or bash (Unix) is available."
            )
        except Exception as e:
            return ToolResult.fail(f"Execution error: {e}")
