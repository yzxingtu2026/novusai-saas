"""
WebSocket 实时推送通知渠道 / WebSocket Notification Channel

通过 Socket.IO 实时推送通知到已连接的客户端。
Pushes notifications to connected clients in real-time via Socket.IO.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import LogManager
from app.services.common.channels.base import NotificationChannel

logger = LogManager.get_logger("app")


class WSChannel(NotificationChannel):
    """WebSocket 渠道 — Socket.IO 实时推送 / WebSocket channel — Socket.IO real-time push."""

    @property
    def channel_code(self) -> str:
        return "ws"

    @property
    def channel_name(self) -> str:
        return "WebSocket"

    async def is_enabled(self) -> bool:
        """WS 跟随通知系统总开关 / WS follows notification system master switch."""
        try:
            from app.sio.ws_config import get_ws_config

            return bool(await get_ws_config("notification_enabled"))
        except Exception:
            return True

    async def deliver(
        self,
        db: AsyncSession,
        user_type: str,
        user_id: int,
        title: str,
        body: str | None,
        data: dict[str, Any] | None,
        link: str | None,
        priority: str,
        template_code: str,
        tenant_id: int | None = None,
        **kwargs: Any,
    ) -> bool:
        _ = (db, tenant_id)
        try:
            ns_map = {
                "admin": "/admin",
                "tenant_admin": "/tenant",
                "tenant_user": "/user",
            }
            namespace = ns_map[user_type]

            category = template_code.split(".")[0] if "." in template_code else "system"

            payload = {
                "type": template_code,
                "category": category,
                "title": title,
                "body": body,
                "data": data,
                "link": link,
                "priority": priority,
            }
            notification_id = kwargs.get("notification_id")
            if type(notification_id) is int and notification_id > 0:
                payload["id"] = notification_id
            if kwargs.get("_worker_write_only"):
                await _emit_worker_notification(payload, user_id, namespace)
                return True

            from app.core.socketio_server import sio

            await sio.emit(
                "notification",
                payload,
                room=f"user:{user_id}",
                namespace=namespace,
            )
            return True
        except Exception:
            logger.warning("WSChannel delivery acceptance unknown")
            return False


async def _emit_worker_notification(payload, user_id, namespace):
    """中文: 单次发布且每次释放 Redis 资源。 EN: Publish once and release invocation-owned Redis resources."""
    import socketio
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff

    from app.core.config import settings

    class SinglePublishManager(socketio.AsyncRedisManager):
        async def _publish(self, data):
            # 中文: 库默认重试会重发未知提交；此处必须传播首次失败。
            # EN: The library retries ambiguous writes; propagate the first failure instead.
            if not self.connected:
                self._redis_connect()
            return await self.redis.publish(self.channel, self.json.dumps(data))

    manager = SinglePublishManager(
        settings.REDIS_URL,
        write_only=True,
        redis_options={
            "protocol": 2,
            "socket_connect_timeout": 5,
            "socket_keepalive": True,
            "socket_timeout": 10,
            "retry_on_timeout": False,
            "retry": Retry(NoBackoff(), 0),
        },
    )
    try:
        await manager.emit(
            "notification", payload, room=f"user:{user_id}", namespace=namespace
        )
    finally:
        for resource in (
            getattr(manager, "pubsub", None),
            getattr(manager, "redis", None),
        ):
            if resource is not None:
                with suppress(Exception):
                    await resource.aclose()


__all__ = ["WSChannel"]
