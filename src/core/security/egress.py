"""Central outbound network policy checks for tools and plugins."""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class EgressDecision:
    """Result of an outbound URL policy check."""

    allowed: bool
    reason: str


def _normalize_domain_rule(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or raw
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    return raw.strip(".")


def _is_private_host(host: str) -> bool:
    h = str(host or "").strip().lower().strip("[]")
    if not h:
        return False
    if h in {"localhost", "localhost.localdomain"}:
        return True
    if h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def _host_matches_rule(host: str, rule: str) -> bool:
    h = str(host or "").strip().lower()
    r = _normalize_domain_rule(rule)
    if not h or not r:
        return False

    if "*" in r or "?" in r:
        return fnmatch.fnmatch(h, r)

    if h == r:
        return True
    return h.endswith(f".{r}")


class EgressBroker:
    """Evaluate outbound URLs against global and tool-specific policy."""

    _GLOBAL_GATEWAY_LOGS: deque[dict[str, Any]] = deque(maxlen=2000)
    _GLOBAL_LOCK = threading.Lock()
    _GATEWAY_AUDIT_CALLBACK: Any = None

    def __init__(self, config: Any | None = None) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._config, "enabled", True))

    @property
    def max_response_bytes(self) -> int:
        try:
            return max(0, int(getattr(self._config, "max_response_bytes", 0) or 0))
        except Exception:
            return 0

    @property
    def gateway_enabled(self) -> bool:
        return bool(getattr(self._config, "gateway_enabled", False))

    @property
    def gateway_mode(self) -> str:
        mode = str(getattr(self._config, "gateway_mode", "enforce") or "enforce")
        mode = mode.strip().lower()
        return mode if mode in {"enforce", "shadow"} else "enforce"

    @property
    def gateway_proxy_url(self) -> str:
        return str(getattr(self._config, "gateway_proxy_url", "") or "").strip()

    @property
    def gateway_bypass_domains(self) -> list[str]:
        return self._rule_list("gateway_bypass_domains")

    @property
    def gateway_include_private_network(self) -> bool:
        return bool(getattr(self._config, "gateway_include_private_network", False))

    @property
    def gateway_rollout_percent(self) -> int:
        try:
            return max(0, min(100, int(getattr(self._config, "gateway_rollout_percent", 100) or 0)))
        except Exception:
            return 100

    @property
    def gateway_rollout_seed(self) -> str:
        return str(getattr(self._config, "gateway_rollout_seed", "kuro-gateway-rollout") or "kuro-gateway-rollout")

    @staticmethod
    def _normalize_rules(values: list[str] | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values or []:
            rule = _normalize_domain_rule(str(item or ""))
            if not rule or rule in seen:
                continue
            seen.add(rule)
            out.append(rule)
        return out

    def _rule_list(self, attr_name: str) -> list[str]:
        raw = getattr(self._config, attr_name, []) if self._config is not None else []
        if not isinstance(raw, list):
            return []
        return self._normalize_rules([str(v) for v in raw])

    @staticmethod
    def _matches_any(host: str, rules: list[str]) -> bool:
        if not rules:
            return False
        return any(_host_matches_rule(host, rule) for rule in rules)

    def evaluate_url(
        self,
        url: str,
        *,
        tool_name: str = "",
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        allow_private_network: bool | None = None,
    ) -> EgressDecision:
        """Check whether a URL is allowed by outbound policy."""
        raw = str(url or "").strip()
        if not raw:
            return EgressDecision(False, "Empty URL")

        parsed = urlparse(raw)
        scheme = str(parsed.scheme or "").lower()
        host = str(parsed.hostname or "").lower().strip(".")
        scope = f" for {tool_name}" if tool_name else ""

        if scheme not in {"http", "https"}:
            return EgressDecision(False, f"Only http/https URLs are allowed{scope}")
        if not host:
            return EgressDecision(False, f"URL host is missing{scope}")

        if not self.enabled:
            return EgressDecision(True, "egress policy disabled")

        allow_http = bool(getattr(self._config, "allow_http", True))
        if scheme == "http" and not allow_http:
            return EgressDecision(False, f"Plain HTTP is disabled by policy{scope}")

        global_blocked = self._rule_list("blocked_domains")
        override_blocked = self._normalize_rules(blocked_domains)
        blocked = list(dict.fromkeys(global_blocked + override_blocked))
        if self._matches_any(host, blocked):
            return EgressDecision(False, f"Host '{host}' is blocked by egress policy{scope}")

        global_allowed = self._rule_list("allowed_domains")
        override_allowed = self._normalize_rules(allowed_domains)
        if global_allowed and not self._matches_any(host, global_allowed):
            return EgressDecision(False, f"Host '{host}' is outside global allowlist{scope}")
        if override_allowed and not self._matches_any(host, override_allowed):
            return EgressDecision(False, f"Host '{host}' is outside tool allowlist{scope}")

        default_action = str(getattr(self._config, "default_action", "allow") or "allow").lower()
        if default_action == "deny" and not global_allowed and not override_allowed:
            return EgressDecision(False, f"Default egress action is deny{scope}")

        private_allowed = (
            bool(allow_private_network)
            if allow_private_network is not None
            else bool(getattr(self._config, "allow_private_network", True))
        )
        if not private_allowed and _is_private_host(host):
            return EgressDecision(False, f"Private-network host '{host}' is blocked{scope}")

        return EgressDecision(True, "allowed")

    def check_url(
        self,
        url: str,
        *,
        tool_name: str = "",
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        allow_private_network: bool | None = None,
    ) -> tuple[bool, str]:
        """Tuple helper: (allowed, reason)."""
        decision = self.evaluate_url(
            url,
            tool_name=tool_name,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            allow_private_network=allow_private_network,
        )
        return decision.allowed, decision.reason

    async def read_limited_bytes(self, response: Any, *, max_bytes: int | None = None) -> bytes:
        """Read HTTP response body with a byte cap.

        Raises:
            ValueError: if body size exceeds policy limit.
        """
        limit = self.max_response_bytes if max_bytes is None else max(0, int(max_bytes))
        if limit <= 0:
            return await response.read()

        buf = bytearray()
        while True:
            chunk = await response.content.read(64 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > limit:
                raise ValueError(f"Response exceeded max_response_bytes={limit}")
        return bytes(buf)

    def resolve_proxy(self, url: str, *, tool_name: str = "") -> str | None:
        """Return gateway proxy URL when request should be routed via Lite Gateway.

        Rules:
        - Disabled when gateway_enabled is false or gateway_proxy_url is empty.
        - Respect gateway_bypass_domains.
        - Private-network hosts are bypassed unless gateway_include_private_network=true.
        """
        proxy_url = self.gateway_proxy_url
        mode = self.gateway_mode
        if not self.gateway_enabled:
            return None

        raw = str(url or "").strip()
        if not proxy_url:
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host="",
                route="direct",
                reason="missing_proxy_url",
            )
            return None

        if not raw:
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host="",
                route="direct",
                reason="invalid_url",
            )
            return None
        parsed = urlparse(raw)
        host = str(parsed.hostname or "").lower().strip(".")
        if not host:
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host="",
                route="direct",
                reason="missing_host",
            )
            return None

        bypass_rules = self.gateway_bypass_domains
        if self._matches_any(host, bypass_rules):
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host=host,
                route="direct",
                reason="bypass_domain",
                proxy_url=proxy_url,
            )
            return None

        if not self.gateway_include_private_network and _is_private_host(host):
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host=host,
                route="direct",
                reason="private_network_bypass",
                proxy_url=proxy_url,
            )
            return None

        if mode == "shadow":
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host=host,
                route="shadow",
                reason="shadow_mode_candidate",
                proxy_url=proxy_url,
            )
            return None

        if not self._rollout_selected(
            tool_name=tool_name,
            target_url=raw,
            host=host,
        ):
            self._record_gateway_decision(
                tool_name=tool_name,
                target_url=raw,
                host=host,
                route="direct",
                reason="rollout_not_selected",
                proxy_url=proxy_url,
            )
            return None

        self._record_gateway_decision(
            tool_name=tool_name,
            target_url=raw,
            host=host,
            route="gateway",
            reason="routed_via_gateway",
            proxy_url=proxy_url,
        )
        return proxy_url

    @staticmethod
    def _sanitize_target_url(url: str) -> str:
        """Return a safe, compact target URL for UI logs (without query/fragment)."""
        raw = str(url or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw)
        scheme = str(parsed.scheme or "").lower()
        host = str(parsed.hostname or "").strip()
        if not host:
            return raw[:220]
        port = f":{parsed.port}" if parsed.port else ""
        path = str(parsed.path or "/")
        compact = f"{scheme}://{host}{port}{path}"
        if len(compact) > 220:
            return compact[:217] + "..."
        return compact

    @staticmethod
    def _sanitize_proxy_url(proxy_url: str | None) -> str:
        """Mask proxy URL to avoid leaking credentials."""
        raw = str(proxy_url or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.hostname:
            return raw[:120]
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    def _rollout_selected(
        self,
        *,
        tool_name: str,
        target_url: str,
        host: str,
    ) -> bool:
        """Deterministic rollout bucket selection for enforce-mode routing."""
        percent = self.gateway_rollout_percent
        if percent >= 100:
            return True
        if percent <= 0:
            return False
        seed = self.gateway_rollout_seed
        key = (
            f"{seed}|{str(tool_name or '').strip().lower()}|"
            f"{str(host or '').strip().lower()}|{str(target_url or '').strip()}"
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 100
        return bucket < percent

    @classmethod
    def set_gateway_audit_callback(cls, callback: Any | None) -> None:
        """Set a callback invoked for each gateway routing decision entry."""
        cls._GATEWAY_AUDIT_CALLBACK = callback

    def _record_gateway_decision(
        self,
        *,
        tool_name: str,
        target_url: str,
        host: str,
        route: str,
        reason: str,
        proxy_url: str | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": str(tool_name or "").strip() or "unknown",
            "host": str(host or "").strip().lower(),
            "target": self._sanitize_target_url(target_url),
            "route": (
                "gateway"
                if str(route).strip().lower() == "gateway"
                else ("shadow" if str(route).strip().lower() == "shadow" else "direct")
            ),
            "reason": str(reason or "").strip().lower() or "unknown",
            "proxy": self._sanitize_proxy_url(proxy_url),
        }
        with self._GLOBAL_LOCK:
            self._GLOBAL_GATEWAY_LOGS.append(entry)
        cb = self._GATEWAY_AUDIT_CALLBACK
        if callable(cb):
            try:
                cb(dict(entry))
            except Exception:
                # Gateway routing must never fail because audit callback fails.
                pass

    def get_recent_gateway_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent gateway routing decisions (newest first)."""
        max_items = max(1, int(limit or 100))
        with self._GLOBAL_LOCK:
            data = list(self._GLOBAL_GATEWAY_LOGS)
        if not data:
            return []
        return data[-max_items:][::-1]
