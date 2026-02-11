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
from src.core.engine import ApprovalCallback, Engine
from src.core.types import Session
from src.tools.base import RiskLevel

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

    def __init__(self, timeout: int = 60) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._websockets: dict[str, WebSocket] = {}
        self._timeout = timeout

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
            session.trust_level = "high"

        return action in ("approve", "trust")

    def resolve_approval(self, approval_id: str, action: str) -> bool:
        fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(action)
        return True


class WebServer:
    """FastAPI-based web server with WebSocket chat."""

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        self.engine = engine
        self.config = config
        self.approval_cb = WebApprovalCallback(
            timeout=config.web_ui.port  # reuse port field... no, use 60s default
        )
        # Fix: use a sensible timeout
        self.approval_cb._timeout = 60
        self.engine.approval_cb = self.approval_cb
        self._connections: dict[str, ConnectionState] = {}
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
            models = await self.engine.model.list_models()
            return {
                "default": self.engine.model.default_model,
                "available": models,
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

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await self._handle_websocket(ws)

        return app

    async def _handle_websocket(self, ws: WebSocket) -> None:
        await ws.accept()

        session = Session(adapter="web")
        conn = ConnectionState(session=session)
        self._connections[session.id] = conn
        self.approval_cb.register_websocket(session.id, ws)

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
            self._connections.pop(old_id, None)
            self._connections[conn.session.id] = conn
            await ws.send_json({
                "type": "status",
                "model": conn.model_override or self.engine.model.default_model,
                "trust_level": conn.session.trust_level,
                "session_id": conn.session.id,
            })

        elif command == "trust":
            level = args if args in ("low", "medium", "high") else "low"
            conn.session.trust_level = level
            await ws.send_json({
                "type": "status",
                "model": conn.model_override or self.engine.model.default_model,
                "trust_level": conn.session.trust_level,
                "session_id": conn.session.id,
            })

        else:
            await ws.send_json({
                "type": "error",
                "message": f"Unknown command: {command}",
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
