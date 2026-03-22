"""MCP client bridge: expose remote MCP tools as local Kuro tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import structlog

from src import __version__
from src.config import MCPConfig, MCPServerConfig
from src.core.tool_system import ToolRegistry
from src.tools.base import RiskLevel, ToolContext, ToolResult
from src.tools.mcp.proxy import MCPProxyTool

logger = structlog.get_logger()

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_]+")
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _local_no_proxy_defaults() -> list[str]:
    """Default NO_PROXY patterns used when private network bypass is enabled."""
    return [
        "localhost",
        "127.0.0.1",
        "::1",
        "*.local",
        "10.*",
        "192.168.*",
        "172.16.*",
        "172.17.*",
        "172.18.*",
        "172.19.*",
        "172.20.*",
        "172.21.*",
        "172.22.*",
        "172.23.*",
        "172.24.*",
        "172.25.*",
        "172.26.*",
        "172.27.*",
        "172.28.*",
        "172.29.*",
        "172.30.*",
        "172.31.*",
    ]


def _mask_proxy_url(proxy_url: str) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return raw[:120]
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def build_gateway_proxy_env_for_subprocess(
    egress_broker: Any | None,
    *,
    inherited_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build proxy env overrides for subprocesses under Lite Gateway policy.

    Returns an empty dict when gateway routing should not be enforced.
    """
    if egress_broker is None:
        return {}

    if not bool(getattr(egress_broker, "gateway_enabled", False)):
        return {}

    mode = str(getattr(egress_broker, "gateway_mode", "enforce") or "enforce")
    mode = mode.strip().lower()
    if mode != "enforce":
        return {}

    proxy_url = str(getattr(egress_broker, "gateway_proxy_url", "") or "").strip()
    if not proxy_url:
        return {}

    no_proxy_values: list[str] = []
    bypass = getattr(egress_broker, "gateway_bypass_domains", []) or []
    for item in bypass:
        val = str(item or "").strip()
        if val:
            no_proxy_values.append(val)

    include_private = bool(
        getattr(egress_broker, "gateway_include_private_network", False)
    )
    if not include_private:
        no_proxy_values.extend(_local_no_proxy_defaults())

    inherited = inherited_env or {}
    existing = str(
        inherited.get("NO_PROXY")
        or inherited.get("no_proxy")
        or ""
    ).strip()
    if existing:
        no_proxy_values.extend([v.strip() for v in existing.split(",") if v.strip()])

    deduped: list[str] = []
    seen: set[str] = set()
    for raw in no_proxy_values:
        key = raw.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(raw)

    updates = {k: proxy_url for k in _PROXY_ENV_KEYS}
    if deduped:
        joined = ",".join(deduped)
        updates["NO_PROXY"] = joined
        updates["no_proxy"] = joined
    return updates


