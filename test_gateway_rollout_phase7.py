"""Phase 7 baseline tests: gateway gradual rollout controls."""

from __future__ import annotations

from src.config import EgressPolicyConfig
from src.core.security.egress import EgressBroker


def test_gateway_rollout_zero_percent_skips_routing() -> None:
    broker = EgressBroker(
        EgressPolicyConfig(
            gateway_enabled=True,
            gateway_mode="enforce",
            gateway_proxy_url="http://127.0.0.1:8080",
            gateway_rollout_percent=0,
        )
    )
    proxy = broker.resolve_proxy("https://example.com/path", tool_name="web_browse")
    assert proxy is None
    logs = broker.get_recent_gateway_logs(1)
    assert logs
    assert logs[0].get("reason") in {"rollout_not_selected", "missing_proxy_url"}


def test_gateway_rollout_hundred_percent_routes_all() -> None:
    broker = EgressBroker(
        EgressPolicyConfig(
            gateway_enabled=True,
            gateway_mode="enforce",
            gateway_proxy_url="http://127.0.0.1:8080",
            gateway_rollout_percent=100,
        )
    )
    proxy = broker.resolve_proxy("https://example.com/path", tool_name="web_browse")
    assert proxy == "http://127.0.0.1:8080"
    logs = broker.get_recent_gateway_logs(1)
    assert logs
    assert logs[0].get("route") == "gateway"


def test_gateway_rollout_is_deterministic_for_same_input() -> None:
    cfg = EgressPolicyConfig(
        gateway_enabled=True,
        gateway_mode="enforce",
        gateway_proxy_url="http://127.0.0.1:8080",
        gateway_rollout_percent=37,
        gateway_rollout_seed="seed-1",
    )
    broker = EgressBroker(cfg)
    first = broker.resolve_proxy("https://example.org/resource", tool_name="web_crawl_batch")
    second = broker.resolve_proxy("https://example.org/resource", tool_name="web_crawl_batch")
    assert first == second
