"""Web UI port fallback resolution tests."""

from __future__ import annotations

from src.ui.web_server import resolve_web_bind_port


def test_resolve_web_bind_port_returns_requested_when_available() -> None:
    selected = resolve_web_bind_port(
        "127.0.0.1",
        7860,
        auto_fallback_port=True,
        search_limit=20,
        availability_fn=lambda _host, port: port == 7860,
    )
    assert selected == 7860


def test_resolve_web_bind_port_falls_back_to_next_available() -> None:
    selected = resolve_web_bind_port(
        "127.0.0.1",
        7860,
        auto_fallback_port=True,
        search_limit=5,
        availability_fn=lambda _host, port: port == 7862,
    )
    assert selected == 7862


def test_resolve_web_bind_port_respects_disabled_fallback() -> None:
    selected = resolve_web_bind_port(
        "127.0.0.1",
        7860,
        auto_fallback_port=False,
        search_limit=5,
        availability_fn=lambda _host, _port: False,
    )
    assert selected == 7860

