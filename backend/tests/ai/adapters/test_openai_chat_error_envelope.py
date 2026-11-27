"""Behavioral protocol test with a synthetic HTTP response and the real SDK."""

from __future__ import annotations

import httpx
import pytest

from app.ai.adapters.openai_adapter import OpenAIAdapter
from app.ai.exceptions import ProviderError, is_retryable
from app.core.i18n import _

pytestmark = pytest.mark.behavioral


@pytest.mark.parametrize(
    "envelope", [False, True], ids=("empty-result", "provider-error")
)
async def test_chat_completions_distinguishes_http_200_error_from_empty_result(
    envelope: bool,
) -> None:
    payload = {"object": "chat.completion", "choices": []}
    if envelope:
        payload["error"] = {"message": "synthetic provider diagnostic"}
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    adapter = OpenAIAdapter(
        api_key="fixture-key", base_url="https://provider.fixture.invalid/v1/"
    )
    original = adapter.client
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(receive),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            adapter.client = original.with_options(http_client=client)
            request_kwargs = {
                "wire_api": "chat_completions",
                "messages": [],
                "model": "fixture-model",
            }
            if envelope:
                with pytest.raises(ProviderError) as raised:
                    await adapter.execute_protocol_chat(**request_kwargs)
                error = raised.value
                assert (error.status_code, error.error_code) == (
                    200,
                    "provider_error_envelope",
                )
                assert (error.provider_code, error.model_code) == (
                    "openai",
                    "fixture-model",
                )
                assert error.message == _("ai.request_failed")
                assert error.original_error is None
                assert not is_retryable(error)
                assert "synthetic provider diagnostic" not in repr(error.to_dict())
            else:
                response = await adapter.execute_protocol_chat(**request_kwargs)
                assert (response.message.content, response.finish_reason) == (
                    "",
                    "stop",
                )
                assert response.metadata["protocol_path"] == "chat_completions"
    finally:
        await original.close()

    (request,) = requests
    assert request.method == "POST"
    assert request.url == httpx.URL(
        "https://provider.fixture.invalid/v1/chat/completions"
    )
