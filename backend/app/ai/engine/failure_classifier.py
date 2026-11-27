"""Failure classification for orchestration diagnostics."""

from __future__ import annotations

from typing import Any

from app.ai.exceptions import (
    AIGatewayError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.ai.tools.types import ToolResult

from .types import ProviderFailureKind


class FailureClassifier:
    @staticmethod
    def classify_exception(
        exc: BaseException,
    ) -> tuple[ProviderFailureKind, dict[str, Any]]:
        if isinstance(exc, AIGatewayError):
            status_code = exc.upstream_status_code or exc.status_code
            details: dict[str, Any] = {"error": str(exc)}
            if exc.upstream_status_code is not None:
                details["status_code"] = status_code
                if status_code != exc.status_code:
                    details["effective_status_code"] = exc.status_code

            kind: ProviderFailureKind
            if isinstance(exc, ProviderTimeoutError):
                kind = "provider_timeout"
            elif isinstance(exc, ProviderRateLimitError):
                kind = "provider_rate_limit"
            elif isinstance(exc, ProviderConnectionError):
                kind = "provider_unavailable"
            else:
                kind = (
                    "provider_http_5xx"
                    if 500 <= status_code < 600
                    else "provider_bad_response"
                )
                details["status_code"] = status_code
            return kind, {"kind": kind, **details}
        if exc.__class__.__name__ in {"CancelledError", "GeneratorExit"}:
            return "server_interrupt", {"kind": "server_interrupt", "error": str(exc)}
        return "none", {}

    @staticmethod
    def classify_tool_results(
        tool_results: list[ToolResult],
    ) -> tuple[ProviderFailureKind, list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        kind: ProviderFailureKind = "none"
        for result in tool_results:
            if result.success:
                continue
            events.append(
                {
                    "tool_name": result.name,
                    "error_type": result.error_type or "tool_execution_error",
                    "error": result.error,
                }
            )
            if result.error_type == "timeout":
                kind = "tool_timeout"
            elif kind == "none":
                kind = "tool_execution_error"
        return kind, events


__all__ = ["FailureClassifier"]
