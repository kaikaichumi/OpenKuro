"""LangSmith tracing integration for Kuro.

Provides observability for LLM calls, tool executions, and agent operations.
Uses LangSmith's ``@traceable`` decorator and ``RunTree`` for span management.

Setup:
  1. pip install langsmith  (or: poetry install -E tracing)
  2. Set environment variables:
       LANGCHAIN_TRACING_V2=true
       LANGCHAIN_API_KEY=lsv2_...
       LANGCHAIN_PROJECT=kuro
  3. Enable in config.yaml:
       tracing:
         enabled: true

What you get:
  - Every LLM call as a span with model name, token usage, latency
  - Tool executions as child spans with parameters and results
  - Agent delegations as nested traces
  - Full conversation flow visualization in LangSmith dashboard
"""

from __future__ import annotations

import os
import functools
import time
from typing import Any, Callable

import structlog

logger = structlog.get_logger()

# Lazy flag: None = not yet checked, True/False = resolved
_langsmith_available: bool | None = None
_initialized: bool = False


def _check_langsmith() -> bool:
    """Check if langsmith is installed and importable."""
    global _langsmith_available
    if _langsmith_available is None:
        try:
            import langsmith  # noqa: F401
            _langsmith_available = True
        except ImportError:
            _langsmith_available = False
    return _langsmith_available


def init_tracing(project_name: str = "kuro", tags: list[str] | None = None) -> bool:
    """Initialize LangSmith tracing.

    Sets the required environment variables and verifies connectivity.
    Returns True if tracing is active, False otherwise.
    """
    global _initialized

    if not _check_langsmith():
        logger.info("tracing_unavailable", hint="pip install langsmith")
        return False

    # Ensure env vars are set
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT", project_name)

    # Check API key
    api_key = os.environ.get("LANGCHAIN_API_KEY", "")
    if not api_key:
        logger.warning(
            "tracing_no_api_key",
            hint="Set LANGCHAIN_API_KEY environment variable",
        )
        return False

    _initialized = True
    logger.info(
        "tracing_initialized",
        project=project_name,
        tags=tags or [],
    )
    return True


def is_tracing_active() -> bool:
    """Return True if tracing is initialized and active."""
    return _initialized and _check_langsmith()


def traceable_llm(
    name: str = "llm_call",
    metadata: dict[str, Any] | None = None,
) -> Callable:
    """Decorator to trace an LLM call with LangSmith.

    Wraps an async function that makes LLM calls, recording:
      - Input messages (as inputs)
      - Model name, token usage, latency (as metadata)
      - Response content (as outputs)

    Falls back to a no-op wrapper if LangSmith is not available.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not is_tracing_active():
                return await func(*args, **kwargs)

            from langsmith import traceable

            # Build a traced version on-the-fly
            traced = traceable(
                name=name,
                run_type="llm",
                metadata=metadata or {},
            )(func)
            return await traced(*args, **kwargs)

        return wrapper
    return decorator


def traceable_tool(
    name: str = "tool_call",
    metadata: dict[str, Any] | None = None,
) -> Callable:
    """Decorator to trace a tool execution with LangSmith."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not is_tracing_active():
                return await func(*args, **kwargs)

            from langsmith import traceable

            traced = traceable(
                name=name,
                run_type="tool",
                metadata=metadata or {},
            )(func)
            return await traced(*args, **kwargs)

        return wrapper
    return decorator


def trace_llm_call(
    model: str,
    messages: list[dict[str, Any]],
    response: Any,
    usage: dict[str, int] | None = None,
    latency_ms: float | None = None,
    error: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Log a completed LLM call to LangSmith as a standalone run.

    This is the primary integration point — called after every
    litellm.acompletion() in ModelRouter.
    """
    if not is_tracing_active():
        return

    try:
        from langsmith import Client

        client = Client()

        inputs = {
            "messages": _sanitize_messages(messages),
            "model": model,
        }

        outputs: dict[str, Any] = {}
        if error:
            outputs["error"] = error
        elif response is not None:
            outputs["content"] = getattr(response, "content", str(response))
            if hasattr(response, "tool_calls") and response.tool_calls:
                outputs["tool_calls"] = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                    if hasattr(tc, "name")
                ]

        run_metadata: dict[str, Any] = {"model": model}
        if usage:
            run_metadata["token_usage"] = usage
        if latency_ms is not None:
            run_metadata["latency_ms"] = round(latency_ms, 1)

        client.create_run(
            name=f"llm:{model.split('/')[-1]}",
            run_type="llm",
            inputs=inputs,
            outputs=outputs,
            extra={"metadata": run_metadata},
            tags=tags or [],
            error=error,
        )
    except Exception as e:
        # Never let tracing errors break the main flow
        logger.debug("trace_llm_call_failed", error=str(e))


def trace_tool_call(
    tool_name: str,
    params: dict[str, Any],
    result_output: str | None = None,
    result_error: str | None = None,
    success: bool = True,
    latency_ms: float | None = None,
    tags: list[str] | None = None,
) -> None:
    """Log a completed tool execution to LangSmith."""
    if not is_tracing_active():
        return

    try:
        from langsmith import Client

        client = Client()

        inputs = {
            "tool_name": tool_name,
            "parameters": _sanitize_params(params),
        }

        outputs: dict[str, Any] = {"success": success}
        if result_output:
            # Truncate large outputs
            outputs["output"] = result_output[:2000]
        if result_error:
            outputs["error"] = result_error

        run_metadata: dict[str, Any] = {"tool": tool_name}
        if latency_ms is not None:
            run_metadata["latency_ms"] = round(latency_ms, 1)

        client.create_run(
            name=f"tool:{tool_name}",
            run_type="tool",
            inputs=inputs,
            outputs=outputs,
            extra={"metadata": run_metadata},
            tags=tags or [],
            error=result_error if not success else None,
        )
    except Exception as e:
        logger.debug("trace_tool_call_failed", error=str(e))


def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize messages for tracing — strip base64 images to save bandwidth."""
    sanitized = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, list):
            # Multimodal: replace image data with placeholder
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    parts.append({"type": "image_url", "image_url": {"url": "[base64_image]"}})
                else:
                    parts.append(part)
            m["content"] = parts
        elif isinstance(content, str) and len(content) > 5000:
            m["content"] = content[:5000] + f"... [truncated, {len(content)} chars total]"
        sanitized.append(m)
    return sanitized


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool parameters — truncate large values."""
    sanitized = {}
    for k, v in params.items():
        if isinstance(v, str) and len(v) > 2000:
            sanitized[k] = v[:2000] + f"... [truncated, {len(v)} chars]"
        else:
            sanitized[k] = v
    return sanitized
