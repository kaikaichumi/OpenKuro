"""Core engine: main agent loop orchestrating model calls, tools, and memory.

The engine receives a user message, builds context, calls the LLM,
handles tool calls with approval, and returns the final response.
Integrates with the security layer (approval, sandbox, audit, sanitizer).
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

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

logger = structlog.get_logger()

# Default maximum number of tool call rounds (overridable via config.max_tool_rounds)
_DEFAULT_MAX_TOOL_ROUNDS = 10


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
        self.memory = memory_manager or MemoryManager()
        self.skills = skills_manager
        self.agent_manager = agent_manager

        # Security components
        self.approval_policy = ApprovalPolicy(config.security)
        self.sandbox = Sandbox(config.sandbox)
        self.sanitizer = Sanitizer()
        self.audit = audit_log or AuditLog()

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

        lines = [
            "[Available Sub-Agents]",
            "You have the following sub-agents that run on DIFFERENT models.",
            "To delegate work to them, you MUST call the `delegate_to_agent` tool.",
            "Do NOT pretend to delegate — you must use the tool for it to actually run.",
            "Do NOT answer on behalf of a sub-agent — delegate the task and return their result.",
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

        return Message(role=Role.SYSTEM, content="\n".join(lines))

    async def process_message(
        self,
        user_text: str,
        session: Session,
        model: str | None = None,
    ) -> str:
        """Process a user message and return the assistant's response.

        This is the main entry point for the agent loop.
        """
        # Sanitize user input
        user_text = self.sanitizer.sanitize_user_input(user_text)

        # Add user message
        user_msg = Message(role=Role.USER, content=user_text)
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

                    # Add tool result as a message
                    tool_msg = Message(
                        role=Role.TOOL,
                        content=output,
                        name=tc.name,
                        tool_call_id=tc.id,
                    )
                    session.add_message(tool_msg)
                    context_messages.append(tool_msg)
            else:
                # No tool calls - we have the final response
                content = response.content or ""
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
            approved = await self.approval_cb.request_approval(
                tool_call.name,
                tool_call.arguments,
                tool.risk_level,
                session,
            )
            if not approved:
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
