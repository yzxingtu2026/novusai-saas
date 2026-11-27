"""认证限流的客户端身份及共享计数 / Client identity and shared login counters.

Test type: behavioral.
"""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from hashlib import sha256
from ipaddress import IPv6Address
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core import rate_limit, redis
from app.core.config import Settings, settings
from app.exceptions import ServiceUnavailableException


def test_proxy_setting_normalizes_and_validates_entries() -> None:
    configured = Settings(
        _env_file=None,
        APP_ENV="development",
        DEBUG=False,
        TRUSTED_PROXY_IPS=" 10.42.0.8, 2001:db8:1::/48, ",
    )
    assert configured.TRUSTED_PROXY_IPS == "10.42.0.8,2001:db8:1::/48"
    assert configured.trusted_proxy_ips_list == ["10.42.0.8", "2001:db8:1::/48"]
    for invalid in ("*", "10.42.0.8/16"):
        with pytest.raises(ValidationError, match="TRUSTED_PROXY_IPS"):
            Settings(
                _env_file=None,
                APP_ENV="development",
                DEBUG=False,
                TRUSTED_PROXY_IPS=invalid,
            )


@pytest.mark.parametrize(
    ("trusted", "peer", "forwarded", "real_ip", "expected"),
    [
        ("", "127.0.0.1", "192.0.2.21", "192.0.2.22", "127.0.0.1"),
        ("", "2001:0DB8:0000::21%fixture", None, None, "2001:db8::21"),
        ("10.42.0.0/16", "invalid-address", "192.0.2.21", "192.0.2.22", None),
        ("10.42.0.0/16", "192.0.2.10", "192.0.2.21", "192.0.2.22", "192.0.2.10"),
        (
            "10.42.0.0/16, 2001:db8:1::/48",
            "10.42.0.8",
            "192.0.2.99, 192.0.2.21, 2001:db8:1::3",
            "192.0.2.22",
            "192.0.2.21",
        ),
        ("10.42.0.8", "10.42.0.8", None, "2001:db8:0000::1", "2001:db8::1"),
        (
            "10.42.0.0/16",
            "10.42.0.8",
            "192.0.2.21, invalid",
            "192.0.2.22",
            "10.42.0.8",
        ),
    ],
    ids=(
        "default-local-peer",
        "scoped-ipv6-peer",
        "invalid-peer",
        "untrusted-peer",
        "proxy-chain",
        "real-ip",
        "invalid-hop",
    ),
)
def test_client_ip_requires_explicit_trusted_proxy(
    monkeypatch, trusted, peer, forwarded, real_ip, expected
) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", trusted)
    assert rate_limit.resolve_client_ip(peer, forwarded, real_ip) == expected


def test_multiple_forwarded_lines_are_read_in_wire_order(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.42.0.0/16")
    request = Request(
        {
            "type": "http",
            "client": ("10.42.0.8", 43210),
            "headers": [
                (b"x-forwarded-for", b"198.51.100.56"),
                (b"x-forwarded-for", b"192.0.2.33, 10.42.0.11"),
            ],
        }
    )
    assert rate_limit.get_request_client_ip(request) == "192.0.2.33"


@pytest.mark.asyncio
async def test_uninitialized_redis_returns_service_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        rate_limit,
        "cache_increment",
        AsyncMock(side_effect=RuntimeError("Redis fixture")),
    )
    request = Request({"type": "http", "client": ("192.0.2.21", 43210), "headers": []})
    with pytest.raises(ServiceUnavailableException) as captured:
        await rate_limit.check_login_rate_limit(request)
    assert (captured.value.status_code, captured.value.code) == (503, 5030)


@pytest.mark.asyncio
async def test_login_counter_shares_a_fixed_window_across_redis_clients(
    monkeypatch,
) -> None:
    port = os.environ.get("NOVUSAI_TEST_REDIS_PORT")
    if port is None:
        pytest.skip("Set NOVUSAI_TEST_REDIS_PORT for disposable loopback Redis")

    worker_connection: ContextVar[Redis] = ContextVar("worker_connection")
    monkeypatch.setattr(redis, "get_redis_client", worker_connection.get)
    suffix = uuid4().hex[:8]
    peer = str(IPv6Address(f"2001:db8::{suffix[:4]}:{suffix[4:]}"))
    key = f"auth:login_rate:{sha256(peer.encode()).hexdigest()}"
    request = Request({"type": "http", "client": (peer, 43210), "headers": []})

    async def check(client: Redis):
        token = worker_connection.set(client)
        try:
            return await rate_limit.check_login_rate_limit(request)
        finally:
            worker_connection.reset(token)

    connection = {
        "host": "127.0.0.1",
        "port": int(port),
        "decode_responses": True,
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    }
    async with Redis(**connection) as first, Redis(**connection) as second:
        try:
            results = await asyncio.gather(
                *(check(client) for client in (first, second) for _ in range(6))
            )
            assert sum(result is None for result in results) == 10
            assert [result.status_code for result in results if result is not None] == [
                429,
                429,
            ]
            assert await second.get(key) == "12"
            assert 0 < await first.ttl(key) <= 60

            assert await first.expire(key, 15)
            limited = await check(second)
            assert limited.status_code == 429
            assert await first.get(key) == "13"
            assert 0 < await second.ttl(key) <= 15
        finally:
            await first.delete(key)
