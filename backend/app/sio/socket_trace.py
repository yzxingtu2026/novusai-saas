"""
Socket.IO trace/session helpers.

This mixin keeps ordinary realtime namespaces able to propagate trace ids.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

import socketio

from app.middleware.trace import (
    extract_optional_trace_id,
    normalize_trace_id,
    trace_id_var,
)


def _extract_trace_id(payload: dict[str, Any] | None = None) -> str | None:
    if not isinstance(payload, dict):
        return None
    return extract_optional_trace_id(payload.get("trace_id"))


def _tenant_socket_sessions_key(tenant_id: int) -> str:
    return f"socketio:tenant-sessions:{tenant_id}"


def _socket_entry(namespace: str, sid: str, epoch: int) -> str:
    return json.dumps([namespace, sid, epoch], separators=(",", ":"))


async def register_tenant_socket_session(
    tenant_id: int, namespace: str, sid: str, epoch: int
) -> None:
    from app.core.redis import get_redis_client

    await get_redis_client().sadd(
        _tenant_socket_sessions_key(tenant_id), _socket_entry(namespace, sid, epoch)
    )


async def unregister_tenant_socket_session(
    tenant_id: int, namespace: str, sid: str, epoch: int
) -> None:
    from app.core.redis import get_redis_client

    await get_redis_client().srem(
        _tenant_socket_sessions_key(tenant_id), _socket_entry(namespace, sid, epoch)
    )


async def disconnect_tenant_socket_sessions(tenant_id: int, current_epoch: int) -> bool:
    from app.core.redis import get_redis_client
    from app.core.socketio_server import get_sio

    redis = get_redis_client()
    key = _tenant_socket_sessions_key(tenant_id)
    complete = True
    async for entry in redis.sscan_iter(key):
        try:
            namespace, sid, epoch = json.loads(entry)
            if (
                not isinstance(namespace, str)
                or not namespace.startswith("/")
                or not isinstance(sid, str)
                or not sid
                or type(epoch) is not int
            ):
                raise ValueError("Invalid tenant socket entry")
        except (TypeError, ValueError):
            await redis.srem(key, entry)
            continue
        if epoch >= current_epoch:
            continue
        try:
            await get_sio().disconnect(sid, namespace=namespace)
        except Exception:
            complete = False
            continue
        await redis.srem(key, entry)
    return complete


async def socket_token_is_current(session: dict[str, Any] | None) -> bool:
    """Verify stored token and family state for an authenticated socket."""
    from app.core.security import (
        REFRESH_TOKEN_FAMILY_CLAIM,
        is_refresh_token_family_revoked,
        is_token_revoked,
    )

    if not isinstance(session, dict):
        return False
    jti, expires = session.get("token_jti"), session.get("token_exp")
    if (
        not isinstance(jti, str)
        or not jti
        or type(expires) is not int
        or expires <= time.time()
    ):
        return False
    if await is_token_revoked(jti):
        return False
    family_id = session.get(REFRESH_TOKEN_FAMILY_CLAIM)
    return family_id is None or not await is_refresh_token_family_revoked(family_id)


async def tenant_socket_session_is_current(session: dict[str, Any] | None) -> bool:
    """复核当前租户会话 / Check the tenant session for the next inbound or outbound event."""
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.core.security import (
        TENANT_SESSION_EPOCH_CLAIM,
        TOKEN_SCOPE_TENANT_ADMIN,
        TOKEN_SCOPE_TENANT_USER,
        normalize_positive_subject_id,
        tenant_session_epoch_matches,
    )
    from app.models import Tenant, TenantAdmin, TenantUser

    if not isinstance(session, dict):
        return False
    user_type = session.get("user_type")
    identity = {
        TOKEN_SCOPE_TENANT_ADMIN: TenantAdmin,
        TOKEN_SCOPE_TENANT_USER: TenantUser,
    }.get(user_type)
    user_id = normalize_positive_subject_id(session.get("user_id"))
    tenant_id = normalize_positive_subject_id(session.get("tenant_id"))
    if identity is None or user_id is None or tenant_id is None:
        return False
    try:
        if not await socket_token_is_current(session):
            return False
        async with async_session_factory() as db:
            epoch = await db.scalar(
                select(Tenant.session_revocation_epoch)
                .join(identity, Tenant.id == identity.tenant_id)
                .where(
                    identity.id == user_id,
                    identity.tenant_id == tenant_id,
                    identity.is_active.is_(True),
                    identity.is_deleted.is_(False),
                    Tenant.is_active.is_(True),
                    Tenant.is_deleted.is_(False),
                )
            )
            return tenant_session_epoch_matches(
                {TENANT_SESSION_EPOCH_CLAIM: session.get(TENANT_SESSION_EPOCH_CLAIM)},
                epoch,
            )
    except Exception:
        return False


class SocketTraceMixin:
    """Bind trace context for generic Socket.IO namespace events."""

    async def trigger_event(
        self: socketio.AsyncNamespace,
        event: str,
        *args: Any,
    ) -> Any:
        if event in ("connect", "disconnect"):
            return await super().trigger_event(event, *args)

        sid = str(args[0]) if args else ""
        payload = args[1] if len(args) > 1 and isinstance(args[1], dict) else None
        try:
            if sid:
                session = await self.bind_socket_trace(sid, payload)
                if self.namespace == "/admin" and not await socket_token_is_current(
                    session
                ):
                    await self.disconnect(sid)
                    return None
                if self.namespace in {"/tenant", "/user"} and not (
                    await tenant_socket_session_is_current(session)
                ):
                    await self.disconnect(sid)
                    return None
            return await super().trigger_event(event, *args)
        finally:
            trace_id_var.set("")

    async def get_socket_session_with_fallback(
        self: socketio.AsyncNamespace,
        sid: str,
    ) -> dict[str, Any] | None:
        with contextlib.suppress(Exception):
            session = await self.get_session(sid)
            if isinstance(session, dict):
                return session

        sid_sessions = getattr(self, "_sid_sessions", None)
        if isinstance(sid_sessions, dict):
            fallback = sid_sessions.get(sid)
            if isinstance(fallback, dict):
                return fallback
        return None

    async def bind_socket_trace(
        self: socketio.AsyncNamespace,
        sid: str,
        payload: dict[str, Any] | None = None,
        default_trace_id: str | None = None,
    ) -> dict[str, Any] | None:
        session = await self.get_socket_session_with_fallback(sid)
        trace_id = normalize_trace_id(
            _extract_trace_id(payload) or (session or {}).get("trace_id"),
            default=default_trace_id,
        )
        trace_id_var.set(trace_id)

        updated_session = session
        if session is not None and session.get("trace_id") != trace_id:
            updated_session = {**session, "trace_id": trace_id}
            with contextlib.suppress(Exception):
                await self.save_session(sid, updated_session)
            sid_sessions = getattr(self, "_sid_sessions", None)
            if isinstance(sid_sessions, dict):
                sid_sessions[sid] = updated_session

        return updated_session
