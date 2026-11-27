"""chat.completions protocol helpers for OpenAI-compatible adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from openai import APIError

from app.ai.adapters.openai_compatible.support.client_options import (
    with_client_retry_override,
)
from app.ai.adapters.openai_compatible.support.protocol_chat_completions_helpers import (
    build_chat_completion_error,
    convert_chat_chunk,
    convert_chat_response,
)
from app.ai.exceptions import ProviderError
from app.ai.types import ChatChunk, ChatMessage, ChatResponse
from app.core.i18n import _
from app.core.logging import LogManager

logger = LogManager.get_logger("ai")


class ChatCompletionsAdapterProtocol(Protocol):
    client: Any

    def _log_upstream_request(
        self,
        *,
        endpoint_path: str,
        model: str,
        stream: bool,
        wire_api: str | None = None,
    ) -> None: ...

    def _extract_usage_tokens(
        self,
        usage: Any,
    ) -> tuple[int | None, int | None, int | None]: ...

    async def _next_stream_event_with_timeout(
        self,
        stream: Any,
        *,
        timeout_seconds: float | None,
        model: str,
        wire_api: str,
    ) -> Any: ...


async def execute_chat_via_chat_completions(
    *,
    adapter: ChatCompletionsAdapterProtocol,
    request_params: dict[str, Any],
    messages: list[ChatMessage],
    model: str,
) -> ChatResponse:
    request_params = dict(request_params)
    client_max_retries = request_params.pop("_client_max_retries", None)
    effective_model = str(request_params.get("model") or model)
    adapter._log_upstream_request(
        endpoint_path="chat/completions",
        model=effective_model,
        stream=False,
        wire_api="chat_completions",
    )
    logger.info("Chat request: model={} messages={}", effective_model, len(messages))

    client = with_client_retry_override(
        adapter.client,
        max_retries=client_max_retries,
    )
    raw_response = await client.chat.completions.with_raw_response.create(
        **request_params
    )
    response = raw_response.parse()

    if isinstance(response, str):
        logger.error(
            "Chat response returned invalid string payload: model={} status_code={}",
            model,
            raw_response.status_code,
        )
        raise ProviderError(
            _("ai.request_failed"),
            provider_code="openai",
            model_code=model,
            upstream_status_code=raw_response.status_code,
        )

    return convert_chat_response(
        adapter=adapter,
        response=response,
        model=model,
        upstream_status_code=raw_response.status_code,
    )


async def execute_stream_chat_via_chat_completions(
    *,
    adapter: ChatCompletionsAdapterProtocol,
    request_params: dict[str, Any],
    model: str,
    aclose_stream: Callable[[Any], Awaitable[None]],
    normalize_timeout: Callable[[Any], float | None],
) -> AsyncIterator[ChatChunk]:
    request_params = dict(request_params)
    client_max_retries = request_params.pop("_client_max_retries", None)
    effective_model = str(request_params.get("model") or model)
    adapter._log_upstream_request(
        endpoint_path="chat/completions",
        model=effective_model,
        stream=True,
        wire_api="chat_completions",
    )
    logger.info("Stream chat request: model={}", effective_model)

    client = with_client_retry_override(
        adapter.client,
        max_retries=client_max_retries,
    )
    stream = await client.chat.completions.create(**request_params)
    stream_iter = aiter(stream)
    stream_timeout_seconds = normalize_timeout(request_params.get("timeout"))
    upstream_status_code = getattr(
        getattr(stream, "response", None), "status_code", 200
    )

    try:
        while True:
            try:
                chunk = await adapter._next_stream_event_with_timeout(
                    stream_iter,
                    timeout_seconds=stream_timeout_seconds,
                    model=effective_model,
                    wire_api="chat_completions",
                )
            except StopAsyncIteration:
                return

            chat_chunk = convert_chat_chunk(
                adapter=adapter,
                chunk=chunk,
                model=model,
                upstream_status_code=upstream_status_code,
            )
            yield chat_chunk
            if chat_chunk.finish_reason is not None:
                logger.info(
                    "Stream finish_reason received, closing upstream: model={} finish_reason={} wire_api=chat_completions",
                    model,
                    chat_chunk.finish_reason,
                )
                return
    except APIError as exc:
        # The SDK raises APIError with the raw body for an SSE error envelope.
        if type(exc) is not APIError or exc.body is None:
            raise
        error = build_chat_completion_error(
            exc.body, model=model, upstream_status_code=upstream_status_code
        )
    finally:
        await aclose_stream(stream)

    # Raise outside the SDK exception handler so the provider body cannot leak via chaining.
    raise error
