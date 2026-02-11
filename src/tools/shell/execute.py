"""Shell execution tool: run commands with sandbox protection."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


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

        if not command:
            return ToolResult.fail("Command is required")

        # Resolve working directory
        if working_dir:
            cwd = os.path.expanduser(working_dir)
        elif context.working_directory:
            cwd = context.working_directory
        else:
            cwd = os.path.expanduser("~")

        if not os.path.isdir(cwd):
            return ToolResult.fail(f"Working directory not found: {cwd}")

        try:
            # Choose shell based on platform
            if sys.platform == "win32":
                # Use PowerShell on Windows
                shell_cmd = ["powershell", "-NoProfile", "-Command", command]
                proc = await asyncio.create_subprocess_exec(
                    *shell_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    creationflags=getattr(asyncio, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
                )
            else:
                # Use shell on Unix
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
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
                )

            return ToolResult.ok(output, exit_code=0)

        except FileNotFoundError:
            return ToolResult.fail(
                "Shell not found. Ensure PowerShell (Windows) or bash (Unix) is available."
            )
        except Exception as e:
            return ToolResult.fail(f"Execution error: {e}")
