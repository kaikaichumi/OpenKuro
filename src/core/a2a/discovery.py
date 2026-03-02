"""A2A Discovery: find and track remote Kuro instances on the network."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from src.core.a2a.client import A2AClient
from src.core.a2a.protocol import AgentCapability

logger = structlog.get_logger()


class AgentDiscovery:
    """Discovers and tracks remote Kuro instances and their capabilities.

    Supports:
    - Manual peer registration (from config)
    - Background capability refresh
    - Finding the best remote agent for a given task
    """

    def __init__(
        self,
        known_peers: list[str] | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._client = A2AClient(auth_token=auth_token)
        self._known_peers: set[str] = set(known_peers or [])
        self._capabilities: dict[str, list[AgentCapability]] = {}  # endpoint -> caps
        self._last_refresh: datetime | None = None

    def add_peer(self, endpoint: str) -> None:
        """Add a known peer endpoint."""
        self._known_peers.add(endpoint.rstrip("/"))

    def remove_peer(self, endpoint: str) -> None:
        """Remove a peer endpoint."""
        self._known_peers.discard(endpoint.rstrip("/"))
        self._capabilities.pop(endpoint.rstrip("/"), None)

    @property
    def known_peers(self) -> list[str]:
        """List of known peer endpoints."""
        return sorted(self._known_peers)

    async def refresh_capabilities(self) -> dict[str, list[AgentCapability]]:
        """Fetch capabilities from all known peers.

        Returns a dict of endpoint -> list of capabilities.
        """
        results: dict[str, list[AgentCapability]] = {}

        async def _fetch_one(endpoint: str) -> tuple[str, list[AgentCapability]]:
            caps = await self._client.get_capabilities(endpoint)
            return endpoint, caps

        # Fetch all peers in parallel
        tasks = [_fetch_one(ep) for ep in self._known_peers]
        if not tasks:
            return results

        completed = await asyncio.gather(*tasks, return_exceptions=True)
        for result in completed:
            if isinstance(result, tuple):
                endpoint, caps = result
                if caps:
                    results[endpoint] = caps
                    logger.debug(
                        "a2a_capabilities_refreshed",
                        endpoint=endpoint,
                        agent_count=len(caps),
                    )

        self._capabilities = results
        self._last_refresh = datetime.now(timezone.utc)
        return results

    async def check_peers_health(self) -> dict[str, bool]:
        """Ping all known peers and return their health status."""
        results: dict[str, bool] = {}

        async def _ping_one(endpoint: str) -> tuple[str, bool]:
            alive = await self._client.ping(endpoint)
            return endpoint, alive

        tasks = [_ping_one(ep) for ep in self._known_peers]
        if not tasks:
            return results

        completed = await asyncio.gather(*tasks, return_exceptions=True)
        for result in completed:
            if isinstance(result, tuple):
                endpoint, alive = result
                results[endpoint] = alive

        return results

    def get_all_remote_agents(self) -> list[AgentCapability]:
        """Get a flat list of all known remote agent capabilities."""
        agents: list[AgentCapability] = []
        for caps in self._capabilities.values():
            agents.extend(caps)
        return agents

    def find_agent(
        self,
        agent_name: str | None = None,
        specialty: str | None = None,
    ) -> AgentCapability | None:
        """Find a remote agent by name or specialty.

        Args:
            agent_name: Exact agent name to find.
            specialty: Required specialty (e.g., "coding", "research").

        Returns the first matching capability, or None.
        """
        for caps in self._capabilities.values():
            for cap in caps:
                if agent_name and cap.agent_name == agent_name:
                    return cap
                if specialty and specialty in cap.specialties:
                    return cap
        return None

    async def delegate_to_remote(
        self,
        agent_name: str,
        task: str,
        endpoint: str | None = None,
        timeout: int = 120,
    ) -> Any:
        """Delegate a task to a remote agent.

        If endpoint is not provided, auto-discovers which peer has the agent.

        Returns the A2AResponse.
        """
        if not endpoint:
            cap = self.find_agent(agent_name=agent_name)
            if not cap:
                # Refresh and try again
                await self.refresh_capabilities()
                cap = self.find_agent(agent_name=agent_name)
            if not cap:
                from src.core.a2a.protocol import A2AResponse
                return A2AResponse(
                    success=False,
                    error=f"Remote agent '{agent_name}' not found on any peer",
                )
            endpoint = cap.endpoint

        return await self._client.delegate(
            endpoint, agent_name, task, timeout=timeout
        )
