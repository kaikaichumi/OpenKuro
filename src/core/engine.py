"""Core engine: main agent loop orchestrating model calls, tools, and memory.

The engine receives a user message, builds context, calls the LLM,
handles tool calls with approval, and returns the final response.
Integrates with the security layer (approval, sandbox, audit, sanitizer).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from src.config import KuroConfig
from src.core.action_log import ActionLogger
from src.core.analytics import get_budget_manager
from src.core.memory.manager import MemoryManager
from src.core.model_router import (
    ContextOverflowError,
    ModelRouter,
    VisionNotSupportedError,
)
from src.core.security.approval import ApprovalPolicy
from src.core.security.audit import AuditLog
from src.core.security.egress import EgressBroker
from src.core.security.sandbox import Sandbox
from src.core.security.sanitizer import Sanitizer
from src.core.security.tool_policy import ToolPolicyCore
from src.core.tool_system import ToolSystem
from src.core.types import Message, ModelResponse, Role, Session, ToolCall
from src.openai_catalog import is_openai_compatible_local_base_url
from src.tools.base import RiskLevel, ToolContext, ToolResult

if TYPE_CHECKING:
    from src.core.complexity import ComplexityEstimator, ComplexityResult

logger = structlog.get_logger()

# Default maximum number of tool call rounds (overridable via config.max_tool_rounds)
_DEFAULT_MAX_TOOL_ROUNDS = 10

# Result reporting enforcement: minimum response length after tool use
_MIN_REPORT_LENGTH = 5  # characters — responses shorter than this trigger a retry

# Vague response patterns (stripped of trailing punctuation)
_VAGUE_RESPONSES = {
    "done", "ok", "okay", "got it", "sure", "completed", "finished",
    "完成", "好的", "已完成", "好了", "做完了", "搞定", "可以了", "了解",
    "done!", "ok!", "okay!", "完成！", "好的！", "已完成！", "好了！",
}

# Maximum image size for vision (resize if larger to save tokens)
_MAX_SCREENSHOT_DIMENSION = 1280

# Shell command risk heuristics used by execution guard
_SHELL_DESTRUCTIVE_RE = re.compile(
    r"\b(remove-item|del\s|erase\s|rm\s|rmdir\s|move-item|mv\s|ren\s|rename-item)\b",
    re.I,
)
_SHELL_DOWNLOAD_RE = re.compile(
    r"\b(curl|wget|invoke-webrequest|start-bitstransfer|bitsadmin|aria2c)\b",
    re.I,
)
_SHELL_BULK_HINT_RE = re.compile(
    r"(\*|--recursive|\s-r\b|-recurse|\s/s\b|get-childitem|find\s|forfiles|xcopy|robocopy)",
    re.I,
)

# Tool results that should retain multimodal image context for follow-up actions.
# Image generation tools (e.g. comfyui_*) are intentionally excluded to avoid
# pushing large base64 payloads back into the next LLM turn.
_MULTIMODAL_TOOL_CONTEXT_ALLOWLIST = {
    "screenshot",
    "computer_use",
}


def _encode_image_base64(image_path: str) -> str | None:
    """Read an image file and return its base64-encoded data URI string.

    Resizes large screenshots to save LLM tokens.
    Returns None if the file cannot be read.
    """
    try:
        path = Path(image_path)
        if not path.exists():
            return None

        from PIL import Image
        import io

        img = Image.open(path)
        w, h = img.size

        # Resize if too large
        if max(w, h) > _MAX_SCREENSHOT_DIMENSION:
            ratio = _MAX_SCREENSHOT_DIMENSION / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.warning("image_encode_failed", path=image_path, error=str(e))
        return None


class ToolExecutionCallback:
    """Optional callback invoked after each tool execution.

    UI implementations can override this to push live updates
    (e.g. screen previews to the Web GUI).
    """

    async def on_tool_executed(
        self,
        tool_name: str,
        params: dict[str, Any],
        result: "ToolResult",
    ) -> None:
        pass


class ApprovalCallback:
    """Interface for requesting human approval of tool calls.

    Override this in UI implementations (CLI, Web, Telegram, etc.)
    """

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        """Ask the user to approve a tool call. Returns True if approved."""
        # Default: auto-approve LOW, deny everything else
        return risk_level == RiskLevel.LOW


class Engine:
    """Main agent loop that orchestrates model, tools, and security."""

    def __init__(
        self,
        config: KuroConfig,
        model_router: ModelRouter,
        tool_system: ToolSystem,
        action_logger: ActionLogger,
        approval_callback: ApprovalCallback | None = None,
        audit_log: AuditLog | None = None,
        memory_manager: MemoryManager | None = None,
        skills_manager: "SkillsManager | None" = None,
        agent_manager: "AgentManager | None" = None,
    ) -> None:
        self.config = config
        self.model = model_router
        self.tools = tool_system
        self.action_log = action_logger
        self.approval_cb = approval_callback or ApprovalCallback()
        self.tool_callback: ToolExecutionCallback | None = None
        self.memory = memory_manager or MemoryManager()
        self.skills = skills_manager
        self.agent_manager = agent_manager
        self.team_manager: Any = None  # Set externally after construction (Phase 2)
        self.instance_manager: Any = None  # AgentInstanceManager (Primary Agent instances)
        self.mcp_bridge: Any = None  # MCPBridgeManager (set by build_engine)
        self._mcp_init_lock = asyncio.Lock()

        # Event bus for live dashboard (set by build_engine)
        self.event_bus: Any = None

        # Security components
        self.approval_policy = ApprovalPolicy(config.security)
        self.sandbox = Sandbox(config.sandbox)
        self.egress_broker = EgressBroker(getattr(config, "egress_policy", None))
        self.tool_policy = ToolPolicyCore(
            getattr(config, "tool_policy", None),
            self.egress_broker,
        )
        self.sanitizer = Sanitizer()
        self.audit = audit_log or AuditLog()
        self.budget_manager = get_budget_manager()

        # Task complexity estimator (set externally after construction)
        self.complexity_estimator: ComplexityEstimator | None = None

        # Initialize LangSmith tracing if configured
        if config.tracing.enabled:
            try:
                from src.core.tracing import init_tracing
                init_tracing(
                    project_name=config.tracing.project_name,
                    tags=config.tracing.tags,
                )
            except Exception as e:
                logger.debug("tracing_init_skipped", error=str(e))

        # Per-session locks for concurrency safety
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session asyncio lock."""
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    async def execute_tool(self, tool_name: str, params: dict[str, Any]) -> str:
        """Execute a tool directly (used by scheduler/workflow).

        Creates a minimal ToolContext and returns the output string.
        """
        await self._ensure_mcp_tools_loaded()
        context = ToolContext(
            session_id="scheduler",
            config=self.config,
            model_router=self.model,
            active_model=self.model.default_model,
            working_directory=None,
            allowed_directories=[
                str(d) for d in self.sandbox.allowed_directories
            ],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
            agent_manager=self.agent_manager,
            team_manager=self.team_manager,
            instance_manager=self.instance_manager,
            memory_manager=self.memory,
            agent_instance_id=getattr(self, "agent_instance_id", None),
        )
        result = await self.tools.execute(tool_name, params, context)
        if result.success:
            return result.output
        raise RuntimeError(result.error or "Tool execution failed")

    async def _ensure_mcp_tools_loaded(self) -> None:
        """Best-effort MCP tool bootstrap (lazy-loaded)."""
        bridge = getattr(self, "mcp_bridge", None)
        if bridge is None:
            return
        try:
            async with self._mcp_init_lock:
                await bridge.ensure_initialized(self.tools.registry)
        except Exception as e:
            logger.warning("mcp_bridge_init_failed", error=str(e))

    async def execute_agent(self, agent_name: str, task: str) -> str:
        """Execute a sub-agent directly (used by scheduler).

        Runs the named agent with the given task description and returns
        the final text output.
        """
        if not self.agent_manager:
            raise RuntimeError("Agent manager not available")

        result = await self.agent_manager.run_agent(name=agent_name, task=task)
        return result

    def _get_system_message(self) -> Message:
        """Build the system message with security rules."""
        return Message(role=Role.SYSTEM, content=self.config.system_prompt)

    def _normalize_system_messages(
        self, messages: list[Message]
    ) -> list[Message]:
        """Normalize message order for strict chat templates.

        Some local templates (e.g., certain llama.cpp chat templates) require
        SYSTEM messages to appear only at the beginning. This keeps relative
        order within SYSTEM and non-SYSTEM groups while making SYSTEM-first.
        """
        if not messages:
            return messages

        system_msgs = [m for m in messages if m.role == Role.SYSTEM]
        if not system_msgs:
            return messages

        non_system_msgs = [m for m in messages if m.role != Role.SYSTEM]

        # Some local templates only allow a single system message.
        # Merge all system instructions into one top message.
        if len(system_msgs) > 1:
            merged_parts: list[str] = []
            for sm in system_msgs:
                if isinstance(sm.content, str):
                    text = sm.content.strip()
                else:
                    text = str(sm.content).strip()
                if text:
                    merged_parts.append(text)
            merged_system = Message(
                role=Role.SYSTEM,
                content="\n\n".join(merged_parts),
            )
            logger.debug(
                "system_messages_merged_for_template",
                original=len(system_msgs),
            )
            return [merged_system] + non_system_msgs

        already_normalized = (
            messages[0].role == Role.SYSTEM
            and all(m.role != Role.SYSTEM for m in messages[1:])
        )
        if already_normalized:
            return messages

        logger.debug(
            "system_messages_reordered_for_template",
            total=len(messages),
            system=len(system_msgs),
        )
        return system_msgs + non_system_msgs

    def _get_agent_context_message(self) -> Message | None:
        """Build an agent-awareness message so the LLM knows available agents.

        Without this, the LLM will pretend to delegate by writing text like
        "I'll hand this to agent X" without actually calling the tool.
        """
        if not self.agent_manager:
            return None
        definitions = self.agent_manager.list_definitions()
        if not definitions:
            return None

        agent_names = [defn.name for defn in definitions]

        lines = [
            "[Available Sub-Agents]",
            "You have the following sub-agents that run on DIFFERENT models.",
            "To delegate work to them, you MUST call the `delegate_to_agent` tool.",
            "Do NOT pretend to delegate — you must use the tool for it to actually run.",
            "Do NOT answer on behalf of a sub-agent — delegate the task and return their result.",
            "Do NOT worry about permissions — just call the tool. The system handles approval automatically.",
            "",
        ]
        for defn in definitions:
            tools_info = ""
            if defn.allowed_tools:
                tools_info = f" | tools: {', '.join(defn.allowed_tools)}"
            lines.append(
                f"- name: \"{defn.name}\" | model: {defn.model} | "
                f"tier: {defn.complexity_tier} | max_rounds: {defn.max_tool_rounds}{tools_info}"
            )

        # Dynamic creation hint
        lines.append("")
        lines.append("[Dynamic Agent Creation]")
        lines.append(
            "You can create new agents at runtime with `create_agent` and "
            "delete them with `delete_agent`. Created agents support recursive "
            "delegation (depth-limited) and optional structured JSON output."
        )

        # Scheduler integration hint
        lines.append("")
        lines.append("[Scheduling Sub-Agents]")
        lines.append(
            "Sub-agents can also be scheduled with `schedule_add`. "
            "Set task_type='agent', tool_name to the agent name "
            f"(one of: {', '.join(agent_names)}), and agent_task to a "
            "description of what the agent should do. Example:"
        )
        lines.append(
            '  schedule_add(task_type="agent", tool_name="researcher", '
            'agent_task="Monitor US market pre-opening for AMD, TSLA", ...)'
        )

        # Phase 2: Agent Teams awareness
        if self.team_manager:
            team_defs = self.team_manager.list_definitions()
            if team_defs:
                lines.append("")
                lines.append("[Available Agent Teams]")
                lines.append(
                    "Teams are groups of agents that collaborate with shared workspace "
                    "and messaging. Use `run_team` to execute, `create_team` to create, "
                    "`list_teams` to see all teams."
                )
                for tdef in team_defs:
                    roles_str = ", ".join(r.name for r in tdef.roles)
                    lines.append(
                        f"- team: \"{tdef.name}\" | roles: {roles_str} | "
                        f"max_rounds: {tdef.max_rounds}"
                    )
            else:
                lines.append("")
                lines.append("[Agent Teams]")
                lines.append(
                    "No teams registered yet. Use `create_team` to create a team "
                    "of agents that can collaborate on complex tasks."
                )

        return Message(role=Role.SYSTEM, content="\n".join(lines))

    # ------------------------------------------------------------------
    # Result reporting enforcement
    # ------------------------------------------------------------------

    @staticmethod
    def _is_vague_response(content: str) -> bool:
        """Check if a response is too vague to serve as a proper result report.

        Returns True when the response is very short or matches known
        placeholder patterns like "Done!", "OK", "完成", etc.
        """
        stripped = content.strip()
        if not stripped:
            return True
        if len(stripped) < _MIN_REPORT_LENGTH:
            # Normalize for comparison: lowercase, strip punctuation
            normalized = stripped.lower().rstrip("!！。.~～?？")
            if normalized in _VAGUE_RESPONSES:
                return True
            # Also catch short responses that are just a greeting + vague word
            # e.g., "好的，完成了"
            for vague in _VAGUE_RESPONSES:
                if normalized.endswith(vague):
                    return True
        return False

    # ------------------------------------------------------------------
    # Emergency context compression (triggered by ContextOverflowError)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Vision fallback helpers
    # ------------------------------------------------------------------

    def _build_image_content(
        self,
        text_output: str,
        image_path: str,
        model: str | None,
    ) -> str | list[dict[str, Any]]:
        """Build content for a tool result that includes an image.

        Respects ``config.vision.image_analysis_mode``:
          - auto:     vision model → raw image; text-only → OCR/SVG analysis
          - always:   vision model → raw image + analysis; text-only → analysis
          - disabled: vision model → raw image; text-only → text only (skip image)
        """
        vision_cfg = self.config.vision
        mode = vision_cfg.image_analysis_mode  # auto | always | disabled
        has_vision = self.model.supports_vision(model)

        if mode == "disabled":
            if has_vision:
                return self._multimodal_content(text_output, image_path)
            return text_output  # skip image entirely

        if mode == "always":
            analysis = self._run_image_analysis(image_path)
            combined_text = f"{text_output}\n\n{analysis}"
            if has_vision:
                return self._multimodal_content(combined_text, image_path)
            return combined_text

        # mode == "auto" (default)
        if has_vision:
            return self._multimodal_content(text_output, image_path)

        # Text-only model: auto-convert via OCR/SVG
        analysis = self._run_image_analysis(image_path)
        logger.info(
            "vision_fallback_ocr",
            image_path=image_path,
            analysis_length=len(analysis),
        )
        return f"{text_output}\n\n{analysis}"

    @staticmethod
    def _multimodal_content(text: str, image_path: str) -> str | list[dict[str, Any]]:
        """Create multimodal content with text + base64 image."""
        data_uri = _encode_image_base64(image_path)
        if data_uri:
            return [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        return text  # encoding failed, fall back to text only

    @staticmethod
    def _should_attach_tool_image_to_context(tool_name: str) -> bool:
        """Return True if a tool image should be attached back to model context."""
        name = str(tool_name or "").strip().lower()
        return name in _MULTIMODAL_TOOL_CONTEXT_ALLOWLIST

    def _run_image_analysis(self, image_path: str) -> str:
        """Run OCR + OpenCV analysis on an image, returning text description."""
        from src.tools.screen.analyze_image import run_image_analysis

        vision_cfg = self.config.vision
        return run_image_analysis(
            image_path,
            fallback_format=vision_cfg.fallback_format,
            detail_level=vision_cfg.fallback_detail_level,
            grid_size=vision_cfg.grid_size,
            max_elements=vision_cfg.max_elements,
        )

    def _is_local_model_target(self, model_name: str | None) -> bool:
        """Return True when the model target is a local runtime endpoint."""
        target = str(model_name or "").strip()
        if not target:
            return False
        provider = target.split("/", 1)[0].strip().lower()
        if provider in {"ollama", "llama"}:
            return True
        if provider == "openai":
            cfg = self.config.models.providers.get("openai")
            base_url = str(getattr(cfg, "base_url", "") or "").strip()
            return is_openai_compatible_local_base_url(base_url)
        return False

    @staticmethod
    def _is_http_image_url(value: str) -> bool:
        raw = str(value or "").strip().lower()
        return raw.startswith("http://") or raw.startswith("https://")

    def _convert_images_to_text(self, messages: list[Message]) -> list[Message]:
        """Strip image content from multimodal messages (for vision error retry)."""
        converted = []
        for m in messages:
            if isinstance(m.content, list):
                text_parts = []
                for part in m.content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part["text"])
                        elif part.get("type") == "image_url":
                            text_parts.append(
                                "[Image removed — model does not support vision]"
                            )
                converted.append(Message(
                    role=m.role,
                    content="\n".join(text_parts),
                    name=m.name,
                    tool_call_id=m.tool_call_id,
                    tool_calls=m.tool_calls,
                ))
            else:
                converted.append(m)
        return converted

    def _strip_non_context_tool_images(self, messages: list[Message]) -> list[Message]:
        """Drop image payloads from tool messages that don't need visual follow-up.

        This keeps request payloads stable for providers that are sensitive to
        large data-URI image blobs in historical tool results (e.g. image
        generation tools).
        """
        converted: list[Message] = []
        for m in messages:
            if (
                m.role == Role.TOOL
                and isinstance(m.content, list)
                and not self._should_attach_tool_image_to_context(m.name or "")
            ):
                text_parts: list[str] = []
                had_image = False
                for part in m.content:
                    if not isinstance(part, dict):
                        continue
                    p_type = str(part.get("type", "")).strip().lower()
                    if p_type == "text":
                        txt = part.get("text")
                        if isinstance(txt, str) and txt:
                            text_parts.append(txt)
                    elif p_type == "image_url":
                        had_image = True
                if had_image:
                    merged = "\n".join(text_parts).strip() or "[Tool image omitted]"
                    if "[Image omitted from model context]" not in merged:
                        merged = f"{merged}\n\n[Image omitted from model context]"
                    converted.append(Message(
                        role=m.role,
                        content=merged,
                        name=m.name,
                        tool_call_id=m.tool_call_id,
                        tool_calls=m.tool_calls,
                    ))
                    continue
            converted.append(m)
        return converted

    async def _emergency_compress(
        self,
        messages: list[Message],
        limit_tokens: int | None,
    ) -> list[Message]:
        """Force-compress context after the LLM returned a context overflow error.

        1. First tries the existing ContextCompressor (LLM-based summarization).
        2. If the compressor is unavailable or the result is still too large,
           falls back to aggressive truncation (trim tool results + drop old msgs).
        """
        _CHARS_PER_TOKEN = 4

        # --- Try the existing compressor first ---
        if hasattr(self.memory, "compressor") and self.memory.compressor:
            try:
                compressed = await self.memory.compressor.compress_if_needed(messages)
                # Force a compress even if compress_if_needed thought it wasn't needed
                if compressed is messages:
                    # Override: temporarily lower the budget to force compression
                    orig_budget = self.memory.compressor.config.token_budget
                    orig_threshold = self.memory.compressor.config.trigger_threshold
                    self.memory.compressor.config.token_budget = limit_tokens or 4096
                    self.memory.compressor.config.trigger_threshold = 0.0
                    compressed = await self.memory.compressor.compress_if_needed(messages)
                    self.memory.compressor.config.token_budget = orig_budget
                    self.memory.compressor.config.trigger_threshold = orig_threshold

                est = sum(
                    len(m.content) if isinstance(m.content, str) else 0
                    for m in compressed
                ) // _CHARS_PER_TOKEN
                target = int((limit_tokens or 8192) * 0.75)

                if est <= target:
                    logger.info(
                        "engine_overflow_compressed",
                        strategy="compressor",
                        est_tokens=est,
                        target=target,
                    )
                    return compressed
            except Exception as e:
                logger.warning("engine_emergency_compress_failed", error=str(e))

        # --- Fallback: aggressive truncation (same approach as AgentRunner) ---
        budget = limit_tokens or 8192
        target = int(budget * 0.75)

        # Step 1: truncate long tool results
        truncated: list[Message] = []
        for m in messages:
            if m.role == Role.TOOL and isinstance(m.content, str) and len(m.content) > 800:
                short = m.content[:400] + "\n...(truncated)...\n" + m.content[-200:]
                truncated.append(
                    Message(
                        role=m.role, content=short,
                        name=m.name, tool_call_id=m.tool_call_id,
                    )
                )
            else:
                truncated.append(m)

        est = sum(
            len(m.content) if isinstance(m.content, str) else 0
            for m in truncated
        ) // _CHARS_PER_TOKEN
        if est <= target:
            logger.info("engine_overflow_compressed", strategy="truncate_tool_results", est_tokens=est)
            return truncated

        # Step 2: drop old middle messages
        system_msgs: list[Message] = []
        first_user: Message | None = None
        rest: list[Message] = []
        for m in truncated:
            if m.role == Role.SYSTEM:
                system_msgs.append(m)
            elif first_user is None and m.role == Role.USER:
                first_user = m
            else:
                rest.append(m)

        keep_recent = max(6, len(rest) // 2)
        while keep_recent > 2:
            candidate = system_msgs[:]
            if first_user:
                candidate.append(first_user)
            candidate.extend(rest[-keep_recent:])
            est = sum(
                len(m.content) if isinstance(m.content, str) else 0
                for m in candidate
            ) // _CHARS_PER_TOKEN
            if est <= target:
                logger.info(
                    "engine_overflow_compressed",
                    strategy="drop_old_messages",
                    est_tokens=est,
                    kept_recent=keep_recent,
                )
                return candidate
            keep_recent -= 2

        # Last resort
        final = system_msgs[:]
        if first_user:
            final.append(first_user)
        logger.warning("engine_overflow_compressed_aggressive")
        return final

    async def process_message(
        self,
        user_text: str,
        session: Session,
        model: str | None = None,
        images: list[str] | None = None,
    ) -> str:
        """Process a user message and return the assistant's response.

        This is the main entry point for the agent loop.
        Uses per-session locking to safely handle concurrent messages.

        Args:
            user_text: The user's text message.
            session: The conversation session.
            model: Optional model override.
            images: Optional list of image file paths or data URIs to
                    include as multimodal content with the user message.
        """
        async with self._get_session_lock(session.id):
            return await self._process_message_locked(
                user_text, session, model, images=images
            )

    def _resolve_event_agent_id(self, session: Session | None = None) -> str:
        """Resolve source agent id for dashboard events."""
        if session is not None:
            meta = getattr(session, "metadata", None)
            if isinstance(meta, dict):
                raw = meta.get("_dashboard_agent_id")
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        inst_id = getattr(self, "agent_instance_id", None)
        if isinstance(inst_id, str) and inst_id.strip():
            return inst_id.strip()
        return "main"

    def _emit(self, event_type: str, agent_id: str | None = None, **kwargs: Any) -> None:
        """Emit an event to the event bus (best-effort, never raises)."""
        bus = getattr(self, "event_bus", None)
        if bus:
            try:
                from src.core.agent_events import AgentEvent
                resolved_agent = (
                    agent_id.strip()
                    if isinstance(agent_id, str) and agent_id.strip()
                    else self._resolve_event_agent_id(None)
                )
                bus.emit(AgentEvent(
                    event_type=event_type,
                    source_agent=resolved_agent,
                    **kwargs,
                ))
            except Exception:
                pass

    async def _check_budget_stop_guard(self, model: str, session: Session) -> str | None:
        """Return a user-facing block message when a hard budget limit is exceeded."""
        manager = getattr(self, "budget_manager", None)
        if manager is None:
            return None
        try:
            result = await manager.check_stop_limits(model=model)
        except Exception as e:
            logger.debug("budget_stop_check_failed", model=model, error=str(e))
            return None

        if not result.get("blocked"):
            return None

        matches = result.get("matches") or []
        details: list[str] = []
        for item in matches[:3]:
            try:
                spent_val = float(item.get("spent_usd", 0.0) or 0.0)
            except Exception:
                spent_val = 0.0
            try:
                limit_val = float(item.get("limit_usd", 0.0) or 0.0)
            except Exception:
                limit_val = 0.0
            details.append(
                f"{item.get('name', item.get('id', 'rule'))} "
                f"(${spent_val:.4f}/${limit_val:.4f})"
            )
        joined = "; ".join(details) if details else "budget limit exceeded"
        return (
            "此模型已達到預算硬上限，請求已被阻擋。"
            f"{joined}。請到 Analytics 調整預算規則或切換模型。"
        )

    async def _notify_budget_threshold(self, model: str, session: Session) -> None:
        """Send budget threshold notifications via configured adapters."""
        manager = getattr(self, "budget_manager", None)
        if manager is None:
            return
        adapter_manager = getattr(self, "adapter_manager", None)
        if adapter_manager is None:
            return
        try:
            result = await manager.check_and_notify(
                model=model,
                session=session,
                adapter_manager=adapter_manager,
            )
            sent = int(result.get("sent", 0) or 0)
            if sent > 0:
                logger.info("budget_notifications_sent", model=model, sent=sent)
        except Exception as e:
            logger.debug("budget_notify_failed", model=model, error=str(e))

    @staticmethod
    def _new_execution_guard_state() -> dict[str, Any]:
        """Initialize per-task execution guard counters."""
        return {
            "tool_calls": 0,
            "shell_calls": 0,
            "destructive_shell_ops": 0,
            "download_ops": 0,
            "signature_counts": {},
            "bulk_confirmed": set(),
            "high_risk_plan_cache": {},
        }

    @staticmethod
    def _normalize_tool_args(arguments: dict[str, Any]) -> str:
        """Normalize tool arguments into a deterministic JSON string."""
        try:
            return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            return str(arguments)

    def _tool_signature(self, tool_call: ToolCall) -> str:
        """Build a stable signature for duplicate-call detection."""
        return f"{tool_call.name}:{self._normalize_tool_args(tool_call.arguments)}"

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """Best-effort int conversion for untrusted values."""
        try:
            if isinstance(value, bool):
                return int(value)
            if value is None:
                return default
            return int(str(value).strip())
        except Exception:
            return default

    @staticmethod
    def _is_network_tool_name(tool_name: str) -> bool:
        """Heuristic classification for network/egress-heavy tools."""
        name = str(tool_name or "").strip().lower()
        if not name:
            return False
        return (
            name.startswith("web_")
            or name.startswith("comfyui_")
            or name.startswith("mcp_")
            or name in {"send_message", "a2a_discover_peers", "a2a_call_agent"}
        )

    @staticmethod
    def _get_session_labels(session: Session) -> set[str]:
        """Get normalized data labels associated with a session."""
        raw = session.metadata.get("_data_labels", [])
        if not isinstance(raw, list):
            return set()
        return {str(v).strip().lower() for v in raw if str(v).strip()}

    @staticmethod
    def _set_session_labels(session: Session, labels: set[str]) -> None:
        """Persist normalized data labels to session metadata."""
        session.metadata["_data_labels"] = sorted(
            {str(v).strip().lower() for v in labels if str(v).strip()}
        )

    @staticmethod
    def _estimate_result_bytes(result: ToolResult) -> int:
        """Approximate tool result payload size in bytes."""
        total = 0
        try:
            total += len(str(result.output or "").encode("utf-8"))
        except Exception:
            pass
        try:
            total += len(str(result.error or "").encode("utf-8"))
        except Exception:
            pass
        try:
            total += len(json.dumps(result.data or {}, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            pass
        image_path = str(getattr(result, "image_path", "") or "").strip()
        if image_path:
            try:
                p = Path(image_path)
                if p.is_file():
                    total += max(0, int(p.stat().st_size))
            except Exception:
                pass
        return max(0, total)

    def _get_budget_fuse_state(self, session: Session) -> dict[str, Any]:
        """Get mutable session budget-fuse counters."""
        raw = session.metadata.get("_budget_fuse", {})
        state = dict(raw) if isinstance(raw, dict) else {}
        state["tool_calls"] = self._safe_int(state.get("tool_calls"), 0)
        state["network_calls"] = self._safe_int(state.get("network_calls"), 0)
        state["network_bytes"] = self._safe_int(state.get("network_bytes"), 0)
        state["locked"] = bool(state.get("locked", False))
        state["network_bytes_over"] = bool(state.get("network_bytes_over", False))
        return state

    @staticmethod
    def _set_budget_fuse_state(session: Session, state: dict[str, Any]) -> None:
        """Persist budget-fuse counters into session metadata."""
        session.metadata["_budget_fuse"] = {
            "tool_calls": max(0, int(state.get("tool_calls", 0) or 0)),
            "network_calls": max(0, int(state.get("network_calls", 0) or 0)),
            "network_bytes": max(0, int(state.get("network_bytes", 0) or 0)),
            "locked": bool(state.get("locked", False)),
            "network_bytes_over": bool(state.get("network_bytes_over", False)),
        }

    @staticmethod
    def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
        """Extract and parse the first JSON object from model output."""
        text = (raw_text or "").strip()
        if not text:
            return None

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)

        candidates = [text]
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            candidates.append(match.group(0))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue

        return None

    @staticmethod
    def _inspect_shell_command(command: str) -> dict[str, Any]:
        """Heuristic shell command inspection for bulk/destructive risk."""
        raw = str(command or "")
        lower = raw.lower()

        is_destructive = bool(_SHELL_DESTRUCTIVE_RE.search(lower))
        is_download = bool(_SHELL_DOWNLOAD_RE.search(lower))
        has_bulk_hint = bool(_SHELL_BULK_HINT_RE.search(raw))
        is_chained = any(op in raw for op in ("|", ";", "&&", "||"))

        score = 0
        if is_destructive:
            score += 2
        if is_download:
            score += 2
        if has_bulk_hint:
            score += 1
        if is_chained:
            score += 1

        est_items = 1
        if "*" in raw:
            est_items += 12
        if has_bulk_hint:
            est_items += 20
        if is_chained:
            est_items += 8
        if is_destructive:
            est_items += 10

        return {
            "is_destructive": is_destructive,
            "is_download": is_download,
            "has_bulk_hint": has_bulk_hint,
            "is_chained": is_chained,
            "bulk_score": score,
            "estimated_items": est_items,
        }

    async def _build_high_risk_plan(
        self,
        tool_call: ToolCall,
        session: Session,
        requested_model: str | None,
        user_text: str | None = None,
    ) -> dict[str, Any] | None:
        """Ask the model for a compact JSON execution plan for high-risk tools."""
        guard_cfg = getattr(self.config, "execution_guard", None)
        if not guard_cfg:
            return None

        latest_user = (user_text or "").strip()
        if not latest_user:
            for msg in reversed(session.messages):
                if msg.role == Role.USER and isinstance(msg.content, str) and msg.content.strip():
                    latest_user = msg.content.strip()
                    break

        latest_user = latest_user[:1600]
        model_name = (
            str(getattr(guard_cfg, "plan_model", "") or "").strip()
            or requested_model
            or str(session.metadata.get("_active_model") or self.model.default_model)
        )

        tool_payload = {
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
        }
        tool_json = json.dumps(tool_payload, ensure_ascii=False, default=str)[:2400]

        planning_prompt = (
            "Return JSON only (no markdown).\n"
            "Schema: {"
            "\"objective\":string,"
            "\"estimated_tool_calls\":int,"
            "\"estimated_shell_calls\":int,"
            "\"estimated_download_ops\":int,"
            "\"estimated_destructive_shell_ops\":int,"
            "\"estimated_files_touched\":int,"
            "\"risk_level\":\"low|medium|high\","
            "\"rationale\":string"
            "}.\n"
            "Use conservative (higher) estimates when uncertain."
        )

        plan_input = (
            f"User request:\n{latest_user}\n\n"
            f"Planned tool call:\n{tool_json}"
        )

        try:
            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": planning_prompt},
                    {"role": "user", "content": plan_input},
                ],
                model=model_name,
                tools=None,
                temperature=0.1,
                max_tokens=max(64, self._safe_int(getattr(guard_cfg, "plan_max_tokens", 280), 280)),
            )
        except Exception as e:
            logger.debug("execution_guard_plan_failed", error=str(e), tool=tool_call.name)
            return None

        plan = self._extract_json_object(response.content or "")
        if not plan:
            logger.debug("execution_guard_plan_parse_failed", tool=tool_call.name)
            return None

        plan.setdefault("objective", "")
        plan.setdefault("estimated_tool_calls", 1)
        plan.setdefault("estimated_shell_calls", 1 if tool_call.name == "shell_execute" else 0)
        plan.setdefault("estimated_download_ops", 0)
        plan.setdefault("estimated_destructive_shell_ops", 0)
        plan.setdefault("estimated_files_touched", 1)
        plan.setdefault("risk_level", "medium")
        plan.setdefault("rationale", "")
        return plan

    def _validate_high_risk_plan(
        self,
        plan: dict[str, Any],
        tool_call: ToolCall,
    ) -> list[str]:
        """Validate high-risk plan estimates against execution-guard limits."""
        guard_cfg = getattr(self.config, "execution_guard", None)
        if not guard_cfg:
            return []

        issues: list[str] = []
        max_tools = max(0, self._safe_int(getattr(guard_cfg, "max_tool_calls_per_task", 0), 0))
        max_shell = max(0, self._safe_int(getattr(guard_cfg, "max_shell_calls_per_task", 0), 0))
        max_download = max(0, self._safe_int(getattr(guard_cfg, "max_download_ops_per_task", 0), 0))
        max_destructive = max(
            0,
            self._safe_int(getattr(guard_cfg, "max_destructive_shell_ops_per_task", 0), 0),
        )

        est_tools = max(0, self._safe_int(plan.get("estimated_tool_calls"), 0))
        est_shell = max(0, self._safe_int(plan.get("estimated_shell_calls"), 0))
        est_download = max(0, self._safe_int(plan.get("estimated_download_ops"), 0))
        est_destructive = max(0, self._safe_int(plan.get("estimated_destructive_shell_ops"), 0))

        if max_tools and est_tools > max_tools:
            issues.append(f"estimated_tool_calls={est_tools} exceeds limit={max_tools}")

        if tool_call.name == "shell_execute":
            if max_shell and est_shell > max_shell:
                issues.append(f"estimated_shell_calls={est_shell} exceeds limit={max_shell}")
            if max_download and est_download > max_download:
                issues.append(f"estimated_download_ops={est_download} exceeds limit={max_download}")
            if max_destructive and est_destructive > max_destructive:
                issues.append(
                    f"estimated_destructive_shell_ops={est_destructive} exceeds limit={max_destructive}"
                )

        return issues

    async def _process_message_locked(
        self,
        user_text: str,
        session: Session,
        model: str | None = None,
        images: list[str] | None = None,
    ) -> str:
        """Internal message processing (called with session lock held)."""
        await self._ensure_mcp_tools_loaded()

        # Sanitize user input
        user_text = self.sanitizer.sanitize_user_input(user_text)
        event_agent_id = self._resolve_event_agent_id(session)

        # --- Task Complexity Routing ---
        trigger = self.config.task_complexity.trigger_mode
        if (
            self.complexity_estimator
            and self.config.task_complexity.enabled
            and trigger in ("auto", "auto_silent")
        ):
            try:
                complexity = await self.complexity_estimator.estimate(user_text, session)

                # Log for learning
                if self.config.task_complexity.track_accuracy:
                    try:
                        await self.action_log.log_complexity(session.id, complexity.to_dict())
                    except (AttributeError, TypeError):
                        pass  # action_log may not have log_complexity yet

                # If decomposition needed, delegate to sub-tasks
                if complexity.needs_decomposition and self.agent_manager:
                    # Add user message first so session has it
                    user_msg = Message(role=Role.USER, content=user_text)
                    session.add_message(user_msg)
                    await self.action_log.log_conversation(session.id, "user", user_text)
                    return await self._handle_complex_decomposition(
                        user_text, complexity, session
                    )

                # Otherwise, override model selection for this call only when
                # caller did not explicitly pin a model.
                if complexity.suggested_model and model is None:
                    model = complexity.suggested_model
                elif complexity.suggested_model and model is not None:
                    logger.info(
                        "complexity_model_suggestion_ignored",
                        requested_model=model,
                        suggested_model=complexity.suggested_model,
                    )
            except Exception as e:
                logger.debug("complexity_estimation_skipped", error=str(e))

        # Add user message (multimodal if images provided)
        user_content: str | list[dict] = user_text
        if images:
            target_for_images = model or self.model.default_model
            auth_mode = str(session.metadata.get("model_auth_mode", "api")).strip().lower()
            local_target = (
                auth_mode != "oauth"
                and self._is_local_model_target(target_for_images)
            )
            parts: list[dict] = [{"type": "text", "text": user_text}]
            for img in images:
                img_ref = str(img or "").strip()
                if not img_ref:
                    continue
                if img_ref.startswith("data:"):
                    url = img_ref
                elif self._is_http_image_url(img_ref) and not local_target:
                    # For cloud models, keep remote URL to avoid large base64 payloads.
                    url = img_ref
                else:
                    url = _encode_image_base64(img_ref)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            if len(parts) > 1:
                user_content = parts
        user_msg = Message(role=Role.USER, content=user_content)
        session.add_message(user_msg)

        # Emit message_received event for dashboard
        self._emit(
            "message_received",
            agent_id=event_agent_id,
            content=user_text[:120],
        )

        # Log conversation if in full mode
        await self.action_log.log_conversation(session.id, "user", user_text)

        # Build context with memory system (core_prompt + system_prompt + skills + MEMORY.md + RAG + conversation)
        active_skills = self.skills.get_active_skills() if self.skills else []
        try:
            context_messages = await self.memory.build_context(
                session,
                self.config.system_prompt,
                core_prompt=self.config.core_prompt,
                active_skills=active_skills,
            )
        except Exception as e:
            logger.warning("memory_context_failed", error=str(e))
            # Fallback: just use system prompt + session messages
            if not session.messages or session.messages[0].role != Role.SYSTEM:
                session.messages.insert(0, self._get_system_message())
            context_messages = session.messages

        # Inject agent-awareness context so the LLM knows about sub-agents
        agent_ctx = self._get_agent_context_message()
        if agent_ctx:
            # Insert after system messages but before conversation
            insert_idx = 0
            for i, m in enumerate(context_messages):
                if m.role != Role.SYSTEM:
                    insert_idx = i
                    break
            else:
                insert_idx = len(context_messages)
            context_messages.insert(insert_idx, agent_ctx)

        # Inject diagnostic guidance so the LLM knows about self-diagnostic tools
        try:
            from src.tools.analytics.diagnostic_tools import get_diagnostic_guidance_message
            diag_guidance = get_diagnostic_guidance_message(self.config)
            if diag_guidance:
                # Insert after system messages but before conversation
                insert_idx = 0
                for i, m in enumerate(context_messages):
                    if m.role != Role.SYSTEM:
                        insert_idx = i
                        break
                else:
                    insert_idx = len(context_messages)
                context_messages.insert(insert_idx, Message(
                    role=Role.SYSTEM, content=diag_guidance,
                ))
        except Exception:
            pass  # Diagnostic guidance injection is best-effort

        # Agent loop: call LLM -> handle tool calls -> repeat
        effective_model = model or self.model.default_model
        session.metadata["_active_model"] = effective_model
        logger.info(
            "engine_model_selected",
            adapter=session.adapter,
            session_id=session.id,
            requested_model=model,
            effective_model=effective_model,
        )
        max_rounds = getattr(self.config, "max_tool_rounds", _DEFAULT_MAX_TOOL_ROUNDS)
        blocked_tools_by_policy: set[str] = set()
        guard_state = self._new_execution_guard_state()
        risk_map = {
            "low": RiskLevel.LOW,
            "medium": RiskLevel.MEDIUM,
            "high": RiskLevel.HIGH,
            "critical": RiskLevel.CRITICAL,
        }
        full_access_mode = bool(
            getattr(self.config.security, "full_access_mode", False)
        )
        max_risk_name = str(
            getattr(self.config.security, "max_risk_level", "critical")
        ).strip().lower()
        max_risk_level = risk_map.get(max_risk_name, RiskLevel.CRITICAL)
        for round_num in range(max_rounds):
            active_model_for_budget = str(
                session.metadata.get("_active_model")
                or model
                or self.model.default_model
            )
            budget_block_msg = await self._check_budget_stop_guard(
                active_model_for_budget, session
            )
            if budget_block_msg:
                self._emit(
                    "error",
                    agent_id=event_agent_id,
                    content=budget_block_msg[:120],
                    metadata={
                        "budget_block": True,
                        "model": active_model_for_budget,
                    },
                )
                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=budget_block_msg,
                )
                session.add_message(assistant_msg)
                await self.action_log.log_conversation(
                    session.id, "assistant", budget_block_msg
                )
                return budget_block_msg

            request_context = self._normalize_system_messages(
                self._strip_non_context_tool_images(context_messages)
            )
            messages = [m.to_litellm() for m in request_context]
            tool_defs: list[dict[str, Any]] = []
            for t in self.tools.registry.get_all():
                if (not full_access_mode) and (t.risk_level > max_risk_level):
                    continue
                if t.name in blocked_tools_by_policy:
                    continue
                tool_defs.append(t.to_openai_tool())
            tools = tool_defs or None

            self._emit(
                "stream_start",
                agent_id=event_agent_id,
                content=f"LLM call round {round_num + 1}",
            )
            try:
                response = await self.model.complete(
                    messages=messages,
                    model=model,
                    tools=tools,
                )
            except ContextOverflowError as exc:
                # Auto-compress context and retry once
                logger.info(
                    "engine_context_overflow_compressing",
                    request_tokens=exc.token_count,
                    limit_tokens=exc.limit,
                    model=exc.model,
                )
                context_messages = await self._emergency_compress(
                    context_messages, exc.limit
                )
                request_context = self._normalize_system_messages(
                    self._strip_non_context_tool_images(context_messages)
                )
                messages = [m.to_litellm() for m in request_context]
                response = await self.model.complete(
                    messages=messages,
                    model=model,
                    tools=tools,
                )
            except VisionNotSupportedError as exc:
                # Model can't handle images — strip images and retry
                logger.info(
                    "engine_vision_fallback",
                    model=exc.model,
                )
                context_messages = self._convert_images_to_text(
                    context_messages
                )
                request_context = self._normalize_system_messages(
                    self._strip_non_context_tool_images(context_messages)
                )
                messages = [m.to_litellm() for m in request_context]
                response = await self.model.complete(
                    messages=messages,
                    model=model,
                    tools=tools,
                )

            resolved_response_model = (
                response.model
                or model
                or self.model.default_model
            )
            session.metadata["_active_model"] = resolved_response_model

            # Log token usage
            if response.usage:
                try:
                    await self.audit.log_token_usage(
                        session_id=session.id,
                        model=resolved_response_model,
                        prompt_tokens=response.usage.get("prompt_tokens", 0),
                        completion_tokens=response.usage.get("completion_tokens", 0),
                        total_tokens=response.usage.get("total_tokens", 0),
                    )
                    await self._notify_budget_threshold(
                        resolved_response_model,
                        session,
                    )
                except Exception:
                    pass  # Don't let token logging break the main loop

            if response.has_tool_calls:
                # Handle tool calls
                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
                session.add_message(assistant_msg)
                context_messages.append(assistant_msg)

                for tc in response.tool_calls:
                    self._emit(
                        "tool_call",
                        agent_id=event_agent_id,
                        content=f"Tool: {tc.name}",
                        metadata={"tool_name": tc.name},
                    )
                    result = await self._handle_tool_call(
                        tc,
                        session,
                        guard_state=guard_state,
                        model=model,
                        user_text=user_text,
                    )

                    # Emit tool_result or error event
                    if result.success:
                        self._emit(
                            "tool_result",
                            agent_id=event_agent_id,
                            content=f"{tc.name}: ok",
                            metadata={"tool_name": tc.name},
                        )
                    else:
                        self._emit(
                            "error",
                            agent_id=event_agent_id,
                            content=f"{tc.name}: {(result.error or 'error')[:120]}",
                            metadata={"tool_name": tc.name},
                        )

                    if result.data.get("policy_denied") or result.data.get("guard_denied"):
                        blocked_tools_by_policy.add(tc.name)

                    # Track generated images in session metadata so adapters
                    # can reliably send them as platform attachments.
                    if result.image_path:
                        generated = session.metadata.setdefault("generated_images", [])
                        if isinstance(generated, list):
                            if result.image_path not in generated:
                                generated.append(result.image_path)
                            # Keep bounded history to avoid unbounded growth.
                            if len(generated) > 100:
                                del generated[:-100]

                    # Sanitize tool output before adding to context
                    output = result.output if result.success else (result.error or "Error")
                    output = self.sanitizer.sanitize_tool_output(output)

                    # Check for injection in tool output
                    is_suspicious, matched = self.sanitizer.check_injection(output)
                    if is_suspicious:
                        await self.audit.log_security_event(
                            "injection_detected",
                            session_id=session.id,
                            details=f"Tool {tc.name} output matched: {matched}",
                        )

                    # Build tool result message (multimodal if screenshot)
                    content_value: str | list[dict[str, Any]] = output
                    if result.image_path:
                        if self._should_attach_tool_image_to_context(tc.name):
                            content_value = self._build_image_content(
                                output, result.image_path, model
                            )
                        else:
                            # Keep context compact/stable for generated images and
                            # other non-screen tools; adapters still send attachments.
                            content_value = (
                                f"{output}\n\n[Image saved at: {result.image_path}]"
                            )

                    tool_msg = Message(
                        role=Role.TOOL,
                        content=content_value,
                        name=tc.name,
                        tool_call_id=tc.id,
                    )
                    session.add_message(tool_msg)
                    context_messages.append(tool_msg)

                    # Notify UI callback (e.g. Web GUI screen preview)
                    if self.tool_callback:
                        try:
                            await self.tool_callback.on_tool_executed(
                                tc.name, tc.arguments, result
                            )
                        except Exception as cb_err:
                            logger.warning("tool_callback_error", error=str(cb_err))

                    # Code feedback loop: auto-check written code files
                    if (
                        tc.name == "file_write"
                        and result.success
                        and hasattr(self, "code_feedback")
                        and self.code_feedback
                    ):
                        file_path = tc.arguments.get("path", "")
                        try:
                            feedback = await self.code_feedback.post_write_check(file_path)
                            if feedback:
                                feedback_msg = Message(
                                    role=Role.SYSTEM,
                                    content=f"[Code Quality Feedback]\n{feedback}",
                                )
                                session.add_message(feedback_msg)
                                context_messages.append(feedback_msg)
                        except Exception as fb_err:
                            logger.debug("code_feedback_error", error=str(fb_err))
            else:
                # No tool calls - we have the final response
                content = response.content or ""

                # Enforce result reporting: if tools were used but response
                # is too brief or vague, inject a reminder and retry ONCE.
                if round_num > 0 and self._is_vague_response(content):
                    logger.info(
                        "enforcing_result_report",
                        session_id=session.id[:8],
                        vague_content=content[:60],
                        round_num=round_num,
                    )
                    retry_context = list(context_messages)
                    retry_context.append(
                        Message(
                            role=Role.USER,
                            content=(
                                "[Result Reporting Required] Your response was too brief. "
                                "You MUST report the specific results of the tools you used. "
                                "Include: what was done, what data was returned, and the "
                                "concrete outcome. Re-answer now with full details."
                            ),
                        )
                    )
                    try:
                        retry_context = self._normalize_system_messages(
                            retry_context
                        )
                        retry_messages = [m.to_litellm() for m in retry_context]
                        retry_resp = await self.model.complete(
                            messages=retry_messages, model=model, tools=None,
                        )
                        retry_content = retry_resp.content or ""
                        if len(retry_content.strip()) > len(content.strip()):
                            content = retry_content
                    except Exception as retry_err:
                        logger.debug("result_report_retry_failed", error=str(retry_err))
                        # Keep original content if retry fails

                # Warn if LLM mentions permissions without calling any tools
                if round_num == 0 and any(
                    kw in content
                    for kw in ("權限", "permission", "Permission", "denied", "Denied", "授權")
                ):
                    logger.warning(
                        "llm_phantom_permission_denial",
                        session_id=session.id[:8],
                        content_preview=content[:120],
                    )

                assistant_msg = Message(role=Role.ASSISTANT, content=content)
                session.add_message(assistant_msg)

                # Emit stream_end + response events for dashboard
                self._emit(
                    "stream_end",
                    agent_id=event_agent_id,
                    content="LLM finished",
                )
                self._emit(
                    "response",
                    agent_id=event_agent_id,
                    content=content[:120],
                )

                await self.action_log.log_conversation(
                    session.id, "assistant", content
                )

                # Persist session to history
                try:
                    await self.memory.save_session(session)
                except Exception as e:
                    logger.warning("session_save_failed", error=str(e))

                return content

        # Exhausted all tool rounds — force a final answer without tools
        # Use updated context_messages which now includes all tool results
        logger.warning("tool_rounds_exhausted", rounds=max_rounds)
        try:
            final_context = self._normalize_system_messages(context_messages)
            messages = [m.to_litellm() for m in final_context]
            final = await self.model.complete(
                messages=messages, model=model, tools=None,
            )
            content = final.content or ""
        except Exception:
            content = ""

        if not content:
            content = "I've reached the maximum number of tool call rounds. Please try a simpler request."

        session.add_message(Message(role=Role.ASSISTANT, content=content))
        self._emit(
            "stream_end",
            agent_id=event_agent_id,
            content="LLM finished (rounds exhausted)",
        )
        self._emit(
            "response",
            agent_id=event_agent_id,
            content=content[:120],
        )
        return content

    async def _handle_complex_decomposition(
        self,
        user_text: str,
        complexity: ComplexityResult,
        session: Session,
    ) -> str:
        """Handle a task that needs decomposition into sub-tasks.

        Uses TaskDecomposer to split the task, then delegates each sub-task
        to an agent via AgentManager, and finally synthesizes the results.
        """
        from src.core.complexity import SubTaskResult, TaskDecomposer
        from src.core.types import AgentDefinition

        decomposer = TaskDecomposer(self.model, self.config.task_complexity)
        sub_tasks = await decomposer.decompose(user_text, complexity, session)

        if not sub_tasks:
            logger.warning("decomposition_empty", task_preview=user_text[:80])
            # Fall back to normal processing with frontier model
            return await self._process_message_locked(
                user_text, session, model=complexity.suggested_model,
            )

        logger.info(
            "executing_subtasks",
            count=len(sub_tasks),
            parallel=self.config.task_complexity.parallel_subtasks,
        )

        # Group sub-tasks by execution_order for parallel execution
        order_groups: dict[int, list] = {}
        for st in sub_tasks:
            order_groups.setdefault(st.execution_order, []).append(st)

        results: list[SubTaskResult] = []
        import time as _time

        for order in sorted(order_groups.keys()):
            group = order_groups[order]

            async def _run_subtask(task) -> SubTaskResult:
                """Run a single sub-task via AgentManager."""
                defn_name = f"_complexity_subtask_{task.id}"
                defn = AgentDefinition(
                    name=defn_name,
                    model=task.suggested_model or complexity.suggested_model or self.model.default_model,
                    max_tool_rounds=5,
                    created_by="complexity_decomposer",
                )
                self.agent_manager.register(defn)
                start = _time.monotonic()
                try:
                    result_text = await self.agent_manager.delegate(
                        defn_name, task.description, parent_session=session,
                    )
                    duration = int((_time.monotonic() - start) * 1000)
                    return SubTaskResult(
                        task=task,
                        result=result_text,
                        model_used=defn.model,
                        duration_ms=duration,
                        success=True,
                    )
                except Exception as e:
                    duration = int((_time.monotonic() - start) * 1000)
                    logger.warning("subtask_failed", task_id=task.id, error=str(e))
                    return SubTaskResult(
                        task=task,
                        result=f"Error: {e}",
                        model_used=defn.model,
                        duration_ms=duration,
                        success=False,
                    )
                finally:
                    self.agent_manager.unregister(defn_name)

            if self.config.task_complexity.parallel_subtasks and len(group) > 1:
                # Run tasks with same execution_order in parallel
                group_results = await asyncio.gather(
                    *[_run_subtask(t) for t in group],
                    return_exceptions=False,
                )
                results.extend(group_results)
            else:
                # Sequential execution
                for task in group:
                    result = await _run_subtask(task)
                    results.append(result)

        # Synthesize results
        final = await decomposer.synthesize(user_text, results)

        # Store as assistant message
        assistant_msg = Message(role=Role.ASSISTANT, content=final)
        session.add_message(assistant_msg)
        await self.action_log.log_conversation(session.id, "assistant", final)

        # Persist session
        try:
            await self.memory.save_session(session)
        except Exception as e:
            logger.warning("session_save_failed", error=str(e))

        return final

    async def stream_message(
        self,
        user_text: str,
        session: Session,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream a response for simple messages (no tool calls).

        For messages that require tool calls, falls back to process_message.
        """
        await self._ensure_mcp_tools_loaded()

        user_text = self.sanitizer.sanitize_user_input(user_text)
        event_agent_id = self._resolve_event_agent_id(session)

        # Keep stream path aligned with process_message complexity behavior.
        trigger = self.config.task_complexity.trigger_mode
        if (
            self.complexity_estimator
            and self.config.task_complexity.enabled
            and trigger in ("auto", "auto_silent")
        ):
            try:
                complexity = await self.complexity_estimator.estimate(user_text, session)
                if self.config.task_complexity.track_accuracy:
                    try:
                        await self.action_log.log_complexity(
                            session.id, complexity.to_dict()
                        )
                    except (AttributeError, TypeError):
                        pass
                if complexity.suggested_model and model is None:
                    model = complexity.suggested_model
                elif complexity.suggested_model and model is not None:
                    logger.info(
                        "complexity_model_suggestion_ignored",
                        requested_model=model,
                        suggested_model=complexity.suggested_model,
                    )
            except Exception as e:
                logger.debug("complexity_estimation_skipped", error=str(e))

        logger.info(
            "engine_stream_model_selected",
            adapter=session.adapter,
            session_id=session.id,
            requested_model=model,
            effective_model=model or self.model.default_model,
        )

        user_msg = Message(role=Role.USER, content=user_text)
        session.add_message(user_msg)

        await self.action_log.log_conversation(session.id, "user", user_text)

        # Build the same rich context as process_message so streaming path keeps
        # personality, MEMORY.md, RAG memories and active skills consistent.
        active_skills = self.skills.get_active_skills() if self.skills else []
        try:
            context_messages = await self.memory.build_context(
                session,
                self.config.system_prompt,
                core_prompt=self.config.core_prompt,
                active_skills=active_skills,
            )
        except Exception as e:
            logger.warning("memory_context_failed_stream", error=str(e))
            if not session.messages or session.messages[0].role != Role.SYSTEM:
                session.messages.insert(0, self._get_system_message())
            context_messages = session.messages

        agent_ctx = self._get_agent_context_message()
        if agent_ctx:
            insert_idx = 0
            for i, m in enumerate(context_messages):
                if m.role != Role.SYSTEM:
                    insert_idx = i
                    break
            else:
                insert_idx = len(context_messages)
            context_messages.insert(insert_idx, agent_ctx)

        try:
            from src.tools.analytics.diagnostic_tools import get_diagnostic_guidance_message
            diag_guidance = get_diagnostic_guidance_message(self.config)
            if diag_guidance:
                insert_idx = 0
                for i, m in enumerate(context_messages):
                    if m.role != Role.SYSTEM:
                        insert_idx = i
                        break
                else:
                    insert_idx = len(context_messages)
                context_messages.insert(insert_idx, Message(
                    role=Role.SYSTEM, content=diag_guidance,
                ))
        except Exception:
            pass

        normalized = self._normalize_system_messages(context_messages)
        messages = [m.to_litellm() for m in normalized]
        tools = self.tools.registry.get_openai_tools() or None

        active_model_for_budget = str(
            session.metadata.get("_active_model")
            or model
            or self.model.default_model
        )
        budget_block_msg = await self._check_budget_stop_guard(
            active_model_for_budget, session
        )
        if budget_block_msg:
            self._emit(
                "error",
                agent_id=event_agent_id,
                content=budget_block_msg[:120],
                metadata={
                    "budget_block": True,
                    "model": active_model_for_budget,
                },
            )
            assistant_msg = Message(
                role=Role.ASSISTANT,
                content=budget_block_msg,
            )
            session.add_message(assistant_msg)
            await self.action_log.log_conversation(
                session.id, "assistant", budget_block_msg
            )
            yield budget_block_msg
            return

        # First try a non-streaming call to check for tool calls
        response = await self.model.complete(
            messages=messages,
            model=model,
            tools=tools,
        )

        resolved_response_model = (
            response.model
            or model
            or self.model.default_model
        )
        session.metadata["_active_model"] = resolved_response_model

        # Log token usage for stream path
        if response.usage:
            try:
                await self.audit.log_token_usage(
                    session_id=session.id,
                    model=resolved_response_model,
                    prompt_tokens=response.usage.get("prompt_tokens", 0),
                    completion_tokens=response.usage.get("completion_tokens", 0),
                    total_tokens=response.usage.get("total_tokens", 0),
                )
                await self._notify_budget_threshold(
                    resolved_response_model,
                    session,
                )
            except Exception:
                pass

        if response.has_tool_calls:
            # Has tool calls - process them and return final text
            # Remove the user message we just added (process_message will add it again)
            session.messages.pop()
            result = await self.process_message(user_text, session, model)
            yield result
        else:
            # Streaming no-tool path previously skipped dashboard events,
            # causing /dashboard stats to remain at zero for Web UI chats.
            self._emit(
                "message_received",
                agent_id=event_agent_id,
                content=user_text[:120],
            )
            self._emit(
                "stream_start",
                agent_id=event_agent_id,
                content="LLM streaming response",
            )

            # No tool calls - return the response
            content = response.content or ""
            assistant_msg = Message(role=Role.ASSISTANT, content=content)
            session.add_message(assistant_msg)

            await self.action_log.log_conversation(
                session.id, "assistant", content
            )

            # Yield in chunks to simulate streaming
            chunk_size = 4
            for i in range(0, len(content), chunk_size):
                yield content[i : i + chunk_size]

            self._emit(
                "stream_end",
                agent_id=event_agent_id,
                content="LLM finished",
            )
            self._emit(
                "response",
                agent_id=event_agent_id,
                content=content[:120],
            )

    async def _handle_tool_call(
        self,
        tool_call: ToolCall,
        session: Session,
        *,
        guard_state: dict[str, Any] | None = None,
        model: str | None = None,
        user_text: str | None = None,
    ) -> ToolResult:
        """Handle a single tool call with security checks, approval, and logging."""
        tool = self.tools.registry.get(tool_call.name)
        if tool is None:
            return ToolResult.fail(f"Unknown tool: {tool_call.name}")

        full_access_mode = bool(getattr(self.config.security, "full_access_mode", False))
        guard_cfg = getattr(self.config, "execution_guard", None)
        guard_enabled = bool(guard_cfg and getattr(guard_cfg, "enabled", False) and not full_access_mode)
        if guard_enabled and guard_state is None:
            guard_state = self._new_execution_guard_state()
        active_model = str(
            session.metadata.get("_active_model")
            or session.metadata.get("model_override")
            or self.model.default_model
        )
        policy_decision: Any = None
        force_explicit_approval = False
        forced_approval_reasons: list[str] = []

        async def _deny(
            reason: str,
            *,
            policy_denied: bool = False,
            guard_denied: bool = False,
            result_summary: str | None = None,
            extra_data: dict[str, Any] | None = None,
        ) -> ToolResult:
            await self.audit.log_tool_execution(
                session_id=session.id,
                source=session.adapter,
                tool_name=tool_call.name,
                parameters=tool_call.arguments,
                approved=False,
                risk_level=tool.risk_level.value,
                result_summary=result_summary or reason,
            )
            await self.action_log.log_tool_call(
                session_id=session.id,
                tool_name=tool_call.name,
                params=tool_call.arguments,
                status="denied",
                error=reason,
            )
            data: dict[str, Any] = {}
            if policy_denied:
                data["policy_denied"] = True
            if guard_denied:
                data["guard_denied"] = True
            if extra_data:
                data.update(extra_data)
            return ToolResult(success=False, error=f"Denied: {reason}", data=data)

        # === Security Layer 0: Tool availability check ===
        if (not full_access_mode) and (tool_call.name in self.config.security.disabled_tools):
            return await _deny(
                f"Tool '{tool_call.name}' is disabled by configuration",
                result_summary="Disabled by configuration",
            )

        # === Execution Guard Layer: duplicate loops + risky shell budgets ===
        tool_signature = ""
        if guard_enabled and guard_state is not None:
            tool_signature = self._tool_signature(tool_call)
            signature_counts = guard_state.setdefault("signature_counts", {})
            repeat_count = self._safe_int(signature_counts.get(tool_signature), 0) + 1
            signature_counts[tool_signature] = repeat_count

            max_repeat = max(0, self._safe_int(getattr(guard_cfg, "max_repeat_tool_call", 0), 0))
            if max_repeat and repeat_count > max_repeat:
                return await _deny(
                    (
                        "Execution guard blocked repeated tool call "
                        f"({tool_call.name}, repeat={repeat_count}, limit={max_repeat})"
                    ),
                    guard_denied=True,
                    result_summary="Execution guard: repeated tool call",
                    extra_data={"repeat_count": repeat_count, "repeat_limit": max_repeat},
                )

            guard_state["tool_calls"] = self._safe_int(guard_state.get("tool_calls"), 0) + 1
            max_tool_calls = max(0, self._safe_int(getattr(guard_cfg, "max_tool_calls_per_task", 0), 0))
            if max_tool_calls and guard_state["tool_calls"] > max_tool_calls:
                return await _deny(
                    (
                        "Execution guard blocked tool-call budget overflow "
                        f"(count={guard_state['tool_calls']}, limit={max_tool_calls})"
                    ),
                    guard_denied=True,
                    result_summary="Execution guard: tool-call budget exceeded",
                    extra_data={"tool_calls": guard_state["tool_calls"], "tool_call_limit": max_tool_calls},
                )

            if tool_call.name == "shell_execute":
                guard_state["shell_calls"] = self._safe_int(guard_state.get("shell_calls"), 0) + 1
                max_shell_calls = max(0, self._safe_int(getattr(guard_cfg, "max_shell_calls_per_task", 0), 0))
                if max_shell_calls and guard_state["shell_calls"] > max_shell_calls:
                    return await _deny(
                        (
                            "Execution guard blocked shell-call budget overflow "
                            f"(count={guard_state['shell_calls']}, limit={max_shell_calls})"
                        ),
                        guard_denied=True,
                        result_summary="Execution guard: shell-call budget exceeded",
                        extra_data={"shell_calls": guard_state["shell_calls"], "shell_call_limit": max_shell_calls},
                    )

                command = str(tool_call.arguments.get("command", "") or "")
                shell_meta = self._inspect_shell_command(command)

                if shell_meta["is_destructive"]:
                    guard_state["destructive_shell_ops"] = (
                        self._safe_int(guard_state.get("destructive_shell_ops"), 0) + 1
                    )
                    max_destructive = max(
                        0,
                        self._safe_int(getattr(guard_cfg, "max_destructive_shell_ops_per_task", 0), 0),
                    )
                    if max_destructive and guard_state["destructive_shell_ops"] > max_destructive:
                        return await _deny(
                            (
                                "Execution guard blocked destructive shell-operation budget overflow "
                                f"(count={guard_state['destructive_shell_ops']}, limit={max_destructive})"
                            ),
                            guard_denied=True,
                            result_summary="Execution guard: destructive shell-op budget exceeded",
                            extra_data={
                                "destructive_shell_ops": guard_state["destructive_shell_ops"],
                                "destructive_shell_op_limit": max_destructive,
                            },
                        )

                if shell_meta["is_download"]:
                    guard_state["download_ops"] = self._safe_int(guard_state.get("download_ops"), 0) + 1
                    max_download = max(0, self._safe_int(getattr(guard_cfg, "max_download_ops_per_task", 0), 0))
                    if max_download and guard_state["download_ops"] > max_download:
                        return await _deny(
                            (
                                "Execution guard blocked download-operation budget overflow "
                                f"(count={guard_state['download_ops']}, limit={max_download})"
                            ),
                            guard_denied=True,
                            result_summary="Execution guard: download-op budget exceeded",
                            extra_data={"download_ops": guard_state["download_ops"], "download_op_limit": max_download},
                        )

                threshold = max(1, self._safe_int(getattr(guard_cfg, "bulk_shell_score_threshold", 4), 4))
                if bool(getattr(guard_cfg, "require_confirm_for_bulk_shell", True)) and shell_meta["bulk_score"] >= threshold:
                    confirmed = guard_state.setdefault("bulk_confirmed", set())
                    if tool_signature not in confirmed:
                        approved_bulk = await self.approval_cb.request_approval(
                            "shell_execute (bulk safety check)",
                            {
                                "command": command,
                                "bulk_score": shell_meta["bulk_score"],
                                "estimated_items": shell_meta["estimated_items"],
                                "reason": "Bulk shell operation detected by execution guard",
                            },
                            tool.risk_level,
                            session,
                        )
                        if not approved_bulk:
                            return await _deny(
                                "Execution guard denied bulk shell operation",
                                guard_denied=True,
                                result_summary="Execution guard: bulk shell confirmation denied",
                                extra_data={
                                    "bulk_score": shell_meta["bulk_score"],
                                    "estimated_items": shell_meta["estimated_items"],
                                },
                            )
                        confirmed.add(tool_signature)

            if (
                bool(getattr(guard_cfg, "require_plan_for_high_risk", True))
                and tool.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            ):
                plan_cache = guard_state.setdefault("high_risk_plan_cache", {})
                plan = plan_cache.get(tool_signature)
                if plan is None:
                    plan = await self._build_high_risk_plan(tool_call, session, model, user_text=user_text)
                    if plan is not None:
                        plan_cache[tool_signature] = plan

                if isinstance(plan, dict):
                    violations = self._validate_high_risk_plan(plan, tool_call)
                    if violations:
                        return await _deny(
                            "Execution guard plan rejected: " + "; ".join(violations),
                            guard_denied=True,
                            result_summary="Execution guard: high-risk plan rejected",
                            extra_data={"plan_violations": violations, "plan": plan},
                        )

        # === Security Layer 0.5: Tool Policy Core (context-aware rules) ===
        if not full_access_mode:
            tool_policy = getattr(self, "tool_policy", None)
            if tool_policy is not None:
                try:
                    policy_decision = tool_policy.evaluate(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        session=session,
                        active_model=active_model,
                        guard_state=guard_state,
                        session_labels=self._get_session_labels(session),
                    )
                except Exception as e:
                    logger.warning("tool_policy_check_failed", tool=tool_call.name, error=str(e))
                else:
                    if not bool(getattr(policy_decision, "allowed", True)):
                        return await _deny(
                            str(getattr(policy_decision, "reason", "Denied by tool policy")),
                            policy_denied=True,
                            result_summary="Tool policy denied",
                            extra_data={
                                "policy_rule": str(getattr(policy_decision, "matched_rule", "default")),
                            },
                        )

                    isolation_tier = str(
                        getattr(policy_decision, "isolation_tier", "standard") or "standard"
                    ).strip().lower()
                    if isolation_tier in {"restricted", "sealed"}:
                        force_explicit_approval = True
                        forced_approval_reasons.append(
                            f"Tool policy isolation tier '{isolation_tier}' requires explicit approval"
                        )
                    if (
                        isolation_tier == "sealed"
                        and tool.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
                    ):
                        return await _deny(
                            (
                                "Tool policy sealed isolation tier blocks high-risk tools "
                                f"({tool_call.name}, risk={tool.risk_level.value})"
                            ),
                            policy_denied=True,
                            result_summary="Tool policy sealed-tier denial",
                            extra_data={
                                "policy_rule": str(getattr(policy_decision, "matched_rule", "default")),
                                "isolation_tier": isolation_tier,
                            },
                        )

        # === Security Layer 0.6: Budget Fuse (session circuit breaker) ===
        budget_fuse_state: dict[str, Any] | None = None
        is_network_tool = self._is_network_tool_name(tool_call.name)
        budget_fuse_cfg = getattr(self.config, "budget_fuse", None)
        if (
            not full_access_mode
            and budget_fuse_cfg is not None
            and bool(getattr(budget_fuse_cfg, "enabled", False))
        ):
            action = str(getattr(budget_fuse_cfg, "action", "deny") or "deny").strip().lower()
            if action not in {"deny", "require_approval"}:
                action = "deny"

            budget_fuse_state = self._get_budget_fuse_state(session)
            if budget_fuse_state.get("locked"):
                return await _deny(
                    "Budget fuse is locked for this session (limit exceeded earlier)",
                    policy_denied=True,
                    result_summary="Budget fuse locked",
                    extra_data={"budget_fuse_locked": True},
                )

            next_tool_calls = int(budget_fuse_state.get("tool_calls", 0) or 0) + 1
            next_network_calls = int(budget_fuse_state.get("network_calls", 0) or 0)
            if is_network_tool:
                next_network_calls += 1

            max_tool_calls = max(
                0,
                self._safe_int(getattr(budget_fuse_cfg, "max_tool_calls_per_session", 0), 0),
            )
            max_network_calls = max(
                0,
                self._safe_int(getattr(budget_fuse_cfg, "max_network_calls_per_session", 0), 0),
            )
            max_network_bytes = max(
                0,
                self._safe_int(getattr(budget_fuse_cfg, "max_network_bytes_per_session", 0), 0),
            )

            violations: list[str] = []
            if max_tool_calls and next_tool_calls > max_tool_calls:
                violations.append(f"tool_calls={next_tool_calls}/{max_tool_calls}")
            if is_network_tool and max_network_calls and next_network_calls > max_network_calls:
                violations.append(f"network_calls={next_network_calls}/{max_network_calls}")
            if (
                is_network_tool
                and max_network_bytes
                and bool(budget_fuse_state.get("network_bytes_over", False))
            ):
                violations.append(
                    "network_bytes already above limit "
                    f"({int(budget_fuse_state.get('network_bytes', 0) or 0)}/{max_network_bytes})"
                )

            if violations:
                reason = "Budget fuse threshold reached: " + "; ".join(violations)
                if action == "deny":
                    budget_fuse_state["locked"] = True
                    self._set_budget_fuse_state(session, budget_fuse_state)
                    return await _deny(
                        reason,
                        policy_denied=True,
                        result_summary="Budget fuse denied",
                        extra_data={"budget_fuse_violation": violations},
                    )
                force_explicit_approval = True
                forced_approval_reasons.append(reason)

            budget_fuse_state["tool_calls"] = next_tool_calls
            if is_network_tool:
                budget_fuse_state["network_calls"] = next_network_calls
            self._set_budget_fuse_state(session, budget_fuse_state)

        # === Security Layer 1: Sandbox pre-check ===
        if (not full_access_mode) and tool_call.name == "shell_execute":
            command = tool_call.arguments.get("command", "")
            if not self.sandbox.is_command_allowed(command):
                return await _deny(
                    "Command blocked by sandbox policy",
                    result_summary="Blocked by sandbox",
                )

        if (not full_access_mode) and (tool_call.name in ("file_read", "file_write", "file_search")):
            path = tool_call.arguments.get("path", tool_call.arguments.get("directory", ""))
            if path:
                allowed, reason = self.sandbox.validate_file_operation(
                    path,
                    operation="write" if tool_call.name == "file_write" else "read",
                )
                if not allowed:
                    return await _deny(reason, result_summary=f"Blocked: {reason}")

        # === Security Layer 2: Approval check ===
        decision = self.approval_policy.check(tool_call.name, tool.risk_level, session.id)
        if decision.method == "policy_denied":
            logger.warning(
                "tool_policy_denied",
                tool=tool_call.name,
                risk=tool.risk_level.value,
                reason=decision.reason,
            )
            return await _deny(
                decision.reason,
                policy_denied=True,
                result_summary=decision.reason,
            )

        needs_explicit_approval = force_explicit_approval or (not decision.approved)
        if needs_explicit_approval:
            if forced_approval_reasons:
                approval_reason = "; ".join(forced_approval_reasons)
            else:
                approval_reason = decision.reason
            logger.info(
                "tool_approval_requested",
                tool=tool_call.name,
                risk=tool.risk_level.value,
                reason=approval_reason,
                forced=bool(forced_approval_reasons),
            )
            approval_args = dict(tool_call.arguments)
            if forced_approval_reasons:
                approval_args["_forced_approval_reasons"] = list(forced_approval_reasons)
            approved = await self.approval_cb.request_approval(
                tool_call.name,
                approval_args,
                tool.risk_level,
                session,
            )
            if not approved:
                logger.warning(
                    "tool_approval_denied",
                    tool=tool_call.name,
                    risk=tool.risk_level.value,
                )
                return await _deny("User denied the action")

        # === Execute with timing ===
        context = ToolContext(
            session_id=session.id,
            config=self.config,
            model_router=self.model,
            active_model=active_model,
            allowed_directories=[str(p) for p in self.config.sandbox.allowed_directories],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
            agent_manager=self.agent_manager,
            session=session,
            team_manager=self.team_manager,
            instance_manager=self.instance_manager,
            memory_manager=self.memory,
            agent_instance_id=getattr(self, "agent_instance_id", None),
        )

        start = time.monotonic()
        result = await self.tools.execute(tool_call.name, tool_call.arguments, context)
        duration_ms = int((time.monotonic() - start) * 1000)

        if result.success and policy_decision is not None:
            taint_labels = list(getattr(policy_decision, "taint_on_success", []) or [])
            if taint_labels:
                labels_before = self._get_session_labels(session)
                merged = set(labels_before)
                merged.update(
                    str(v).strip().lower()
                    for v in taint_labels
                    if str(v).strip()
                )
                if merged != labels_before:
                    self._set_session_labels(session, merged)
                    logger.info(
                        "session_labels_updated",
                        session_id=session.id,
                        labels=sorted(merged),
                        tool=tool_call.name,
                    )

        if budget_fuse_state is not None and is_network_tool:
            action = str(getattr(budget_fuse_cfg, "action", "deny") or "deny").strip().lower()
            if action not in {"deny", "require_approval"}:
                action = "deny"
            max_network_bytes = max(
                0,
                self._safe_int(getattr(budget_fuse_cfg, "max_network_bytes_per_session", 0), 0),
            )
            used_bytes = self._estimate_result_bytes(result)
            budget_fuse_state["network_bytes"] = int(budget_fuse_state.get("network_bytes", 0) or 0) + used_bytes
            if max_network_bytes and budget_fuse_state["network_bytes"] > max_network_bytes:
                if action == "deny":
                    budget_fuse_state["locked"] = True
                else:
                    budget_fuse_state["network_bytes_over"] = True
                result.data["budget_fuse_warning"] = (
                    "Network byte budget exceeded: "
                    f"{budget_fuse_state['network_bytes']}/{max_network_bytes}"
                )
            self._set_budget_fuse_state(session, budget_fuse_state)

        # === Security Layer 3: Audit log ===
        await self.audit.log_tool_execution(
            session_id=session.id,
            source=session.adapter,
            tool_name=tool_call.name,
            parameters=tool_call.arguments,
            approved=True,
            risk_level=tool.risk_level.value,
            result_summary=f"{'ok' if result.success else 'error'} ({duration_ms}ms)",
        )

        # === Action history log ===
        await self.action_log.log_tool_call(
            session_id=session.id,
            tool_name=tool_call.name,
            params=tool_call.arguments,
            result_output=result.output if result.success else (result.error or ""),
            status="ok" if result.success else "error",
            duration_ms=duration_ms,
            error=result.error,
        )

        # === LangSmith tool trace ===
        if self.config.tracing.enabled and self.config.tracing.trace_tools:
            try:
                from src.core.tracing import trace_tool_call
                trace_tool_call(
                    tool_name=tool_call.name,
                    params=tool_call.arguments,
                    result_output=result.output if result.success else None,
                    result_error=result.error,
                    success=result.success,
                    latency_ms=float(duration_ms),
                    tags=self.config.tracing.tags,
                )
            except Exception:
                pass

        return result
