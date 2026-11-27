"""Bounded outbound HTTP for sources and tenant webhooks."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Collection, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

_DEFAULT_RESPONSE_LIMIT = 8 * 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}


class EgressPolicyError(ValueError):
    """The destination or response did not meet the public HTTP policy."""


def _public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            raise EgressPolicyError("private_network_not_allowed")
        address = address.ipv4_mapped or address
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise EgressPolicyError("private_network_not_allowed")
    return address


def _parse_url(value: str, allowed_hosts: Collection[str] | None) -> httpx.URL:
    if not value or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        raise EgressPolicyError("invalid_url")
    if "\\" in value or "#" in value:
        raise EgressPolicyError("invalid_url")

    try:
        parsed = urlsplit(value)
        port = parsed.port
        url = httpx.URL(value)
    except (ValueError, httpx.InvalidURL) as exc:
        raise EgressPolicyError("invalid_url") from exc

    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise EgressPolicyError("invalid_url")

    host = url.raw_host.decode("ascii").lower().rstrip(".")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise EgressPolicyError("host_not_allowed")
    return url


async def _resolve_public_address(
    host: str,
    port: int,
    resolver: Callable[[str], Sequence[str]] | None,
) -> str:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    try:
        if literal is not None:
            answers = [str(literal)]
        elif resolver is not None:
            answers = await asyncio.to_thread(resolver, host)
        else:
            results = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
            answers = [result[4][0] for result in results]
        addresses = {_public_address(answer) for answer in answers}
    except EgressPolicyError:
        raise
    except (OSError, ValueError) as exc:
        raise EgressPolicyError("dns_resolution_failed") from exc

    if not addresses:
        raise EgressPolicyError("dns_resolution_failed")
    return str(min(addresses, key=lambda address: (address.version, int(address))))


async def request_public_http(
    method: str,
    url: str,
    *,
    resolver: Callable[[str], Sequence[str]] | None = None,
    allowed_hosts: Collection[str] | None = None,
    json: Any = None,
    timeout: float = 10.0,
    deadline_seconds: float = 30.0,
    max_response_bytes: int = _DEFAULT_RESPONSE_LIMIT,
) -> httpx.Response:
    """Resolve once, retain TLS host identity, and return a bounded response.

    Redirects remain the caller's responsibility. Each approved next hop must
    pass this policy before any connection is made.
    """
    original = _parse_url(url, allowed_hosts)
    host = original.raw_host.decode("ascii").lower().rstrip(".")
    port = original.port or (443 if original.scheme == "https" else 80)

    async def send() -> httpx.Response:
        address = await _resolve_public_address(host, port, resolver)
        pinned = original.copy_with(host=address)
        async with (
            httpx.AsyncClient(
                timeout=timeout, trust_env=False, follow_redirects=False
            ) as client,
            client.stream(
                method,
                pinned,
                json=json,
                headers={
                    "Host": original.netloc.decode("ascii"),
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": host},
            ) as response,
        ):
            body = bytearray()
            # GitHub validates the next location without reading its body.
            if response.status_code not in _REDIRECTS:
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_response_bytes:
                        raise EgressPolicyError("response_too_large")
                    body.extend(chunk)
            headers = httpx.Headers(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=httpx.Request(method, original),
            )

    try:
        return await asyncio.wait_for(send(), timeout=deadline_seconds)
    except asyncio.TimeoutError as exc:
        raise EgressPolicyError("deadline_exceeded") from exc
