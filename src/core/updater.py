"""Self-update mechanism for Kuro.

Checks for updates via git remote and performs in-place updates
using `git pull` + `poetry install`. User configuration in ~/.kuro/
is completely separate from the code directory, so updates are safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from src import __version__

logger = logging.getLogger(__name__)


@dataclass
class UpdateInfo:
    """Information about available updates."""

    has_update: bool
    current_hash: str
    remote_hash: str
    commits_behind: int
    summary: str  # Recent commit messages


@dataclass
class UpdateResult:
    """Result of an update operation."""

    success: bool
    old_version: str
    new_version: str
    message: str
    needs_restart: bool


class Updater:
    """Manages Kuro version checking and updates.

    Uses git to check for and pull updates from the remote repository.
    Does NOT require GitHub releases — works with any git remote.
    """

    def __init__(self, repo_dir: Path | None = None) -> None:
        self._repo_dir = repo_dir or Path(__file__).parent.parent.parent

    def get_current_version(self) -> str:
        """Get the current version string."""
        return __version__

    def is_git_repo(self) -> bool:
        """Check if the code directory is a git repository."""
        return (self._repo_dir / ".git").is_dir()

    async def _run_git(self, *args: str) -> tuple[int, str]:
        """Run a git command and return (returncode, stdout)."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self._repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and stderr:
            output += "\n" + stderr.decode("utf-8", errors="replace").strip()
        return proc.returncode, output

    async def get_current_hash(self) -> str | None:
        """Get the current git commit hash."""
        rc, output = await self._run_git("rev-parse", "--short", "HEAD")
        return output if rc == 0 else None

    async def get_branch(self) -> str:
        """Get the current git branch name."""
        rc, output = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        return output if rc == 0 else "unknown"

    async def check_for_updates(self) -> UpdateInfo | None:
        """Check if there are newer commits on the remote.

        Returns UpdateInfo or None if check fails.
        """
        if not self.is_git_repo():
            return None

        try:
            # Fetch remote info without downloading objects
            rc, _ = await self._run_git("fetch", "--dry-run", "origin")
            if rc != 0:
                # Try actual fetch (dry-run may not be supported)
                rc, _ = await self._run_git("fetch", "origin")
                if rc != 0:
                    return None

            # Get current branch
            branch = await self.get_branch()

            # Get local and remote hashes
            rc_local, local_hash = await self._run_git("rev-parse", "HEAD")
            rc_remote, remote_hash = await self._run_git(
                "rev-parse", f"origin/{branch}"
            )

            if rc_local != 0 or rc_remote != 0:
                return None

            local_hash = local_hash.strip()
            remote_hash = remote_hash.strip()

            if local_hash == remote_hash:
                return UpdateInfo(
                    has_update=False,
                    current_hash=local_hash[:8],
                    remote_hash=remote_hash[:8],
                    commits_behind=0,
                    summary="Already up to date.",
                )

            # Count commits behind
            rc, count_output = await self._run_git(
                "rev-list", "--count", f"HEAD..origin/{branch}"
            )
            commits_behind = int(count_output) if rc == 0 else 0

            # Get recent commit messages from remote
            rc, log_output = await self._run_git(
                "log", "--oneline", f"HEAD..origin/{branch}", "-10"
            )
            summary = log_output if rc == 0 else ""

            return UpdateInfo(
                has_update=True,
                current_hash=local_hash[:8],
                remote_hash=remote_hash[:8],
                commits_behind=commits_behind,
                summary=summary,
            )

        except Exception as e:
            logger.error("update_check_failed", error=str(e))
            return None

    async def perform_update(self) -> UpdateResult:
        """Perform the update: git pull + poetry install if needed.

        Steps:
        1. Stash any local changes
        2. Pull latest from remote
        3. Install new dependencies (if poetry.lock changed)
        4. Pop stash if needed
        """
        if not self.is_git_repo():
            return UpdateResult(
                success=False,
                old_version=self.get_current_version(),
                new_version=self.get_current_version(),
                message="Not a git repository. Cannot update.",
                needs_restart=False,
            )

        old_version = self.get_current_version()
        old_hash = await self.get_current_hash() or "unknown"
        branch = await self.get_branch()

        # Check for local modifications
        rc, status = await self._run_git("status", "--porcelain")
        has_local_changes = bool(status.strip()) if rc == 0 else False

        stashed = False
        if has_local_changes:
            rc, _ = await self._run_git("stash", "push", "-m", "kuro-auto-update")
            stashed = rc == 0

        try:
            # Pull latest
            rc, pull_output = await self._run_git("pull", "origin", branch)
            if rc != 0:
                return UpdateResult(
                    success=False,
                    old_version=old_version,
                    new_version=old_version,
                    message=f"git pull failed:\n{pull_output}",
                    needs_restart=False,
                )

            # Check if poetry.lock changed
            rc, diff = await self._run_git(
                "diff", "--name-only", f"{old_hash}..HEAD", "--", "poetry.lock"
            )
            lock_changed = bool(diff.strip()) if rc == 0 else False

            install_msg = ""
            if lock_changed:
                # Run poetry install for new dependencies
                proc = await asyncio.create_subprocess_exec(
                    "poetry", "install",
                    cwd=str(self._repo_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    install_msg = "\nDependencies updated."
                else:
                    install_msg = (
                        "\nWarning: poetry install had issues. "
                        "Run `poetry install` manually if needed."
                    )

            new_hash = await self.get_current_hash() or "unknown"

            # Re-read version (it may have changed)
            # We reload from pyproject.toml since __version__ is cached in memory
            new_version = self._read_version_from_pyproject() or old_version

            return UpdateResult(
                success=True,
                old_version=old_version,
                new_version=new_version,
                message=(
                    f"Updated from {old_hash} to {new_hash}.\n"
                    f"{pull_output}{install_msg}"
                ),
                needs_restart=True,
            )

        finally:
            # Pop stash if we stashed
            if stashed:
                await self._run_git("stash", "pop")

    def _read_version_from_pyproject(self) -> str | None:
        """Read version from pyproject.toml."""
        pyproject = self._repo_dir / "pyproject.toml"
        if not pyproject.exists():
            return None
        try:
            content = pyproject.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.strip().startswith("version"):
                    # Parse: version = "X.Y.Z"
                    _, _, value = line.partition("=")
                    return value.strip().strip('"').strip("'")
        except Exception:
            pass
        return None
