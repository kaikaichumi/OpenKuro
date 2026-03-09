"""Agent Event Bus: real-time event tracking across all agents.

Captures message flow, tool calls, delegations, and errors to power
the live visualization dashboard.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import structlog

logger = structlog.get_logger()


@dataclass
class AgentEvent:
    """A single event in the agent system."""

    timestamp: float = field(default_factory=time.time)
    event_type: str = ""
    # "message_received" | "tool_call" | "tool_result" | "delegation" |
    # "stream_start" | "stream_end" | "response" | "error" | "status_change"
    source_agent: str = "main"  # agent instance ID
    target_agent: str | None = None  # for delegations
    content: str = ""  # Human-readable summary
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class AgentEventBus:
    """Central event bus for agent activity.

    Stores a bounded history of events and notifies subscribers in real-time.
    Intended for the dashboard WebSocket push and REST API.
    """

    def __init__(self, max_history: int = 1000) -> None:
        self._listeners: list[Callable[[AgentEvent], Any]] = []
        self._history: deque[AgentEvent] = deque(maxlen=max_history)
        # Per-agent stats (counters)
        self._agent_stats: dict[str, dict[str, int]] = {}

    # ── Public API ──────────────────────────────────────────────

    def emit(self, event: AgentEvent) -> None:
        """Record an event and notify all listeners."""
        self._history.append(event)
        self._update_stats(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.warning("event_listener_error", event_type=event.event_type)

    def subscribe(self, callback: Callable[[AgentEvent], Any]) -> None:
        """Register a listener that is called for every emitted event."""
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[AgentEvent], Any]) -> None:
        """Remove a listener."""
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return the *limit* most recent events as dicts."""
        events = list(self._history)
        return [e.to_dict() for e in events[-limit:]]

    def get_stats(self) -> dict:
        """Return aggregated statistics across all agents."""
        total_events = len(self._history)
        agents = {}
        for agent_id, counters in self._agent_stats.items():
            total_for_agent = sum(int(v) for v in counters.values())
            agents[agent_id] = {
                "total_events": total_for_agent,
                "messages": counters.get("message_received", 0),
                "tool_calls": counters.get("tool_call", 0),
                "delegations": counters.get("delegation", 0),
                "errors": counters.get("error", 0),
                "responses": counters.get("response", 0),
            }

        # Determine agent states (simplified: "busy" if recent stream_start without stream_end)
        agent_states = {}
        recent = list(self._history)[-200:]
        active_streams: dict[str, bool] = {}
        for evt in recent:
            if evt.event_type == "stream_start":
                active_streams[evt.source_agent] = True
            elif evt.event_type in ("stream_end", "response", "error"):
                active_streams[evt.source_agent] = False
        for agent_id in self._agent_stats:
            agent_states[agent_id] = "busy" if active_streams.get(agent_id) else "idle"

        return {
            "total_events": total_events,
            "agents": agents,
            "agent_states": agent_states,
        }

    # ── Convenience emitters ────────────────────────────────────

    def emit_message_received(
        self, agent_id: str = "main", content: str = "", **meta: Any
    ) -> None:
        self.emit(AgentEvent(
            event_type="message_received",
            source_agent=agent_id,
            content=content[:120],
            metadata=meta,
        ))

    def emit_tool_call(
        self, agent_id: str = "main", tool_name: str = "", **meta: Any
    ) -> None:
        self.emit(AgentEvent(
            event_type="tool_call",
            source_agent=agent_id,
            content=f"Tool: {tool_name}",
            metadata={"tool_name": tool_name, **meta},
        ))

    def emit_delegation(
        self, source: str, target: str, task: str = "", **meta: Any
    ) -> None:
        self.emit(AgentEvent(
            event_type="delegation",
            source_agent=source,
            target_agent=target,
            content=f"Delegated to {target}: {task[:80]}",
            metadata=meta,
        ))

    def emit_response(
        self, agent_id: str = "main", content: str = "", **meta: Any
    ) -> None:
        self.emit(AgentEvent(
            event_type="response",
            source_agent=agent_id,
            content=content[:120],
            metadata=meta,
        ))

    def emit_error(
        self, agent_id: str = "main", error: str = "", **meta: Any
    ) -> None:
        self.emit(AgentEvent(
            event_type="error",
            source_agent=agent_id,
            content=error[:200],
            metadata=meta,
        ))

    def emit_status_change(
        self, agent_id: str = "main", status: str = "", **meta: Any
    ) -> None:
        self.emit(AgentEvent(
            event_type="status_change",
            source_agent=agent_id,
            content=status,
            metadata=meta,
        ))

    # ── Internal ────────────────────────────────────────────────

    def _update_stats(self, event: AgentEvent) -> None:
        agent = event.source_agent or "main"
        if agent not in self._agent_stats:
            self._agent_stats[agent] = {}
        counters = self._agent_stats[agent]
        counters[event.event_type] = counters.get(event.event_type, 0) + 1
