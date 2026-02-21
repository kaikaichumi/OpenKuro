"""Update tools for the LLM to check and perform Kuro updates."""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class CheckUpdateTool(BaseTool):
    """Check if a newer version of Kuro is available."""

    name = "check_update"
    description = (
        "Check if a newer version of Kuro is available on GitHub. "
        "Returns update status, commits behind, and recent changes."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        from src.core.updater import Updater

        updater = Updater()
        if not updater.is_git_repo():
            return ToolResult.error("Not a git repository. Cannot check for updates.")

        info = await updater.check_for_updates()
        if info is None:
            return ToolResult.error(
                "Failed to check for updates. Network might be unavailable."
            )

        if not info.has_update:
            return ToolResult.ok(
                f"Already up to date (commit: {info.current_hash})."
            )

        lines = [
            f"Update available!",
            f"  Current: {info.current_hash}",
            f"  Latest:  {info.remote_hash}",
            f"  Behind:  {info.commits_behind} commit(s)",
        ]
        if info.summary:
            lines.append(f"\nRecent changes:\n{info.summary}")
        lines.append("\nUse perform_update tool to update.")

        return ToolResult.ok("\n".join(lines))


class PerformUpdateTool(BaseTool):
    """Update Kuro to the latest version from GitHub."""

    name = "perform_update"
    description = (
        "Update Kuro to the latest version by pulling from GitHub. "
        "This will run git pull and install new dependencies. "
        "A restart is required after updating."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.HIGH

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        from src.core.updater import Updater

        updater = Updater()
        if not updater.is_git_repo():
            return ToolResult.error("Not a git repository. Cannot update.")

        result = await updater.perform_update()

        if result.success:
            msg = f"✅ Updated successfully!\n\n{result.message}"
            if result.needs_restart:
                msg += "\n\n⚠️ Please restart Kuro for the update to take effect."
            return ToolResult.ok(msg)
        else:
            return ToolResult.error(f"Update failed: {result.message}")


class VersionTool(BaseTool):
    """Show current Kuro version."""

    name = "get_version"
    description = "Show the current version of Kuro, including git commit hash."
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        from src import __version__
        from src.core.updater import Updater

        updater = Updater()
        lines = [f"Kuro v{__version__}"]

        if updater.is_git_repo():
            hash_ = await updater.get_current_hash()
            branch = await updater.get_branch()
            lines.append(f"Git: {branch} @ {hash_ or 'unknown'}")
        else:
            lines.append("Not a git repository")

        return ToolResult.ok("\n".join(lines))
