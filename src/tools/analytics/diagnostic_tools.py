"""LLM-callable diagnostic tools for self-debugging and auto-repair.

These tools let the LLM introspect Kuro's internal state when things go
wrong — query recent errors, inspect session health, profile performance,
and trigger self-repair.  All tools are LOW risk and auto-approved.

Configurable via config.yaml:
  diagnostics:
    enabled: true
    auto_diagnose_on_error: true
    error_threshold: 3
    repair_model: "main"           # "main" or custom model name
    include_in_agents: true
    only_matching_model: false
    enabled_tools:
      - debug_recent_errors
      - debug_session_info
      - debug_performance
      - diagnose_and_repair

Usage scenario:
  User: "Why did the last operation fail?"
  LLM → calls debug_recent_errors → sees the actual error details
  LLM → explains the root cause to user

  User: "Fix the system"
  LLM → calls diagnose_and_repair → runs full diagnostic scan → suggests fixes
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import get_kuro_home
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


# ---------------------------------------------------------------------------
# Helper: check if a diagnostic tool is enabled in config
# ---------------------------------------------------------------------------


def _is_tool_enabled(tool_name: str, context: ToolContext | None = None) -> bool:
    """Check if a specific diagnostic tool is enabled in the config.

    Falls back to True if config is not accessible (backward compatibility).
    """
    try:
        config = getattr(context, "config", None) if context else None
        if config is None:
            return True  # No config → allow (backward compat)

        diag = getattr(config, "diagnostics", None)
        if diag is None:
            return True  # No diagnostics config → allow

        if not diag.enabled:
            return False

        if diag.enabled_tools and tool_name not in diag.enabled_tools:
            return False

        return True
    except Exception:
        return True  # If anything fails, default to allowing


# ---------------------------------------------------------------------------
# Tool 1: Recent Errors
# ---------------------------------------------------------------------------


class DebugRecentErrorsTool(BaseTool):
    """Query recent tool failures and errors from action logs."""

    name = "debug_recent_errors"
    description = (
        "Query the most recent tool execution errors and failures. "
        "Returns tool name, parameters, error message, and timestamp. "
        "Use this when something went wrong and you need to diagnose why. "
        "Also useful to check if a recurring error pattern exists."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of errors to return (default: 10)",
                "minimum": 1,
                "maximum": 50,
            },
            "tool_name": {
                "type": "string",
                "description": (
                    "Filter by specific tool name (e.g., 'shell_execute', "
                    "'mouse_action'). Leave empty for all tools."
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if not _is_tool_enabled(self.name, context):
            return ToolResult.fail(
                "[Diagnostics] debug_recent_errors is disabled in config. "
                "Enable it via diagnostics.enabled_tools in config.yaml."
            )

        limit = params.get("limit", 10)
        tool_filter = params.get("tool_name", "")

        try:
            errors = await _read_recent_entries(
                entry_type="tool_call",
                status_filter="error",
                tool_filter=tool_filter,
                limit=limit,
            )

            if not errors:
                return ToolResult.ok(
                    "[Diagnostics] No recent errors found. All tool calls succeeded."
                )

            lines = [f"[Diagnostics] {len(errors)} recent error(s):\n"]
            for i, entry in enumerate(errors, 1):
                ts = entry.get("ts", "?")
                tool = entry.get("tool", "?")
                err = entry.get("error", "unknown error")
                dur = entry.get("duration_ms", 0)
                params_str = _format_params(entry.get("params", {}))

                lines.append(
                    f"--- Error #{i} ---\n"
                    f"  Time: {ts}\n"
                    f"  Tool: {tool}\n"
                    f"  Params: {params_str}\n"
                    f"  Error: {err}\n"
                    f"  Duration: {dur}ms"
                )

            # Add error frequency summary
            tool_counts: dict[str, int] = {}
            for e in errors:
                t = e.get("tool", "?")
                tool_counts[t] = tool_counts.get(t, 0) + 1

            if len(tool_counts) > 1:
                lines.append("\n--- Error Frequency ---")
                for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]):
                    lines.append(f"  {t}: {c} error(s)")

            return ToolResult.ok("\n".join(lines))

        except Exception as e:
            return ToolResult.fail(f"Diagnostics failed: {e}")


# ---------------------------------------------------------------------------
# Tool 2: Session Info
# ---------------------------------------------------------------------------


class DebugSessionInfoTool(BaseTool):
    """Inspect the current session state for debugging."""

    name = "debug_session_info"
    description = (
        "Get diagnostic information about the current conversation session: "
        "message count, estimated token usage, active model, DPI scale, "
        "memory state, and session age. Use this to diagnose why responses "
        "are slow (context too large), why vision is not working (wrong model), "
        "or why the agent behaves unexpectedly."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if not _is_tool_enabled(self.name, context):
            return ToolResult.fail(
                "[Diagnostics] debug_session_info is disabled in config. "
                "Enable it via diagnostics.enabled_tools in config.yaml."
            )

        try:
            lines = ["[Session Diagnostics]\n"]

            # Session basics
            session = getattr(context, "session", None)
            if session:
                msg_count = len(session.messages)
                role_counts: dict[str, int] = {}
                total_chars = 0
                image_count = 0

                for m in session.messages:
                    role_name = m.role.value if hasattr(m.role, "value") else str(m.role)
                    role_counts[role_name] = role_counts.get(role_name, 0) + 1
                    if isinstance(m.content, str):
                        total_chars += len(m.content)
                    elif isinstance(m.content, list):
                        for part in m.content:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    total_chars += len(part.get("text", ""))
                                elif part.get("type") == "image_url":
                                    image_count += 1

                est_tokens = total_chars // 4  # rough estimate

                lines.append(f"  Session ID: {session.id[:12]}...")
                lines.append(f"  Adapter: {session.adapter}")
                lines.append(f"  Messages: {msg_count}")
                for role, count in sorted(role_counts.items()):
                    lines.append(f"    {role}: {count}")
                lines.append(f"  Images in context: {image_count}")
                lines.append(f"  Est. tokens: ~{est_tokens:,}")

                if est_tokens > 50000:
                    lines.append("  \u26a0 WARNING: Context is very large, may trigger compression")
                elif est_tokens > 20000:
                    lines.append("  \u26a0 Context is getting large, consider /clear if sluggish")

                # Session age
                if hasattr(session, "created_at") and session.created_at:
                    age = datetime.now(timezone.utc) - session.created_at
                    hours = age.total_seconds() / 3600
                    lines.append(f"  Age: {hours:.1f} hours")

                # Metadata
                gen_images = session.metadata.get("generated_images", [])
                if gen_images:
                    lines.append(f"  Generated images: {len(gen_images)}")
            else:
                lines.append("  (No session object available)")

            # DPI info
            try:
                from src.tools.screen.dpi import get_dpi_scale
                scale = get_dpi_scale()
                lines.append(f"\n  DPI scale: {scale}x" + (
                    " (scaled)" if scale != 1.0 else " (no scaling)"
                ))
            except Exception:
                lines.append("\n  DPI scale: unknown")

            # Memory info
            memory_mgr = getattr(context, "memory_manager", None)
            if memory_mgr:
                lines.append("\n  Memory manager: active")
                try:
                    mem_dir = get_kuro_home() / "memory"
                    memory_md = mem_dir / "MEMORY.md"
                    if memory_md.exists():
                        size = memory_md.stat().st_size
                        lines.append(f"  MEMORY.md: {size / 1024:.1f} KB")

                    vec_dir = mem_dir / "vector_store"
                    if vec_dir.exists():
                        vec_size = sum(f.stat().st_size for f in vec_dir.rglob("*") if f.is_file())
                        lines.append(f"  Vector store: {vec_size / 1024:.1f} KB")
                except Exception:
                    pass

            # Diagnostics config info
            config = getattr(context, "config", None)
            if config and hasattr(config, "diagnostics"):
                diag = config.diagnostics
                lines.append(f"\n  Diagnostics: {'enabled' if diag.enabled else 'disabled'}")
                lines.append(f"  Repair model: {diag.repair_model}")
                lines.append(f"  Auto-diagnose on error: {diag.auto_diagnose_on_error}")
                lines.append(f"  Include in agents: {diag.include_in_agents}")

            # Tracing status
            try:
                from src.core.tracing import is_tracing_active
                lines.append(f"\n  LangSmith tracing: {'active' if is_tracing_active() else 'inactive'}")
            except Exception:
                lines.append("\n  LangSmith tracing: unavailable")

            return ToolResult.ok("\n".join(lines))

        except Exception as e:
            return ToolResult.fail(f"Session diagnostics failed: {e}")


# ---------------------------------------------------------------------------
# Tool 3: Performance Profile
# ---------------------------------------------------------------------------


class DebugPerformanceTool(BaseTool):
    """Profile tool execution performance and model routing decisions."""

    name = "debug_performance"
    description = (
        "Get performance diagnostics: tool execution durations, model "
        "routing (which models were used, fallback events), and slow "
        "operations. Use this to find bottlenecks or understand why "
        "a specific model was selected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of recent operations to analyze (default: 50)",
                "minimum": 10,
                "maximum": 200,
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if not _is_tool_enabled(self.name, context):
            return ToolResult.fail(
                "[Diagnostics] debug_performance is disabled in config. "
                "Enable it via diagnostics.enabled_tools in config.yaml."
            )

        limit = params.get("limit", 50)

        try:
            entries = await _read_recent_entries(
                entry_type="tool_call",
                limit=limit,
            )

            if not entries:
                return ToolResult.ok(
                    "[Performance] No recent tool executions found."
                )

            lines = [f"[Performance Diagnostics] Analyzing {len(entries)} recent tool calls\n"]

            # Tool duration analysis
            tool_stats: dict[str, list[int]] = {}
            error_count = 0
            total_duration = 0

            for entry in entries:
                tool = entry.get("tool", "?")
                dur = entry.get("duration_ms", 0)
                status = entry.get("status", "ok")

                tool_stats.setdefault(tool, []).append(dur)
                total_duration += dur
                if status != "ok":
                    error_count += 1

            # Summary
            lines.append(f"  Total calls: {len(entries)}")
            lines.append(f"  Total time: {total_duration / 1000:.1f}s")
            lines.append(f"  Errors: {error_count} ({error_count * 100 // max(len(entries), 1)}%)")

            # Per-tool breakdown (sorted by total time)
            lines.append("\n--- Tool Duration Breakdown ---")
            sorted_tools = sorted(
                tool_stats.items(),
                key=lambda x: sum(x[1]),
                reverse=True,
            )
            for tool, durations in sorted_tools:
                avg = sum(durations) // max(len(durations), 1)
                mx = max(durations) if durations else 0
                total = sum(durations)
                count = len(durations)
                lines.append(
                    f"  {tool}: {count} calls, "
                    f"avg {avg}ms, max {mx}ms, total {total}ms"
                )

            # Slow operations (>2 seconds)
            slow = [
                e for e in entries
                if e.get("duration_ms", 0) > 2000
            ]
            if slow:
                lines.append(f"\n--- Slow Operations (>{2}s) ---")
                for s in slow[:10]:
                    lines.append(
                        f"  {s.get('tool', '?')}: {s.get('duration_ms', 0)}ms "
                        f"at {s.get('ts', '?')}"
                    )

            # Model routing info from audit (token usage)
            try:
                from src.core.security.audit import AuditLog
                audit = AuditLog()
                daily = await audit.get_daily_stats()

                if daily and "models" in daily:
                    lines.append("\n--- Model Usage (Today) ---")
                    for model, stats in daily["models"].items():
                        total_tok = stats.get("total_tokens", 0)
                        calls = stats.get("calls", 0)
                        lines.append(
                            f"  {model}: {calls} calls, {total_tok:,} tokens"
                        )
            except Exception:
                pass

            # Complexity routing history
            complexity_entries = await _read_recent_entries(
                entry_type="complexity",
                limit=10,
            )
            if complexity_entries:
                lines.append("\n--- Recent Complexity Routing ---")
                for c in complexity_entries[:5]:
                    tier = c.get("tier", "?")
                    score = c.get("score", 0)
                    model = c.get("model", "default")
                    method = c.get("method", "?")
                    decompose = c.get("decompose", False)
                    lines.append(
                        f"  {tier} (score={score:.2f}) \u2192 {model or 'default'} "
                        f"[{method}]"
                        + (" [DECOMPOSED]" if decompose else "")
                    )

            return ToolResult.ok("\n".join(lines))

        except Exception as e:
            return ToolResult.fail(f"Performance diagnostics failed: {e}")


# ---------------------------------------------------------------------------
# Tool 4: Self-Repair (Diagnose & Repair)
# ---------------------------------------------------------------------------


class DiagnoseAndRepairTool(BaseTool):
    """Run a comprehensive diagnostic scan and generate repair recommendations.

    This tool performs a full system health check:
    1. Scans recent errors for patterns
    2. Checks session health (context size, token usage)
    3. Identifies performance bottlenecks
    4. Generates actionable repair recommendations

    Can be triggered manually by the user ("fix the system", "diagnose")
    or auto-triggered when error_threshold consecutive errors are detected.
    """

    name = "diagnose_and_repair"
    description = (
        "Run a full diagnostic scan and generate repair recommendations. "
        "Analyzes recent errors, session health, performance metrics, and "
        "configuration to identify issues and suggest fixes. "
        "Use this when the user says 'fix', 'repair', 'diagnose', or when "
        "multiple errors have occurred. Returns a structured diagnosis with "
        "severity levels and recommended actions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["full", "errors", "performance", "config"],
                "description": (
                    "Scope of the diagnosis: "
                    "'full' (all checks), 'errors' (error patterns only), "
                    "'performance' (bottlenecks only), 'config' (configuration audit). "
                    "Default: 'full'"
                ),
            },
            "auto_fix": {
                "type": "boolean",
                "description": (
                    "If true, attempt to apply automatic fixes where safe "
                    "(e.g., clear stale sessions, reset error counters). "
                    "Default: false"
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if not _is_tool_enabled(self.name, context):
            return ToolResult.fail(
                "[Diagnostics] diagnose_and_repair is disabled in config. "
                "Enable it via diagnostics.enabled_tools in config.yaml."
            )

        scope = params.get("scope", "full")
        auto_fix = params.get("auto_fix", False)

        try:
            lines = ["[Self-Repair Diagnostic Report]\n"]
            issues: list[dict[str, str]] = []  # severity, category, description, fix
            fixes_applied: list[str] = []

            # --- 1. Error Pattern Analysis ---
            if scope in ("full", "errors"):
                lines.append("=" * 40)
                lines.append("1. ERROR PATTERN ANALYSIS")
                lines.append("=" * 40)

                errors = await _read_recent_entries(
                    entry_type="tool_call",
                    status_filter="error",
                    limit=50,
                )

                if not errors:
                    lines.append("  \u2705 No recent errors found.")
                else:
                    lines.append(f"  Found {len(errors)} recent error(s)")

                    # Group by tool
                    tool_errors: dict[str, list[dict]] = {}
                    for e in errors:
                        tool = e.get("tool", "?")
                        tool_errors.setdefault(tool, []).append(e)

                    for tool, errs in sorted(tool_errors.items(), key=lambda x: -len(x[1])):
                        lines.append(f"\n  [{tool}] — {len(errs)} error(s)")
                        # Show unique error messages
                        unique_msgs: dict[str, int] = {}
                        for e in errs:
                            msg = e.get("error", "?")[:100]
                            unique_msgs[msg] = unique_msgs.get(msg, 0) + 1
                        for msg, count in sorted(unique_msgs.items(), key=lambda x: -x[1]):
                            lines.append(f"    x{count}: {msg}")

                        # Detect patterns
                        if len(errs) >= 3:
                            issues.append({
                                "severity": "HIGH",
                                "category": "Recurring Error",
                                "description": f"Tool '{tool}' has {len(errs)} errors",
                                "fix": f"Review {tool} configuration and parameters",
                            })

                    # Check for rapid error bursts (many errors in short time)
                    if len(errors) >= 5:
                        try:
                            first_ts = errors[-1].get("ts", "")
                            last_ts = errors[0].get("ts", "")
                            if first_ts and last_ts:
                                t1 = datetime.fromisoformat(first_ts)
                                t2 = datetime.fromisoformat(last_ts)
                                span_minutes = (t2 - t1).total_seconds() / 60
                                if span_minutes < 5:
                                    issues.append({
                                        "severity": "CRITICAL",
                                        "category": "Error Burst",
                                        "description": f"{len(errors)} errors in {span_minutes:.1f} minutes",
                                        "fix": "System may be in an error loop. Consider /clear and retry.",
                                    })
                        except Exception:
                            pass

            # --- 2. Session Health ---
            if scope in ("full", "performance"):
                lines.append(f"\n{'=' * 40}")
                lines.append("2. SESSION HEALTH")
                lines.append("=" * 40)

                session = getattr(context, "session", None)
                if session:
                    msg_count = len(session.messages)
                    total_chars = 0
                    image_count = 0

                    for m in session.messages:
                        if isinstance(m.content, str):
                            total_chars += len(m.content)
                        elif isinstance(m.content, list):
                            for part in m.content:
                                if isinstance(part, dict):
                                    if part.get("type") == "text":
                                        total_chars += len(part.get("text", ""))
                                    elif part.get("type") == "image_url":
                                        image_count += 1

                    est_tokens = total_chars // 4
                    lines.append(f"  Messages: {msg_count}")
                    lines.append(f"  Est. tokens: ~{est_tokens:,}")
                    lines.append(f"  Images: {image_count}")

                    if est_tokens > 80000:
                        issues.append({
                            "severity": "CRITICAL",
                            "category": "Context Overflow",
                            "description": f"Context is ~{est_tokens:,} tokens (very large)",
                            "fix": "Use /clear to reset conversation or enable context compression",
                        })
                    elif est_tokens > 50000:
                        issues.append({
                            "severity": "HIGH",
                            "category": "Large Context",
                            "description": f"Context is ~{est_tokens:,} tokens",
                            "fix": "Consider /clear if responses are slow",
                        })

                    if image_count > 10:
                        issues.append({
                            "severity": "MEDIUM",
                            "category": "Many Images",
                            "description": f"{image_count} images in context",
                            "fix": "Images consume many tokens. Consider /clear.",
                        })
                else:
                    lines.append("  (No session available)")

            # --- 3. Performance Bottlenecks ---
            if scope in ("full", "performance"):
                lines.append(f"\n{'=' * 40}")
                lines.append("3. PERFORMANCE BOTTLENECKS")
                lines.append("=" * 40)

                entries = await _read_recent_entries(
                    entry_type="tool_call",
                    limit=100,
                )

                if entries:
                    # Find tools with high avg latency
                    tool_stats: dict[str, list[int]] = {}
                    for e in entries:
                        tool = e.get("tool", "?")
                        dur = e.get("duration_ms", 0)
                        tool_stats.setdefault(tool, []).append(dur)

                    slow_tools = []
                    for tool, durations in tool_stats.items():
                        avg = sum(durations) / max(len(durations), 1)
                        if avg > 5000:  # > 5s average
                            slow_tools.append((tool, avg, max(durations)))
                            issues.append({
                                "severity": "MEDIUM",
                                "category": "Slow Tool",
                                "description": f"'{tool}' avg latency: {avg:.0f}ms",
                                "fix": f"Consider optimizing {tool} or using a faster alternative",
                            })

                    if slow_tools:
                        for tool, avg, mx in sorted(slow_tools, key=lambda x: -x[1]):
                            lines.append(f"  \u26a0 {tool}: avg {avg:.0f}ms, max {mx}ms")
                    else:
                        lines.append("  \u2705 No significant bottlenecks found.")
                else:
                    lines.append("  (No recent tool calls)")

            # --- 4. Configuration Audit ---
            if scope in ("full", "config"):
                lines.append(f"\n{'=' * 40}")
                lines.append("4. CONFIGURATION AUDIT")
                lines.append("=" * 40)

                config = getattr(context, "config", None)
                if config:
                    # Check diagnostics config
                    diag = getattr(config, "diagnostics", None)
                    if diag:
                        lines.append(f"  Diagnostics enabled: {diag.enabled}")
                        lines.append(f"  Repair model: {diag.repair_model}")
                        lines.append(f"  Auto-diagnose: {diag.auto_diagnose_on_error}")
                        lines.append(f"  Include in agents: {diag.include_in_agents}")
                        lines.append(f"  Only matching model: {diag.only_matching_model}")
                        lines.append(f"  Enabled tools: {', '.join(diag.enabled_tools)}")

                        # Resolve actual repair model
                        if diag.repair_model == "main":
                            actual_model = config.models.default
                        else:
                            actual_model = diag.repair_model
                        lines.append(f"  Resolved repair model: {actual_model}")
                    else:
                        lines.append("  \u26a0 No diagnostics config found (using defaults)")
                        issues.append({
                            "severity": "LOW",
                            "category": "Missing Config",
                            "description": "No diagnostics section in config.yaml",
                            "fix": "Add diagnostics section to config.yaml for customization",
                        })

                    # Check tracing
                    tracing = getattr(config, "tracing", None)
                    if tracing:
                        lines.append(f"  Tracing: {'enabled' if tracing.enabled else 'disabled'}")
                    else:
                        lines.append("  Tracing: not configured")

                    # Check security
                    sec = getattr(config, "security", None)
                    if sec:
                        disabled = sec.disabled_tools
                        if disabled:
                            lines.append(f"  Disabled tools: {', '.join(disabled)}")
                            # Check if diagnostic tools are accidentally disabled
                            diag_disabled = [
                                t for t in disabled
                                if t.startswith("debug_") or t == "diagnose_and_repair"
                            ]
                            if diag_disabled:
                                issues.append({
                                    "severity": "HIGH",
                                    "category": "Tools Disabled",
                                    "description": f"Diagnostic tools disabled: {diag_disabled}",
                                    "fix": "Remove diagnostic tools from security.disabled_tools",
                                })
                else:
                    lines.append("  (No config available)")

            # --- 5. Memory Health ---
            if scope == "full":
                lines.append(f"\n{'=' * 40}")
                lines.append("5. MEMORY HEALTH")
                lines.append("=" * 40)

                try:
                    mem_dir = get_kuro_home() / "memory"
                    memory_md = mem_dir / "MEMORY.md"
                    if memory_md.exists():
                        size_kb = memory_md.stat().st_size / 1024
                        lines.append(f"  MEMORY.md: {size_kb:.1f} KB")
                        if size_kb > 50:
                            issues.append({
                                "severity": "MEDIUM",
                                "category": "Large Memory",
                                "description": f"MEMORY.md is {size_kb:.1f} KB",
                                "fix": "Consider running memory consolidation",
                            })
                    else:
                        lines.append("  MEMORY.md: not found")

                    vec_dir = mem_dir / "vector_store"
                    if vec_dir.exists():
                        vec_size = sum(f.stat().st_size for f in vec_dir.rglob("*") if f.is_file())
                        lines.append(f"  Vector store: {vec_size / 1024:.1f} KB")
                    else:
                        lines.append("  Vector store: not initialized")

                    # Check action log disk usage
                    log_dir = get_kuro_home() / "action_logs"
                    if log_dir.exists():
                        log_size = sum(f.stat().st_size for f in log_dir.glob("*.jsonl"))
                        lines.append(f"  Action logs: {log_size / (1024 * 1024):.1f} MB")
                        if log_size > 100 * 1024 * 1024:  # > 100MB
                            issues.append({
                                "severity": "MEDIUM",
                                "category": "Large Logs",
                                "description": f"Action logs are {log_size / (1024*1024):.1f} MB",
                                "fix": "Run log cleanup or reduce retention_days",
                            })
                except Exception as e:
                    lines.append(f"  (Memory check failed: {e})")

            # --- Issue Summary & Recommendations ---
            lines.append(f"\n{'=' * 40}")
            lines.append("DIAGNOSIS SUMMARY")
            lines.append("=" * 40)

            if not issues:
                lines.append("  \u2705 System is healthy. No issues detected.")
            else:
                # Sort by severity
                severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
                issues.sort(key=lambda x: severity_order.get(x["severity"], 99))

                lines.append(f"  Found {len(issues)} issue(s):\n")
                for i, issue in enumerate(issues, 1):
                    sev = issue["severity"]
                    icon = {
                        "CRITICAL": "\U0001f534",
                        "HIGH": "\U0001f7e0",
                        "MEDIUM": "\U0001f7e1",
                        "LOW": "\U0001f535",
                    }.get(sev, "\u2753")
                    lines.append(
                        f"  {icon} [{sev}] {issue['category']}: {issue['description']}"
                    )
                    lines.append(f"     Fix: {issue['fix']}")

            # Auto-fix actions
            if auto_fix and fixes_applied:
                lines.append(f"\n--- Auto-fixes Applied ---")
                for fix in fixes_applied:
                    lines.append(f"  \u2705 {fix}")

            # Repair model info
            config = getattr(context, "config", None)
            if config and hasattr(config, "diagnostics"):
                repair_model = config.diagnostics.repair_model
                if repair_model == "main":
                    repair_model = config.models.default
                lines.append(f"\n  Repair model: {repair_model}")

            return ToolResult.ok("\n".join(lines))

        except Exception as e:
            return ToolResult.fail(f"Self-repair diagnostics failed: {e}")


# ---------------------------------------------------------------------------
# Shared helper: read action log entries
# ---------------------------------------------------------------------------


async def _read_recent_entries(
    entry_type: str | None = None,
    status_filter: str | None = None,
    tool_filter: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read recent entries from today's (and yesterday's) action log files.

    Returns entries in reverse chronological order (newest first).
    """
    log_dir = get_kuro_home() / "action_logs"
    if not log_dir.exists():
        return []

    # Get the most recent log files (today + yesterday)
    log_files = sorted(log_dir.glob("actions-*.jsonl"), reverse=True)[:2]
    if not log_files:
        return []

    all_entries: list[dict[str, Any]] = []

    for log_file in log_files:
        try:
            text = log_file.read_text(encoding="utf-8")
            for line in reversed(text.strip().split("\n")):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Apply filters
                if entry_type and entry.get("type") != entry_type:
                    continue
                if status_filter and entry.get("status") != status_filter:
                    continue
                if tool_filter and entry.get("tool") != tool_filter:
                    continue

                all_entries.append(entry)
                if len(all_entries) >= limit:
                    return all_entries
        except Exception:
            continue

    return all_entries


