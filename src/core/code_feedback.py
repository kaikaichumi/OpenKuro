"""Code quality feedback loop: auto-check code after writing.

When enabled, automatically runs linting, type-checking, and/or tests
after file_write operations on code files. Errors are injected back
into the LLM context for auto-fix, creating a "neural reflex arc."
"""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path
from typing import Any

import structlog

from src.config import CodeFeedbackConfig

logger = structlog.get_logger()


class CodeFeedbackLoop:
    """Post-write code quality checking with auto-fix feedback."""

    def __init__(self, config: CodeFeedbackConfig) -> None:
        self.config = config

    def should_check(self, file_path: str) -> bool:
        """Check if this file should trigger a code quality check."""
        if not self.config.enabled:
            return False

        path = Path(file_path)
        return any(
            fnmatch.fnmatch(path.name, pattern)
            for pattern in self.config.file_patterns
        )

    async def post_write_check(self, file_path: str) -> str | None:
        """Run code quality checks on a file after writing.

        Returns error messages (to inject into LLM context) or None if all clear.
        """
        if not self.should_check(file_path):
            return None

        results: list[str] = []

        # Lint check
        if self.config.lint_on_write:
            lint_result = await self._run_lint(file_path)
            if lint_result:
                results.append(f"[Lint Errors]\n{lint_result}")

        # Type check
        if self.config.type_check_on_write:
            type_result = await self._run_type_check(file_path)
            if type_result:
                results.append(f"[Type Errors]\n{type_result}")

        # Test check
        if self.config.test_on_write:
            test_result = await self._run_related_tests(file_path)
            if test_result:
                results.append(f"[Test Failures]\n{test_result}")

        if not results:
            return None

        feedback = (
            f"⚠️ Code quality issues found in {file_path}:\n\n"
            + "\n\n".join(results)
            + "\n\nPlease fix these issues."
        )

        logger.info(
            "code_feedback_issues",
            file=file_path,
            issues=len(results),
        )

        return feedback

    async def _run_lint(self, file_path: str) -> str | None:
        """Run ruff (Python linter) on the file."""
        path = Path(file_path)

        if path.suffix == ".py":
            return await self._exec_cmd(f"ruff check {file_path} --no-fix", timeout=15)
        elif path.suffix in (".ts", ".tsx", ".js", ".jsx"):
            return await self._exec_cmd(f"npx eslint {file_path} --no-fix", timeout=30)

        return None

    async def _run_type_check(self, file_path: str) -> str | None:
        """Run type checker on the file."""
        path = Path(file_path)

        if path.suffix == ".py":
            return await self._exec_cmd(f"pyright {file_path}", timeout=30)
        elif path.suffix in (".ts", ".tsx"):
            return await self._exec_cmd(f"npx tsc --noEmit {file_path}", timeout=30)

        return None

    async def _run_related_tests(self, file_path: str) -> str | None:
        """Find and run tests related to the modified file."""
        path = Path(file_path)

        if path.suffix != ".py":
            return None

        # Common test file patterns
        test_candidates = [
            path.parent / f"test_{path.name}",
            path.parent / "tests" / f"test_{path.name}",
            path.parent.parent / "tests" / f"test_{path.name}",
        ]

        for test_file in test_candidates:
            if test_file.exists():
                return await self._exec_cmd(f"pytest {test_file} -x --tb=short", timeout=60)

        return None

    async def _exec_cmd(self, cmd: str, timeout: int = 30) -> str | None:
        """Execute a shell command and return output if there are errors.

        Returns None if the command succeeds (exit code 0).
        Returns the stderr+stdout if it fails.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            if proc.returncode != 0:
                output = (stderr or b"").decode("utf-8", errors="replace")
                if not output:
                    output = (stdout or b"").decode("utf-8", errors="replace")
                # Truncate long outputs
                if len(output) > 3000:
                    output = output[:3000] + "\n... (truncated)"
                return output

        except asyncio.TimeoutError:
            logger.warning("code_feedback_timeout", cmd=cmd[:50])
            return None
        except FileNotFoundError:
            # Tool not installed — silently skip
            logger.debug("code_feedback_tool_not_found", cmd=cmd.split()[0])
            return None
        except Exception as e:
            logger.debug("code_feedback_exec_error", error=str(e))
            return None

        return None
