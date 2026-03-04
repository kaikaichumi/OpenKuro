"""Agent Instance Manager: lifecycle management for Primary Agent instances.

Handles creation, initialization, and lookup of AgentInstance objects.
Each instance gets its own Engine, MemoryManager, and AgentManager
while sharing heavy infrastructure with the main engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from src.config import AgentInstanceConfig, KuroConfig, get_kuro_home
from src.core.agent_instance import AgentInstance
from src.core.memory.history import ConversationHistory
from src.core.memory.longterm import LongTermMemory
from src.core.memory.manager import MemoryManager
from src.core.memory.working import WorkingMemory
from src.core.types import AgentDefinition

logger = structlog.get_logger()


class AgentInstanceManager:
    """Registry and lifecycle manager for Primary Agent instances."""

    def __init__(
        self,
        config: KuroConfig,
        model_router: Any,
        tool_system: Any,
        approval_policy: Any,
        approval_callback: Any,
        audit_log: Any,
        main_memory_manager: MemoryManager,
        skills_manager: Any,
        action_logger: Any,
    ) -> None:
        self._config = config
        self._model_router = model_router
        self._tool_system = tool_system
        self._approval_policy = approval_policy
        self._approval_callback = approval_callback
        self._audit_log = audit_log
        self._main_memory = main_memory_manager
        self._skills_manager = skills_manager
        self._action_logger = action_logger
        self._instances: dict[str, AgentInstance] = {}

    async def initialize_all(self) -> None:
        """Initialize all configured agent instances."""
        # Two-pass init to handle linked memory dependencies
        # Pass 1: Create all instances with independent/shared memory
        for inst_cfg in self._config.agents.instances:
            if not inst_cfg.enabled:
                continue
            if inst_cfg.memory.mode != "linked":
                await self._create_instance_internal(inst_cfg)

        # Pass 2: Create linked-memory instances (dependencies now exist)
        for inst_cfg in self._config.agents.instances:
            if not inst_cfg.enabled:
                continue
            if inst_cfg.memory.mode == "linked":
                await self._create_instance_internal(inst_cfg)

        logger.info(
            "agent_instances_initialized",
            count=len(self._instances),
            ids=list(self._instances.keys()),
        )

    def get(self, instance_id: str) -> AgentInstance | None:
        """Get an agent instance by ID."""
        return self._instances.get(instance_id)

    def list_all(self) -> list[AgentInstance]:
        """List all agent instances."""
        return list(self._instances.values())

    async def create_instance(self, cfg: AgentInstanceConfig) -> AgentInstance:
        """Create a new agent instance at runtime."""
        if cfg.id in self._instances:
            raise ValueError(f"Agent instance '{cfg.id}' already exists")
        return await self._create_instance_internal(cfg)

    async def delete_instance(self, instance_id: str) -> bool:
        """Delete an agent instance."""
        inst = self._instances.pop(instance_id, None)
        if inst is None:
            return False
        logger.info("agent_instance_deleted", id=instance_id)
        return True

    def resolve_for_adapter(
        self, adapter_type: str, token_env: str
    ) -> AgentInstance | None:
        """Find the agent instance bound to a specific adapter."""
        for inst in self._instances.values():
            binding = inst.config.bot_binding
            if (
                binding.adapter_type == adapter_type
                and binding.bot_token_env == token_env
            ):
                return inst
        return None

    async def _create_instance_internal(
        self, cfg: AgentInstanceConfig
    ) -> AgentInstance:
        """Internal: build an AgentInstance with all components."""
        agent_home = get_kuro_home() / "agents" / cfg.id
        agent_home.mkdir(parents=True, exist_ok=True)

        # --- 1. Build MemoryManager based on mode ---
        mm = self._build_memory(cfg, agent_home)

        # Initialize advanced features (compression, lifecycle)
        mm.setup_advanced_features(model_router=self._model_router)

        # --- 2. Resolve personality path ---
        personality_path: Path | None = None
        if cfg.personality_mode == "independent":
            personality_path = agent_home / "personality.md"
            if not personality_path.exists():
                personality_path.write_text(
                    f"# {cfg.name} Personality\n\n"
                    f"Edit this file to customize {cfg.name}'s personality.\n",
                    encoding="utf-8",
                )
            mm._personality_path = personality_path
        # "shared" mode: mm._personality_path stays None → uses default

        # --- 3. Build instance-specific KuroConfig ---
        instance_config = self._build_instance_config(cfg)

        # --- 4. Build Engine ---
        from src.core.engine import ApprovalCallback, Engine

        instance_engine = Engine(
            config=instance_config,
            model_router=self._model_router,
            tool_system=self._tool_system,
            action_logger=self._action_logger,
            approval_callback=self._approval_callback,
            audit_log=self._audit_log,
            memory_manager=mm,
            skills_manager=self._skills_manager,
        )

        # --- 5. Build this instance's own AgentManager (sub-agent pool) ---
        from src.core.agents import AgentManager

        instance_agent_manager = AgentManager(
            config=instance_config,
            model_router=self._model_router,
            tool_system=self._tool_system,
            approval_policy=instance_engine.approval_policy,
            approval_callback=self._approval_callback,
            audit_log=self._audit_log,
            engine=instance_engine,
        )

        # Load this instance's sub-agents from config
        for sa_cfg in cfg.sub_agents:
            defn = AgentDefinition(
                name=sa_cfg.name,
                model=sa_cfg.model,
                system_prompt=sa_cfg.system_prompt,
                allowed_tools=list(sa_cfg.allowed_tools),
                denied_tools=list(sa_cfg.denied_tools),
                max_tool_rounds=sa_cfg.max_tool_rounds,
                temperature=sa_cfg.temperature,
                max_tokens=sa_cfg.max_tokens,
                created_by="config",
                max_depth=sa_cfg.max_depth,
                inherit_context=sa_cfg.inherit_context,
                output_schema=sa_cfg.output_schema,
            )
            instance_agent_manager.register(defn)

        instance_engine.agent_manager = instance_agent_manager

        # --- 6. Build AgentInstance ---
        instance = AgentInstance(
            id=cfg.id,
            name=cfg.name,
            config=cfg,
            engine=instance_engine,
            memory_manager=mm,
            agent_manager=instance_agent_manager,
            personality_path=personality_path,
        )
        self._instances[cfg.id] = instance

        logger.info(
            "agent_instance_created",
            id=cfg.id,
            name=cfg.name,
            model=cfg.model,
            memory_mode=cfg.memory.mode,
            sub_agents=len(cfg.sub_agents),
        )
        return instance

    def _build_memory(
        self, cfg: AgentInstanceConfig, agent_home: Path
    ) -> MemoryManager:
        """Build a MemoryManager based on the instance's memory mode."""
        mode = cfg.memory.mode

        if mode == "shared":
            # Reuse main engine's memory (same object)
            return self._main_memory

        if mode == "independent":
            # Fully independent memory
            (agent_home / "memory").mkdir(parents=True, exist_ok=True)
            history = ConversationHistory(
                db_path=str(agent_home / "history.db")
            )
            longterm = LongTermMemory(
                data_dir=str(agent_home / "memory")
            )
            return MemoryManager(
                working=WorkingMemory(),
                history=history,
                longterm=longterm,
                config=self._config,
            )

        if mode == "linked":
            # Own history, shared longterm with linked agent
            (agent_home / "memory").mkdir(parents=True, exist_ok=True)
            history = ConversationHistory(
                db_path=str(agent_home / "history.db")
            )

            # Find the linked agent's longterm memory
            linked_lt = None
            for linked_id in cfg.memory.linked_agents:
                linked_inst = self._instances.get(linked_id)
                if linked_inst:
                    linked_lt = linked_inst.memory_manager.longterm
                    break

            # Fallback to main memory if linked agent not found
            if linked_lt is None:
                linked_lt = self._main_memory.longterm
                logger.warning(
                    "linked_agent_not_found_fallback_to_main",
                    instance=cfg.id,
                    linked_agents=cfg.memory.linked_agents,
                )

            return MemoryManager(
                working=WorkingMemory(),
                history=history,
                longterm=linked_lt,
                config=self._config,
            )

        # Unknown mode — default to independent
        logger.warning("unknown_memory_mode", mode=mode, instance=cfg.id)
        return MemoryManager(config=self._config)

    def _build_instance_config(self, cfg: AgentInstanceConfig) -> KuroConfig:
        """Build a KuroConfig tailored for this instance.

        Inherits most settings from the global config, overriding only
        what the instance specifies.
        """
        # Deep copy the global config
        data = self._config.model_dump()

        # Override model settings if specified
        if cfg.model:
            data["models"]["default"] = cfg.model
        if cfg.temperature is not None:
            data["models"]["temperature"] = cfg.temperature
        if cfg.max_tokens is not None:
            data["models"]["max_tokens"] = cfg.max_tokens
        if cfg.system_prompt is not None:
            data["system_prompt"] = cfg.system_prompt
        if cfg.max_tool_rounds:
            data["max_tool_rounds"] = cfg.max_tool_rounds

        # Instance's sub_agents become the agents.sub_agents for its engine
        data["agents"]["sub_agents"] = [
            sa.model_dump() for sa in cfg.sub_agents
        ]
        data["agents"]["predefined"] = []
        data["agents"]["instances"] = []  # Don't recurse

        # Tool restrictions
        if cfg.allowed_tools:
            data["security"]["disabled_tools"] = []
        if cfg.denied_tools:
            existing = data["security"].get("disabled_tools", [])
            data["security"]["disabled_tools"] = list(
                set(existing) | set(cfg.denied_tools)
            )

        return KuroConfig(**data)
