"""Multi-agent system: create and manage sub-agents with different models.

Sub-agents are lightweight runners that share the main engine's infrastructure
(ModelRouter, ToolSystem, security) but operate with their own model, session,
system prompt, and optional tool restrictions.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from src.config import KuroConfig
from src.core.model_router import ModelRouter
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


class AgentRunner:
    """Runs a single sub-agent task.

    An AgentRunner is a lightweight agent loop that:
    1. Uses a specific model (from AgentDefinition)
    2. Has its own Session (conversation context)
    3. Optionally restricts available tools
    4. Shares the parent's ModelRouter, ToolSystem, and security stack

    It does NOT have:
    - Its own memory manager (sub-agents are ephemeral)
    - Its own skills system
    - The ability to spawn further sub-agents (no recursion)
    """

    def __init__(
        self,
        definition: AgentDefinition,
        model_router: ModelRouter,
        tool_system: ToolSystem,
        config: KuroConfig,
        approval_policy: ApprovalPolicy,
        approval_callback: ApprovalCallback,
        audit_log: AuditLog,
    ) -> None:
        self.definition = definition
        self.model = model_router
        self.tools = tool_system
        self.config = config
        self.approval_policy = approval_policy
        self.approval_cb = approval_callback
        self.audit = audit_log

        # Sub-agent security
        self.sandbox = Sandbox(config.sandbox)
        self.sanitizer = Sanitizer()

        # Own session
        self.session = Session(
            adapter="agent",
            user_id=f"agent:{definition.name}",
            metadata={"agent_name": definition.name, "agent_model": definition.model},
        )

        # State
        self._started_at: float | None = None
        self._completed = False
        self._result: str | None = None

    async def run(self, task: str) -> str:
        """Execute a task and return the result as a string.

        This is the sub-agent's main loop, similar to Engine.process_message()
        but simplified:
        - No memory context building (sub-agents are ephemeral)
        - Uses the agent's specific model
        - Respects tool allow/deny lists
        - Has its own max_tool_rounds
        """
        self._started_at = time.monotonic()

        # Build context: system prompt + task
        messages: list[Message] = []

        # System prompt (agent-specific or main config fallback)
        system_content = self.definition.system_prompt or self.config.system_prompt
        messages.append(Message(role=Role.SYSTEM, content=system_content))

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
                self._completed = True
                self._result = content
                return content

        # Exhausted rounds — force final answer
        logger.warning(
            "agent_tool_rounds_exhausted",
            agent=self.definition.name,
            rounds=max_rounds,
        )
        try:
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
        self._result = content
        return content

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

    async def _handle_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Handle a tool call with the same security pipeline as the main Engine."""
        tool = self.tools.registry.get(tool_call.name)
        if tool is None:
            return ToolResult.fail(f"Unknown tool: {tool_call.name}")

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

        # Approval check
        decision = self.approval_policy.check(
            tool_call.name, tool.risk_level, self.session.id
        )
        if not decision.approved:
            approved = await self.approval_cb.request_approval(
                tool_call.name,
                tool_call.arguments,
                tool.risk_level,
                self.session,
            )
            if not approved:
                return ToolResult.denied("User denied the action")

        # Execute
        context = ToolContext(
            session_id=self.session.id,
            allowed_directories=[
                str(p) for p in self.config.sandbox.allowed_directories
            ],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
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
    ) -> None:
        self.config = config
        self.model = model_router
        self.tools = tool_system
        self.approval_policy = approval_policy
        self.approval_cb = approval_callback
        self.audit = audit_log

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

    def get_definition(self, name: str) -> AgentDefinition | None:
        """Get an agent definition by name."""
        return self._definitions.get(name)

    def list_definitions(self) -> list[AgentDefinition]:
        """List all registered agent definitions."""
        return list(self._definitions.values())

    async def run_agent(self, name: str, task: str) -> str:
        """Run a registered agent with the given task.

        Creates an AgentRunner, executes the task, and returns the result.
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

        runner = AgentRunner(
            definition=defn,
            model_router=self.model,
            tool_system=self.tools,
            config=self.config,
            approval_policy=self.approval_policy,
            approval_callback=self.approval_cb,
            audit_log=self.audit,
        )

        self._running[name] = runner
        try:
            result = await runner.run(task)
            return result
        finally:
            self._running.pop(name, None)

    async def delegate(self, agent_name: str, task: str) -> str:
        """Delegate a task to a named agent. Called by the delegate_to_agent tool."""
        return await self.run_agent(agent_name, task)

    @property
    def definition_count(self) -> int:
        """Number of registered agent definitions."""
        return len(self._definitions)

    @property
    def running_count(self) -> int:
        """Number of currently running agents."""
        return len(self._running)
