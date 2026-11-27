"""Address-pinned HTTP transport for operator-configured AI providers."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from app.ai.provider_url_policy import resolve_provider_url


class PinnedProviderAsyncTransport(httpx.AsyncBaseTransport):
    """Resolve every request and connect to the validated address.

    The URL host is replaced only for the socket connection.  The original
    host is retained in ``Host`` and ``sni_hostname`` so virtual-hosted HTTPS
    providers continue to work while DNS rebinding cannot redirect a request.
    """

    def __init__(
        self,
        *,
        allow_private_network: bool = False,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._allow_private_network = allow_private_network
        self._timeout_seconds = timeout_seconds
        self._transport = httpx.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = await _resolve_async(
            request.url,
            self._allow_private_network,
            self._timeout_seconds,
        )
        headers = request.headers.copy()
        headers["Host"] = target.host_header
        parsed_target = urlsplit(target.request_url)
        pinned = request.url.copy_with(
            host=parsed_target.hostname,
            port=parsed_target.port,
        )
        request = httpx.Request(
            request.method,
            pinned,
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": target.sni_hostname},
        )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


async def _resolve_async(
    url: httpx.URL,
    allow_private_network: bool,
    timeout_seconds: float,
):
    """Call the synchronous resolver without blocking the event loop."""
    import asyncio

    # Provider policy validates an origin, while the request URL may contain
    # query parameters that are valid for the API but must not be interpreted
    # as part of base-URL validation.
    origin = url.copy_with(path="/", query=None, fragment=None)
    return await asyncio.to_thread(
        resolve_provider_url,
        str(origin),
        allow_private_network=allow_private_network,
        timeout_seconds=timeout_seconds,
    )


__all__ = ["PinnedProviderAsyncTransport"]
