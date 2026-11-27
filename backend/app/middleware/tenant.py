"""
Tenant Identification Middleware / 企业识别中间件

Resolves tenant info from request Host header, supports / 根据 Host 头解析企业信息，支持：
1. Subdomain mode / 子域名模式: {tenant_code}.app.novusai.com
2. Custom domain mode / 自定义域名模式: custom.domain.com -> tenant_domains table
"""

from sqlalchemy import func, select
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.datastructures import Headers, State
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.domains import (
    normalize_configured_host,
    normalize_host_header,
    normalize_tenant_suffix,
    parse_http_origin,
)
from app.core.i18n import _
from app.core.response import error
from app.models import Tenant, TenantDomain


class TenantContext:
    """
    Tenant Context.
    企业上下文。

    Stores tenant info resolved from request / 存储从请求中解析出的企业信息
    """

    def __init__(
        self,
        tenant_id: int | None = None,
        tenant_code: str | None = None,
        tenant: Tenant | None = None,
        domain_type: str = "unknown",  # subdomain, custom, unknown / 域名类型：子域名、自定义、未知
        origin_host: str | None = None,
    ):
        self.tenant_id = tenant_id
        self.tenant_code = tenant_code
        self.tenant = tenant
        self.domain_type = domain_type
        self.origin_host = origin_host

    @property
    def is_resolved(self) -> bool:
        """Whether tenant is resolved / 企业是否已解析"""
        return self.tenant is not None

    def __repr__(self) -> str:
        return f"<TenantContext(tenant_id={self.tenant_id}, code={self.tenant_code}, type={self.domain_type})>"


def is_development_loopback(host: str) -> bool:
    return settings.APP_ENV.strip().lower() == "development" and host in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def is_platform_host(host: str) -> bool:
    return bool(host) and host in {
        normalize_configured_host(item) for item in settings.platform_domains_list
    }


def parse_tenant_from_host(host: str) -> tuple[str | None, str]:
    """
    Parse tenant info from Host header / 从 Host 头解析企业信息

    Args:
        host: Request Host header / 请求的 Host 头, e.g. "abc.app.novusai.com" or "custom.com"

    Returns:
        tuple: (tenant_code, domain_type)
        - Subdomain / 子域名: (tenant_code, "subdomain")
        - Custom domain / 自定义域名: (None, "custom")
        - Unresolvable / 无法解析: (None, "unknown")
    """
    host = normalize_host_header(host)
    if not host or is_platform_host(host) or is_development_loopback(host):
        return None, "unknown"

    # Check if subdomain mode / 检查是否是子域名模式
    configured_suffix = settings.TENANT_DOMAIN_SUFFIX.strip()
    suffix = normalize_tenant_suffix(configured_suffix) if configured_suffix else ""
    if suffix and host.endswith(suffix):
        # Extract subdomain part / 提取子域名部分
        subdomain = host[: -len(suffix)]
        if subdomain and "." not in subdomain:
            # Valid tenant subdomain (no dots) / 合法的企业子域名
            return subdomain, "subdomain"

    # Development mode: support {tenant_code}.localhost pattern / 开发模式：支持 {tenant_code}.localhost 格式
    if (
        host.endswith(".localhost")
        and settings.APP_ENV.strip().lower() == "development"
    ):
        subdomain = host[: -len(".localhost")]
        if subdomain and "." not in subdomain:
            return subdomain, "subdomain"

    # Possibly custom domain / 可能是自定义域名
    # Exclude platform main domain / 排除平台主域名
    if host == suffix.lstrip("."):
        return None, "unknown"

    return None, "custom"