def _format_params(params: dict[str, Any]) -> str:
    """Format tool params for display, truncating large values."""
    if not params:
        return "{}"
    parts = []
    for k, v in params.items():
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        parts.append(f"{k}={v_str}")
    result = ", ".join(parts)
    if len(result) > 200:
        result = result[:197] + "..."
    return result


# ---------------------------------------------------------------------------
# Helper: get the list of diagnostic tool names
# ---------------------------------------------------------------------------


DIAGNOSTIC_TOOL_NAMES = frozenset({
    "debug_recent_errors",
    "debug_session_info",
    "debug_performance",
    "diagnose_and_repair",
})


def get_enabled_diagnostic_tool_names(config: Any) -> frozenset[str]:
    """Get the set of enabled diagnostic tool names from config.

    Used by AgentRunner and AgentInstanceManager to decide which
    diagnostic tools to include for sub-agents and instances.
    """
    diag = getattr(config, "diagnostics", None)
    if diag is None or not diag.enabled:
        return frozenset()

    if diag.enabled_tools:
        return frozenset(diag.enabled_tools) & DIAGNOSTIC_TOOL_NAMES
    return DIAGNOSTIC_TOOL_NAMES


def get_diagnostic_guidance_message(config: Any) -> str | None:
    """Build a concise system-level guidance message about diagnostic tools.

    Returns None if diagnostics is disabled or no tools are enabled.
    Injected dynamically into the LLM context so it knows *when* and *how*
    to use diagnostic tools proactively — without hardcoding in system_prompt.
    """
    diag = getattr(config, "diagnostics", None)
    if diag is None or not diag.enabled:
        return None

    enabled = get_enabled_diagnostic_tool_names(config)
    if not enabled:
        return None

    lines = ["[Self-Diagnostics Available]"]
    lines.append(
        "You have self-diagnostic tools. Use them proactively when issues arise:"
    )

    if "debug_recent_errors" in enabled:
        lines.append(
            "• When a tool call fails or the user asks why something went wrong "
            "→ call `debug_recent_errors` to see detailed error info."
        )
    if "debug_session_info" in enabled:
        lines.append(
            "• When responses feel slow or you need session state "
            "→ call `debug_session_info` for context size, token estimate, DPI, memory."
        )
    if "debug_performance" in enabled:
        lines.append(
            "• When the user reports slowness or you want to find bottlenecks "
            "→ call `debug_performance` for tool latency analysis."
        )
    if "diagnose_and_repair" in enabled:
        lines.append(
            "• When multiple errors occur or the user says \"fix\" / \"repair\" / \"diagnose\" "
            "→ call `diagnose_and_repair` for a full system health scan with recommendations."
        )

    lines.append(
        "Do NOT wait for explicit requests — if you notice repeated failures, "
        "investigate proactively."
    )
    return "\n".join(lines)


def should_include_diagnostics_for_agent(
    config: Any,
    agent_model: str,
) -> bool:
    """Determine if diagnostic tools should be included for a sub-agent.

    Checks:
    1. diagnostics.enabled must be True
    2. diagnostics.include_in_agents must be True
    3. If diagnostics.only_matching_model is True, the agent's model
       must match the main model
    """
    diag = getattr(config, "diagnostics", None)
    if diag is None or not diag.enabled:
        return False
    if not diag.include_in_agents:
        return False

    if diag.only_matching_model:
        main_model = getattr(config, "models", None)
        if main_model is None:
            return False
        return agent_model == main_model.default

    return True
