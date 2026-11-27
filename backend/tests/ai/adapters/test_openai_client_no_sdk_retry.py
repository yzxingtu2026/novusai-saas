"""Test types: structural and behavioral. Only HTTP transport is replaced."""

from __future__ import annotations

import httpx
import pytest

from app.ai.adapters.openai_compatible.client_factory import build_openai_client
from app.ai.adapters.openai_compatible.pinned_transport import (
    PinnedProviderAsyncTransport,
)
from app.ai.provider_url_policy import ProviderUrlPolicyError


@pytest.mark.structural
async def test_default_openai_client_sets_explicit_endpoint_and_network_options(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment.example.invalid/v1")
    async with build_openai_client(api_key="fixture-key", base_url=None) as client:
        assert client.base_url == httpx.URL("https://api.openai.com/v1/")
        assert client.max_retries == 0
        assert client._client.trust_env is False
        assert client._client.follow_redirects is False


@pytest.mark.behavioral
@pytest.mark.parametrize("allow_private_network", [False, True])
async def test_custom_provider_uses_approved_connection_target(
    monkeypatch, allow_private_network: bool
) -> None:
    captured = []

    def receive(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    async with build_openai_client(
        api_key="fixture-key",
        base_url="http://127.0.0.1:8180/v1",
        provider_config={"allow_private_network": allow_private_network},
    ) as client:
        transport = client._client._transport
        assert isinstance(transport, PinnedProviderAsyncTransport)
        await transport._transport.aclose()
        monkeypatch.setattr(transport, "_transport", httpx.MockTransport(receive))
        url = client.base_url.join("models?limit=1")
        if allow_private_network:
            response = await client._client.get(url)
            assert response.status_code == 204
            (request,) = captured
            assert request.url == url
            assert request.headers["host"] == "127.0.0.1:8180"
            assert request.extensions["sni_hostname"] == "127.0.0.1"
        else:
            with pytest.raises(ProviderUrlPolicyError) as raised:
                await client._client.get(url)
            assert raised.value.reason == "private_network_not_allowed"
            assert captured == []
