"""Phase 4 baseline tests: isolated runner shell profile."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.config import IsolatedRunnerConfig
from src.tools.base import ToolContext
from src.tools.shell.execute import ShellExecuteTool


class _DummyProc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"ok\n", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def test_isolated_runner_config_normalization() -> None:
    cfg = IsolatedRunnerConfig(
        tools=[" shell_execute ", "", "computer_use"],
        enforce_tiers=["Restricted", "sealed", "invalid"],
        max_execution_time_seconds=1,
        hard_allow_command_prefixes=["ls", " ls ", "DIR", ""],
        hard_external_sandbox_prefix=["firejail", " firejail ", "--quiet", ""],
    )
    assert cfg.tools == ["shell_execute", "computer_use"]
    assert cfg.enforce_tiers == ["restricted", "sealed"]
    assert cfg.max_execution_time_seconds >= 5
    assert cfg.hard_allow_command_prefixes == ["ls", "DIR"]
    assert cfg.hard_external_sandbox_prefix == ["firejail", "--quiet"]


def test_shell_execute_uses_isolated_env(tmp_path) -> None:
    tool = ShellExecuteTool()
    isolated_env = {"PATH": "/usr/bin"}
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
        ),
    )
    context = ToolContext(
        session_id="s-iso",
        config=cfg,
        working_directory=str(tmp_path),
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="sealed",
        isolated_env=isolated_env,
    )

    exec_mock = AsyncMock(return_value=_DummyProc())
    shell_mock = AsyncMock(return_value=_DummyProc())
    async def _run():
        with (
            patch("asyncio.create_subprocess_exec", exec_mock),
            patch("asyncio.create_subprocess_shell", shell_mock),
        ):
            return await tool.execute({"command": "echo hello"}, context)

    result = asyncio.run(_run())

    assert result.success is True
    if exec_mock.await_count > 0:
        called_kwargs = exec_mock.call_args.kwargs
    else:
        called_kwargs = shell_mock.call_args.kwargs
    assert called_kwargs.get("env") == isolated_env


def test_shell_execute_blocks_workdir_override_in_isolated_mode(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
        ),
    )
    context = ToolContext(
        session_id="s-iso-lock",
        config=cfg,
        working_directory=str(tmp_path),
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    async def _run():
        return await tool.execute(
            {
                "command": "echo hello",
                "working_directory": str(tmp_path),
            },
            context,
        )

    result = asyncio.run(_run())
    assert result.success is False
    assert "working_directory override" in str(result.error or "").lower()


def test_shell_execute_hard_mode_blocks_network_commands(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
            hard_mode=True,
            hard_block_network_commands=True,
            hard_block_write_commands=False,
            hard_block_process_spawn=False,
            hard_block_shell_redirects=False,
            hard_allow_command_prefixes=[],
            hard_restrict_to_allowed_directories=False,
        ),
    )
    context = ToolContext(
        session_id="s-iso-net",
        config=cfg,
        working_directory=str(tmp_path),
        allowed_directories=[str(tmp_path)],
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    async def _run():
        return await tool.execute({"command": "curl https://example.com"}, context)

    result = asyncio.run(_run())
    assert result.success is False
    assert "network-related" in str(result.error or "").lower()


def test_shell_execute_hard_mode_blocks_redirects(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
            hard_mode=True,
            hard_block_network_commands=False,
            hard_block_write_commands=False,
            hard_block_process_spawn=False,
            hard_block_shell_redirects=True,
            hard_allow_command_prefixes=[],
            hard_restrict_to_allowed_directories=False,
        ),
    )
    context = ToolContext(
        session_id="s-iso-redir",
        config=cfg,
        working_directory=str(tmp_path),
        allowed_directories=[str(tmp_path)],
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    async def _run():
        return await tool.execute({"command": "echo hi > out.txt"}, context)

    result = asyncio.run(_run())
    assert result.success is False
    assert "redirection" in str(result.error or "").lower()


def test_shell_execute_hard_mode_allow_prefixes_enforced(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
            hard_mode=True,
            hard_block_network_commands=False,
            hard_block_write_commands=False,
            hard_block_process_spawn=False,
            hard_block_shell_redirects=False,
            hard_allow_command_prefixes=["ls", "dir"],
            hard_restrict_to_allowed_directories=False,
        ),
    )
    context = ToolContext(
        session_id="s-iso-allow",
        config=cfg,
        working_directory=str(tmp_path),
        allowed_directories=[str(tmp_path)],
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    async def _run():
        return await tool.execute({"command": "python -V"}, context)

    result = asyncio.run(_run())
    assert result.success is False
    assert "allowlist prefixes" in str(result.error or "").lower()


def test_shell_execute_hard_mode_requires_external_sandbox_prefix(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
            hard_mode=True,
            hard_block_network_commands=False,
            hard_block_write_commands=False,
            hard_block_process_spawn=False,
            hard_block_shell_redirects=False,
            hard_allow_command_prefixes=[],
            hard_restrict_to_allowed_directories=False,
            hard_external_sandbox_required=True,
            hard_external_sandbox_prefix=[],
        ),
    )
    context = ToolContext(
        session_id="s-iso-sandbox-required",
        config=cfg,
        working_directory=str(tmp_path),
        allowed_directories=[str(tmp_path)],
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    async def _run():
        return await tool.execute({"command": "echo hello"}, context)

    result = asyncio.run(_run())
    assert result.success is False
    assert "requires external sandbox" in str(result.error or "").lower()


def test_shell_execute_hard_mode_external_sandbox_runner_missing(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
            hard_mode=True,
            hard_block_network_commands=False,
            hard_block_write_commands=False,
            hard_block_process_spawn=False,
            hard_block_shell_redirects=False,
            hard_allow_command_prefixes=[],
            hard_restrict_to_allowed_directories=False,
            hard_external_sandbox_required=False,
            hard_external_sandbox_prefix=["__missing_runner__"],
        ),
    )
    context = ToolContext(
        session_id="s-iso-sandbox-missing",
        config=cfg,
        working_directory=str(tmp_path),
        allowed_directories=[str(tmp_path)],
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    async def _run():
        with patch("shutil.which", return_value=None):
            return await tool.execute({"command": "echo hello"}, context)

    result = asyncio.run(_run())
    assert result.success is False
    assert "runner not found" in str(result.error or "").lower()


def test_shell_execute_hard_mode_external_sandbox_executes_with_prefix(tmp_path) -> None:
    tool = ShellExecuteTool()
    cfg = SimpleNamespace(
        isolated_runner=SimpleNamespace(
            enabled=True,
            disallow_working_directory_override=True,
            hard_mode=True,
            hard_block_network_commands=False,
            hard_block_write_commands=False,
            hard_block_process_spawn=False,
            hard_block_shell_redirects=False,
            hard_allow_command_prefixes=[],
            hard_restrict_to_allowed_directories=False,
            hard_external_sandbox_required=True,
            hard_external_sandbox_prefix=["sandboxctl", "--strict"],
        ),
    )
    context = ToolContext(
        session_id="s-iso-sandbox-exec",
        config=cfg,
        working_directory=str(tmp_path),
        allowed_directories=[str(tmp_path)],
        max_execution_time=10,
        max_output_size=10_000,
        isolation_tier="restricted",
        isolated_env={"PATH": "/usr/bin"},
    )

    exec_mock = AsyncMock(return_value=_DummyProc(stdout=b"sandboxed\n"))
    shell_mock = AsyncMock(return_value=_DummyProc(stdout=b"native\n"))

    async def _run():
        with (
            patch("shutil.which", return_value="sandboxctl"),
            patch("asyncio.create_subprocess_exec", exec_mock),
            patch("asyncio.create_subprocess_shell", shell_mock),
        ):
            return await tool.execute({"command": "echo hello"}, context)

    result = asyncio.run(_run())
    assert result.success is True
    assert result.data.get("execution_backend") == "external_sandbox"
    assert exec_mock.await_count == 1
    assert shell_mock.await_count == 0
    cmd = list(exec_mock.call_args.args)
    assert cmd[:2] == ["sandboxctl", "--strict"]
    if sys.platform == "win32":
        assert cmd[2:5] == ["powershell", "-NoProfile", "-Command"]
    else:
        assert cmd[2:4] == ["bash", "-lc"]
