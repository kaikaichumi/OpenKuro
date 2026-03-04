"""Agent Instance: a full Primary Agent with own memory, personality, and sub-agents.

An AgentInstance wraps an independent Engine instance that shares heavy
infrastructure (ModelRouter, ToolSystem, AuditLog) with the main engine
but owns its own MemoryManager, AgentManager, and personality.

This is distinct from sub-agents (AgentRunner) which are ephemeral task
executors with no memory or personality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import AgentInstanceConfig


@dataclass
class AgentInstance:
    """A fully independent Primary Agent with own memory and sessions."""

    id: str
    name: str
    config: AgentInstanceConfig
    engine: Any  # Engine (Any avoids circular import)
    memory_manager: Any  # MemoryManager
    agent_manager: Any  # AgentManager (this instance's sub-agent pool)
    personality_path: Path | None = None
    sessions: dict[str, Any] = field(default_factory=dict)  # persistent sessions
    bound_adapter: Any = None  # BaseAdapter bound to this instance

    async def process_message(
        self, text: str, session: Any, model: str | None = None
    ) -> str:
        """Process a message through this instance's engine."""
        return await self.engine.process_message(text, session, model)

    async def stream_message(
        self, text: str, session: Any, model: str | None = None
    ):
        """Stream a message through this instance's engine."""
        async for chunk in self.engine.stream_message(text, session, model):
            yield chunk

    def get_info(self) -> dict:
        """Return a summary dict for API/UI consumption."""
        cfg = self.config
        sub_agent_names = []
        if self.agent_manager:
            sub_agent_names = [a.name for a in self.agent_manager.list_definitions()]
        return {
            "id": self.id,
            "name": self.name,
            "enabled": cfg.enabled,
            "model": cfg.model,
            "temperature": cfg.temperature,
            "personality_mode": cfg.personality_mode,
            "memory_mode": cfg.memory.mode,
            "memory_linked_agents": cfg.memory.linked_agents,
            "bot_binding": {
                "adapter_type": cfg.bot_binding.adapter_type,
                "bot_token_env": cfg.bot_binding.bot_token_env,
            } if cfg.bot_binding.adapter_type else None,
            "invocation": {
                "allow_web_ui": cfg.invocation.allow_web_ui,
                "allow_main_agent": cfg.invocation.allow_main_agent,
                "allow_agents": cfg.invocation.allow_agents,
            },
            "sub_agents": sub_agent_names,
            "active_sessions": len(self.sessions),
        }
