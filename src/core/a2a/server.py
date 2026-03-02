"""A2A Server: HTTP endpoints for receiving remote agent requests.

Exposes API endpoints on the existing WebServer:
- POST /a2a/delegate  — Accept a remote task delegation
- GET  /a2a/capabilities — Advertise local agent capabilities
- POST /a2a/ping — Health check
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from src.core.a2a.protocol import A2ARequest, A2AResponse, AgentCapability

if TYPE_CHECKING:
    from aiohttp import web
    from src.core.agents import AgentManager
    from src.config import A2AConfig

logger = structlog.get_logger()

# Instance ID persists for the lifetime of this process
_INSTANCE_ID = str(uuid4())[:8]


class A2AServer:
    """Handles incoming A2A requests from remote Kuro instances.

    Designed to be mounted on an existing aiohttp web.Application:
        a2a_server = A2AServer(agent_manager, config)
        a2a_server.add_routes(app)
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        config: A2AConfig,
    ) -> None:
        self.agents = agent_manager
        self.config = config
        self._auth_token = self._resolve_auth_token()

    def _resolve_auth_token(self) -> str | None:
        """Resolve the auth token from environment."""
        env_var = self.config.auth_token_env
        return os.environ.get(env_var)

    def _check_auth(self, request: web.Request) -> bool:
        """Validate the Authorization header."""
        if not self._auth_token:
            # No token configured = open access (local network use)
            return True
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {self._auth_token}"

    def add_routes(self, app: web.Application) -> None:
        """Register A2A routes on an aiohttp application."""
        app.router.add_post("/a2a/delegate", self.handle_delegate)
        app.router.add_get("/a2a/capabilities", self.handle_capabilities)
        app.router.add_post("/a2a/ping", self.handle_ping)
        app.router.add_get("/a2a/status", self.handle_status)
        logger.info("a2a_server_routes_added")

    async def handle_delegate(self, request: web.Request) -> web.Response:
        """Handle a remote task delegation request."""
        from aiohttp import web

        if not self._check_auth(request):
            return web.json_response(
                {"error": "Unauthorized"}, status=401
            )

        try:
            data = await request.json()
            req = A2ARequest.from_dict(data)
        except Exception as e:
            return web.json_response(
                {"error": f"Invalid request: {e}"}, status=400
            )

        if not req.agent_name:
            return web.json_response(
                {"error": "agent_name is required"}, status=400
            )

        if not self.agents.has_agent(req.agent_name):
            return web.json_response(
                A2AResponse(
                    request_id=req.id,
                    success=False,
                    error=f"Agent '{req.agent_name}' not found on this instance",
                    instance_id=_INSTANCE_ID,
                ).to_dict(),
                status=404,
            )

        logger.info(
            "a2a_delegate_received",
            from_instance=req.from_instance,
            agent=req.agent_name,
            task_preview=req.task[:80],
        )

        start = time.monotonic()
        try:
            result = await self.agents.run_agent(req.agent_name, req.task)
            duration_ms = int((time.monotonic() - start) * 1000)

            defn = self.agents.get_definition(req.agent_name)
            model_used = defn.model if defn else ""

            resp = A2AResponse(
                request_id=req.id,
                success=True,
                result=result,
                duration_ms=duration_ms,
                model_used=model_used,
                instance_id=_INSTANCE_ID,
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            resp = A2AResponse(
                request_id=req.id,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
                instance_id=_INSTANCE_ID,
            )

        return web.json_response(resp.to_dict())

    async def handle_capabilities(self, request: web.Request) -> web.Response:
        """Return the capabilities of all local agents."""
        from aiohttp import web

        capabilities = []
        for defn in self.agents.list_definitions():
            cap = AgentCapability(
                agent_name=defn.name,
                instance_id=_INSTANCE_ID,
                model=defn.model,
                tools=defn.allowed_tools,
                endpoint=str(request.url.origin()),
            )
            capabilities.append(cap.to_dict())

        return web.json_response({
            "instance_id": _INSTANCE_ID,
            "capabilities": capabilities,
        })

    async def handle_ping(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        from aiohttp import web

        return web.json_response({
            "status": "ok",
            "instance_id": _INSTANCE_ID,
            "agents": self.agents.definition_count,
        })

    async def handle_status(self, request: web.Request) -> web.Response:
        """Detailed status endpoint."""
        from aiohttp import web

        return web.json_response({
            "instance_id": _INSTANCE_ID,
            "agents_registered": self.agents.definition_count,
            "agents_running": self.agents.running_count,
            "a2a_enabled": self.config.enabled,
        })
