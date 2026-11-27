"""向当前租户会话投递事件 / Deliver events to current tenant sessions."""

from __future__ import annotations

from typing import Any

from socketio import AsyncRedisManager

from app.sio.socket_trace import tenant_socket_session_is_current

_NAMESPACE_SCOPES = {
    "/tenant": "tenant_admin",
    "/user": "tenant_user",
}


class SessionValidatedRedisManager(AsyncRedisManager):
    """复核本机收件会话，保留跨进程发布协议 / Filter local sessions and retain the Pub/Sub protocol."""

    async def _unavailable_recipients(
        self,
        event: str,
        namespace: str,
        room: str | list[str] | None,
        skip_sid: str | list[str] | None,
    ) -> str | list[str] | None:
        scope = _NAMESPACE_SCOPES.get(namespace)
        if scope is None:
            from app.plugins.sio_auth import PluginAuthNamespaceWrapper

            handler = self.server.namespace_handlers.get(namespace)
            if not isinstance(handler, PluginAuthNamespaceWrapper):
                return skip_sid

        skipped = list(skip_sid) if isinstance(skip_sid, list) else []
        if isinstance(skip_sid, str):
            skipped.append(skip_sid)
        excluded = set(skipped)
        eligible: set[str] = set()
        for sid, _ in self.get_participants(namespace, room):
            if sid in excluded:
                continue
            try:
                session = await self.server.get_session(sid, namespace=namespace)
                if not isinstance(session, dict):
                    continue
                user_type = session.get("user_type")
                if scope is None and user_type == "admin":
                    eligible.add(sid)
                    continue
                if scope is not None and user_type != scope:
                    continue
                # 强退事件用于结束已撤销的会话 / Force-logout tells revoked sessions to close.
                force_logout = (
                    event == "force_logout" and room == f"user:{session.get('user_id')}"
                )
                if force_logout or await tenant_socket_session_is_current(session):
                    eligible.add(sid)
            except Exception:
                continue
        # 投递快照只包含已复核的成员 / Deliver only to members that were validated.
        skipped.extend(
            sid
            for sid, _ in self.get_participants(namespace, room)
            if sid not in eligible and sid not in excluded
        )
        return skipped

    async def _handle_emit(self, message: dict[str, Any]) -> None:
        skipped = await self._unavailable_recipients(
            message["event"],
            message.get("namespace") or "/",
            message.get("room"),
            message.get("skip_sid"),
        )
        await super()._handle_emit({**message, "skip_sid": skipped})

    async def emit(
        self,
        event: str,
        data: Any,
        namespace: str | None = None,
        room: str | list[str] | None = None,
        skip_sid: str | list[str] | None = None,
        callback: Any = None,
        to: str | list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if kwargs.get("ignore_queue"):
            skip_sid = await self._unavailable_recipients(
                event, namespace or "/", to or room, skip_sid
            )
        await super().emit(
            event,
            data,
            namespace=namespace or "/",
            room=room,
            skip_sid=skip_sid,
            callback=callback,
            to=to,
            **kwargs,
        )
