"""Tool to launch a preconfigured restart script."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


def _resolve_script_path(
    context: ToolContext,
    *,
    config_key: str,
    env_var: str,
) -> Path | None:
    cfg = getattr(context.config, "restart_tool", None) if context else None
    raw = str(getattr(cfg, config_key, "") or "").strip()
    if not raw:
        raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = Path(context.working_directory or os.getcwd())
        path = (base / path).resolve()
    return path


def _resolve_working_dir(
    context: ToolContext,
    script_path: Path,
    *,
    config_key: str,
    env_var: str,
) -> Path:
    cfg = getattr(context.config, "restart_tool", None) if context else None
    raw = str(getattr(cfg, config_key, "") or "").strip()
    if not raw:
        raw = os.environ.get(env_var, "").strip()
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
        "when it is in a bad state. Can optionally run stop-then-start sequence "
        "with a delay. Requires explicit approval."
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
            "start_after_stop": {
                "type": "boolean",
                "description": (
                    "If true, launch start script after primary script. "
                    "Defaults to true only when a start script is configured."
                ),
            },
            "restart_delay_seconds": {
                "type": "integer",
                "description": (
                    "Delay between primary script and start script (0-300). "
                    "Defaults to restart_tool.restart_delay_seconds."
                ),
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

        stop_script = _resolve_script_path(
            context,
            config_key="script_path",
            env_var="KURO_RESTART_SCRIPT",
        )
        if stop_script is None:
            return ToolResult.fail(
                "Restart script is not configured. Set restart_tool.script_path "
                "or env KURO_RESTART_SCRIPT."
            )
        if not stop_script.exists() or not stop_script.is_file():
            return ToolResult.fail(f"Restart script not found: {stop_script}")

        stop_working_dir = _resolve_working_dir(
            context,
            stop_script,
            config_key="working_dir",
            env_var="KURO_RESTART_SCRIPT_CWD",
        )
        if not stop_working_dir.exists() or not stop_working_dir.is_dir():
            return ToolResult.fail(f"Restart working directory not found: {stop_working_dir}")

        start_script = _resolve_script_path(
            context,
            config_key="start_script_path",
            env_var="KURO_RESTART_START_SCRIPT",
        )
        if start_script is not None and (not start_script.exists() or not start_script.is_file()):
            return ToolResult.fail(f"Start script not found: {start_script}")
        start_working_dir: Path | None = None
        if start_script is not None:
            start_working_dir = _resolve_working_dir(
                context,
                start_script,
                config_key="start_working_dir",
                env_var="KURO_RESTART_START_SCRIPT_CWD",
            )
            if not start_working_dir.exists() or not start_working_dir.is_dir():
                return ToolResult.fail(f"Start working directory not found: {start_working_dir}")

        wait_seconds_raw = params.get("wait_seconds", 0)
        try:
            wait_seconds = int(wait_seconds_raw)
        except Exception:
            wait_seconds = 0
        wait_seconds = max(0, min(wait_seconds, 300))

        if "restart_delay_seconds" in params:
            restart_delay_raw = params.get("restart_delay_seconds", 0)
        else:
            restart_delay_raw = getattr(cfg, "restart_delay_seconds", 3) if cfg else 3
        try:
            restart_delay_seconds = int(restart_delay_raw)
        except Exception:
            restart_delay_seconds = 3
        restart_delay_seconds = max(0, min(restart_delay_seconds, 300))

        if "start_after_stop" in params:
            start_after_stop = bool(params.get("start_after_stop"))
        else:
            start_after_stop = start_script is not None
        if start_after_stop and start_script is None:
            return ToolResult.fail(
                "start_after_stop=true but no start script configured. "
                "Set restart_tool.start_script_path or env KURO_RESTART_START_SCRIPT."
            )

        dry_run = bool(params.get("dry_run", False))
        reason = str(params.get("reason", "") or "").strip()
        stop_command = _build_command(stop_script)
        stop_command_pretty = " ".join(shlex.quote(p) for p in stop_command)
        start_command: list[str] | None = _build_command(start_script) if start_script else None
        start_command_pretty = (
            " ".join(shlex.quote(p) for p in start_command)
            if start_command
            else ""
        )

        if dry_run:
            lines = [
                "Dry run only. Restart script was not executed.\n"
                f"Stop script: {stop_script}\n"
                f"Stop working dir: {stop_working_dir}\n"
                f"Stop command: {stop_command_pretty}",
            ]
            if start_after_stop and start_script and start_working_dir and start_command_pretty:
                lines.extend([
                    f"Start script: {start_script}",
                    f"Start working dir: {start_working_dir}",
                    f"Start command: {start_command_pretty}",
                    f"Restart delay: {restart_delay_seconds}s",
                ])
            return ToolResult.ok(
                "\n".join(lines),
                script_path=str(stop_script),
                working_dir=str(stop_working_dir),
                command=stop_command,
                start_script_path=str(start_script) if start_script else "",
                start_working_dir=str(start_working_dir) if start_working_dir else "",
                start_command=start_command or [],
                restart_delay_seconds=restart_delay_seconds,
                start_after_stop=start_after_stop,
                reason=reason,
                dry_run=True,
            )

        def _launch(command: list[str], cwd: Path) -> tuple[int | None, str | None]:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(cwd),
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
                return proc.pid, None
            except Exception as e:  # pragma: no cover - platform dependent
                return None, str(e)

        if wait_seconds:
            await asyncio.sleep(wait_seconds)

        stop_pid, stop_err = _launch(stop_command, stop_working_dir)
        if stop_err:
            return ToolResult.fail(f"Failed to launch restart script: {stop_err}")

        start_pid: int | None = None
        if start_after_stop and start_script and start_working_dir and start_command:
            if restart_delay_seconds:
                await asyncio.sleep(restart_delay_seconds)
            start_pid, start_err = _launch(start_command, start_working_dir)
            if start_err:
                return ToolResult.fail(
                    "Stop script launched but start script failed: "
                    f"{start_err}"
                )

        msg = [
            "Restart sequence launched." if start_pid is not None else "Restart script launched.",
            f"Stop script: {stop_script}",
            f"Stop working dir: {stop_working_dir}",
            f"Stop command: {stop_command_pretty}",
            f"Stop PID: {stop_pid}",
        ]
        if start_pid is not None and start_script and start_working_dir and start_command_pretty:
            msg.extend([
                f"Start script: {start_script}",
                f"Start working dir: {start_working_dir}",
                f"Start command: {start_command_pretty}",
                f"Start PID: {start_pid}",
                f"Restart delay: {restart_delay_seconds}s",
            ])
        if reason:
            msg.append(f"Reason: {reason}")
        return ToolResult.ok(
            "\n".join(msg),
            script_path=str(stop_script),
            working_dir=str(stop_working_dir),
            command=stop_command,
            pid=stop_pid,
            start_script_path=str(start_script) if start_script else "",
            start_working_dir=str(start_working_dir) if start_working_dir else "",
            start_command=start_command or [],
            start_pid=start_pid,
            start_after_stop=start_after_stop,
            reason=reason,
            delay_seconds=wait_seconds,
            restart_delay_seconds=restart_delay_seconds,
        )
