"""Client factories for OpenAI-compatible adapters."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from app.ai.adapters.openai_compatible.pinned_transport import (
    PinnedProviderAsyncTransport,
)
from app.ai.provider_url_policy import provider_config_allows_private_network

_DEFAULT_OPENAI_MAX_RETRIES = 0
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def build_openai_client(
    *,
    api_key: str,
    base_url: str | None,
    provider_config: dict[str, Any] | None = None,
) -> AsyncOpenAI:
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url or _DEFAULT_OPENAI_BASE_URL,
        "max_retries": _DEFAULT_OPENAI_MAX_RETRIES,
        # Provider credentials must never inherit an operator's proxy settings,
        # and an upstream redirect must not silently select a new destination.
        "http_client": DefaultAsyncHttpxClient(
            trust_env=False,
            follow_redirects=False,
            transport=(
                PinnedProviderAsyncTransport(
                    allow_private_network=provider_config_allows_private_network(
                        provider_config
                    )
                )
                if base_url
                else None
            ),
        ),
    }
    return AsyncOpenAI(**kwargs)
