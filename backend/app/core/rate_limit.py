"""
IP 级速率限制 / IP-based rate limiting.

Redis-backed login throttling shared by API workers and replicas.
登录限流由 API 进程和副本共享 Redis 计数。
"""

from __future__ import annotations

from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

from fastapi import Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.i18n import _
from app.core.redis import cache_increment
from app.core.response import build_error_payload
from app.exceptions import ServiceUnavailableException

_LOGIN_WINDOW = 60  # seconds / 窗口秒数
_LOGIN_MAX = 10  # requests per window / 窗口内最大请求数


def _canonical_ip(address: IPv4Address | IPv6Address) -> str:
    """生成稳定的客户端 IP / Use one identity for an address regardless of IPv6 scope."""
    return address.compressed.partition("%")[0]


def _is_trusted_proxy(
    address: IPv4Address | IPv6Address, configured: list[str]
) -> bool:
    for entry in configured:
        try:
            network = ip_network(entry)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def resolve_client_ip(
    peer: str,
    forwarded: str | None = None,
    real_ip: str | None = None,
) -> str | None:
    """仅信任明确配置的代理 / Accept forwarded client IPs from configured peers."""
    try:
        peer_address = ip_address(peer)
    except ValueError:
        # Uvicorn 可能已改写 ASGI 地址 / Uvicorn may have rewritten the ASGI address.
        return None
    peer_ip = _canonical_ip(peer_address)
    trusted = settings.trusted_proxy_ips_list
    if not _is_trusted_proxy(peer_address, trusted):
        return peer_ip

    if forwarded:
        # 从最近的代理向外遍历 / Walk from the nearest proxy outward.
        hops = forwarded.split(",")
        for hop in reversed(hops):
            try:
                address = ip_address(hop.strip())
            except ValueError:
                return peer_ip
            if not _is_trusted_proxy(address, trusted):
                return _canonical_ip(address)
        return _canonical_ip(ip_address(hops[0].strip()))
    if real_ip:
        try:
            return _canonical_ip(ip_address(real_ip.strip()))
        except ValueError:
            return peer_ip
    return peer_ip


def get_request_client_ip(request: Request) -> str | None:
    """登录和限流使用同一客户端 IP / Resolve the same client IP for login and limits."""
    if request.client is None:
        return None
    return resolve_client_ip(
        request.client.host,
        ",".join(request.headers.getlist("x-forwarded-for")),
        ",".join(request.headers.getlist("x-real-ip")),
    )


async def check_login_rate_limit(request: Request) -> JSONResponse | None:
    """每 IP 60 秒允许 10 次登录请求 / Allow ten requests per IP in a 60-second window."""
    client_ip = get_request_client_ip(request) or "unknown"
    digest = sha256(client_ip.encode("utf-8", errors="replace")).hexdigest()
    try:
        count = await cache_increment(f"auth:login_rate:{digest}", ttl=_LOGIN_WINDOW)
    except (RedisError, RuntimeError) as exc:
        raise ServiceUnavailableException() from exc

    if count > _LOGIN_MAX:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(_LOGIN_WINDOW)},
            content=build_error_payload(
                message=_("auth.rate_limited"),
                code=4290,
                extra={"retry_after": _LOGIN_WINDOW},
            ),
        )

    return None
