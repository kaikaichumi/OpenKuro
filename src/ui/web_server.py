"""Web GUI server: FastAPI + WebSocket with approval support.

Provides a browser-based chat interface at http://localhost:7860.
Uses WebSocket for real-time streaming and tool approval dialogs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.config import KuroConfig
from src.core.engine import ApprovalCallback, Engine, ToolExecutionCallback, _encode_image_base64
from src.core.types import Session
from src.tools.base import RiskLevel, ToolResult

logger = structlog.get_logger()

# Path to static web files
WEB_DIR = Path(__file__).parent / "web"


@dataclass
class ConnectionState:
    """Mutable state for each WebSocket connection."""

    session: Session
    model_override: str | None = None
    chat_task: asyncio.Task | None = None


class WebApprovalCallback(ApprovalCallback):
    """Tool approval via WebSocket + asyncio.Future."""

    def __init__(self, timeout: int = 60, approval_policy=None) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._websockets: dict[str, WebSocket] = {}
        self._timeout = timeout
        self.approval_policy = approval_policy

    def register_websocket(self, session_id: str, ws: WebSocket) -> None:
        self._websockets[session_id] = ws

    def unregister_websocket(self, session_id: str) -> None:
        self._websockets.pop(session_id, None)
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
        try:
            await ws.send_json({
                "type": "approval_request",
                "approval_id": approval_id,
                "tool_name": tool_name,
                "params": params,
                "risk_level": risk_level.value,
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

    def register_websocket(self, session_id: str, ws: WebSocket) -> None:
        self._websockets[session_id] = ws
        self._step_counter[session_id] = 0

    def unregister_websocket(self, session_id: str) -> None:
        self._websockets.pop(session_id, None)
        self._step_counter.pop(session_id, None)

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

                if tool_name in self._SCREEN_TOOLS and result.image_path:
                    data_uri = _encode_image_base64(result.image_path)
                    if data_uri:
                        await ws.send_json({
                            "type": "screen_update",
                            "image": data_uri,
                            "action": f"Screenshot ({tool_name})",
                            "step": step,
                        })
                elif tool_name in self._ACTION_TOOLS:
                    action_desc = self._describe_action(tool_name, params)
                    await ws.send_json({
                        "type": "screen_action",
                        "action": action_desc,
                        "step": step,
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
        from src.core.collaboration import CollaborationHub

        self.engine = engine
        self.config = config
        self.approval_cb = WebApprovalCallback(
            timeout=60,
            approval_policy=engine.approval_policy,
        )
        self.engine.approval_cb = self.approval_cb
        self._tool_cb = WebToolCallback()
        self.engine.tool_callback = self._tool_cb
        self._connections: dict[str, ConnectionState] = {}

        # Collaborative session support
        self.collab_hub = CollaborationHub()
        # session_id -> {user_id -> WebSocket}
        self._collab_connections: dict[str, dict[str, WebSocket]] = {}

        self.app = self._create_app()

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
        async def get_models():
            groups = await self.engine.model.list_models_grouped()
            flat = await self.engine.model.list_models()
            return {
                "default": self.engine.model.default_model,
                "groups": groups,
                "available": flat,  # backward compat
            }

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
            names = self.engine.tools.registry.get_names()
            return {"tools": sorted(names), "count": len(names)}

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
        async def update_personality(request):
            """Update personality settings."""
            from src.config import get_kuro_home
            data = await request.json()
            content = data.get("content", "")
            personality_path = get_kuro_home() / "personality.md"
            personality_path.write_text(content, encoding="utf-8")
            return {"status": "ok", "message": "Personality updated"}

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

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await self._handle_websocket(ws)

        # === Collaboration API ===

        @app.get("/collab")
        async def collab_page():
            collab_file = WEB_DIR / "collab.html"
            if collab_file.exists():
                return FileResponse(str(collab_file), media_type="text/html")
            return HTMLResponse("<h1>Collaboration</h1><p>collab.html not found</p>")

        @app.post("/api/collab/create")
        async def create_collab(request_body: dict = None):
            try:
                from fastapi import Request
            except ImportError:
                pass
            data = request_body or {}
            name = data.get("name", "Collaboration Session")
            user_id = data.get("user_id", f"user_{uuid.uuid4().hex[:6]}")
            display_name = data.get("display_name", "Host")
            collab = self.collab_hub.create_session(
                name=name,
                owner_user_id=user_id,
                owner_display_name=display_name,
            )
            return {
                "session_id": collab.id,
                "invite_code": collab.invite_code,
                "name": collab.name,
            }

        @app.post("/api/collab/join")
        async def join_collab(request_body: dict = None):
            data = request_body or {}
            invite_code = data.get("invite_code", "")
            user_id = data.get("user_id", f"user_{uuid.uuid4().hex[:6]}")
            display_name = data.get("display_name", "Guest")
            collab = self.collab_hub.join_by_invite(
                invite_code=invite_code,
                user_id=user_id,
                display_name=display_name,
            )
            if collab is None:
                return {"error": "Invalid invite code"}, 404
            return {
                "session_id": collab.id,
                "name": collab.name,
                "participants": [p.to_dict() for p in collab.participants.values()],
            }

        @app.get("/api/collab/sessions")
        async def list_collab_sessions(user_id: str = Query(...)):
            sessions = self.collab_hub.get_user_sessions(user_id)
            return {
                "sessions": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "participants": len(s.participants),
                        "invite_code": s.invite_code,
                    }
                    for s in sessions
                ]
            }

        @app.get("/api/collab/{session_id}")
        async def get_collab_session(session_id: str):
            collab = self.collab_hub.get_session(session_id)
            if collab is None:
                return {"error": "Session not found"}, 404
            return collab.to_dict()

        @app.websocket("/ws/collab/{session_id}")
        async def collab_websocket(ws: WebSocket, session_id: str):
            await self._handle_collab_websocket(ws, session_id)

        return app

    async def _handle_websocket(self, ws: WebSocket) -> None:
        await ws.accept()

        session = Session(adapter="web")
        conn = ConnectionState(session=session)
        self._connections[session.id] = conn
        self.approval_cb.register_websocket(session.id, ws)
        self._tool_cb.register_websocket(session.id, ws)

        # Send initial status
        try:
            await ws.send_json({
                "type": "status",
                "model": self.engine.model.default_model,
                "trust_level": session.trust_level,
                "session_id": session.id,
            })
        except Exception:
            return

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = data.get("type")

                if msg_type == "message":
                    text = data.get("text", "").strip()
                    if not text:
                        continue
                    # Run chat in background to keep receive loop free for approvals
                    if conn.chat_task and not conn.chat_task.done():
                        await ws.send_json({
                            "type": "error",
                            "message": "Please wait for the current response to finish.",
                        })
                        continue
                    conn.chat_task = asyncio.create_task(
                        self._handle_chat_message(ws, conn, text)
                    )

                elif msg_type == "approval_response":
                    approval_id = data.get("approval_id", "")
                    action = data.get("action", "deny")
                    resolved = self.approval_cb.resolve_approval(approval_id, action)
                    await ws.send_json({
                        "type": "approval_result",
                        "approval_id": approval_id,
                        "status": "resolved" if resolved else "not_found",
                    })

                elif msg_type == "command":
                    await self._handle_command(ws, conn, data)

                else:
                    await ws.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    })

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("websocket_error", error=str(e))
        finally:
            self.approval_cb.unregister_websocket(session.id)
            self._tool_cb.unregister_websocket(session.id)
            self._connections.pop(session.id, None)

    async def _handle_chat_message(
        self, ws: WebSocket, conn: ConnectionState, text: str
    ) -> None:
        """Process a chat message in the background."""
        try:
            await ws.send_json({"type": "stream_start"})

            # Use stream_message for streaming support
            async for chunk in self.engine.stream_message(
                text, conn.session, model=conn.model_override
            ):
                await ws.send_json({"type": "stream_chunk", "text": chunk})

            await ws.send_json({"type": "stream_end"})
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("chat_error", error=str(e))
            try:
                await ws.send_json({
                    "type": "error",
                    "message": f"Error: {str(e)}",
                })
            except Exception:
                pass

    async def _handle_command(
        self, ws: WebSocket, conn: ConnectionState, data: dict
    ) -> None:
        """Handle a command message."""
        command = data.get("command", "")
        args = data.get("args", "")

        if command == "model":
            if args:
                conn.model_override = args
            await ws.send_json({
                "type": "status",
                "model": conn.model_override or self.engine.model.default_model,
                "trust_level": conn.session.trust_level,
                "session_id": conn.session.id,
            })

        elif command == "clear":
            old_id = conn.session.id
            conn.session = Session(adapter="web")
            self.approval_cb.unregister_websocket(old_id)
            self.approval_cb.register_websocket(conn.session.id, ws)
            self._tool_cb.unregister_websocket(old_id)
            self._tool_cb.register_websocket(conn.session.id, ws)
            self._connections.pop(old_id, None)
            self._connections[conn.session.id] = conn
            await ws.send_json({
                "type": "status",
                "model": conn.model_override or self.engine.model.default_model,
                "trust_level": conn.session.trust_level,
                "session_id": conn.session.id,
            })

        elif command == "trust":
            level = args if args in ("low", "medium", "high", "critical") else "low"
            conn.session.trust_level = level
            level_map = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM,
                         "high": RiskLevel.HIGH, "critical": RiskLevel.CRITICAL}
            self.engine.approval_policy.elevate_session_trust(
                conn.session.id, level_map[level])
            await ws.send_json({
                "type": "status",
                "model": conn.model_override or self.engine.model.default_model,
                "trust_level": conn.session.trust_level,
                "session_id": conn.session.id,
            })

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
                })
            else:
                await ws.send_json({"type": "error", "message": "Usage: skill <name>"})

        else:
            await ws.send_json({
                "type": "error",
                "message": f"Unknown command: {command}",
            })

    async def _handle_collab_websocket(self, ws: WebSocket, session_id: str) -> None:
        """Handle a collaborative session WebSocket connection.

        Protocol:
          1. Client connects and sends: {"user_id": "<id>"}
          2. Server confirms with collab_joined + current participants
          3. Client sends messages with type: message | typing | vote
          4. Server broadcasts updates to all participants in the session
        """
        from src.core.collaboration import Permission, VotingApproval

        await ws.accept()

        collab = self.collab_hub.get_session(session_id)
        if collab is None:
            await ws.send_json({"type": "error", "message": "Session not found"})
            await ws.close()
            return

        # First message must authenticate the user
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            auth = json.loads(raw)
            user_id = auth.get("user_id", "")
        except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
            await ws.send_json({"type": "error", "message": "Auth required: send {user_id}"})
            await ws.close()
            return

        if not user_id or user_id not in collab.participants:
            await ws.send_json({"type": "error", "message": "User not in session"})
            await ws.close()
            return

        # Register connection
        if session_id not in self._collab_connections:
            self._collab_connections[session_id] = {}
        self._collab_connections[session_id][user_id] = ws

        # Ensure a VotingApproval instance exists for this session
        if not hasattr(self, "_collab_voting"):
            self._collab_voting: dict[str, VotingApproval] = {}
        if session_id not in self._collab_voting:
            self._collab_voting[session_id] = VotingApproval(collab)

        # Mark online, send welcome, broadcast presence
        collab.set_online(user_id, True)
        display_name = collab.participants[user_id].display_name

        try:
            await ws.send_json({
                "type": "collab_joined",
                "session_id": session_id,
                "name": collab.name,
                "user_id": user_id,
                "participants": [p.to_dict() for p in collab.participants.values()],
            })
        except Exception:
            self._collab_connections.get(session_id, {}).pop(user_id, None)
            return

        await self._broadcast_collab(session_id, {
            "type": "collab_presence",
            "user_id": user_id,
            "display_name": display_name,
            "online": True,
        }, exclude_user_id=user_id)

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = data.get("type")

                if msg_type == "message":
                    if not collab.has_permission(user_id, Permission.WRITE):
                        await ws.send_json({"type": "error", "message": "No write permission"})
                        continue

                    text = data.get("text", "").strip()
                    if not text:
                        continue

                    collab.set_typing(user_id, False)

                    # Broadcast who is sending and what
                    await self._broadcast_collab(session_id, {
                        "type": "collab_stream_start",
                        "author_user_id": user_id,
                        "author_name": display_name,
                        "text": text,
                    })

                    try:
                        response = await self.engine.process_message(
                            text, collab.session, author_user_id=user_id
                        )
                        await self._broadcast_collab(session_id, {
                            "type": "collab_response",
                            "author_user_id": user_id,
                            "author_name": display_name,
                            "response": response,
                        })
                    except Exception as e:
                        logger.error("collab_message_error", session_id=session_id, error=str(e))
                        await self._broadcast_collab(session_id, {
                            "type": "error",
                            "message": f"Error: {e}",
                        })

                elif msg_type == "typing":
                    is_typing = bool(data.get("is_typing", False))
                    collab.set_typing(user_id, is_typing)
                    await self._broadcast_collab(session_id, {
                        "type": "collab_typing",
                        "user_id": user_id,
                        "display_name": display_name,
                        "is_typing": is_typing,
                    }, exclude_user_id=user_id)

                elif msg_type == "vote":
                    voting = self._collab_voting.get(session_id)
                    if voting is None:
                        await ws.send_json({"type": "error", "message": "No active vote"})
                        continue
                    approval_id = data.get("approval_id", "")
                    approve = bool(data.get("approve", False))
                    result = voting.cast_vote(approval_id, user_id, approve)
                    await self._broadcast_collab(session_id, {
                        "type": "collab_vote_update",
                        "approval_id": approval_id,
                        "voter_id": user_id,
                        "voter_name": display_name,
                        **result,
                    })

                else:
                    await ws.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    })

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("collab_websocket_error", session_id=session_id, error=str(e))
        finally:
            self._collab_connections.get(session_id, {}).pop(user_id, None)
            collab.set_online(user_id, False)
            await self._broadcast_collab(session_id, {
                "type": "collab_presence",
                "user_id": user_id,
                "display_name": display_name,
                "online": False,
            })

    async def _broadcast_collab(
        self,
        session_id: str,
        data: dict,
        exclude_user_id: str | None = None,
    ) -> None:
        """Broadcast a message to all WebSocket connections in a collab session."""
        conns = self._collab_connections.get(session_id, {})
        for uid, ws in list(conns.items()):
            if exclude_user_id and uid == exclude_user_id:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                pass  # Connection may have already closed

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