class TenantMiddleware:
    """
    Tenant Identification Middleware (pure ASGI implementation). / 企业识别中间件（纯 ASGI 实现）。

    Resolves tenant info in each request and stores to request.state.tenant_ctx.
    在每个请求中解析企业信息并存储到 request.state.tenant_ctx。
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    # Path prefixes that need tenant domain resolution / 需要执行企业域名解析的路径前缀
    # Note: No /api/v1/ routes in this project (API routes use /admin/, /tenant/, /api/user/, /api/public/)
    TENANT_PATHS = (
        "/tenant/",
        "/api/user/",
        "/api/public/tenant",
        "/api/public/captcha",
        "/api/public/plugins/",
        "/plugin-public-assets/tenant/",
        "/plugin-public-assets/user/",
        "/plugin-assets/",
    )

    @staticmethod
    def _needs_tenant_resolution(path: str) -> bool:
        """Check if request path needs tenant domain resolution / 判断请求路径是否需要企业域名解析"""
        return any(path.startswith(prefix) for prefix in TenantMiddleware.TENANT_PATHS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        tenant_ctx = TenantContext()
        if self._needs_tenant_resolution(scope.get("path", "")):
            headers = Headers(scope=scope)
            host_header = headers.get("host", "")
            host = normalize_host_header(host_header)
            origin_header = headers.get("origin")
            origin = parse_http_origin(origin_header) if origin_header else None
            if not host or (origin_header and origin is None):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = error(
                    message=_("common.bad_request"), code=4000, status_code=400
                )
                response.headers["cache-control"] = "no-store"
                await response(scope, receive, send)
                return
            tenant_ctx = await self.resolve_request_context(
                host, host_header, origin.host if origin else None
            )

        state = scope.setdefault("state", {})
        if isinstance(state, State):
            state.tenant_ctx = tenant_ctx
        else:
            state["tenant_ctx"] = tenant_ctx
        await self.app(scope, receive, send)

    @classmethod
    async def resolve_request_context(
        cls, host: str, host_header: str, origin_host: str | None
    ) -> TenantContext:
        tenant_code, domain_type = parse_tenant_from_host(host_header)
        context = TenantContext(tenant_code=tenant_code, domain_type=domain_type)
        remote_origin = (
            origin_host
            if origin_host
            and origin_host != host
            and not is_platform_host(origin_host)
            and not is_development_loopback(origin_host)
            else None
        )
        central_host = is_platform_host(host) or is_development_loopback(host)
        if domain_type == "unknown" and not (central_host and remote_origin):
            return context
        async with async_session_factory() as db:
            tenant = await cls._resolve_tenant(db, tenant_code, host, domain_type)
            if remote_origin and (tenant is not None or central_host):
                authority = (
                    f"[{remote_origin}]" if ":" in remote_origin else remote_origin
                )
                origin_code, origin_type = parse_tenant_from_host(authority)
                origin_tenant = await cls._resolve_tenant(
                    db,
                    origin_code,
                    remote_origin,
                    origin_type,
                )
                if not origin_tenant or (tenant and tenant.id != origin_tenant.id):
                    return context
                if tenant is None:
                    tenant = origin_tenant
                    context.domain_type = origin_type
            if tenant is not None:
                context.tenant = tenant
                context.tenant_id = tenant.id
                context.tenant_code = tenant.code
                context.origin_host = origin_host
        return context

    @staticmethod
    async def _resolve_tenant(
        db: AsyncSession,
        tenant_code: str | None,
        host: str,
        domain_type: str,
    ) -> Tenant | None:
        """
        Resolve tenant from database / 从数据库解析企业

        Args:
            db: Database session / 数据库会话
            tenant_code: Tenant code (subdomain mode) / 企业代码
            host: Original Host (custom domain mode) / 原始 Host
            domain_type: Domain type / 域名类型

        Returns:
            Tenant or None
        """
        if domain_type == "subdomain" and tenant_code:
            # Subdomain mode: query by code directly / 子域名模式：直接按 code 查询
            result = await db.execute(
                select(Tenant).where(
                    Tenant.code == tenant_code,
                    Tenant.is_active.is_(True),
                    Tenant.is_deleted.is_(False),
                )
            )
            return result.scalar_one_or_none()

        elif domain_type == "custom":
            # Custom domain mode: query tenant_domains table / 自定义域名模式

            result = await db.execute(
                select(TenantDomain)
                .options(selectinload(TenantDomain.tenant))
                .where(
                    func.lower(func.rtrim(TenantDomain.domain, ".")) == host,
                    TenantDomain.is_verified.is_(True),
                    TenantDomain.is_deleted.is_(False),
                )
            )
            try:
                tenant_domain = result.scalar_one_or_none()
            except MultipleResultsFound:
                return None

            if (
                tenant_domain
                and tenant_domain.tenant
                and tenant_domain.tenant.is_active
                and not tenant_domain.tenant.is_deleted
            ):
                # 中文: 运行时域名解析只负责企业身份识别；套餐自定义域名权益由新增、SSL、续期等写侧流程校验。
                # EN: Runtime domain resolution only identifies the tenant; create/SSL/renewal flows enforce custom-domain entitlements.
                return tenant_domain.tenant

        return None


def get_tenant_context(request: Request) -> TenantContext | None:
    """
    Get tenant context from request / 从请求中获取企业上下文

    Usage:
        tenant_ctx = get_tenant_context(request)
        if tenant_ctx and tenant_ctx.is_resolved:
            logger.debug("Tenant: {}", tenant_ctx.tenant.name)
    """
    return getattr(request.state, "tenant_ctx", None)


def get_current_tenant(request: Request) -> Tenant | None:
    """
    Get current tenant from request / 从请求中获取当前企业

    Usage:
        tenant = get_current_tenant(request)
        if tenant:
            logger.debug("Tenant: {}", tenant.name)
    """
    ctx = get_tenant_context(request)
    if ctx and ctx.is_resolved:
        return ctx.tenant
    return None


def request_matches_authenticated_tenant(request: Request, tenant_id: int) -> bool:
    """绑定已解析域名与认证租户 / Bind the resolved domain to the authenticated tenant."""
    origin_header = request.headers.get("origin")
    origin = parse_http_origin(origin_header) if origin_header else None
    if origin_header and origin is None:
        return False
    host = normalize_host_header(request.headers.get("host", ""))
    return tenant_context_matches(
        get_tenant_context(request), tenant_id, host, origin.host if origin else None
    )


def tenant_context_matches(
    context: TenantContext | None, tenant_id: int, host: str, origin_host: str | None
) -> bool:
    """HTTP 与套接字共用租户边界 / Share the tenant boundary across HTTP and sockets."""
    if (
        not host
        or isinstance(tenant_id, bool)
        or not isinstance(tenant_id, int)
        or tenant_id < 1
    ):
        return False
    if context and context.is_resolved:
        return context.tenant_id == tenant_id and context.origin_host == origin_host
    # 中心 API 保留桌面端和服务端调用 / Preserve native and server clients on central API hosts.
    return (is_platform_host(host) or is_development_loopback(host)) and (
        origin_host is None
        or is_platform_host(origin_host)
        or is_development_loopback(origin_host)
    )


__all__ = [
    "TenantMiddleware",
    "TenantContext",
    "get_tenant_context",
    "get_current_tenant",
    "parse_tenant_from_host",
    "request_matches_authenticated_tenant",
]


__all__ = [
    "TenantMiddleware",
    "TenantContext",
    "get_tenant_context",
    "get_current_tenant",
    "parse_tenant_from_host",
]
