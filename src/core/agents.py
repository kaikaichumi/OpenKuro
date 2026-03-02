"""Multi-agent system: create and manage sub-agents with different models.

Sub-agents are lightweight runners that share the main engine's infrastructure
(ModelRouter, ToolSystem, security) but operate with their own model, session,
system prompt, and optional tool restrictions.

Phase 1 enhancements (OpenClaw parity):
- Parent context injection: sub-agents can receive a summary of the parent conversation
- Recursive delegation: sub-agents can spawn further sub-agents (depth-limited)
- Structured output: sub-agents can return JSON dicts via output_schema
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from src.config import KuroConfig
from src.core.model_router import ContextOverflowError, ModelRouter
from src.core.security.approval import ApprovalPolicy
from src.core.security.audit import AuditLog
from src.core.security.sandbox import Sandbox
from src.core.security.sanitizer import Sanitizer
from src.core.tool_system import ToolSystem
from src.core.types import AgentDefinition, Message, Role, Session, ToolCall
from src.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from src.core.engine import ApprovalCallback

logger = structlog.get_logger()

# Minimum response length after tool use to trigger result reporting enforcement
_MIN_REPORT_LENGTH = 5

# Known vague placeholder responses
_VAGUE_RESPONSES = {
    "done", "ok", "okay", "got it", "sure", "completed", "finished",
    "完成", "好的", "已完成", "好了", "做完了", "搞定", "可以了", "了解",
    "done!", "ok!", "okay!", "完成！", "好的！", "已完成！", "好了！",
}


def _is_vague_response(content: str) -> bool:
    """Check if a response is too vague to serve as a proper result report."""
    stripped = content.strip()
    if not stripped:
        return True
    if len(stripped) < _MIN_REPORT_LENGTH:
        normalized = stripped.lower().rstrip("!！。.~～?？")
        if normalized in _VAGUE_RESPONSES:
            return True
        for vague in _VAGUE_RESPONSES:
            if normalized.endswith(vague):
                return True
    return False



class AgentRunner:
    """Runs a single sub-agent task.

    An AgentRunner is a lightweight agent loop that:
    1. Uses a specific model (from AgentDefinition)
    2. Has its own Session (conversation context)
    3. Optionally restricts available tools
    4. Shares the parent's ModelRouter, ToolSystem, and security stack
    5. Can inherit parent context (when inherit_context=True)
    6. Can recursively delegate to sub-agents (depth-limited by max_depth)
    7. Can return structured JSON output (when output_schema is set)

    It does NOT have:
    - Its own memory manager (sub-agents are ephemeral)
    - Its own skills system
    """

    # Maximum number of parent messages to include in context summary
    _MAX_PARENT_CONTEXT_MESSAGES = 10

    def __init__(
        self,
        definition: AgentDefinition,
        model_router: ModelRouter,
        tool_system: ToolSystem,
        config: KuroConfig,
        approval_policy: ApprovalPolicy,
        approval_callback: ApprovalCallback,
        audit_log: AuditLog,
        parent_session: Session | None = None,
        # Phase 1 enhancements
        parent_context: list[Message] | None = None,
        depth: int = 0,
        agent_manager: AgentManager | None = None,
    ) -> None:
        self.definition = definition
        self.model = model_router
        self.tools = tool_system
        self.config = config
        self.approval_policy = approval_policy
        self.approval_cb = approval_callback
        self.audit = audit_log
        # Parent session used for approval callbacks so the adapter
        # (e.g. Discord) can find the correct channel to send buttons to.
        self._parent_session = parent_session

        # Phase 1: parent context, recursion depth, agent manager reference
        self._parent_context = parent_context
        self._depth = depth
        self.agent_manager = agent_manager

        # Sub-agent security
        self.sandbox = Sandbox(config.sandbox)
        self.sanitizer = Sanitizer()

        # Own session
        self.session = Session(
            adapter="agent",
            user_id=f"agent:{definition.name}",
            metadata={
                "agent_name": definition.name,
                "agent_model": definition.model,
                "depth": depth,
            },
        )

        # State
        self._started_at: float | None = None
        self._completed = False
        self._result: str | dict[str, Any] | None = None

    async def run(self, task: str) -> str | dict[str, Any]:
        """Execute a task and return the result.

        Returns a string by default, or a dict if output_schema is configured.

        This is the sub-agent's main loop, similar to Engine.process_message()
        but simplified:
        - No memory context building (sub-agents are ephemeral)
        - Uses the agent's specific model
        - Respects tool allow/deny lists
        - Has its own max_tool_rounds
        - Can inject parent conversation context (Phase 1)
        - Can return structured JSON (Phase 1)
        """
        self._started_at = time.monotonic()

        # Build context: system prompt + task
        messages: list[Message] = []

        # System prompt (agent-specific or main config fallback)
        system_content = self.definition.system_prompt or self.config.system_prompt
        messages.append(Message(role=Role.SYSTEM, content=system_content))

        # Inject parent context summary (Phase 1: context inheritance)
        if self._parent_context and self.definition.inherit_context:
            context_summary = self._summarize_parent_context(self._parent_context)
            if context_summary:
                messages.append(Message(
                    role=Role.SYSTEM,
                    content=f"[Parent Conversation Context]\n{context_summary}",
                ))

        # Inject structured output instructions if schema is set
        if self.definition.output_schema:
            schema_str = json.dumps(self.definition.output_schema, ensure_ascii=False)
            messages.append(Message(
                role=Role.SYSTEM,
                content=(
                    "[Structured Output Required]\n"
                    "You MUST return your final response as valid JSON matching this schema:\n"
                    f"```json\n{schema_str}\n```\n"
                    "Do NOT wrap in markdown code blocks. Return raw JSON only."
                ),
            ))

        # Inject depth info for recursive agents
        if self._depth > 0:
            messages.append(Message(
                role=Role.SYSTEM,
                content=f"[Agent Depth: {self._depth}/{self.definition.max_depth}]",
            ))

        # User task
        task = self.sanitizer.sanitize_user_input(task)
        messages.append(Message(role=Role.USER, content=task))
        self.session.add_message(Message(role=Role.USER, content=task))

        # Get filtered tools
        tools = self.tools.registry.get_openai_tools_filtered(
            allowed=self.definition.allowed_tools or None,
            denied=self.definition.denied_tools or None,
        ) or None

        # Agent loop
        max_rounds = self.definition.max_tool_rounds
        for _round_num in range(max_rounds):
            litellm_msgs = [m.to_litellm() for m in messages]

            try:
                response = await self.model.complete(
                    messages=litellm_msgs,
                    model=self.definition.model,
                    tools=tools,
                    temperature=self.definition.temperature,
                    max_tokens=self.definition.max_tokens,
                )
            except ContextOverflowError as exc:
                # Auto-compress: trim tool results & old messages, then retry
                logger.info(
                    "agent_context_overflow_compressing",
                    agent=self.definition.name,
                    request_tokens=exc.token_count,
                    limit_tokens=exc.limit,
                )
                messages = self._compress_for_overflow(messages, exc.limit)
                litellm_msgs = [m.to_litellm() for m in messages]
                response = await self.model.complete(
                    messages=litellm_msgs,
                    model=self.definition.model,
                    tools=tools,
                    temperature=self.definition.temperature,
                    max_tokens=self.definition.max_tokens,
                )

            # Log token usage for sub-agent
            await self._log_tokens(response)

            if response.has_tool_calls:
                # Handle tool calls (same security pipeline as main engine)
                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
                messages.append(assistant_msg)
                self.session.add_message(assistant_msg)

                for tc in response.tool_calls:
                    result = await self._handle_tool_call(tc)
                    output = result.output if result.success else (result.error or "Error")
                    output = self.sanitizer.sanitize_tool_output(output)

                    # Vision fallback for sub-agents: convert images to text
                    if result.image_path:
                        try:
                            from src.tools.screen.analyze_image import run_image_analysis

                            analysis = run_image_analysis(result.image_path)
                            output = f"{output}\n\n{analysis}"
                        except Exception:
                            output = f"{output}\n\n[Image at: {result.image_path}]"

                    tool_msg = Message(
                        role=Role.TOOL,
                        content=output,
                        name=tc.name,
                        tool_call_id=tc.id,
                    )
                    messages.append(tool_msg)
                    self.session.add_message(tool_msg)
            else:
                content = response.content or ""

                # Enforce result reporting for sub-agents too:
                # if tools were used but the final response is vague, retry once.
                if _round_num > 0 and _is_vague_response(content):
                    logger.info(
                        "agent_enforcing_result_report",
                        agent=self.definition.name,
                        vague_content=content[:60],
                    )
                    messages.append(Message(role=Role.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=Role.SYSTEM,
                            content=(
                                "[Result Reporting Required] Your response was too brief. "
                                "Report the specific results of the tools you used. "
                                "Include concrete outcomes and data."
                            ),
                        )
                    )
                    try:
                        retry_msgs = [m.to_litellm() for m in messages]
                        retry_resp = await self.model.complete(
                            messages=retry_msgs,
                            model=self.definition.model,
                            tools=None,
                        )
                        retry_content = retry_resp.content or ""
                        if len(retry_content.strip()) > len(content.strip()):
                            content = retry_content
                    except Exception:
                        pass  # Keep original content

                self._completed = True
                result = self._maybe_parse_structured(content)
                self._result = result
                return result

        # Exhausted rounds — force final answer
        logger.warning(
            "agent_tool_rounds_exhausted",
            agent=self.definition.name,
            rounds=max_rounds,
        )
        try:
            litellm_msgs = [m.to_litellm() for m in messages]
            try:
                final = await self.model.complete(
                    messages=litellm_msgs,
                    model=self.definition.model,
                    tools=None,
                )
            except ContextOverflowError as exc:
                messages = self._compress_for_overflow(messages, exc.limit)
                litellm_msgs = [m.to_litellm() for m in messages]
                final = await self.model.complete(
                    messages=litellm_msgs,
                    model=self.definition.model,
                    tools=None,
                )
            await self._log_tokens(final)
            content = final.content or ""
        except Exception:
            content = (
                f"Agent '{self.definition.name}' exhausted tool rounds "
                f"without a final answer."
            )

        self._completed = True
        result = self._maybe_parse_structured(content)
        self._result = result
        return result

    async def _log_tokens(self, response: Any) -> None:
        """Log token usage from a sub-agent LLM response."""
        if not getattr(response, "usage", None):
            return
        try:
            await self.audit.log_token_usage(
                session_id=self.session.id,
                model=response.model or self.definition.model,
                prompt_tokens=response.usage.get("prompt_tokens", 0),
                completion_tokens=response.usage.get("completion_tokens", 0),
                total_tokens=response.usage.get("total_tokens", 0),
            )
        except Exception:
            pass  # Don't let token logging break the agent loop

    # ------------------------------------------------------------------
    # Phase 1: Parent context & structured output helpers
    # ------------------------------------------------------------------

    def _summarize_parent_context(self, parent_messages: list[Message]) -> str:
        """Build a concise summary of the parent conversation for context injection.

        Takes the most recent user/assistant messages and formats them as a
        compressed context block. Limits total characters to avoid token bloat.
        """
        relevant: list[str] = []
        max_msgs = self._MAX_PARENT_CONTEXT_MESSAGES
        max_chars_per_msg = 300

        # Walk backwards to get the most recent messages
        for msg in reversed(parent_messages):
            if msg.role in (Role.USER, Role.ASSISTANT) and isinstance(msg.content, str):
                truncated = msg.content[:max_chars_per_msg]
                if len(msg.content) > max_chars_per_msg:
                    truncated += "..."
                relevant.append(f"[{msg.role.value}]: {truncated}")
                if len(relevant) >= max_msgs:
                    break

        if not relevant:
            return ""

        relevant.reverse()  # Chronological order
        return "\n".join(relevant)

    def _maybe_parse_structured(self, content: str) -> str | dict[str, Any]:
        """Parse LLM response as structured JSON if output_schema is configured.

        Returns the original string if no schema is set or parsing fails.
        """
        if not self.definition.output_schema:
            return content

        text = content.strip()
        # Remove markdown code block wrappers
        if "```" in text:
            parts = text.split("```")
            for part in parts[1:]:
                clean = part.strip()
                if clean.startswith("json"):
                    clean = clean[4:].strip()
                if clean.startswith("{") or clean.startswith("["):
                    text = clean
                    break

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            # Wrap non-dict JSON (arrays, etc.) in a dict
            return {"data": parsed}
        except json.JSONDecodeError:
            logger.debug(
                "agent_structured_output_parse_failed",
                agent=self.definition.name,
                content_preview=content[:100],
            )
            return {"raw": content, "_parse_error": True}

    # ------------------------------------------------------------------
    # Context overflow auto-compression
    # ------------------------------------------------------------------

    _CHARS_PER_TOKEN = 4  # heuristic matching compressor.py

    def _compress_for_overflow(
        self, messages: list[Message], limit_tokens: int | None
    ) -> list[Message]:
        """Emergency-compress messages after a context overflow error.

        Strategy (from lightest to most aggressive):
        1. Truncate long tool-result messages (> 800 chars → keep first 400 + last 200).
        2. If still over budget, drop old middle messages keeping system + task + recent.
        """
        # --- Step 1: truncate long tool results ---
        compressed = []
        for m in messages:
            if m.role == Role.TOOL and isinstance(m.content, str) and len(m.content) > 800:
                truncated = m.content[:400] + "\n...(truncated)...\n" + m.content[-200:]
                compressed.append(
                    Message(
                        role=m.role,
                        content=truncated,
                        name=m.name,
                        tool_call_id=m.tool_call_id,
                    )
                )
            else:
                compressed.append(m)

        # Estimate tokens
        est = sum(
            len(m.content) if isinstance(m.content, str) else 0
            for m in compressed
        ) // self._CHARS_PER_TOKEN

        budget = limit_tokens or 8192  # conservative fallback
        target = int(budget * 0.75)  # leave 25% headroom for completion

        if est <= target:
            logger.info(
                "agent_overflow_compressed",
                strategy="truncate_tool_results",
                est_tokens=est,
                target=target,
            )
            return compressed

        # --- Step 2: drop old middle messages ---
        # Keep: system msgs + first user msg (task) + last N messages
        system_msgs: list[Message] = []
        first_user: Message | None = None
        rest: list[Message] = []

        for m in compressed:
            if m.role == Role.SYSTEM:
                system_msgs.append(m)
            elif first_user is None and m.role == Role.USER:
                first_user = m
            else:
                rest.append(m)

        # Keep trimming from the front of `rest` until under budget
        keep_recent = max(6, len(rest) // 2)
        while keep_recent > 2:
            candidate = system_msgs[:]
            if first_user:
                candidate.append(first_user)
            candidate.extend(rest[-keep_recent:])

            est = sum(
                len(m.content) if isinstance(m.content, str) else 0
                for m in candidate
            ) // self._CHARS_PER_TOKEN

            if est <= target:
                logger.info(
                    "agent_overflow_compressed",
                    strategy="drop_old_messages",
                    est_tokens=est,
                    target=target,
                    kept_recent=keep_recent,
                    dropped=len(rest) - keep_recent,
                )
                return candidate
            keep_recent -= 2

        # Last resort: system + task only
        final = system_msgs[:]
        if first_user:
            final.append(first_user)
        logger.warning(
            "agent_overflow_compressed_aggressive",
            est_tokens=sum(
                len(m.content) if isinstance(m.content, str) else 0
                for m in final
            ) // self._CHARS_PER_TOKEN,
        )
        return final

    async def _handle_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Handle a tool call with the same security pipeline as the main Engine."""
        tool = self.tools.registry.get(tool_call.name)
        if tool is None:
            return ToolResult.fail(f"Unknown tool: {tool_call.name}")

        # Phase 1: Recursive delegation depth check
        if tool_call.name == "delegate_to_agent":
            if self._depth >= self.definition.max_depth:
                return ToolResult.denied(
                    f"Maximum agent depth ({self.definition.max_depth}) reached at "
                    f"depth {self._depth}. Cannot create nested sub-agents."
                )
            if not self.agent_manager:
                return ToolResult.denied(
                    "Agent manager not available for recursive delegation."
                )

        # Check tool restrictions for this agent
        if (
            self.definition.allowed_tools
            and tool_call.name not in self.definition.allowed_tools
        ):
            return ToolResult.denied(
                f"Tool '{tool_call.name}' not allowed for agent '{self.definition.name}'"
            )

        if tool_call.name in self.definition.denied_tools:
            return ToolResult.denied(
                f"Tool '{tool_call.name}' denied for agent '{self.definition.name}'"
            )

        # Disabled tools check
        if tool_call.name in self.config.security.disabled_tools:
            return ToolResult.denied(
                f"Tool '{tool_call.name}' is disabled by configuration"
            )

        # Sandbox pre-check (same as Engine)
        if tool_call.name == "shell_execute":
            command = tool_call.arguments.get("command", "")
            if not self.sandbox.is_command_allowed(command):
                return ToolResult.denied("Command blocked by sandbox policy")

        if tool_call.name in ("file_read", "file_write", "file_search"):
            path = tool_call.arguments.get(
                "path", tool_call.arguments.get("directory", "")
            )
            if path:
                allowed, reason = self.sandbox.validate_file_operation(
                    path,
                    operation="write" if tool_call.name == "file_write" else "read",
                )
                if not allowed:
                    return ToolResult.denied(reason)

        # Approval check — use parent session for channel lookup so
        # adapter-based callbacks (Discord buttons, Telegram inline, etc.)
        # can find the correct channel/chat to send the approval prompt to.
        approval_session = self._parent_session or self.session
        decision = self.approval_policy.check(
            tool_call.name, tool.risk_level, approval_session.id
        )
        if not decision.approved:
            approved = await self.approval_cb.request_approval(
                tool_call.name,
                tool_call.arguments,
                tool.risk_level,
                approval_session,
            )
            if not approved:
                return ToolResult.denied("User denied the action")

        # Execute — pass agent_manager for recursive delegation (Phase 1)
        context = ToolContext(
            session_id=self.session.id,
            allowed_directories=[
                str(p) for p in self.config.sandbox.allowed_directories
            ],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
            agent_manager=self.agent_manager,
            session=self._parent_session or self.session,
        )

        start = time.monotonic()
        result = await self.tools.execute(
            tool_call.name, tool_call.arguments, context
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        # Audit
        await self.audit.log_tool_execution(
            session_id=self.session.id,
            source=f"agent:{self.definition.name}",
            tool_name=tool_call.name,
            parameters=tool_call.arguments,
            approved=True,
            risk_level=tool.risk_level.value,
            result_summary=f"{'ok' if result.success else 'error'} ({duration_ms}ms)",
        )

        return result

    @property
    def elapsed_seconds(self) -> float:
        """Time elapsed since the agent started running."""
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at


class AgentManager:
    """Registry and lifecycle manager for sub-agents.

    Responsibilities:
    - Store agent definitions (both predefined from config and user-created)
    - Create AgentRunner instances on demand
    - Track running agents
    - Enforce concurrency limits
    """

    def __init__(
        self,
        config: KuroConfig,
        model_router: ModelRouter,
        tool_system: ToolSystem,
        approval_policy: ApprovalPolicy,
        approval_callback: ApprovalCallback,
        audit_log: AuditLog,
        engine: Any = None,
    ) -> None:
        self.config = config
        self.model = model_router
        self.tools = tool_system
        self.approval_policy = approval_policy
        self.approval_cb = approval_callback
        self.audit = audit_log
        # Keep engine reference so we can read the *current* approval_cb
        # (adapters replace engine.approval_cb after AgentManager is created)
        self._engine = engine

        # Agent definitions registry
        self._definitions: dict[str, AgentDefinition] = {}

        # Running agents
        self._running: dict[str, AgentRunner] = {}

        # Load predefined agents from config
        self._load_predefined()

    def _load_predefined(self) -> None:
        """Load predefined agent definitions from config."""
        if not self.config.agents.enabled:
            return
        for agent_cfg in self.config.agents.predefined:
            defn = AgentDefinition(
                name=agent_cfg.name,
                model=agent_cfg.model,
                system_prompt=agent_cfg.system_prompt,
                allowed_tools=list(agent_cfg.allowed_tools),
                denied_tools=list(agent_cfg.denied_tools),
                max_tool_rounds=agent_cfg.max_tool_rounds,
                temperature=agent_cfg.temperature,
                max_tokens=agent_cfg.max_tokens,
                created_by="config",
                max_depth=agent_cfg.max_depth,
                inherit_context=agent_cfg.inherit_context,
                output_schema=agent_cfg.output_schema,
            )
            self._definitions[defn.name] = defn

    def register(self, definition: AgentDefinition) -> None:
        """Register a new agent definition."""
        self._definitions[definition.name] = definition
        logger.info(
            "agent_registered", name=definition.name, model=definition.model
        )

    def unregister(self, name: str) -> bool:
        """Remove an agent definition. Returns True if found."""
        if name in self._definitions:
            del self._definitions[name]
            return True
        return False

    def has_agent(self, name: str) -> bool:
        """Check if an agent with the given name is registered."""
        return name in self._definitions

    def get_definition(self, name: str) -> AgentDefinition | None:
        """Get an agent definition by name."""
        return self._definitions.get(name)

    def list_definitions(self) -> list[AgentDefinition]:
        """List all registered agent definitions."""
        return list(self._definitions.values())

    def _get_approval_cb(self) -> ApprovalCallback:
        """Get the current approval callback.

        Prefers the engine's callback (which adapters update at runtime)
        over the potentially stale reference stored at construction time.
        """
        if self._engine is not None:
            return self._engine.approval_cb
        return self.approval_cb

    async def run_agent(
        self,
        name: str,
        task: str,
        parent_session: Session | None = None,
        parent_context: list[Message] | None = None,
        depth: int = 0,
    ) -> str | dict[str, Any]:
        """Run a registered agent with the given task.

        Creates an AgentRunner, executes the task, and returns the result.
        Returns a string by default, or a dict if output_schema is configured.

        Args:
            name: Agent name to run.
            task: Task description for the agent.
            parent_session: The caller's session (used for approval channel lookup).
            parent_context: Parent conversation messages for context injection.
            depth: Current recursive delegation depth (0 = top-level).
        """
        defn = self._definitions.get(name)
        if defn is None:
            return f"Error: Agent '{name}' not found. Use /agents to list available agents."

        # Concurrency check
        if len(self._running) >= self.config.agents.max_concurrent_agents:
            return (
                f"Error: Maximum concurrent agents "
                f"({self.config.agents.max_concurrent_agents}) reached."
            )

        # Auto-populate parent_context from session if inherit_context is set
        if parent_context is None and parent_session and defn.inherit_context:
            parent_context = list(parent_session.messages)

        runner = AgentRunner(
            definition=defn,
            model_router=self.model,
            tool_system=self.tools,
            config=self.config,
            approval_policy=self.approval_policy,
            approval_callback=self._get_approval_cb(),
            audit_log=self.audit,
            parent_session=parent_session,
            parent_context=parent_context,
            depth=depth,
            agent_manager=self,  # Allow recursive delegation
        )

        # Use a unique key for running agents to allow parallel same-agent runs
        run_key = f"{name}:{id(runner)}"
        self._running[run_key] = runner
        try:
            result = await runner.run(task)
            return result
        finally:
            self._running.pop(run_key, None)

    async def delegate(
        self,
        agent_name: str,
        task: str,
        parent_session: Session | None = None,
        parent_context: list[Message] | None = None,
        depth: int = 0,
    ) -> str | dict[str, Any]:
        """Delegate a task to a named agent. Called by the delegate_to_agent tool."""
        return await self.run_agent(
            agent_name,
            task,
            parent_session=parent_session,
            parent_context=parent_context,
            depth=depth,
        )

    @property
    def definition_count(self) -> int:
        """Number of registered agent definitions."""
        return len(self._definitions)

    @property
    def running_count(self) -> int:
        """Number of currently running agents."""
        return len(self._running)
