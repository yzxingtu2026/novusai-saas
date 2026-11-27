"""将握手绑定至已认证租户 / Bind the transport handshake to its authenticated tenant."""

from starlette.datastructures import Headers

from app.core.domains import normalize_host_header, parse_http_origin
from app.middleware.tenant import TenantMiddleware, tenant_context_matches


async def socket_matches_authenticated_tenant(environ: dict, tenant_id: int) -> bool:
    scope = environ.get("asgi.scope")
    if scope is not None:
        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        origin_header = headers.get("origin")
    else:
        host_header = environ.get("HTTP_HOST", "")
        origin_header = environ.get("HTTP_ORIGIN")

    host = normalize_host_header(host_header)
    origin = parse_http_origin(origin_header) if origin_header else None
    if not host or (origin_header and origin is None):
        return False
    origin_host = origin.host if origin else None
    context = await TenantMiddleware.resolve_request_context(
        host, host_header, origin_host
    )
    return tenant_context_matches(context, tenant_id, host, origin_host)