def _safe_slug(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "server"
    slug = _SAFE_ID_RE.sub("_", raw).strip("_")
    return slug or "server"


def _parse_risk_level(value: str) -> RiskLevel:
    raw = str(value or "").strip().lower()
    mapping = {
        "low": RiskLevel.LOW,
        "medium": RiskLevel.MEDIUM,
        "high": RiskLevel.HIGH,
        "critical": RiskLevel.CRITICAL,
    }
    return mapping.get(raw, RiskLevel.HIGH)


def _normalize_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        schema = dict(value)
    else:
        schema = {}
    if not schema:
        schema = {"type": "object", "properties": {}, "required": []}
    if "type" not in schema:
        schema["type"] = "object"
    if schema.get("type") == "object":
        if not isinstance(schema.get("properties"), dict):
            schema["properties"] = {}
        if not isinstance(schema.get("required"), list):
            schema["required"] = []
    return schema


def _format_rpc_error(err: Any) -> str:
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message")
        data = err.get("data")
        text = f"{code}: {msg}" if code is not None else str(msg or "RPC error")
        if data is not None:
            text = f"{text} | data={data}"
        return text
    return str(err or "RPC error")


def _result_to_output(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (int, float, bool)):
        return str(result)

    if isinstance(result, dict):
        chunks: list[str] = []
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    item_type = str(item.get("type") or "").strip().lower()
                    if item_type == "text":
                        text = str(item.get("text") or "")
                        if text:
                            chunks.append(text)
                        continue
                    if item_type in {"json", "json_object"} and "json" in item:
                        chunks.append(
                            json.dumps(item.get("json"), ensure_ascii=False, indent=2)
                        )
                        continue
                chunks.append(json.dumps(item, ensure_ascii=False))

        if "structuredContent" in result:
            chunks.append(
                json.dumps(result.get("structuredContent"), ensure_ascii=False, indent=2)
            )
        if "text" in result and str(result.get("text") or "").strip():
            chunks.append(str(result.get("text")))

        if chunks:
            return "\n\n".join([c for c in chunks if c.strip()])

    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return str(result)


@dataclass
class _ToolBinding:
    server_name: str
    remote_name: str
    local_name: str


@dataclass
class _ServerRuntime:
    config: MCPServerConfig
    client: "_StdioMCPClient | None" = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    connected: bool = False
    error: str = ""


class _StdioMCPClient:
    """Minimal stdio JSON-RPC client for MCP tools."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        egress_broker: Any | None = None,
    ) -> None:
        self.config = config
        self._egress_broker = egress_broker
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False

    async def start(self) -> None:
        if self.config.transport != "stdio":
            raise RuntimeError(f"Unsupported MCP transport: {self.config.transport}")
        if not self.config.command:
            raise RuntimeError("MCP command is empty")
        if self._proc and self._proc.returncode is None:
            return

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.config.env.items()})
        gateway_env = build_gateway_proxy_env_for_subprocess(
            self._egress_broker,
            inherited_env=env,
        )
        if gateway_env:
            env.update(gateway_env)
            logger.info(
                "mcp_gateway_proxy_applied",
                server=self.config.name,
                proxy=_mask_proxy_url(gateway_env.get("HTTPS_PROXY", "")),
            )
        self._proc = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._closed = False
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

        _ = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "kuro", "version": __version__},
            },
            timeout=self.config.startup_timeout,
        )
        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {}, timeout=self.config.request_timeout)
        if not isinstance(result, dict):
            return []
        tools = result.get("tools")
        if not isinstance(tools, list):
            return []
        normalized: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            normalized.append(
                {
                    "name": name,
                    "description": str(tool.get("description") or "").strip(),
                    "inputSchema": _normalize_schema(tool.get("inputSchema")),
                }
            )
        return normalized

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=self.config.request_timeout,
        )
        if isinstance(result, dict) and result.get("isError") is True:
            text = _result_to_output(result) or "MCP tool returned an error"
            raise RuntimeError(text)
        return result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self._send(payload)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: int,
    ) -> Any:
        if self._closed:
            raise RuntimeError("MCP client is closed")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            response = await asyncio.wait_for(future, timeout=max(1, int(timeout)))
        finally:
            self._pending.pop(request_id, None)

        if "error" in response:
            raise RuntimeError(_format_rpc_error(response.get("error")))
        return response.get("result")

    async def close(self) -> None:
        self._closed = True

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("MCP client closed"))
        self._pending.clear()

        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(Exception):
                await self._reader_task
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(Exception):
                await self._stderr_task

        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except Exception:
                self._proc.kill()
                with contextlib.suppress(Exception):
                    await self._proc.wait()
        self._proc = None
        self._reader_task = None
        self._stderr_task = None

    async def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("MCP process stdin is unavailable")
        if proc.returncode is not None:
            raise RuntimeError(f"MCP process exited with code {proc.returncode}")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        async with self._send_lock:
            proc.stdin.write(header + body)
            await proc.stdin.drain()

    async def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                msg = await self._read_message(proc.stdout)
                if msg is None:
                    break
                response_id = msg.get("id")
                if response_id is None:
                    # Notification from server, currently ignored.
                    continue
                try:
                    response_id_int = int(response_id)
                except Exception:
                    continue
                fut = self._pending.get(response_id_int)
                if fut and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("mcp_reader_loop_failed", server=self.config.name, error=str(e))
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("MCP connection closed"))

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("mcp_server_stderr", server=self.config.name, line=text[:500])
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _read_message(
        self,
        stream: asyncio.StreamReader,
    ) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = await stream.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            if ":" not in decoded:
                continue
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length_text = headers.get("content-length")
        if not length_text:
            return None
        try:
            length = int(length_text)
        except Exception:
            raise RuntimeError(f"Invalid Content-Length: {length_text}")
        if length <= 0:
            return None

        payload = await stream.readexactly(length)
        try:
            return json.loads(payload.decode("utf-8", errors="replace"))
        except Exception as e:
            raise RuntimeError(f"Invalid MCP JSON payload: {e}") from e


class MCPBridgeManager:
    """Manage MCP connections and register proxy tools into ToolRegistry."""

    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        egress_broker: Any | None = None,
    ) -> None:
        self._config = config or MCPConfig()
        self._egress_broker = egress_broker
        self._servers: dict[str, _ServerRuntime] = {}
        self._bindings: dict[str, _ToolBinding] = {}
        self._registered_tool_names: list[str] = []
        self._status: dict[str, dict[str, Any]] = {}
        self._registry: ToolRegistry | None = None
        self._reload_requested = True
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def update_config(
        self,
        config: MCPConfig,
        *,
        egress_broker: Any | None = None,
    ) -> None:
        self._config = config
        if egress_broker is not None:
            self._egress_broker = egress_broker
        self._reload_requested = True

    def set_egress_broker(self, egress_broker: Any | None) -> None:
        self._egress_broker = egress_broker
        self._reload_requested = True

    async def ensure_initialized(self, registry: ToolRegistry) -> None:
        async with self._init_lock:
            self._registry = registry
            if self._initialized and not self._reload_requested:
                return

            await self._teardown(registry)
            self._status = {}

            if not self._config.enabled:
                self._initialized = True
                self._reload_requested = False
                return

            existing_names = set(registry.get_names())
            for server in self._config.servers:
                runtime = _ServerRuntime(config=server)
                self._status[server.name] = {
                    "name": server.name,
                    "enabled": bool(server.enabled),
                    "connected": False,
                    "tool_count": 0,
                    "tools": [],
                    "error": "",
                    "transport": server.transport,
                    "command": server.command,
                }
                if not server.enabled:
                    continue
                if server.transport != "stdio":
                    runtime.error = f"Unsupported transport: {server.transport}"
                    self._status[server.name]["error"] = runtime.error
                    self._servers[server.name] = runtime
                    continue
                if not server.command:
                    runtime.error = "Missing command"
                    self._status[server.name]["error"] = runtime.error
                    self._servers[server.name] = runtime
                    continue

                client = _StdioMCPClient(
                    server,
                    egress_broker=self._egress_broker,
                )
                try:
                    await client.start()
                    tools = await client.list_tools()
                    runtime.client = client
                    runtime.tools = tools
                    runtime.connected = True

                    enabled_filter = set(server.enabled_tools)
                    if enabled_filter:
                        tools = [t for t in tools if t.get("name") in enabled_filter]

                    for remote_tool in tools:
                        remote_name = str(remote_tool.get("name") or "").strip()
                        if not remote_name:
                            continue
                        local_name = self._build_local_tool_name(
                            server,
                            remote_name,
                            existing_names,
                        )
                        existing_names.add(local_name)
                        self._bindings[local_name] = _ToolBinding(
                            server_name=server.name,
                            remote_name=remote_name,
                            local_name=local_name,
                        )

                        desc = str(remote_tool.get("description") or "").strip()
                        if not desc:
                            desc = f"MCP tool '{remote_name}' from server '{server.name}'"
                        params = _normalize_schema(remote_tool.get("inputSchema"))
                        proxy = MCPProxyTool(
                            local_name=local_name,
                            description=desc,
                            parameters=params,
                            risk_level=_parse_risk_level(server.risk_level),
                            bridge_manager=self,
                        )
                        registry.register(proxy)
                        self._registered_tool_names.append(local_name)

                    self._status[server.name]["connected"] = True
                    self._status[server.name]["tool_count"] = len(tools)
                    self._status[server.name]["tools"] = [
                        str(t.get("name") or "") for t in tools if t.get("name")
                    ]
                except Exception as e:
                    runtime.error = str(e)
                    self._status[server.name]["error"] = runtime.error
                    logger.warning(
                        "mcp_server_init_failed",
                        server=server.name,
                        command=server.command,
                        error=str(e),
                    )
                    with contextlib.suppress(Exception):
                        await client.close()

                self._servers[server.name] = runtime

            self._initialized = True
            self._reload_requested = False

    async def execute_tool(
        self,
        local_name: str,
        params: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        binding = self._bindings.get(local_name)
        if binding is None:
            return ToolResult.fail(f"Unknown MCP proxy tool: {local_name}")

        runtime = self._servers.get(binding.server_name)
        if runtime is None or runtime.client is None or not runtime.connected:
            return ToolResult.fail(
                f"MCP server '{binding.server_name}' is not connected"
            )

        try:
            result = await runtime.client.call_tool(binding.remote_name, params or {})
            output = _result_to_output(result)
            max_size = int(getattr(context, "max_output_size", 0) or 0)
            if max_size > 0 and len(output.encode("utf-8")) > max_size:
                encoded = output.encode("utf-8")[:max_size]
                output = encoded.decode("utf-8", errors="ignore")
                output += "\n\n[truncated: MCP output exceeded max_output_size]"
            return ToolResult.ok(output)
        except Exception as e:
            return ToolResult.fail(f"MCP tool call failed ({binding.remote_name}): {e}")

    async def refresh_now(self) -> None:
        """Force reconnect/reload with current config."""
        self._reload_requested = True
        if self._registry is not None:
            await self.ensure_initialized(self._registry)

    async def shutdown(self) -> None:
        """Shutdown all MCP client processes and unregister proxy tools."""
        registry = self._registry
        if registry is not None:
            await self._teardown(registry)
        else:
            await self._teardown(None)
        self._initialized = False
        self._reload_requested = True

    def list_status(self) -> list[dict[str, Any]]:
        rows = list(self._status.values())
        rows.sort(key=lambda x: str(x.get("name", "")))
        return rows

    def _build_local_tool_name(
        self,
        server: MCPServerConfig,
        remote_name: str,
        existing_names: set[str],
    ) -> str:
        prefix = str(server.tool_prefix or "").strip()
        if not prefix:
            prefix = f"mcp_{_safe_slug(server.name)}_"
        candidate = f"{prefix}{remote_name}"
        if candidate not in existing_names:
            return candidate
        idx = 2
        while f"{candidate}_{idx}" in existing_names:
            idx += 1
        return f"{candidate}_{idx}"

    async def _teardown(self, registry: ToolRegistry | None) -> None:
        if registry is not None:
            for name in list(self._registered_tool_names):
                registry.unregister(name)
        self._registered_tool_names.clear()
        self._bindings.clear()

        for runtime in list(self._servers.values()):
            if runtime.client is not None:
                with contextlib.suppress(Exception):
                    await runtime.client.close()
        self._servers.clear()


async def discover_mcp_tools(
    server_cfg: MCPServerConfig,
    *,
    egress_broker: Any | None = None,
) -> list[dict[str, Any]]:
    """One-shot MCP tool discovery for UI testing/debug."""
    client = _StdioMCPClient(server_cfg, egress_broker=egress_broker)
    try:
        await client.start()
        return await client.list_tools()
    finally:
        with contextlib.suppress(Exception):
            await client.close()
