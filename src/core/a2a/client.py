"""A2A Client: send task delegation requests to remote Kuro instances."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import structlog

from src.core.a2a.protocol import A2ARequest, A2AResponse, AgentCapability

logger = structlog.get_logger()

# Instance ID for this client
_CLIENT_INSTANCE_ID = str(uuid4())[:8]


class A2AClient:
    """HTTP client for communicating with remote Kuro instances.

    Supports:
    - Delegating tasks to remote agents
    - Fetching capabilities from remote instances
    - Health checking remote instances
    """

    def __init__(self, auth_token: str | None = None) -> None:
        self._auth_token = auth_token

    def _headers(self) -> dict[str, str]:
        """Build request headers with optional auth."""
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    async def delegate(
        self,
        endpoint: str,
        agent_name: str,
        task: str,
        timeout: int = 120,
    ) -> A2AResponse:
        """Send a task delegation request to a remote instance.

        Args:
            endpoint: Base URL of the remote instance (e.g. http://192.168.1.100:7860)
            agent_name: Name of the agent on the remote instance
            task: Task description
            timeout: Request timeout in seconds

        Returns:
            A2AResponse with the result or error
        """
        import aiohttp

        req = A2ARequest(
            from_instance=_CLIENT_INSTANCE_ID,
            agent_name=agent_name,
            task=task,
            timeout_seconds=timeout,
        )

        url = f"{endpoint.rstrip('/')}/a2a/delegate"
        start = time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=req.to_dict(),
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json()
                    return A2AResponse.from_dict(data)

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "a2a_delegate_failed",
                endpoint=endpoint,
                agent=agent_name,
                error=str(e),
            )
            return A2AResponse(
                request_id=req.id,
                success=False,
                error=f"Connection failed: {e}",
                duration_ms=duration_ms,
            )

    async def get_capabilities(
        self,
        endpoint: str,
        timeout: int = 10,
    ) -> list[AgentCapability]:
        """Fetch capabilities from a remote instance.

        Returns a list of AgentCapability, or empty list on error.
        """
        import aiohttp

        url = f"{endpoint.rstrip('/')}/a2a/capabilities"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json()
                    caps = []
                    for cap_data in data.get("capabilities", []):
                        cap_data["endpoint"] = endpoint
                        caps.append(AgentCapability.from_dict(cap_data))
                    return caps

        except Exception as e:
            logger.debug("a2a_capabilities_failed", endpoint=endpoint, error=str(e))
            return []

    async def ping(self, endpoint: str, timeout: int = 5) -> bool:
        """Check if a remote instance is alive.

        Returns True if the instance responds to /a2a/ping.
        """
        import aiohttp

        url = f"{endpoint.rstrip('/')}/a2a/ping"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={},
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json()
                    return data.get("status") == "ok"

        except Exception:
            return False
