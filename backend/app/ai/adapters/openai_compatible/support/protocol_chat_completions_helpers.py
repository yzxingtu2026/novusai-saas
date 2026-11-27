"""Helper utilities for chat.completions protocol handling."""

from __future__ import annotations

from typing import Any, Protocol

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from app.ai.exceptions import (
    AIGatewayError,
    ModelNotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.ai.types import ChatChunk, ChatMessage, ChatResponse
from app.core.i18n import _

_ERROR_STATUSES = {
    "invalid_api_key": 401,
    "authentication_error": 401,
    "auth_error": 401,
    "unauthorized": 401,
    "permission_error": 403,
    "permission_denied": 403,
    "model_not_found": 404,
    "request_timeout": 408,
    "rate_limit": 429,
    "rate_limit_error": 429,
    "rate_limit_exceeded": 429,
    "insufficient_quota": 429,
    "invalid_request": 400,
    "invalid_request_error": 400,
    "invalid_parameter": 400,
    "invalid_value": 400,
    "service_unavailable": 503,
    "server_error": 500,
    "internal_server_error": 500,
    "gateway_timeout": 504,
}


class _UsageExtractor(Protocol):
    def _extract_usage_tokens(
        self,
        usage: Any,
    ) -> tuple[int | None, int | None, int | None]: ...


def _chat_error_body(payload: Any) -> object:
    if isinstance(payload, dict):
        return payload.get("error")
    return getattr(payload, "error", None)


def payload_looks_like_api_error(payload: Any) -> bool:
    """True when upstream returned an error object, not a misrouted success body."""
    return _chat_error_body(payload) is not None


def _chat_error_status(error: object) -> int | None:
    if not isinstance(error, dict):
        return None
    for field in ("status_code", "status", "http_status"):
        status = error.get(field)
        if (
            isinstance(status, str)
            and len(status) == 3
            and status.isascii()
            and status.isdecimal()
        ):
            status = int(status)
        if isinstance(status, int) and 400 <= status < 600:
            return status
    for field in ("code", "type"):
        tag = error.get(field)
        if isinstance(tag, str) and tag in _ERROR_STATUSES:
            return _ERROR_STATUSES[tag]
    return None


def build_chat_completion_error(
    error: object, *, model: str, upstream_status_code: int
) -> AIGatewayError:
    """Build a safe failure using recognized fields from a successful HTTP response."""
    status = _chat_error_status(error)
    fields = {
        "provider_code": "openai",
        "model_code": model,
        "status_code": status or upstream_status_code,
        "upstream_status_code": upstream_status_code,
    }
    if status == 429:
        return ProviderRateLimitError(_("ai.error.provider_rate_limit"), **fields)
    if status in {401, 403}:
        return ProviderAuthError(
            _("ai.error.provider_auth"),
            error_code="permission_denied" if status == 403 else None,
            **fields,
        )
    if status == 404:
        return ModelNotFoundError(_("ai.error.model_not_found"), **fields)
    if status in {408, 504}:
        return ProviderTimeoutError(_("ai.error.provider_timeout"), **fields)
    if status is not None and 500 <= status < 600:
        return ProviderError(
            _("ai.error.provider_server_error"), error_code=str(status), **fields
        )
    return ProviderError(
        _("ai.request_failed"),
        error_code=str(status) if status is not None else "provider_error_envelope",
        **fields,
    )


def convert_chat_response(
    *,
    adapter: _UsageExtractor,
    response: ChatCompletion,
    model: str,
    upstream_status_code: int = 200,
) -> ChatResponse:
    """Convert chat.completions sync payload into the shared ChatResponse."""
    error = _chat_error_body(response)
    if error is not None:
        raise build_chat_completion_error(
            error, model=model, upstream_status_code=upstream_status_code
        )
    if not response.choices:
        return ChatResponse(
            message=ChatMessage(role="assistant", content=""),
            model=model,
            finish_reason="stop",
            metadata={"protocol_path": "chat_completions"},
        )

    choice = response.choices[0]
    message = choice.message

    tool_calls_dicts: list[dict[str, Any]] | None = None
    if message.tool_calls:
        tool_calls_dicts = [
            {
                "id": tc.id,
                "type": tc.type,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in message.tool_calls
        ]

    chat_message = ChatMessage(
        role=message.role,
        content=message.content or "",
        tool_calls=tool_calls_dicts,
    )
    input_tokens, output_tokens, total_tokens = adapter._extract_usage_tokens(
        response.usage,
    )

    return ChatResponse(
        message=chat_message,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        model=model,
        finish_reason=choice.finish_reason,
        tool_calls=tool_calls_dicts,
        metadata={"protocol_path": "chat_completions"},
        raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
    )


def convert_chat_chunk(
    *,
    adapter: _UsageExtractor,
    chunk: ChatCompletionChunk,
    model: str,
    upstream_status_code: int = 200,
) -> ChatChunk:
    """Convert chat.completions stream chunk into the shared ChatChunk."""
    error = _chat_error_body(chunk)
    if error is not None:
        raise build_chat_completion_error(
            error, model=model, upstream_status_code=upstream_status_code
        )
    if not chunk.choices:
        return ChatChunk(delta="")

    choice = chunk.choices[0]
    delta = choice.delta
    delta_content = delta.content or ""
    reasoning_delta = getattr(delta, "reasoning_content", None) or ""
    input_tokens, output_tokens, total_tokens = adapter._extract_usage_tokens(
        chunk.usage,
    )

    tool_calls_dicts: list[dict[str, Any]] | None = None
    if delta.tool_calls:
        tool_calls_dicts = []
        for tc in delta.tool_calls:
            func = getattr(tc, "function", None)
            tool_calls_dicts.append(
                {
                    "index": getattr(tc, "index", None),
                    "id": getattr(tc, "id", None) or "",
                    "type": getattr(tc, "type", None) or "function",
                    "function": {
                        "name": getattr(func, "name", None) or "",
                        "arguments": getattr(func, "arguments", None) or "",
                    },
                }
            )

    return ChatChunk(
        delta=delta_content,
        reasoning_delta=reasoning_delta,
        role=delta.role,
        finish_reason=choice.finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        tool_calls=tool_calls_dicts,
    )


__all__ = [
    "build_chat_completion_error",
    "convert_chat_chunk",
    "convert_chat_response",
    "payload_looks_like_api_error",
]
