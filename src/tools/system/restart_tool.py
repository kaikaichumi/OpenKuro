"""Tool to launch a preconfigured restart script."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


def _resolve_script_path(context: ToolContext) -> Path | None:
    cfg = getattr(context.config, "restart_tool", None) if context else None
    raw = str(getattr(cfg, "script_path", "") or "").strip()
    if not raw:
        raw = os.environ.get("KURO_RESTART_SCRIPT", "").strip()
    if not raw:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = Path(context.working_directory or os.getcwd())
        path = (base / path).resolve()
    return path


def _resolve_working_dir(context: ToolContext, script_path: Path) -> Path:
    cfg = getattr(context.config, "restart_tool", None) if context else None
    raw = str(getattr(cfg, "working_dir", "") or "").strip()
    if not raw:
        raw = os.environ.get("KURO_RESTART_SCRIPT_CWD", "").strip()
    if not raw:
        return script_path.parent

    cwd = Path(raw).expanduser()
    if not cwd.is_absolute():
        base = Path(context.working_directory or os.getcwd())
        cwd = (base / cwd).resolve()
    return cwd


def _build_command(script_path: Path) -> list[str]:
    suffix = script_path.suffix.lower()
    script_str = str(script_path)

    if os.name == "nt":
        if suffix == ".ps1":
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_str,
            ]
        if suffix in {".bat", ".cmd"}:
            return ["cmd", "/c", script_str]
        return [script_str]

    if suffix == ".sh":
        return ["bash", script_str]
    return [script_str]


class RestartScriptTool(BaseTool):
    """Launch a user-provided restart script in detached mode."""

    name = "run_restart_script"
    description = (
        "Launch a preconfigured restart script (detached) for recovering the service "
        "when it is in a bad state. Requires explicit approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optional reason for the restart request (for logs/audit).",
            },
            "wait_seconds": {
                "type": "integer",
                "description": "Optional delay before launching the script (0-300).",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, only show resolved command/path without executing.",
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.CRITICAL

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        cfg = getattr(context.config, "restart_tool", None) if context else None
        if cfg is not None and not bool(getattr(cfg, "enabled", False)):
            return ToolResult.fail(
                "Restart tool is disabled in config (restart_tool.enabled=false)."
            )

        script_path = _resolve_script_path(context)
        if script_path is None:
            return ToolResult.fail(
                "Restart script is not configured. Set restart_tool.script_path "
                "or env KURO_RESTART_SCRIPT."
            )
        if not script_path.exists() or not script_path.is_file():
            return ToolResult.fail(f"Restart script not found: {script_path}")

        working_dir = _resolve_working_dir(context, script_path)
        if not working_dir.exists() or not working_dir.is_dir():
            return ToolResult.fail(f"Restart working directory not found: {working_dir}")

        wait_seconds_raw = params.get("wait_seconds", 0)
        try:
            wait_seconds = int(wait_seconds_raw)
        except Exception:
            wait_seconds = 0
        wait_seconds = max(0, min(wait_seconds, 300))

        dry_run = bool(params.get("dry_run", False))
        reason = str(params.get("reason", "") or "").strip()
        command = _build_command(script_path)
        command_pretty = " ".join(shlex.quote(p) for p in command)

        if dry_run:
            return ToolResult.ok(
                "Dry run only. Restart script was not executed.\n"
                f"Script: {script_path}\n"
                f"Working dir: {working_dir}\n"
                f"Command: {command_pretty}",
                script_path=str(script_path),
                working_dir=str(working_dir),
                command=command,
                reason=reason,
                dry_run=True,
            )

        if wait_seconds:
            await asyncio.sleep(wait_seconds)

        popen_kwargs: dict[str, Any] = {
            "cwd": str(working_dir),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            if creationflags:
                popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(command, **popen_kwargs)  # noqa: S603
        except Exception as e:
            return ToolResult.fail(f"Failed to launch restart script: {e}")

        msg = [
            "Restart script launched.",
            f"Script: {script_path}",
            f"Working dir: {working_dir}",
            f"Command: {command_pretty}",
            f"PID: {proc.pid}",
        ]
        if reason:
            msg.append(f"Reason: {reason}")
        return ToolResult.ok(
            "\n".join(msg),
            script_path=str(script_path),
            working_dir=str(working_dir),
            command=command,
            pid=proc.pid,
            reason=reason,
            delay_seconds=wait_seconds,
        )
