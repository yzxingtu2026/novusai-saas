"""
动态 CORS 策略 / Dynamic CORS policy.

统一 HTTP 与 Socket.IO 的 Origin 判定规则。
Provides shared Origin checks for both HTTP and Socket.IO.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.domains import HttpOrigin, normalize_configured_host, parse_http_origin
from app.core.logging import get_logger
from app.middleware.tenant import (
    is_development_loopback,
    is_platform_host,
    parse_tenant_from_host,
)
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain

logger = get_logger(__name__)

SOCKET_ORIGIN_DECISION_KEY = "novusai.socketio.origin_decision"
_verified_custom_domain_hosts: set[str] = set()

DEFAULT_EXPOSE_HEADERS = "X-Trace-ID"
DEFAULT_ALLOW_METHODS = "DELETE,GET,OPTIONS,PATCH,POST,PUT"
DEFAULT_ALLOW_HEADERS = (
    "Accept-Language,Authorization,Cache-Control,Content-Type,Origin,Pragma,"
    "X-Language,X-Trace-ID"
)


def _is_explicit_origin_allowed(origin: HttpOrigin) -> bool:
    for item in settings.cors_origins_list:
        configured = parse_http_origin(item if "://" in item else f"https://{item}")
        if configured and configured.normalized == origin.normalized:
            return True
    return False


def _allows_deployed_origin(origin: HttpOrigin) -> bool:
    if origin.scheme == "https" and origin.port in (None, 443):
        return True
    return settings.APP_ENV.strip().lower() == "development" and (
        origin.host.endswith((".localhost", ".local"))
        or is_development_loopback(origin.host)
    )


def _is_static_origin_allowed(origin: HttpOrigin) -> bool:
    return (
        _is_explicit_origin_allowed(origin)
        or is_development_loopback(origin.host)
        or (is_platform_host(origin.host) and _allows_deployed_origin(origin))
    )


def remember_verified_custom_domain(host: str | None) -> None:
    normalized = normalize_configured_host(host or "")
    if normalized:
        _verified_custom_domain_hosts.add(normalized)


def forget_verified_custom_domain(host: str | None) -> None:
    normalized = normalize_configured_host(host or "")
    if normalized:
        _verified_custom_domain_hosts.discard(normalized)


def list_verified_custom_domains() -> set[str]:
    return set(_verified_custom_domain_hosts)


def is_origin_allowed_sync(origin: str | None, environ=None) -> bool:
    """配置与已验证自定义域快照 / Configuration and verified-domain snapshot."""
    if environ is not None:
        decision = environ.get("asgi.scope", {}).get(SOCKET_ORIGIN_DECISION_KEY)
        if decision is not None:
            return decision == (origin, True)
    parsed = parse_http_origin(origin or "")
    return bool(
        parsed
        and (
            _is_static_origin_allowed(parsed)
            or (
                _allows_deployed_origin(parsed)
                and parsed.host in _verified_custom_domain_hosts
            )
        )
    )


def _verified_active_custom_domains():
    return (
        select(TenantDomain.domain)
        .join(Tenant, Tenant.id == TenantDomain.tenant_id)
        .where(
            TenantDomain.is_verified.is_(True),
            TenantDomain.is_deleted.is_(False),
            Tenant.is_active.is_(True),
            Tenant.is_deleted.is_(False),
        )
    )


async def is_origin_allowed(origin: str | None) -> bool:
    """未显式配置的浏览器来源按活跃租户核对 / Check active tenants for implicit browser origins."""
    parsed = parse_http_origin(origin or "")
    if parsed is None:
        return False
    if _is_static_origin_allowed(parsed):
        return True
    if not _allows_deployed_origin(parsed):
        return False

    host = parsed.host
    authority = f"[{host}]" if ":" in host else host
    code, domain_type = parse_tenant_from_host(authority)
    try:
        async with async_session_factory() as db:
            if domain_type == "subdomain":
                result = await db.execute(
                    select(Tenant.id).where(
                        Tenant.code == code,
                        Tenant.is_active.is_(True),
                        Tenant.is_deleted.is_(False),
                    )
                )
                return result.scalar_one_or_none() is not None
            if domain_type == "custom":
                result = await db.execute(
                    _verified_active_custom_domains().where(
                        func.lower(func.rtrim(TenantDomain.domain, ".")) == host
                    )
                )
                domain = result.scalar_one_or_none()
                if domain is not None:
                    remember_verified_custom_domain(domain)
                    return True
    except Exception as exc:
        logger.debug("CORS domain check unavailable: {}", type(exc).__name__)

    forget_verified_custom_domain(host)
    return False


async def refresh_verified_custom_domain_cache() -> None:
    """刷新已验证自定义域名缓存 / Refresh verified custom-domain cache."""
    try:
        async with async_session_factory() as db:
            result = await db.execute(_verified_active_custom_domains())
            domains = {
                host
                for domain in result.scalars().all()
                if (host := normalize_configured_host(domain))
            }
    except Exception as exc:
        logger.warning("Failed to refresh verified custom domain cache: {}", str(exc))
        return

    _verified_custom_domain_hosts.clear()
    _verified_custom_domain_hosts.update(domains)


def build_cors_headers(
    *,
    origin: str,
    allow_headers: str | None = None,
    preflight: bool = False,
) -> dict[str, str]:
    """
    构建 CORS 头 / Build CORS headers.
    """
    headers = {
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Expose-Headers": DEFAULT_EXPOSE_HEADERS,
        "Vary": "Origin",
    }
    if preflight:
        headers.update(
            {
                "Access-Control-Allow-Headers": allow_headers or DEFAULT_ALLOW_HEADERS,
                "Access-Control-Allow-Methods": DEFAULT_ALLOW_METHODS,
            }
        )
    return headers


async def get_cors_headers_for_origin(
    origin: str | None,
    *,
    allow_headers: str | None = None,
    preflight: bool = False,
) -> dict[str, str]:
    """
    获取允许 Origin 的 CORS 头 / Get CORS headers for an allowed origin.
    """
    if not origin or not await is_origin_allowed(origin):
        return {}
    return build_cors_headers(
        origin=origin,
        allow_headers=allow_headers,
        preflight=preflight,
    )


__all__ = [
    "DEFAULT_ALLOW_HEADERS",
    "DEFAULT_ALLOW_METHODS",
    "DEFAULT_EXPOSE_HEADERS",
    "build_cors_headers",
    "forget_verified_custom_domain",
    "get_cors_headers_for_origin",
    "is_origin_allowed",
    "is_origin_allowed_sync",
    "list_verified_custom_domains",
    "refresh_verified_custom_domain_cache",
    "remember_verified_custom_domain",
]
