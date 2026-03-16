"""Web GUI server: FastAPI + WebSocket with approval support.

Provides a browser-based chat interface at http://localhost:7860.
Uses WebSocket for real-time streaming and tool approval dialogs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.config import KuroConfig, MCPServerConfig
from src.core.engine import ApprovalCallback, Engine, ToolExecutionCallback, _encode_image_base64
from src.core.types import AgentDefinition, Session
from src.openai_catalog import (
    OPENAI_CODEX_OAUTH_MODELS,
    is_codex_oauth_model_supported,
    normalize_openai_model,
)
from src.ui.openai_oauth import OpenAIOAuthManager
from src.ui.page_schema import (
    UIPageSchemaRegistry,
    build_agents_page_schema,
    build_dashboard_page_schema,
    build_security_page_schema,
)
from src.ui.settings_schema import SettingsSchemaRegistry, build_core_settings_schema
from src.tools.base import RiskLevel, ToolResult

logger = structlog.get_logger()
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Path to static web files
WEB_DIR = Path(__file__).parent / "web"
_OPENAI_OAUTH_MODEL_DEFAULTS: list[str] = list(OPENAI_CODEX_OAUTH_MODELS)


@dataclass
class ConnectionState:
    """Mutable state for each WebSocket connection.

    Supports multi-agent panels: ``agent_sessions`` maps agent_id to a
    per-agent Session, and ``agent_chat_tasks`` tracks in-flight chat
    tasks per agent.  The top-level ``session`` / ``chat_task`` are the
    "main" agent's state.
    """

    session: Session
    model_override: str | None = None
    model_auth_mode: str = "api"  # api | oauth
    oauth_session_id: str | None = None
    chat_task: asyncio.Task | None = None

    # Multi-agent panel support
    agent_sessions: dict[str, Session] = field(default_factory=dict)
    agent_chat_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def get_session(self, agent_id: str | None) -> Session:
        """Return the session for *agent_id*, falling back to main."""
        if not agent_id or agent_id == "main":
            return self.session
        return self.agent_sessions.get(agent_id, self.session)

    def set_session(self, agent_id: str | None, session: Session) -> None:
        if not agent_id or agent_id == "main":
            self.session = session
        else:
            self.agent_sessions[agent_id] = session

    def get_chat_task(self, agent_id: str | None) -> asyncio.Task | None:
        if not agent_id or agent_id == "main":
            return self.chat_task
        return self.agent_chat_tasks.get(agent_id)

    def set_chat_task(self, agent_id: str | None, task: asyncio.Task) -> None:
        if not agent_id or agent_id == "main":
            self.chat_task = task
        else:
            self.agent_chat_tasks[agent_id] = task


class WebApprovalCallback(ApprovalCallback):
    """Tool approval via WebSocket + asyncio.Future."""

    def __init__(self, timeout: int = 60, approval_policy=None) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._websockets: dict[str, WebSocket] = {}
        self._session_agent_map: dict[str, str] = {}  # session_id → agent_id
        self._timeout = timeout
        self.approval_policy = approval_policy

    def register_websocket(
        self, session_id: str, ws: WebSocket, agent_id: str = "main"
    ) -> None:
        self._websockets[session_id] = ws
        self._session_agent_map[session_id] = agent_id

    def unregister_websocket(self, session_id: str) -> None:
        self._websockets.pop(session_id, None)
        self._session_agent_map.pop(session_id, None)
        # Deny all pending approvals for this session
        to_remove = []
        for approval_id, fut in self._pending.items():
            if approval_id.startswith(session_id):
                if not fut.done():
                    fut.set_result("deny")
                to_remove.append(approval_id)
        for aid in to_remove:
            self._pending.pop(aid, None)

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        ws = self._websockets.get(session.id)
        if ws is None:
            return risk_level == RiskLevel.LOW

        approval_id = f"{session.id}:{uuid.uuid4().hex[:8]}"
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[approval_id] = future

        # Send approval request to browser
        agent_id = self._session_agent_map.get(session.id, "main")
        try:
            await ws.send_json({
                "type": "approval_request",
                "approval_id": approval_id,
                "tool_name": tool_name,
                "params": params,
                "risk_level": risk_level.value,
                "agent_id": agent_id,
            })
        except Exception:
            self._pending.pop(approval_id, None)
            return False

        # Wait for user response
        try:
            action = await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            action = "deny"
        finally:
            self._pending.pop(approval_id, None)

        if action == "trust":
            session.trust_level = risk_level.value
            if self.approval_policy:
                self.approval_policy.elevate_session_trust(session.id, risk_level)

        return action in ("approve", "trust")

    def resolve_approval(self, approval_id: str, action: str) -> bool:
        fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(action)
        return True


class WebToolCallback(ToolExecutionCallback):
    """Push screen updates to the Web GUI when screenshot-related tools run."""

    # Tools that produce screenshots worth pushing to the frontend
    _SCREEN_TOOLS = {"screenshot", "computer_use", "web_screenshot"}
    # Tools whose actions are worth reporting (so the user can see what the AI did)
    _ACTION_TOOLS = {"mouse_action", "keyboard_action"}

    def __init__(self) -> None:
        self._websockets: dict[str, WebSocket] = {}
        self._step_counter: dict[str, int] = {}
        self._session_agent_map: dict[str, str] = {}  # session_id → agent_id

    def register_websocket(
        self, session_id: str, ws: WebSocket, agent_id: str = "main"
    ) -> None:
        self._websockets[session_id] = ws
        self._step_counter[session_id] = 0
        self._session_agent_map[session_id] = agent_id

    def unregister_websocket(self, session_id: str) -> None:
        self._websockets.pop(session_id, None)
        self._step_counter.pop(session_id, None)
        self._session_agent_map.pop(session_id, None)

    async def on_tool_executed(
        self,
        tool_name: str,
        params: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """Send screen updates or action notifications to all connected clients."""
        if tool_name not in self._SCREEN_TOOLS and tool_name not in self._ACTION_TOOLS:
            return

        for session_id, ws in list(self._websockets.items()):
            try:
                self._step_counter[session_id] = self._step_counter.get(session_id, 0) + 1
                step = self._step_counter[session_id]
                agent_id = self._session_agent_map.get(session_id, "main")

                if tool_name in self._SCREEN_TOOLS and result.image_path:
                    data_uri = _encode_image_base64(result.image_path)
                    if data_uri:
                        await ws.send_json({
                            "type": "screen_update",
                            "image": data_uri,
                            "action": f"Screenshot ({tool_name})",
                            "step": step,
                            "agent_id": agent_id,
                        })
                elif tool_name in self._ACTION_TOOLS:
                    action_desc = self._describe_action(tool_name, params)
                    await ws.send_json({
                        "type": "screen_action",
                        "action": action_desc,
                        "step": step,
                        "agent_id": agent_id,
                    })
            except Exception:
                pass  # Connection may have closed

    @staticmethod
    def _describe_action(tool_name: str, params: dict[str, Any]) -> str:
        """Build a human-readable description of a desktop action."""
        if tool_name == "mouse_action":
            action = params.get("action", "?")
            x, y = params.get("x", "?"), params.get("y", "?")
            if action == "scroll":
                amt = params.get("scroll_amount", 0)
                return f"Scroll {'up' if amt > 0 else 'down'} at ({x}, {y})"
            if action == "drag":
                ex, ey = params.get("end_x", "?"), params.get("end_y", "?")
                return f"Drag ({x},{y}) → ({ex},{ey})"
            return f"{action.replace('_', ' ').title()} at ({x}, {y})"

        if tool_name == "keyboard_action":
            action = params.get("action", "?")
            if action == "type":
                text = params.get("text", "")
                preview = text[:40] + ("..." if len(text) > 40 else "")
                return f'Type: "{preview}"'
            if action == "press":
                return f"Press: {params.get('key', '?')}"
            if action == "hotkey":
                keys = params.get("keys", [])
                return f"Hotkey: {' + '.join(keys)}"

        return tool_name


class WebServer:
    """FastAPI-based web server with WebSocket chat."""

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        self.engine = engine
        self.config = config
        self._oauth = OpenAIOAuthManager()
        self._settings_schema = SettingsSchemaRegistry()
        self._settings_schema.register(
            "core",
            build_core_settings_schema,
            order=0,
        )
        self._page_schema = UIPageSchemaRegistry()
        self._page_schema.register(
            "agents",
            "core_agents",
            build_agents_page_schema,
            order=0,
        )
        self._page_schema.register(
            "dashboard",
            "core_dashboard",
            build_dashboard_page_schema,
            order=0,
        )
        self._page_schema.register(
            "security",
            "core_security",
            build_security_page_schema,
            order=0,
        )
        self.approval_cb = WebApprovalCallback(
            timeout=60,
            approval_policy=engine.approval_policy,
        )
        self.engine.approval_cb = self.approval_cb
        self._tool_cb = WebToolCallback()
        self.engine.tool_callback = self._tool_cb
        self._connections: dict[str, ConnectionState] = {}
        cache_ttl_raw = os.environ.get("KURO_MODELS_CACHE_TTL_SECONDS", "10")
        try:
            cache_ttl = float(cache_ttl_raw)
        except ValueError:
            cache_ttl = 10.0
        self._models_cache_groups: dict[str, list[str]] = {}
        self._models_cache_flat: list[str] = []
        self._models_cache_expires_at: float = 0.0
        self._models_cache_ttl: float = max(0.0, cache_ttl)
        self._models_cache_lock = asyncio.Lock()

        # Session cache: keeps sessions alive after WebSocket disconnects
        # so they can be restored on reconnect (e.g., after page navigation).
        # Key: session_id, Value: (ConnectionState, disconnect_timestamp)
        self._session_cache: dict[str, tuple[ConnectionState, float]] = {}
        self._SESSION_CACHE_TTL = 1800  # 30 minutes

        self.app = self._create_app()

    def _invalidate_models_cache(self) -> None:
        self._models_cache_groups = {}
        self._models_cache_flat = []
        self._models_cache_expires_at = 0.0

    @staticmethod
    def _flatten_model_groups(
        groups: dict[str, list[str]],
        default_model: str,
    ) -> list[str]:
        flat: list[str] = []
        seen: set[str] = set()

        def _add(model: str | None) -> None:
            value = str(model or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            flat.append(value)

        _add(default_model)
        for provider_models in groups.values():
            for model in provider_models:
                _add(model)
        return flat

    async def _get_models_snapshot(self) -> tuple[dict[str, list[str]], list[str]]:
        now = time.monotonic()
        if self._models_cache_groups and now < self._models_cache_expires_at:
            return (
                {k: list(v) for k, v in self._models_cache_groups.items()},
                list(self._models_cache_flat),
            )

        async with self._models_cache_lock:
            now = time.monotonic()
            if self._models_cache_groups and now < self._models_cache_expires_at:
                return (
                    {k: list(v) for k, v in self._models_cache_groups.items()},
                    list(self._models_cache_flat),
                )

            started = time.perf_counter()
            groups = await self.engine.model.list_models_grouped()
            flat = self._flatten_model_groups(groups, self.engine.model.default_model)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "models_snapshot_refreshed",
                model_groups=len(groups),
                model_count=len(flat),
                duration_ms=elapsed_ms,
            )

            self._models_cache_groups = {k: list(v) for k, v in groups.items()}
            self._models_cache_flat = list(flat)
            self._models_cache_expires_at = now + max(0.0, self._models_cache_ttl)
            return (
                {k: list(v) for k, v in self._models_cache_groups.items()},
                list(self._models_cache_flat),
            )

    @staticmethod
    def _parse_model_selection(raw: str) -> tuple[str | None, str]:
        value = (raw or "").strip()
        if not value:
            return None, "api"
        if value.startswith("oauth:"):
            model = value[len("oauth:"):].strip()
            return (model or None), "oauth"
        if value.startswith("api:"):
            model = value[len("api:"):].strip()
            return (model or None), "api"
        return value, "api"

    @staticmethod
    def _is_openai_model(model: str | None) -> bool:
        return bool(model and model.startswith("openai/"))

    @staticmethod
    def _is_openai_oauth_model_candidate(model: str) -> bool:
        return is_codex_oauth_model_supported(model)

    def _build_status_payload(
        self,
        *,
        conn: ConnectionState,
        engine: Engine,
        session: Session,
        agent_id: str,
        restored: bool | None = None,
    ) -> dict[str, Any]:
        model = conn.model_override or engine.model.default_model
        mode = conn.model_auth_mode if self._is_openai_model(model) else "api"
        payload: dict[str, Any] = {
            "type": "status",
            "model": model,
            "model_override": conn.model_override,
            "model_auth_mode": mode,
            "trust_level": session.trust_level,
            "session_id": session.id,
            "agent_id": agent_id,
        }
        if restored is not None:
            payload["restored"] = restored
        return payload

    def _build_model_catalog(
        self,
        groups: dict[str, list[str]],
        oauth_logged_in: bool,
    ) -> list[dict[str, str]]:
        catalog: list[dict[str, str]] = []

        for provider, models in groups.items():
            for model in models:
                short = model.split("/").pop()
                if provider == "openai":
                    catalog.append({
                        "value": f"api:{model}",
                        "model": model,
                        "provider": provider,
                        "auth": "api",
                        "group_label": "OpenAI (API)",
                        "label": f"[API] {short}",
                    })
                else:
                    group_label = provider.capitalize()
                    if provider == "openai-compatible":
                        group_label = "OpenAI-Compatible (Local)"
                    elif provider == "llama":
                        group_label = "Llama (OpenAI-Compatible)"
                    catalog.append({
                        "value": model,
                        "model": model,
                        "provider": provider,
                        "auth": "api",
                        "group_label": group_label,
                        "label": short,
                    })

        if oauth_logged_in:
            openai_models: list[str] = list(_OPENAI_OAUTH_MODEL_DEFAULTS)
            extra_models = [
                m.strip()
                for m in os.environ.get("OPENAI_CODEX_OAUTH_MODELS", "").split(",")
                if m.strip()
            ]
            for m in extra_models:
                candidate = normalize_openai_model(m)
                if self._is_openai_oauth_model_candidate(candidate) and candidate not in openai_models:
                    openai_models.append(candidate)
            if "openai" in groups:
                for m in groups["openai"]:
                    if self._is_openai_oauth_model_candidate(m) and m not in openai_models:
                        openai_models.append(m)
            openai_cfg = self.config.models.providers.get("openai")
            if openai_cfg:
                for m in openai_cfg.known_models:
                    if self._is_openai_oauth_model_candidate(m) and m not in openai_models:
                        openai_models.append(m)

            for model in openai_models:
                short = model.split("/").pop()
                catalog.append({
                    "value": f"oauth:{model}",
                    "model": model,
                    "provider": "openai",
                    "auth": "oauth",
                    "group_label": "OpenAI (OAuth Subscription)",
                    "label": f"[OAuth] {short}",
                })

        return catalog

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Kuro", docs_url=None, redoc_url=None)

        # Static files
        if WEB_DIR.exists():
            app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def index():
            index_file = WEB_DIR / "index.html"
            if index_file.exists():
                return FileResponse(str(index_file), media_type="text/html")
            return HTMLResponse("<h1>Kuro Web GUI</h1><p>index.html not found</p>")

        @app.get("/api/models")
        async def get_models(request: Request):
            groups, flat = await self._get_models_snapshot()
            oauth_sid = self._oauth.get_session_id_from_cookies(request.cookies)
            oauth_status = self._oauth.get_status(oauth_sid)
            catalog = self._build_model_catalog(
                groups=groups,
                oauth_logged_in=bool(oauth_status.get("logged_in")),
            )
            return {
                "default": self.engine.model.default_model,
                "groups": groups,
                "available": flat,  # backward compat
                "catalog": catalog,
                "oauth": oauth_status,
            }

        @app.get("/api/oauth/openai/status")
        async def oauth_openai_status(request: Request):
            sid = self._oauth.get_session_id_from_cookies(request.cookies)
            return self._oauth.get_status(sid)

        @app.get("/api/oauth/openai/login")
        async def oauth_openai_login(request: Request):
            if not self._oauth.configured:
                return RedirectResponse(url="/?oauth=openai_not_configured", status_code=302)
            sid = self._oauth.get_or_create_session_id(request)
            try:
                await self._oauth.ensure_local_bridge()
                login_url = self._oauth.build_login_url(request, sid)
            except Exception:
                logger.exception("oauth_openai_login_build_failed")
                return RedirectResponse(url="/?oauth=openai_login_failed", status_code=302)
            resp = RedirectResponse(url=login_url, status_code=302)
            self._oauth.attach_cookie(resp, sid)
            return resp

        @app.get("/api/oauth/openai/callback")
        @app.get("/auth/callback")
        async def oauth_openai_callback(
            request: Request,
            code: str | None = Query(None),
            state: str | None = Query(None),
            error: str | None = Query(None),
            error_description: str | None = Query(None),
        ):
            sid = self._oauth.get_session_id_from_cookies(request.cookies)
            if not sid:
                resp = RedirectResponse(url="/?oauth=openai_missing_session", status_code=302)
                sid = self._oauth.get_or_create_session_id(request)
                self._oauth.attach_cookie(resp, sid)
                return resp

            if error:
                logger.warning(
                    "oauth_openai_callback_error",
                    error=error,
                    description=error_description or "",
                )
                return RedirectResponse(url="/?oauth=openai_denied", status_code=302)
            if not code or not state:
                return RedirectResponse(url="/?oauth=openai_invalid_callback", status_code=302)

            try:
                await self._oauth.exchange_code(
                    request=request,
                    session_id=sid,
                    state=state,
                    code=code,
                )
                resp = RedirectResponse(url="/?oauth=openai_connected", status_code=302)
                self._oauth.attach_cookie(resp, sid)
                return resp
            except Exception as e:
                logger.warning("oauth_openai_exchange_failed", error=str(e))
                resp = RedirectResponse(url="/?oauth=openai_exchange_failed", status_code=302)
                self._oauth.attach_cookie(resp, sid)
                return resp

        @app.post("/api/oauth/openai/logout")
        async def oauth_openai_logout(request: Request):
            sid = self._oauth.get_session_id_from_cookies(request.cookies)
            await self._oauth.logout(sid)
            resp = JSONResponse({"status": "ok"})
            resp.delete_cookie(
                key=self._oauth.cookie_name,
                path="/",
            )
            return resp

        @app.get("/api/audit")
        async def get_audit(
            limit: int = Query(50, ge=1, le=200),
            session_id: str | None = Query(None),
            event_type: str | None = Query(None),
        ):
            entries = await self.engine.audit.query_recent(
                limit=limit, session_id=session_id, event_type=event_type
            )
            return {"entries": entries}

        @app.get("/api/status")
        async def get_status():
            return {
                "active_connections": len(self._connections),
                "default_model": self.engine.model.default_model,
            }

        @app.get("/api/skills")
        async def get_skills():
            sm = self.engine.skills
            skills = sm.list_skills() if sm else []
            active = sm._active if sm else set()
            return {
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "active": s.name in active,
                        "source": s.source,
                    }
                    for s in skills
                ]
            }

        @app.get("/api/skills/available")
        async def get_skills_available():
            """List skills available for installation."""
            sm = self.engine.skills
            if not sm:
                return {"available": []}
            return {"available": sm.list_available_skills()}

        @app.get("/api/plugins")
        async def get_plugins():
            bridge = getattr(self.engine, "mcp_bridge", None)
            if bridge is not None:
                with contextlib.suppress(Exception):
                    await bridge.ensure_initialized(self.engine.tools.registry)
            names = self.engine.tools.registry.get_names()
            return {"tools": sorted(names), "count": len(names)}

        @app.get("/api/mcp/servers")
        async def get_mcp_servers():
            bridge = getattr(self.engine, "mcp_bridge", None)
            statuses: list[dict[str, Any]] = []
            if bridge is not None:
                with contextlib.suppress(Exception):
                    await bridge.ensure_initialized(self.engine.tools.registry)
                    statuses = bridge.list_status()
            return {
                "enabled": bool(getattr(self.config.mcp, "enabled", False)),
                "servers": statuses,
            }

        @app.post("/api/mcp/discover-tools")
        async def discover_mcp_server_tools(request: Request):
            data = await request.json()
            raw_server = data.get("server", data)
            try:
                server_cfg = MCPServerConfig(**(raw_server or {}))
                from src.core.mcp import discover_mcp_tools

                tools = await discover_mcp_tools(server_cfg)
                return {
                    "status": "ok",
                    "tools": tools,
                    "count": len(tools),
                }
            except Exception as e:
                return JSONResponse(
                    {"status": "error", "message": str(e)},
                    status_code=400,
                )

        # === Personality API ===

        @app.get("/api/personality")
        async def get_personality():
            """Get current personality settings."""
            from src.config import get_kuro_home
            personality_path = get_kuro_home() / "personality.md"
            if personality_path.exists():
                content = personality_path.read_text(encoding="utf-8")
                return {"content": content, "path": str(personality_path)}
            return {"content": "", "path": str(personality_path)}

        @app.put("/api/personality")
        async def update_personality(request: Request):
            """Update personality settings."""
            from src.config import get_kuro_home
            data = await request.json()
            content = data.get("content", "")
            personality_path = get_kuro_home() / "personality.md"
            personality_path.write_text(content, encoding="utf-8")
            return {"status": "ok", "message": "Personality updated"}

        # === Configuration API (hot-reload without restart) ===

        @app.get("/config")
        async def config_page():
            """Serve the configuration page."""
            cfg_file = WEB_DIR / "config.html"
            if cfg_file.exists():
                return FileResponse(str(cfg_file), media_type="text/html")
            return HTMLResponse("<h1>Config</h1><p>config.html not found</p>")

        @app.get("/api/config")
        async def get_config():
            """Get current configuration (excluding secrets)."""
            return {"config": self._public_config_data()}

        @app.put("/api/config")
        async def update_config(request: Request):
            """Update configuration and apply changes live (no restart needed).

            Accepts a partial config dict — only the provided fields are updated.
            Changes are saved to config.yaml and applied to the running engine.
            """
            data = await request.json()
            updates = data.get("config", data)

            new_config, build_error = self._build_updated_config(updates)
            if new_config is None:
                return {"status": "error", "message": f"Invalid config: {build_error}"}

            applied, apply_error = self._save_and_apply_config(new_config)
            if applied is None:
                return {"status": "error", "message": f"Save failed: {apply_error}"}

            return {
                "status": "ok",
                "message": "Configuration updated and applied",
                "applied": applied,
            }

        @app.get("/api/settings/schema")
        async def get_settings_schema():
            """Get schema metadata for settings UI rendering."""
            schema = self._settings_schema.build_schema()
            return {"schema": schema}

        @app.get("/api/settings/values")
        async def get_settings_values():
            """Get current settings values (public-safe)."""
            return {"values": self._public_config_data()}

        @app.put("/api/settings/values")
        async def update_settings_values(request: Request):
            """Update settings values via schema-driven API."""
            data = await request.json()
            updates = data.get("values", data.get("config", data))

            new_config, build_error = self._build_updated_config(updates)
            if new_config is None:
                return {"status": "error", "message": f"Invalid settings: {build_error}"}

            applied, apply_error = self._save_and_apply_config(new_config)
            if applied is None:
                return {"status": "error", "message": f"Save failed: {apply_error}"}

            return {
                "status": "ok",
                "message": "Settings updated and applied",
                "applied": applied,
            }

        @app.get("/api/ui/schema")
        async def get_ui_schema_pages():
            """List available UI page schemas."""
            return {"pages": self._page_schema.list_pages()}

        @app.get("/api/ui/schema/{page_id}")
        async def get_ui_schema(page_id: str):
            """Get schema metadata for non-settings pages."""
            schema = self._page_schema.build_page_schema(page_id)
            if schema is None:
                return JSONResponse(
                    {"status": "error", "message": f"unknown page: {page_id}"},
                    status_code=404,
                )
            return {"schema": schema}

        @app.get("/api/config/lessons")
        async def get_lessons():
            """Get all stored lessons from the learning engine."""
            mm = self.engine.memory
            if mm.learning:
                return {
                    "lessons": mm.learning.get_all_lessons(),
                    "model_stats": mm.learning.get_model_stats(),
                }
            return {"lessons": [], "model_stats": {}}

        @app.get("/api/config/memory-stats")
        async def get_memory_stats():
            """Get memory system statistics including lifecycle info."""
            mm = self.engine.memory
            stats = await mm.get_stats()

            # Add lifecycle-specific stats
            if mm.lifecycle and mm.lifecycle.config.enabled:
                try:
                    all_facts = await mm.longterm.get_all_facts(limit=500)
                    importance_scores = []
                    pinned_count = 0
                    for fact in all_facts:
                        meta = fact.get("metadata", {})
                        imp = float(meta.get("importance", 0.5))
                        importance_scores.append(imp)
                        if meta.get("is_pinned"):
                            pinned_count += 1

                    stats["lifecycle"] = {
                        "total_memories": len(all_facts),
                        "pinned": pinned_count,
                        "avg_importance": round(sum(importance_scores) / len(importance_scores), 3) if importance_scores else 0,
                        "below_threshold": sum(1 for s in importance_scores if s < mm.lifecycle.config.prune_threshold),
                    }
                except Exception:
                    stats["lifecycle"] = {"error": "failed to compute"}

            return stats

        @app.post("/api/config/run-maintenance")
        async def run_maintenance(request: Request):
            """Manually trigger memory maintenance or learning analysis."""
            data = await request.json()
            action = data.get("action", "")

            mm = self.engine.memory

            if action == "daily_lifecycle" and mm.lifecycle:
                result = await mm.lifecycle.daily_maintenance()
                return {"status": "ok", "result": result}
            elif action == "weekly_lifecycle" and mm.lifecycle:
                result = await mm.lifecycle.weekly_consolidation()
                return {"status": "ok", "result": result}
            elif action == "organize_memory_md" and mm.lifecycle:
                result = await mm.lifecycle.manage_memory_md()
                return {"status": "ok", "result": result}
            elif action == "daily_learning" and mm.learning:
                result = await mm.learning.daily_analysis()
                return {"status": "ok", "result": result}
            else:
                return {"status": "error", "message": f"Unknown action: {action}"}

        # === Security Dashboard API ===

        @app.get("/api/security/dashboard")
        async def get_security_dashboard(
            date: str | None = Query(None),
        ):
            """Get comprehensive security dashboard data."""
            stats = await self.engine.audit.get_daily_stats(date)
            blocked = await self.engine.audit.get_blocked_count(7)
            score = await self.engine.audit.get_security_score()
            return {
                "daily_stats": stats,
                "blocked_history": blocked,
                "security_score": score,
            }

        @app.get("/api/security/stats")
        async def get_security_stats(
            date: str | None = Query(None),
        ):
            """Get daily audit statistics."""
            return await self.engine.audit.get_daily_stats(date)

        @app.get("/api/security/blocked")
        async def get_security_blocked(
            days: int = Query(7, ge=1, le=90),
        ):
            """Get blocked operations over the last N days."""
            return await self.engine.audit.get_blocked_count(days)

        @app.get("/api/security/score")
        async def get_security_score():
            """Get current security posture score."""
            return await self.engine.audit.get_security_score()

        @app.get("/api/security/integrity")
        async def get_security_integrity(
            limit: int = Query(100, ge=10, le=1000),
        ):
            """Verify audit log integrity."""
            total, tampered = await self.engine.audit.verify_integrity(limit)
            return {
                "total_checked": total,
                "tampered": tampered,
                "integrity": "ok" if tampered == 0 else "compromised",
            }

        # === Analytics API ===

        @app.get("/api/analytics/usage")
        async def get_analytics_usage():
            """Get tool usage analytics from action logs."""
            from src.core.analytics import UsageAnalyzer
            analyzer = UsageAnalyzer()
            return await analyzer.get_usage_summary()

        @app.get("/api/analytics/costs")
        async def get_analytics_costs():
            """Get estimated cost analytics."""
            from src.core.analytics import CostEstimator
            estimator = CostEstimator()
            return await estimator.estimate_costs()

        @app.get("/api/analytics/suggestions")
        async def get_analytics_suggestions():
            """Get smart optimization suggestions."""
            from src.core.analytics import SmartAdvisor
            advisor = SmartAdvisor()
            return await advisor.get_suggestions()

        @app.get("/api/analytics/pricing")
        async def get_analytics_pricing():
            """Get model pricing table."""
            from src.core.analytics import get_pricing_info
            return get_pricing_info()

        @app.put("/api/analytics/pricing/{model:path}")
        async def update_analytics_pricing(model: str, request: Request):
            """Update pricing for a specific model."""
            from src.core.analytics import update_model_pricing
            body = await request.json()
            input_rate = float(body.get("input", 0))
            output_rate = float(body.get("output", 0))
            result = update_model_pricing(model, input_rate, output_rate)
            return {"model": model, "pricing": result}

        @app.delete("/api/analytics/pricing/{model:path}")
        async def delete_analytics_pricing(model: str):
            """Remove custom pricing override, reverting to built-in default."""
            from src.core.analytics import delete_custom_pricing
            deleted = delete_custom_pricing(model)
            return {"model": model, "deleted": deleted}

        @app.get("/security")
        async def security_page():
            """Serve the security dashboard page."""
            sec_file = WEB_DIR / "security.html"
            if sec_file.exists():
                return FileResponse(str(sec_file), media_type="text/html")
            return HTMLResponse("<h1>Security Dashboard</h1><p>security.html not found</p>")

        @app.get("/analytics")
        async def analytics_page():
            """Serve the analytics page."""
            ana_file = WEB_DIR / "analytics.html"
            if ana_file.exists():
                return FileResponse(str(ana_file), media_type="text/html")
            return HTMLResponse("<h1>Analytics</h1><p>analytics.html not found</p>")

        # === Agents Page & API ===

        @app.get("/agents")
        async def agents_page():
            """Serve the agent instances management page."""
            agents_file = WEB_DIR / "agents.html"
            if agents_file.exists():
                return FileResponse(str(agents_file), media_type="text/html")
            return HTMLResponse("<h1>Agents</h1><p>agents.html not found</p>")

        @app.get("/api/agents/instances")
        async def list_instances():
            """List all agent instances."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"instances": []}
            runtime_map = {inst.id: inst for inst in im.list_all()}
            items = [
                self._instance_info_from_cfg(cfg, runtime_map.get(cfg.id))
                for cfg in self.config.agents.instances
            ]
            return {"instances": items}

        @app.get("/api/agents/instances/{instance_id}")
        async def get_instance(instance_id: str):
            """Get a single agent instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            cfg = self._find_agent_instance_config(instance_id)
            if not cfg:
                return {"status": "error", "message": f"Instance '{instance_id}' not found"}
            inst = im.get(instance_id)
            return self._instance_info_from_cfg(cfg, inst)

        @app.post("/api/agents/instances")
        async def create_instance(request: Request):
            """Create a new agent instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            data = await request.json()
            try:
                from src.config import AgentInstanceConfig

                cfg = AgentInstanceConfig(**data)
                if self._find_agent_instance_config(cfg.id):
                    raise ValueError(f"Agent instance '{cfg.id}' already exists")
                self._validate_instance_binding(cfg)
                inst = None
                if cfg.enabled:
                    inst = await im.create_instance(cfg)
                    self._upsert_agent_instance_config(inst.config)
                else:
                    self._upsert_agent_instance_config(cfg)
                self._save_runtime_config()
                adapter_manager = getattr(self.engine, "adapter_manager", None)
                if adapter_manager and inst:
                    await adapter_manager.sync_instance_adapter(inst)
                return {
                    "status": "ok",
                    "instance": self._instance_info_from_cfg(cfg, inst),
                }
            except Exception as e:
                # If persistence failed after runtime creation, rollback runtime state.
                if "cfg" in locals():
                    try:
                        await im.delete_instance(cfg.id)
                    except Exception:
                        pass
                return {"status": "error", "message": str(e)}

        @app.put("/api/agents/instances/{instance_id}")
        async def update_instance(instance_id: str, request: Request):
            """Update and persist an agent instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            existing_cfg = self._find_agent_instance_config(instance_id)
            if not existing_cfg:
                return {"status": "error", "message": f"Instance '{instance_id}' not found"}
            data = await request.json()
            if "id" in data and data["id"] != instance_id:
                return {"status": "error", "message": "Instance ID cannot be changed"}
            old_cfg = existing_cfg.model_copy(deep=True)
            try:
                from src.config import AgentInstanceConfig

                merged = existing_cfg.model_dump()
                self._deep_merge(merged, data)
                merged["id"] = instance_id
                new_cfg = AgentInstanceConfig(**merged)
                self._validate_instance_binding(new_cfg)

                old_runtime = im.get(instance_id)
                if old_runtime:
                    await im.delete_instance(instance_id)

                new_runtime = None
                if new_cfg.enabled:
                    new_runtime = await im.create_instance(new_cfg)
                self._upsert_agent_instance_config(new_cfg)
                self._save_runtime_config()
                adapter_manager = getattr(self.engine, "adapter_manager", None)
                if adapter_manager:
                    if new_runtime:
                        await adapter_manager.sync_instance_adapter(new_runtime)
                    else:
                        await adapter_manager.remove_instance_adapters(instance_id)
                return {
                    "status": "ok",
                    "instance": self._instance_info_from_cfg(new_cfg, new_runtime),
                }
            except Exception as e:
                # Best-effort rollback to previous config/runtime state
                try:
                    if im.get(instance_id):
                        await im.delete_instance(instance_id)
                    if old_cfg.enabled:
                        restored = await im.create_instance(old_cfg)
                        adapter_manager = getattr(self.engine, "adapter_manager", None)
                        if adapter_manager:
                            await adapter_manager.sync_instance_adapter(restored)
                    else:
                        adapter_manager = getattr(self.engine, "adapter_manager", None)
                        if adapter_manager:
                            await adapter_manager.remove_instance_adapters(instance_id)
                    self._upsert_agent_instance_config(old_cfg)
                    self._save_runtime_config()
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}

        @app.delete("/api/agents/instances/{instance_id}")
        async def delete_instance(instance_id: str):
            """Delete an agent instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            existing_cfg = self._find_agent_instance_config(instance_id)
            if not existing_cfg:
                return {"status": "error", "message": f"Instance '{instance_id}' not found"}
            try:
                adapter_manager = getattr(self.engine, "adapter_manager", None)
                if adapter_manager:
                    await adapter_manager.remove_instance_adapters(instance_id)
                if im.get(instance_id):
                    await im.delete_instance(instance_id)
                self._remove_agent_instance_config(instance_id)
                self._save_runtime_config()
            except Exception as e:
                return {"status": "error", "message": str(e)}
            return {"status": "ok"}

        @app.get("/api/agents/instances/{instance_id}/personality")
        async def get_instance_personality(instance_id: str):
            """Get an instance's personality file content."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"content": ""}
            inst = im.get(instance_id)
            if not inst or not inst.personality_path:
                return {"content": ""}
            try:
                content = inst.personality_path.read_text(encoding="utf-8")
                return {"content": content}
            except Exception:
                return {"content": ""}

        @app.put("/api/agents/instances/{instance_id}/personality")
        async def update_instance_personality(instance_id: str, request: Request):
            """Update an instance's personality file."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            inst = im.get(instance_id)
            if not inst or not inst.personality_path:
                return {"status": "error", "message": "Instance has no independent personality"}
            data = await request.json()
            inst.personality_path.write_text(data.get("content", ""), encoding="utf-8")
            return {"status": "ok"}

        @app.get("/api/agents/instances/{instance_id}/sub-agents")
        async def list_sub_agents(instance_id: str):
            """List sub-agents for an instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"sub_agents": [], "definitions": []}
            inst = im.get(instance_id)
            if inst and inst.agent_manager:
                agents = list(inst.agent_manager.list_definitions())
                return {
                    "sub_agents": [a.name for a in agents],
                    "definitions": [self._agent_definition_to_dict(a) for a in agents],
                }
            cfg = self._find_agent_instance_config(instance_id)
            if not cfg:
                return {"sub_agents": [], "definitions": []}
            defs = list(cfg.sub_agents)
            return {
                "sub_agents": [a.name for a in defs],
                "definitions": [self._agent_definition_to_dict(a) for a in defs],
            }

        @app.get("/api/agents/main/sub-agents")
        async def list_main_sub_agents():
            """List sub-agents for the main agent."""
            defs = self._get_main_sub_agent_definitions()
            return {
                "sub_agents": [d.name for d in defs],
                "definitions": [self._agent_definition_to_dict(d) for d in defs],
            }

        @app.post("/api/agents/main/sub-agents")
        async def add_main_sub_agent(request: Request):
            """Add a sub-agent to the main agent and persist it."""
            manager = getattr(self.engine, "agent_manager", None)
            if manager is None:
                return {"status": "error", "message": "Main agent manager not available"}
            data = await request.json()
            try:
                defn = self._agent_payload_to_definition(data)
                if not defn.name:
                    return {"status": "error", "message": "Sub-agent name is required"}
                if manager.has_agent(defn.name):
                    return {"status": "error", "message": f"Sub-agent '{defn.name}' already exists"}

                manager.register(defn)
                self.config.agents.sub_agents = [
                    *self.config.agents.sub_agents,
                    self._agent_definition_to_config(defn),
                ]
                self._save_runtime_config()
                return {"status": "ok", "sub_agent": self._agent_definition_to_dict(defn)}
            except Exception as e:
                try:
                    if data.get("name"):
                        manager.unregister(str(data.get("name")))
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}

        @app.put("/api/agents/main/sub-agents/{agent_name}")
        async def update_main_sub_agent(agent_name: str, request: Request):
            """Edit a sub-agent in the main agent and persist changes."""
            manager = getattr(self.engine, "agent_manager", None)
            if manager is None:
                return {"status": "error", "message": "Main agent manager not available"}
            data = await request.json()
            existing_runtime = manager.get_definition(agent_name)
            existing_cfg = next(
                (sa for sa in self.config.agents.sub_agents if sa.name == agent_name),
                None,
            )
            if existing_runtime is None and existing_cfg is None:
                return {"status": "error", "message": f"Sub-agent '{agent_name}' not found"}

            base = (
                existing_runtime
                if existing_runtime is not None
                else self._agent_payload_to_definition(existing_cfg.model_dump(), created_by="config")
            )
            try:
                updated = self._agent_payload_to_definition(data, fallback=base)
                if not updated.name:
                    return {"status": "error", "message": "Sub-agent name is required"}
                if updated.name != agent_name and manager.has_agent(updated.name):
                    return {"status": "error", "message": f"Sub-agent '{updated.name}' already exists"}

                manager.unregister(agent_name)
                manager.register(updated)

                replaced = False
                new_cfgs = []
                for sa in self.config.agents.sub_agents:
                    if sa.name == agent_name:
                        new_cfgs.append(self._agent_definition_to_config(updated))
                        replaced = True
                    else:
                        new_cfgs.append(sa)
                if not replaced:
                    new_cfgs.append(self._agent_definition_to_config(updated))
                self.config.agents.sub_agents = new_cfgs
                self._save_runtime_config()
                return {"status": "ok", "sub_agent": self._agent_definition_to_dict(updated)}
            except Exception as e:
                try:
                    manager.unregister(updated.name if "updated" in locals() else agent_name)
                    if existing_runtime is not None:
                        manager.register(existing_runtime)
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}

        @app.delete("/api/agents/main/sub-agents/{agent_name}")
        async def delete_main_sub_agent(agent_name: str):
            """Delete a sub-agent from main agent and persisted config."""
            manager = getattr(self.engine, "agent_manager", None)
            existing_runtime = manager.get_definition(agent_name) if manager else None
            removed_runtime = manager.unregister(agent_name) if manager else False
            old_len = len(self.config.agents.sub_agents)
            self.config.agents.sub_agents = [
                sa for sa in self.config.agents.sub_agents if sa.name != agent_name
            ]
            removed_cfg = len(self.config.agents.sub_agents) != old_len
            if not removed_runtime and not removed_cfg:
                return {"status": "error", "message": f"Sub-agent '{agent_name}' not found"}
            try:
                self._save_runtime_config()
                return {"status": "ok"}
            except Exception as e:
                try:
                    if removed_runtime and existing_runtime is not None and manager:
                        manager.register(existing_runtime)
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}

        @app.post("/api/agents/instances/{instance_id}/sub-agents")
        async def add_sub_agent(instance_id: str, request: Request):
            """Add a sub-agent to an instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            cfg = self._find_agent_instance_config(instance_id)
            if not cfg:
                return {"status": "error", "message": f"Instance '{instance_id}' not found"}
            inst = im.get(instance_id)
            target_cfg = inst.config if inst else cfg
            data = await request.json()
            try:
                defn = self._agent_payload_to_definition(data)
                if not defn.name:
                    return {"status": "error", "message": "Sub-agent name is required"}
                if any(sa.name == defn.name for sa in target_cfg.sub_agents):
                    return {"status": "error", "message": f"Sub-agent '{defn.name}' already exists"}

                if inst and inst.agent_manager:
                    inst.agent_manager.register(defn)
                target_cfg.sub_agents.append(self._agent_definition_to_config(defn))
                if inst:
                    inst.config.sub_agents = list(target_cfg.sub_agents)

                self._upsert_agent_instance_config(target_cfg)
                self._save_runtime_config()
                return {"status": "ok", "sub_agent": self._agent_definition_to_dict(defn)}
            except Exception as e:
                # Best-effort rollback if config save failed after runtime register.
                name = data.get("name")
                if name and inst and inst.agent_manager:
                    try:
                        inst.agent_manager.unregister(name)
                    except Exception:
                        pass
                return {"status": "error", "message": str(e)}

        @app.put("/api/agents/instances/{instance_id}/sub-agents/{agent_name}")
        async def update_sub_agent(instance_id: str, agent_name: str, request: Request):
            """Edit a sub-agent on an instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            cfg = self._find_agent_instance_config(instance_id)
            if not cfg:
                return {"status": "error", "message": f"Instance '{instance_id}' not found"}
            inst = im.get(instance_id)
            target_cfg = inst.config if inst else cfg
            existing_cfg = next((sa for sa in target_cfg.sub_agents if sa.name == agent_name), None)
            if existing_cfg is None:
                return {"status": "error", "message": f"Sub-agent '{agent_name}' not found"}
            data = await request.json()

            base = self._agent_payload_to_definition(existing_cfg.model_dump(), created_by="config")
            try:
                updated = self._agent_payload_to_definition(data, fallback=base)
                if not updated.name:
                    return {"status": "error", "message": "Sub-agent name is required"}
                if updated.name != agent_name and any(
                    sa.name == updated.name for sa in target_cfg.sub_agents
                ):
                    return {"status": "error", "message": f"Sub-agent '{updated.name}' already exists"}

                if inst and inst.agent_manager:
                    inst.agent_manager.unregister(agent_name)
                    inst.agent_manager.register(updated)

                target_cfg.sub_agents = [
                    self._agent_definition_to_config(updated)
                    if sa.name == agent_name
                    else sa
                    for sa in target_cfg.sub_agents
                ]
                if inst:
                    inst.config.sub_agents = list(target_cfg.sub_agents)
                self._upsert_agent_instance_config(target_cfg)
                self._save_runtime_config()
                return {"status": "ok", "sub_agent": self._agent_definition_to_dict(updated)}
            except Exception as e:
                try:
                    if inst and inst.agent_manager:
                        inst.agent_manager.unregister(
                            updated.name if "updated" in locals() else agent_name
                        )
                        inst.agent_manager.register(base)
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}

        @app.delete("/api/agents/instances/{instance_id}/sub-agents/{agent_name}")
        async def delete_sub_agent(instance_id: str, agent_name: str):
            """Remove a sub-agent from an instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"status": "error", "message": "Instance manager not available"}
            cfg = self._find_agent_instance_config(instance_id)
            if not cfg:
                return {"status": "error", "message": f"Instance '{instance_id}' not found"}
            inst = im.get(instance_id)
            target_cfg = inst.config if inst else cfg
            existing_runtime = (
                inst.agent_manager.get_definition(agent_name)
                if inst and inst.agent_manager
                else None
            )
            removed_runtime = (
                inst.agent_manager.unregister(agent_name)
                if inst and inst.agent_manager
                else False
            )
            try:
                old_len = len(target_cfg.sub_agents)
                target_cfg.sub_agents = [
                    sa for sa in target_cfg.sub_agents if sa.name != agent_name
                ]
                removed_cfg = len(target_cfg.sub_agents) != old_len
                if not removed_runtime and not removed_cfg:
                    return {"status": "error", "message": f"Sub-agent '{agent_name}' not found"}
                if inst:
                    inst.config.sub_agents = list(target_cfg.sub_agents)
                self._upsert_agent_instance_config(target_cfg)
                self._save_runtime_config()
            except Exception as e:
                try:
                    if removed_runtime and existing_runtime is not None and inst and inst.agent_manager:
                        inst.agent_manager.register(existing_runtime)
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}
            return {"status": "ok"}

        @app.get("/api/agents/instances/{instance_id}/memory-stats")
        async def get_instance_memory_stats(instance_id: str):
            """Get memory statistics for an instance."""
            im = getattr(self.engine, "instance_manager", None)
            if not im:
                return {"stats": {}}
            inst = im.get(instance_id)
            if not inst:
                return {"stats": {}}
            try:
                stats = await inst.memory_manager.get_stats()
                return {"stats": stats}
            except Exception:
                return {"stats": {}}

        # === Dashboard Page & API ===

        @app.get("/dashboard")
        async def dashboard_page():
            """Serve the real-time agent dashboard page."""
            dash_file = WEB_DIR / "dashboard.html"
            if dash_file.exists():
                return FileResponse(str(dash_file), media_type="text/html")
            return HTMLResponse("<h1>Dashboard</h1><p>dashboard.html not found</p>")

        @app.get("/api/dashboard/stats")
        async def dashboard_stats():
            """Return aggregated agent event statistics."""
            event_bus = getattr(self.engine, "event_bus", None)
            if event_bus:
                payload = event_bus.get_stats()
            else:
                payload = {"total_events": 0, "agents": {}, "agent_states": {}}

            available_agents: set[str] = {"main"}
            im = getattr(self.engine, "instance_manager", None)
            if im:
                for inst in im.list_all():
                    inst_id = getattr(inst, "id", None)
                    if inst_id:
                        available_agents.add(str(inst_id))

            try:
                for cfg in self.config.agents.instances:
                    if cfg.enabled and cfg.id:
                        available_agents.add(cfg.id)
            except Exception:
                pass

            payload["available_agents"] = sorted(available_agents)
            return payload

        @app.get("/api/dashboard/events")
        async def dashboard_events(limit: int = Query(50)):
            """Return recent agent events."""
            event_bus = getattr(self.engine, "event_bus", None)
            if not event_bus:
                return {"events": []}
            return {"events": event_bus.get_recent(limit)}

        @app.websocket("/ws/dashboard")
        async def dashboard_ws(ws: WebSocket):
            """WebSocket endpoint for live dashboard event streaming."""
            await ws.accept()

            event_bus = getattr(self.engine, "event_bus", None)
            if not event_bus:
                await ws.send_json({"type": "error", "message": "Event bus not available"})
                await ws.close()
                return

            queue: asyncio.Queue = asyncio.Queue()

            def on_event(evt):
                try:
                    queue.put_nowait(evt)
                except asyncio.QueueFull:
                    pass

            event_bus.subscribe(on_event)
            try:
                while True:
                    event = await queue.get()
                    await ws.send_json({
                        "type": "agent_event",
                        "event": event.to_dict(),
                    })
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                event_bus.unsubscribe(on_event)

        # === Scheduler Page & API ===

        @app.get("/scheduler")
        async def scheduler_page():
            """Serve the scheduler management page."""
            sched_file = WEB_DIR / "scheduler.html"
            if sched_file.exists():
                return FileResponse(str(sched_file), media_type="text/html")
            return HTMLResponse("<h1>Scheduler</h1><p>scheduler.html not found</p>")

        @app.get("/api/scheduler")
        async def get_tasks():
            """List scheduled tasks (excludes completed one-time tasks)."""
            scheduler = getattr(self.engine, "scheduler", None)
            if not scheduler:
                return {"tasks": []}
            tasks = []
            for task in scheduler.list_tasks():
                t = task.to_dict()
                t["is_enabled"] = task.enabled
                tasks.append(t)
            tasks.sort(key=lambda x: x.get("next_run") or "", reverse=False)
            return {"tasks": tasks}

        @app.put("/api/scheduler/{task_id}")
        async def update_task(task_id: str, request: Request):
            """Update a scheduled task's properties."""
            scheduler = getattr(self.engine, "scheduler", None)
            if not scheduler:
                return {"status": "error", "message": "Scheduler not available"}

            task = scheduler.get_task(task_id)
            if not task:
                return {"status": "error", "message": f"Task '{task_id}' not found"}

            data = await request.json()
            changes = []

            if "notify_adapter" in data:
                task.notify_adapter = data["notify_adapter"] or None
                changes.append("notify_adapter")
            if "notify_user_id" in data:
                task.notify_user_id = data["notify_user_id"] or None
                changes.append("notify_user_id")
            if "schedule_time" in data:
                task.schedule_time = data["schedule_time"]
                task.next_run = scheduler._calculate_next_run(task)
                changes.append("schedule_time")
            if "enabled" in data:
                task.enabled = bool(data["enabled"])
                if task.enabled and not task.next_run:
                    task.next_run = scheduler._calculate_next_run(task)
                changes.append("enabled")
            if "agent_task" in data:
                task.agent_task = data["agent_task"]
                changes.append("agent_task")

            if changes:
                scheduler._save_tasks()

            return {
                "status": "ok",
                "message": f"Updated: {', '.join(changes)}",
                "task": task.to_dict(),
            }

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await self._handle_websocket(ws)

        return app

    @staticmethod
    def _deep_merge(base: dict, updates: dict) -> None:
        """Recursively merge updates into base dict (in-place)."""
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                WebServer._deep_merge(base[key], value)
            else:
                base[key] = value

    @staticmethod
    def _looks_like_secret_key(key: str) -> bool:
        lower = key.lower()
        return (
            "token" in lower
            or "secret" in lower
            or "password" in lower
            or "api_key" in lower
        )

    @classmethod
    def _restore_masked_secrets(cls, updates: Any, current: Any) -> Any:
        """Replace masked secret placeholders with current values before merge."""
        if isinstance(updates, dict):
            restored: dict[str, Any] = {}
            current_dict = current if isinstance(current, dict) else {}
            for key, value in updates.items():
                current_value = current_dict.get(key)
                if isinstance(value, dict):
                    restored[key] = cls._restore_masked_secrets(value, current_value)
                    continue
                if (
                    isinstance(value, str)
                    and value == "***"
                    and cls._looks_like_secret_key(str(key))
                ):
                    restored[key] = current_value
                else:
                    restored[key] = value
            return restored
        return updates

    def _public_config_data(self) -> dict[str, Any]:
        """Return current config as public-safe dict (secrets masked)."""
        data = self.config.model_dump(exclude={"core_prompt"})
        for _, provider_data in data.get("models", {}).get("providers", {}).items():
            if provider_data.get("api_key"):
                provider_data["api_key"] = "***"
        for adapter_name in ("telegram", "discord", "slack", "line", "email"):
            adapter_data = data.get("adapters", {}).get(adapter_name, {})
            for key in list(adapter_data.keys()):
                if "token" in key.lower() or "secret" in key.lower() or "password" in key.lower():
                    if adapter_data[key] and not key.endswith("_env"):
                        adapter_data[key] = "***"
        for server_data in data.get("mcp", {}).get("servers", []) or []:
            env_map = server_data.get("env")
            if not isinstance(env_map, dict):
                continue
            for env_key, env_val in list(env_map.items()):
                if env_val and self._looks_like_secret_key(str(env_key)):
                    env_map[env_key] = "***"
        return data

    def _build_updated_config(self, updates: dict[str, Any]) -> tuple[KuroConfig | None, str | None]:
        """Merge updates into current config and validate into a new KuroConfig."""
        current_data = self.config.model_dump(exclude={"core_prompt"})
        safe_updates = self._restore_masked_secrets(updates, current_data)
        self._deep_merge(current_data, safe_updates)
        try:
            new_config = KuroConfig(**current_data)
            new_config.core_prompt = self.config.core_prompt
            return new_config, None
        except Exception as e:
            return None, str(e)

    def _save_and_apply_config(self, new_config: KuroConfig) -> tuple[list[str] | None, str | None]:
        """Persist config to disk and hot-apply to runtime."""
        from src.config import save_config

        try:
            save_config(new_config)
        except Exception as e:
            return None, str(e)
        applied = self._apply_config_changes(new_config)
        self._invalidate_models_cache()
        return applied, None

    def _find_agent_instance_config(
        self, instance_id: str
    ) -> "AgentInstanceConfig | None":
        """Find an instance config by ID from persisted config."""
        for cfg in self.config.agents.instances:
            if cfg.id == instance_id:
                return cfg
        return None

    @staticmethod
    def _agent_definition_to_dict(defn: Any) -> dict[str, Any]:
        """Serialize an AgentDefinition / AgentDefinitionConfig for API responses."""
        allowed_tiers = {"trivial", "simple", "moderate", "complex", "expert"}
        tier = str(getattr(defn, "complexity_tier", "moderate") or "moderate").strip().lower()
        if tier not in allowed_tiers:
            tier = "moderate"
        return {
            "name": str(getattr(defn, "name", "") or ""),
            "model": str(getattr(defn, "model", "") or ""),
            "system_prompt": str(getattr(defn, "system_prompt", "") or ""),
            "allowed_tools": list(getattr(defn, "allowed_tools", []) or []),
            "denied_tools": list(getattr(defn, "denied_tools", []) or []),
            "max_tool_rounds": int(getattr(defn, "max_tool_rounds", 5) or 5),
            "temperature": getattr(defn, "temperature", None),
            "max_tokens": getattr(defn, "max_tokens", None),
            "complexity_tier": tier,
            "max_depth": int(getattr(defn, "max_depth", 3) or 3),
            "inherit_context": bool(getattr(defn, "inherit_context", False)),
            "output_schema": getattr(defn, "output_schema", None),
        }

    @staticmethod
    def _agent_definition_to_config(defn: AgentDefinition) -> "AgentDefinitionConfig":
        """Convert runtime AgentDefinition to persisted AgentDefinitionConfig."""
        from src.config import AgentDefinitionConfig

        return AgentDefinitionConfig(
            name=defn.name,
            model=defn.model,
            system_prompt=defn.system_prompt,
            allowed_tools=list(defn.allowed_tools),
            denied_tools=list(defn.denied_tools),
            max_tool_rounds=defn.max_tool_rounds,
            temperature=defn.temperature,
            max_tokens=defn.max_tokens,
            complexity_tier=defn.complexity_tier,
            max_depth=defn.max_depth,
            inherit_context=defn.inherit_context,
            output_schema=defn.output_schema,
        )

    @staticmethod
    def _agent_payload_to_definition(
        payload: dict[str, Any],
        *,
        fallback: AgentDefinition | None = None,
        created_by: str = "web_ui",
    ) -> AgentDefinition:
        """Build AgentDefinition from partial JSON payload."""

        def _list(name: str, fallback_list: list[str]) -> list[str]:
            value = payload.get(name, fallback_list)
            if value is None:
                return list(fallback_list)
            if isinstance(value, str):
                value = value.split(",")
            return [str(v).strip() for v in value if str(v).strip()]

        def _int(name: str, fallback_value: int) -> int:
            value = payload.get(name, fallback_value)
            if value in (None, ""):
                return fallback_value
            return int(value)

        def _float_or_none(name: str, fallback_value: float | None) -> float | None:
            value = payload.get(name, fallback_value)
            if value in (None, ""):
                return None
            return float(value)

        def _tier(name: str, fallback_value: str) -> str:
            value = str(payload.get(name, fallback_value) or fallback_value).strip().lower()
            if value not in {"trivial", "simple", "moderate", "complex", "expert"}:
                return "moderate"
            return value

        base = fallback or AgentDefinition(name="", model="")
        return AgentDefinition(
            name=str(payload.get("name", base.name) or "").strip(),
            model=str(payload.get("model", base.model) or "").strip(),
            system_prompt=str(payload.get("system_prompt", base.system_prompt) or ""),
            allowed_tools=_list("allowed_tools", list(base.allowed_tools)),
            denied_tools=_list("denied_tools", list(base.denied_tools)),
            max_tool_rounds=_int("max_tool_rounds", base.max_tool_rounds or 5),
            temperature=_float_or_none("temperature", base.temperature),
            max_tokens=(
                None
                if payload.get("max_tokens", base.max_tokens) in (None, "")
                else int(payload.get("max_tokens", base.max_tokens))
            ),
            complexity_tier=_tier("complexity_tier", base.complexity_tier or "moderate"),
            created_by=created_by,
            max_depth=_int("max_depth", base.max_depth or 3),
            inherit_context=bool(payload.get("inherit_context", base.inherit_context)),
            output_schema=payload.get("output_schema", base.output_schema),
        )

    def _get_main_sub_agent_definitions(self) -> list[Any]:
        """Get main agent sub-agent definitions from runtime, fallback to config."""
        manager = getattr(self.engine, "agent_manager", None)
        if manager is not None:
            try:
                return list(manager.list_definitions())
            except Exception:
                pass
        return list(self.config.agents.sub_agents)

    def _instance_info_from_cfg(
        self, cfg: "AgentInstanceConfig", runtime_inst: Any | None = None
    ) -> dict[str, Any]:
        """Build API info from persisted config, with optional runtime fields."""
        sub_defs: list[Any] = list(cfg.sub_agents)
        sub_agent_names = [a.name for a in sub_defs]
        sub_agent_defs = [self._agent_definition_to_dict(a) for a in sub_defs]
        active_sessions = 0
        if runtime_inst:
            try:
                sub_defs = list(runtime_inst.agent_manager.list_definitions())
                sub_agent_names = [a.name for a in sub_defs]
                sub_agent_defs = [self._agent_definition_to_dict(a) for a in sub_defs]
            except Exception:
                pass
            active_sessions = len(getattr(runtime_inst, "sessions", {}))

        return {
            "id": cfg.id,
            "name": cfg.name,
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
            "allowed_tools": cfg.allowed_tools,
            "denied_tools": cfg.denied_tools,
            "security": {
                "auto_approve_levels": cfg.security.auto_approve_levels,
                "max_risk_level": cfg.security.max_risk_level,
                "allowed_directories": cfg.security.allowed_directories,
                "blocked_commands": cfg.security.blocked_commands,
                "max_execution_time": cfg.security.max_execution_time,
            },
            "feature_overrides": {
                "context_compression_enabled": cfg.feature_overrides.context_compression_enabled,
                "context_compression_summarize_model": cfg.feature_overrides.context_compression_summarize_model,
                "context_compression_trigger_threshold": cfg.feature_overrides.context_compression_trigger_threshold,
                "memory_lifecycle_enabled": cfg.feature_overrides.memory_lifecycle_enabled,
                "learning_enabled": cfg.feature_overrides.learning_enabled,
                "code_feedback_enabled": cfg.feature_overrides.code_feedback_enabled,
                "vision_image_analysis_mode": cfg.feature_overrides.vision_image_analysis_mode,
                "task_complexity_enabled": cfg.feature_overrides.task_complexity_enabled,
            },
            "sub_agents": sub_agent_names,
            "sub_agent_defs": sub_agent_defs,
            "active_sessions": active_sessions,
            "running": bool(runtime_inst),
        }

    def _upsert_agent_instance_config(self, cfg: "AgentInstanceConfig") -> None:
        """Insert or replace an AgentInstanceConfig in self.config.agents.instances."""
        cfg_copy = cfg.model_copy(deep=True)
        for idx, existing in enumerate(self.config.agents.instances):
            if existing.id == cfg.id:
                self.config.agents.instances[idx] = cfg_copy
                break
        else:
            self.config.agents.instances.append(cfg_copy)

    def _remove_agent_instance_config(self, instance_id: str) -> None:
        """Remove an AgentInstanceConfig from self.config by instance ID."""
        self.config.agents.instances = [
            cfg for cfg in self.config.agents.instances if cfg.id != instance_id
        ]

    def _save_runtime_config(self) -> None:
        """Persist current runtime config to ~/.kuro/config.yaml."""
        from src.config import save_config

        save_config(self.config)

    @staticmethod
    def _is_env_var_name(value: str) -> bool:
        """Return True if value is a valid environment variable name."""
        return bool(_ENV_VAR_NAME_RE.fullmatch((value or "").strip()))

    def _validate_instance_binding(self, cfg: "AgentInstanceConfig") -> None:
        """Validate bot binding format for an AgentInstanceConfig."""
        binding = cfg.bot_binding
        if not binding.adapter_type:
            return

        token_env = (binding.bot_token_env or "").strip()
        if not token_env:
            raise ValueError(
                "Bot Token Env Var is required when Bot Adapter is enabled."
            )
        if not self._is_env_var_name(token_env):
            raise ValueError(
                "Bot Token Env Var must be an environment variable name "
                "(e.g. KURO_DISCORD_TOKEN_CS), not a raw token value."
            )

    def _apply_config_changes(self, new_config: "KuroConfig") -> list[str]:
        """Apply config changes to the running engine without restart.

        Returns a list of what was applied.
        """
        applied: list[str] = []
        old = self.config

        # Context compression settings
        if new_config.context_compression != old.context_compression:
            mm = self.engine.memory
            if mm.compressor:
                mm.compressor.config = new_config.context_compression
                applied.append("context_compression")

        # Memory lifecycle settings
        if new_config.memory_lifecycle != old.memory_lifecycle:
            mm = self.engine.memory
            if mm.lifecycle:
                mm.lifecycle.config = new_config.memory_lifecycle
                applied.append("memory_lifecycle")

        # Learning settings
        if new_config.learning != old.learning:
            mm = self.engine.memory
            if mm.learning:
                mm.learning.config = new_config.learning
                applied.append("learning")

        # Code feedback settings
        if new_config.code_feedback != old.code_feedback:
            if new_config.code_feedback.enabled:
                from src.core.code_feedback import CodeFeedbackLoop
                self.engine.code_feedback = CodeFeedbackLoop(new_config.code_feedback)
            else:
                self.engine.code_feedback = None
            applied.append("code_feedback")

        # Vision / image analysis settings
        if new_config.vision != old.vision:
            self.engine.config.vision = new_config.vision
            applied.append("vision")

        # Security settings
        if new_config.security != old.security:
            self.engine.approval_policy = self.engine.approval_policy.__class__(new_config.security)
            self.approval_cb.approval_policy = self.engine.approval_policy
            if getattr(self.engine, "agent_manager", None):
                self.engine.agent_manager.approval_policy = self.engine.approval_policy
                self.engine.agent_manager.config = new_config
            if getattr(self.engine, "instance_manager", None):
                # Keep newly-created instance runners aligned with latest security config.
                with contextlib.suppress(Exception):
                    self.engine.instance_manager._approval_policy = self.engine.approval_policy
                    self.engine.instance_manager._config = new_config
            applied.append("security")

        # Model settings (default model, temperature, etc.)
        if new_config.models.default != old.models.default:
            self.engine.model.config.models.default = new_config.models.default
            applied.append("default_model")

        if new_config.models.temperature != old.models.temperature:
            self.engine.model.config.models.temperature = new_config.models.temperature
            applied.append("temperature")

        if new_config.models.max_tokens != old.models.max_tokens:
            self.engine.model.config.models.max_tokens = new_config.models.max_tokens
            applied.append("max_tokens")

        # Sandbox settings
        if new_config.sandbox != old.sandbox:
            self.engine.sandbox = self.engine.sandbox.__class__(new_config.sandbox)
            applied.append("sandbox")

        # Diagnostics settings (applied via config reference, no engine restart needed)
        if new_config.diagnostics != old.diagnostics:
            applied.append("diagnostics")

        # Tracing settings
        if new_config.tracing != old.tracing:
            if new_config.tracing.enabled:
                try:
                    from src.core.tracing import init_tracing
                    init_tracing(
                        project_name=new_config.tracing.project_name,
                        tags=new_config.tracing.tags,
                    )
                except Exception:
                    pass
            applied.append("tracing")

        # Task complexity + ML classifier settings
        if new_config.task_complexity != old.task_complexity:
            estimator = getattr(self.engine, "complexity_estimator", None)
            if estimator:
                # Hot-reload ML classifier (enable/disable/change mode)
                ml_status = estimator.reload_ml_classifier(new_config.task_complexity)
                applied.append(f"task_complexity({ml_status})")
            elif new_config.task_complexity.enabled:
                # Estimator didn't exist before — create it now
                from src.core.complexity import ComplexityEstimator
                self.engine.complexity_estimator = ComplexityEstimator(
                    config=new_config.task_complexity,
                    model_router=self.engine.model,
                )
                # If ML is also enabled, trigger a reload to load the model
                if new_config.task_complexity.ml_model_enabled:
                    ml_status = self.engine.complexity_estimator.reload_ml_classifier(
                        new_config.task_complexity,
                    )
                    applied.append(f"task_complexity(created+{ml_status})")
                else:
                    applied.append("task_complexity(created)")

        # Delegation complexity routing settings (used directly by delegation tool)
        if new_config.delegation_complexity != old.delegation_complexity:
            applied.append("delegation_complexity")

        # MCP bridge settings (reconnect/reload tools)
        if new_config.mcp != old.mcp:
            bridge = getattr(self.engine, "mcp_bridge", None)
            if bridge is not None:
                bridge.update_config(new_config.mcp)
                with contextlib.suppress(Exception):
                    loop = asyncio.get_running_loop()
                    loop.create_task(bridge.refresh_now())
            applied.append("mcp")

        # Update the stored config reference
        self.config = new_config
        self.engine.config = new_config

        return applied

    def _cache_session(self, session_id: str, conn: ConnectionState) -> None:
        """Cache a session for later restoration after disconnect."""
        self._session_cache[session_id] = (conn, time.time())
        # Evict expired entries
        now = time.time()
        expired = [
            sid for sid, (_, ts) in self._session_cache.items()
            if now - ts > self._SESSION_CACHE_TTL
        ]
        for sid in expired:
            self._session_cache.pop(sid, None)

    def _restore_session(self, session_id: str) -> ConnectionState | None:
        """Try to restore a cached session. Returns None if not found or expired."""
        cached = self._session_cache.pop(session_id, None)
        if cached is None:
            return None
        conn, ts = cached
        if time.time() - ts > self._SESSION_CACHE_TTL:
            return None  # Expired
        return conn

    async def _handle_websocket(self, ws: WebSocket) -> None:
        await ws.accept()

        # Wait for the first message which may contain a session_id for restoration.
        # The frontend always sends { type: "restore", session_id: ..., agent_id: ... }
        conn = None
        restored = False
        replay_data = None  # non-restore message that arrived first (fallback)

        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
            data = json.loads(raw)
            if data.get("type") == "restore":
                sid = data.get("session_id")
                if sid:
                    conn = self._restore_session(sid)
                    if conn:
                        restored = True
                        logger.info("session_restored", session_id=conn.session.id)
            else:
                # First message was NOT a handshake (old client?) — save for replay
                replay_data = data
        except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
            pass

        if conn is None:
            session = Session(adapter="web")
            conn = ConnectionState(session=session)
        conn.oauth_session_id = self._oauth.get_session_id_from_cookies(dict(ws.cookies or {}))
        if conn.model_auth_mode == "oauth" and not conn.oauth_session_id:
            conn.model_auth_mode = "api"

        session = conn.session
        self._connections[session.id] = conn
        self.approval_cb.register_websocket(session.id, ws)
        self._tool_cb.register_websocket(session.id, ws)

        # Send initial status + history if restored
        try:
            agent_id = "main"
            status_msg = self._build_status_payload(
                conn=conn,
                engine=self.engine,
                session=session,
                agent_id=agent_id,
                restored=restored,
            )
            await ws.send_json(status_msg)

            # If restored, send conversation history back to frontend
            if restored and session.messages:
                history = []
                for msg in session.messages:
                    if msg.role.value in ("user", "assistant") and isinstance(msg.content, str):
                        history.append({"role": msg.role.value, "content": msg.content})
                if history:
                    await ws.send_json({"type": "history", "messages": history, "agent_id": agent_id})
        except Exception:
            return

        try:
            # If the first message was not a handshake, process it now
            if replay_data is not None:
                await self._process_ws_message(ws, conn, replay_data)

            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = data.get("type")
                agent_id = data.get("agent_id", "main")

                if msg_type == "restore":
                    # Multi-panel restore: create/restore session for the given agent_id
                    await self._handle_agent_restore(ws, conn, data)
                    continue

                if msg_type == "message":
                    text = data.get("text", "").strip()
                    if not text:
                        continue
                    # Check for in-flight task for this specific agent
                    existing = conn.get_chat_task(agent_id)
                    if existing and not existing.done():
                        await ws.send_json({
                            "type": "error",
                            "message": "Please wait for the current response to finish.",
                            "agent_id": agent_id,
                        })
                        continue
                    task = asyncio.create_task(
                        self._handle_chat_message(ws, conn, text, agent_id=agent_id)
                    )
                    conn.set_chat_task(agent_id, task)

                elif msg_type == "approval_response":
                    approval_id = data.get("approval_id", "")
                    action = data.get("action", "deny")
                    resolved = self.approval_cb.resolve_approval(approval_id, action)
                    await ws.send_json({
                        "type": "approval_result",
                        "approval_id": approval_id,
                        "status": "resolved" if resolved else "not_found",
                        "agent_id": agent_id,
                    })

                elif msg_type == "command":
                    await self._handle_command(ws, conn, data)

                else:
                    await ws.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                        "agent_id": agent_id,
                    })

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("websocket_error", error=str(e))
        finally:
            self.approval_cb.unregister_websocket(session.id)
            self._tool_cb.unregister_websocket(session.id)
            # Also unregister agent sessions
            for asid in list(conn.agent_sessions.keys()):
                agent_sess = conn.agent_sessions[asid]
                self.approval_cb.unregister_websocket(agent_sess.id)
                self._tool_cb.unregister_websocket(agent_sess.id)
            self._connections.pop(session.id, None)
            # Cache the session for potential reconnection
            self._cache_session(session.id, conn)

    async def _handle_agent_restore(
        self, ws: WebSocket, conn: ConnectionState, data: dict
    ) -> None:
        """Handle a restore/handshake for a specific agent panel."""
        agent_id = data.get("agent_id", "main")
        sid = data.get("session_id")

        if agent_id == "main":
            # Main agent already restored in the initial handshake
            return

        # Get or create session for this agent
        existing = conn.agent_sessions.get(agent_id)
        if existing:
            session = existing
        else:
            session = Session(adapter="web")
            conn.agent_sessions[agent_id] = session

        # Register approval callbacks for agent session
        self.approval_cb.register_websocket(session.id, ws, agent_id=agent_id)
        self._tool_cb.register_websocket(session.id, ws, agent_id=agent_id)

        # Resolve the engine for this agent
        engine = self._resolve_engine(agent_id, fallback_to_main=False)
        if engine is None:
            await ws.send_json({
                "type": "error",
                "message": f"Agent '{agent_id}' is not running or unavailable.",
                "agent_id": agent_id,
            })
            return

        await ws.send_json(
            self._build_status_payload(
                conn=conn,
                engine=engine or self.engine,
                session=session,
                agent_id=agent_id,
                restored=False,
            )
        )

    def _resolve_engine(self, agent_id: str, *, fallback_to_main: bool = True):
        """Resolve the Engine for a given agent_id."""
        if not agent_id or agent_id == "main":
            return self.engine
        instance_manager = getattr(self.engine, "instance_manager", None)
        if instance_manager:
            inst = instance_manager.get(agent_id)
            if inst:
                return inst.engine
        logger.warning(
            "agent_engine_not_found",
            agent_id=agent_id,
            fallback_to_main=fallback_to_main,
        )
        if fallback_to_main:
            return self.engine
        return None

    async def _process_ws_message(
        self, ws: WebSocket, conn: ConnectionState, data: dict
    ) -> None:
        """Process a single parsed WebSocket message (used for replaying non-handshake first messages)."""
        msg_type = data.get("type")
        agent_id = data.get("agent_id", "main")
        if msg_type == "message":
            text = data.get("text", "").strip()
            if text:
                task = asyncio.create_task(
                    self._handle_chat_message(ws, conn, text, agent_id=agent_id)
                )
                conn.set_chat_task(agent_id, task)
        elif msg_type == "approval_response":
            approval_id = data.get("approval_id", "")
            action = data.get("action", "deny")
            self.approval_cb.resolve_approval(approval_id, action)
        elif msg_type == "command":
            await self._handle_command(ws, conn, data)

    async def _handle_chat_message(
        self, ws: WebSocket, conn: ConnectionState, text: str,
        *, agent_id: str = "main",
    ) -> None:
        """Process a chat message in the background.

        Routes to the correct engine based on *agent_id*.
        """
        try:
            await ws.send_json({"type": "stream_start", "agent_id": agent_id})

            # Resolve engine and session for this agent
            engine = self._resolve_engine(agent_id, fallback_to_main=False)
            if engine is None:
                await ws.send_json({
                    "type": "error",
                    "message": f"Agent '{agent_id}' is not running or unavailable.",
                    "agent_id": agent_id,
                })
                await ws.send_json({"type": "stream_end", "agent_id": agent_id})
                return
            session = conn.get_session(agent_id)
            logger.info(
                "web_agent_chat_routed",
                agent_id=agent_id,
                session_id=session.id,
                engine_default_model=getattr(engine.model, "default_model", None),
                model_override=conn.model_override,
                model_auth_mode=conn.model_auth_mode,
            )

            # If the agent panel has no session yet, create one
            if session is conn.session and agent_id != "main":
                session = Session(adapter="web")
                conn.set_session(agent_id, session)
                self.approval_cb.register_websocket(session.id, ws, agent_id=agent_id)
                self._tool_cb.register_websocket(session.id, ws, agent_id=agent_id)
            session.metadata["_dashboard_agent_id"] = agent_id or "main"

            model_name = conn.model_override
            model_is_openai = self._is_openai_model(model_name)
            oauth_mode = conn.model_auth_mode == "oauth" and model_is_openai
            session.metadata["model_auth_mode"] = conn.model_auth_mode
            if model_name:
                session.metadata["model_override"] = model_name

            model_ctx = contextlib.nullcontext()
            cached_provider_ctx: dict[str, Any] | None = None
            if conn.oauth_session_id:
                auth_context = await self._oauth.get_auth_context(conn.oauth_session_id)
                if auth_context and auth_context.get("access_token"):
                    cached_provider_ctx = {
                        "mode": "codex_oauth",
                        "access_token": auth_context.get("access_token", ""),
                        "account_id": auth_context.get("account_id", ""),
                        "plan_type": auth_context.get("plan_type", ""),
                        "email": auth_context.get("email", ""),
                        "originator": "codex_cli_rs",
                    }
            if cached_provider_ctx:
                session.metadata["_openai_oauth_provider_ctx"] = cached_provider_ctx
            else:
                session.metadata.pop("_openai_oauth_provider_ctx", None)

            if oauth_mode:
                auth_context = await self._oauth.get_auth_context(conn.oauth_session_id)
                if not auth_context or not auth_context.get("access_token"):
                    await ws.send_json({
                        "type": "error",
                        "message": "OpenAI OAuth token missing or expired. Please sign in again.",
                        "agent_id": agent_id,
                    })
                    await ws.send_json({"type": "stream_end", "agent_id": agent_id})
                    return
                provider_ctx = cached_provider_ctx or {
                    "mode": "codex_oauth",
                    "access_token": auth_context.get("access_token", ""),
                    "account_id": auth_context.get("account_id", ""),
                    "plan_type": auth_context.get("plan_type", ""),
                    "email": auth_context.get("email", ""),
                    "originator": "codex_cli_rs",
                }
                if hasattr(engine.model, "provider_auth_override"):
                    model_ctx = engine.model.provider_auth_override("openai", provider_ctx)
                elif hasattr(engine.model, "provider_api_key_override"):
                    model_ctx = engine.model.provider_api_key_override(
                        "openai",
                        str(auth_context.get("access_token", "")),
                    )

            # Use stream_message for streaming support
            with model_ctx:
                async for chunk in engine.stream_message(
                    text, session, model=model_name
                ):
                    await ws.send_json({"type": "stream_chunk", "text": chunk, "agent_id": agent_id})

            await ws.send_json({"type": "stream_end", "agent_id": agent_id})
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("chat_error", error=str(e), agent_id=agent_id)
            try:
                await ws.send_json({
                    "type": "error",
                    "message": f"Error: {str(e)}",
                    "agent_id": agent_id,
                })
            except Exception:
                pass

    async def _handle_command(
        self, ws: WebSocket, conn: ConnectionState, data: dict
    ) -> None:
        """Handle a command message."""
        command = data.get("command", "")
        args = data.get("args", "")
        agent_id = data.get("agent_id", "main")
        if agent_id != "main":
            resolved = self._resolve_engine(agent_id, fallback_to_main=False)
            if resolved is None:
                await ws.send_json({
                    "type": "error",
                    "message": f"Agent '{agent_id}' is not running or unavailable.",
                    "agent_id": agent_id,
                })
                return

        if command == "model":
            model_name, auth_mode = self._parse_model_selection(args)
            if auth_mode == "oauth":
                if not self._is_openai_model(model_name):
                    await ws.send_json({
                        "type": "error",
                        "message": "OAuth mode is only available for OpenAI models.",
                        "agent_id": agent_id,
                    })
                    engine = self._resolve_engine(agent_id)
                    await ws.send_json(
                        self._build_status_payload(
                            conn=conn,
                            engine=engine,
                            session=conn.get_session(agent_id),
                            agent_id=agent_id,
                        )
                    )
                    return
                if not conn.oauth_session_id:
                    await ws.send_json({
                        "type": "error",
                        "message": "OpenAI OAuth is not connected in this browser session.",
                        "agent_id": agent_id,
                    })
                    engine = self._resolve_engine(agent_id)
                    await ws.send_json(
                        self._build_status_payload(
                            conn=conn,
                            engine=engine,
                            session=conn.get_session(agent_id),
                            agent_id=agent_id,
                        )
                    )
                    return
                auth_context = await self._oauth.get_auth_context(conn.oauth_session_id)
                if not auth_context or not auth_context.get("access_token"):
                    await ws.send_json({
                        "type": "error",
                        "message": "OpenAI OAuth token missing or expired. Please sign in again.",
                        "agent_id": agent_id,
                    })
                    engine = self._resolve_engine(agent_id)
                    await ws.send_json(
                        self._build_status_payload(
                            conn=conn,
                            engine=engine,
                            session=conn.get_session(agent_id),
                            agent_id=agent_id,
                        )
                    )
                    return
                if model_name and not self._is_openai_oauth_model_candidate(model_name):
                    await ws.send_json({
                        "type": "error",
                        "message": (
                            f"Model '{model_name}' is not supported in OpenAI OAuth mode. "
                            "Choose a model from OpenAI (OAuth Subscription)."
                        ),
                        "agent_id": agent_id,
                    })
                    engine = self._resolve_engine(agent_id)
                    await ws.send_json(
                        self._build_status_payload(
                            conn=conn,
                            engine=engine,
                            session=conn.get_session(agent_id),
                            agent_id=agent_id,
                        )
                    )
                    return
            conn.model_override = model_name
            conn.model_auth_mode = auth_mode
            logger.info(
                "web_model_override_set",
                session_id=conn.get_session(agent_id).id,
                agent_id=agent_id,
                model=model_name,
                mode=auth_mode,
                raw=args,
            )
            engine = self._resolve_engine(agent_id)
            await ws.send_json(
                self._build_status_payload(
                    conn=conn,
                    engine=engine,
                    session=conn.get_session(agent_id),
                    agent_id=agent_id,
                )
            )

        elif command == "clear":
            session = conn.get_session(agent_id)
            old_id = session.id
            new_session = Session(adapter="web")
            conn.set_session(agent_id, new_session)
            self.approval_cb.unregister_websocket(old_id)
            self.approval_cb.register_websocket(new_session.id, ws, agent_id=agent_id)
            self._tool_cb.unregister_websocket(old_id)
            self._tool_cb.register_websocket(new_session.id, ws, agent_id=agent_id)
            if agent_id == "main":
                self._connections.pop(old_id, None)
                self._session_cache.pop(old_id, None)
                self._connections[new_session.id] = conn
            engine = self._resolve_engine(agent_id)
            await ws.send_json(
                self._build_status_payload(
                    conn=conn,
                    engine=engine,
                    session=new_session,
                    agent_id=agent_id,
                )
            )

        elif command == "trust":
            level = args if args in ("low", "medium", "high", "critical") else "low"
            session = conn.get_session(agent_id)
            session.trust_level = level
            level_map = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM,
                         "high": RiskLevel.HIGH, "critical": RiskLevel.CRITICAL}
            engine = self._resolve_engine(agent_id)
            engine.approval_policy.elevate_session_trust(
                session.id, level_map[level])
            await ws.send_json(
                self._build_status_payload(
                    conn=conn,
                    engine=engine,
                    session=session,
                    agent_id=agent_id,
                )
            )

        elif command == "skills":
            sm = self.engine.skills
            skills = sm.list_skills() if sm else []
            active = sm._active if sm else set()
            await ws.send_json({
                "type": "skills_list",
                "skills": [
                    {"name": s.name, "description": s.description, "active": s.name in active}
                    for s in skills
                ],
                "agent_id": agent_id,
            })

        elif command == "skill":
            sm = self.engine.skills
            if sm and args:
                if args in sm._active:
                    sm.deactivate(args)
                    toggled = False
                else:
                    toggled = sm.activate(args)
                active = sm._active
                await ws.send_json({
                    "type": "skills_list",
                    "skills": [
                        {"name": s.name, "description": s.description, "active": s.name in active}
                        for s in sm.list_skills()
                    ],
                    "agent_id": agent_id,
                })
            else:
                await ws.send_json({"type": "error", "message": "Usage: skill <name>", "agent_id": agent_id})

        else:
            await ws.send_json({
                "type": "error",
                "message": f"Unknown command: {command}",
                "agent_id": agent_id,
            })

    async def run(self) -> None:
        """Start the uvicorn server."""
        config = uvicorn.Config(
            self.app,
            host=self.config.web_ui.host,
            port=self.config.web_ui.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()
