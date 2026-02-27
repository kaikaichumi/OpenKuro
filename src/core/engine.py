"""Core engine: main agent loop orchestrating model calls, tools, and memory.

The engine receives a user message, builds context, calls the LLM,
handles tool calls with approval, and returns the final response.
Integrates with the security layer (approval, sandbox, audit, sanitizer).
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from src.config import KuroConfig
from src.core.action_log import ActionLogger
from src.core.memory.manager import MemoryManager
from src.core.model_router import ModelRouter
from src.core.security.approval import ApprovalPolicy
from src.core.security.audit import AuditLog
from src.core.security.sandbox import Sandbox
from src.core.security.sanitizer import Sanitizer
from src.core.tool_system import ToolSystem
from src.core.types import Message, ModelResponse, Role, Session, ToolCall
from src.tools.base import RiskLevel, ToolContext, ToolResult

if TYPE_CHECKING:
    from src.core.complexity import ComplexityEstimator, ComplexityResult

logger = structlog.get_logger()

# Default maximum number of tool call rounds (overridable via config.max_tool_rounds)
_DEFAULT_MAX_TOOL_ROUNDS = 10

# Maximum image size for vision (resize if larger to save tokens)
_MAX_SCREENSHOT_DIMENSION = 1280


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

        # Security components
        self.approval_policy = ApprovalPolicy(config.security)
        self.sandbox = Sandbox(config.sandbox)
        self.sanitizer = Sanitizer()
        self.audit = audit_log or AuditLog()

        # Task complexity estimator (set externally after construction)
        self.complexity_estimator: ComplexityEstimator | None = None

        # Per-session locks for collaborative sessions (and general concurrency safety)
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
        context = ToolContext(
            session_id="scheduler",
            working_directory=None,
            allowed_directories=[
                str(d) for d in self.sandbox.allowed_directories
            ],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
            agent_manager=self.agent_manager,
        )
        result = await self.tools.execute(tool_name, params, context)
        if result.success:
            return result.output
        raise RuntimeError(result.error or "Tool execution failed")

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
                f"max_rounds: {defn.max_tool_rounds}{tools_info}"
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

        return Message(role=Role.SYSTEM, content="\n".join(lines))

    async def process_message(
        self,
        user_text: str,
        session: Session,
        model: str | None = None,
        author_user_id: str | None = None,
    ) -> str:
        """Process a user message and return the assistant's response.

        This is the main entry point for the agent loop.
        Uses per-session locking to safely handle concurrent messages
        (important for collaborative sessions with multiple users).
        """
        async with self._get_session_lock(session.id):
            return await self._process_message_locked(
                user_text, session, model, author_user_id
            )

    async def _process_message_locked(
        self,
        user_text: str,
        session: Session,
        model: str | None = None,
        author_user_id: str | None = None,
    ) -> str:
        """Internal message processing (called with session lock held)."""
        # Sanitize user input
        user_text = self.sanitizer.sanitize_user_input(user_text)

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
                    user_msg = Message(role=Role.USER, content=user_text, author_user_id=author_user_id)
                    session.add_message(user_msg)
                    await self.action_log.log_conversation(session.id, "user", user_text)
                    return await self._handle_complex_decomposition(
                        user_text, complexity, session
                    )

                # Otherwise, override model selection for this call
                if complexity.suggested_model:
                    model = complexity.suggested_model
            except Exception as e:
                logger.debug("complexity_estimation_skipped", error=str(e))

        # Add user message (track author for collaborative sessions)
        user_msg = Message(role=Role.USER, content=user_text, author_user_id=author_user_id)
        session.add_message(user_msg)

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

        # Agent loop: call LLM -> handle tool calls -> repeat
        max_rounds = getattr(self.config, "max_tool_rounds", _DEFAULT_MAX_TOOL_ROUNDS)
        for round_num in range(max_rounds):
            messages = [m.to_litellm() for m in context_messages]
            tools = self.tools.registry.get_openai_tools() or None

            response = await self.model.complete(
                messages=messages,
                model=model,
                tools=tools,
            )

            # Log token usage
            if response.usage:
                try:
                    await self.audit.log_token_usage(
                        session_id=session.id,
                        model=response.model or model or self.model.default_model,
                        prompt_tokens=response.usage.get("prompt_tokens", 0),
                        completion_tokens=response.usage.get("completion_tokens", 0),
                        total_tokens=response.usage.get("total_tokens", 0),
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
                    result = await self._handle_tool_call(tc, session)

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
                        data_uri = _encode_image_base64(result.image_path)
                        if data_uri:
                            content_value = [
                                {"type": "text", "text": output},
                                {"type": "image_url", "image_url": {"url": data_uri}},
                            ]

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
            messages = [m.to_litellm() for m in context_messages]
            final = await self.model.complete(
                messages=messages, model=model, tools=None,
            )
            content = final.content or ""
        except Exception:
            content = ""

        if not content:
            content = "I've reached the maximum number of tool call rounds. Please try a simpler request."

        session.add_message(Message(role=Role.ASSISTANT, content=content))
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
        user_text = self.sanitizer.sanitize_user_input(user_text)
        user_msg = Message(role=Role.USER, content=user_text)
        session.add_message(user_msg)

        await self.action_log.log_conversation(session.id, "user", user_text)

        if not session.messages or session.messages[0].role != Role.SYSTEM:
            session.messages.insert(0, self._get_system_message())
            if self.config.core_prompt:
                session.messages.insert(
                    0, Message(role=Role.SYSTEM, content=self.config.core_prompt)
                )

        messages = session.get_litellm_messages()
        tools = self.tools.registry.get_openai_tools() or None

        # First try a non-streaming call to check for tool calls
        response = await self.model.complete(
            messages=messages,
            model=model,
            tools=tools,
        )

        # Log token usage for stream path
        if response.usage:
            try:
                await self.audit.log_token_usage(
                    session_id=session.id,
                    model=response.model or model or self.model.default_model,
                    prompt_tokens=response.usage.get("prompt_tokens", 0),
                    completion_tokens=response.usage.get("completion_tokens", 0),
                    total_tokens=response.usage.get("total_tokens", 0),
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

    async def _handle_tool_call(
        self,
        tool_call: ToolCall,
        session: Session,
    ) -> ToolResult:
        """Handle a single tool call with security checks, approval, and logging."""
        tool = self.tools.registry.get(tool_call.name)
        if tool is None:
            return ToolResult.fail(f"Unknown tool: {tool_call.name}")

        # === Security Layer 0: Tool availability check ===
        if tool_call.name in self.config.security.disabled_tools:
            await self.audit.log_tool_execution(
                session_id=session.id,
                source=session.adapter,
                tool_name=tool_call.name,
                parameters=tool_call.arguments,
                approved=False,
                risk_level=tool.risk_level.value,
                result_summary="Disabled by configuration",
            )
            return ToolResult.denied(
                f"Tool '{tool_call.name}' is disabled by configuration"
            )

        # === Security Layer 1: Sandbox pre-check ===
        if tool_call.name == "shell_execute":
            command = tool_call.arguments.get("command", "")
            if not self.sandbox.is_command_allowed(command):
                await self.audit.log_tool_execution(
                    session_id=session.id,
                    source=session.adapter,
                    tool_name=tool_call.name,
                    parameters=tool_call.arguments,
                    approved=False,
                    risk_level=tool.risk_level.value,
                    result_summary="Blocked by sandbox",
                )
                return ToolResult.denied("Command blocked by sandbox policy")

        if tool_call.name in ("file_read", "file_write", "file_search"):
            path = tool_call.arguments.get("path", tool_call.arguments.get("directory", ""))
            if path:
                allowed, reason = self.sandbox.validate_file_operation(
                    path,
                    operation="write" if tool_call.name == "file_write" else "read",
                )
                if not allowed:
                    await self.audit.log_tool_execution(
                        session_id=session.id,
                        source=session.adapter,
                        tool_name=tool_call.name,
                        parameters=tool_call.arguments,
                        approved=False,
                        risk_level=tool.risk_level.value,
                        result_summary=f"Blocked: {reason}",
                    )
                    return ToolResult.denied(reason)

        # === Security Layer 2: Approval check ===
        decision = self.approval_policy.check(
            tool_call.name, tool.risk_level, session.id
        )

        if not decision.approved:
            # Need human approval
            logger.info(
                "tool_approval_requested",
                tool=tool_call.name,
                risk=tool.risk_level.value,
                reason=decision.reason,
            )
            approved = await self.approval_cb.request_approval(
                tool_call.name,
                tool_call.arguments,
                tool.risk_level,
                session,
            )
            if not approved:
                logger.warning(
                    "tool_approval_denied",
                    tool=tool_call.name,
                    risk=tool.risk_level.value,
                )
                await self.audit.log_tool_execution(
                    session_id=session.id,
                    source=session.adapter,
                    tool_name=tool_call.name,
                    parameters=tool_call.arguments,
                    approved=False,
                    risk_level=tool.risk_level.value,
                )
                await self.action_log.log_tool_call(
                    session_id=session.id,
                    tool_name=tool_call.name,
                    params=tool_call.arguments,
                    status="denied",
                )
                return ToolResult.denied("User denied the action")

        # === Execute with timing ===
        context = ToolContext(
            session_id=session.id,
            allowed_directories=[str(p) for p in self.config.sandbox.allowed_directories],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
            agent_manager=self.agent_manager,
            session=session,
        )

        start = time.monotonic()
        result = await self.tools.execute(tool_call.name, tool_call.arguments, context)
        duration_ms = int((time.monotonic() - start) * 1000)

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

        return result
