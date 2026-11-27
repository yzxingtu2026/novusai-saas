"""中文: 邮件渠道仅保存加密投递意图。 EN: Email channel persists encrypted delivery intent only."""

from __future__ import annotations

from app.services.common.channels.base import NotificationChannel


class EmailChannel(NotificationChannel):
    """中文: SMTP 外发由已提交记录驱动。 EN: Committed records drive SMTP submission."""

    @property
    def channel_code(self):
        return "email"

    @property
    def channel_name(self):
        return "Email"

    async def is_enabled(self):
        from app.core.database import sync_session_factory
        from app.services.common.email_service import _load_smtp_config_sync

        with sync_session_factory() as db:
            return bool(_load_smtp_config_sync(db).enabled)

    async def deliver(
        self,
        db,
        user_type,
        user_id,
        title,
        body,
        data,
        link,
        priority,
        template_code,
        tenant_id=None,
        **kwargs,
    ):
        from app.services.common.notification_delivery_service import (
            prepare_external_delivery,
        )

        delivery = kwargs.pop("delivery_record", None)
        if delivery is None or delivery.channel != "email":
            raise ValueError("notification_delivery_record_required")
        return await prepare_external_delivery(
            db,
            delivery,
            user_type=user_type,
            user_id=user_id,
            title=title,
            body=body,
            data=data,
            link=link,
            priority=priority,
            template_code=template_code,
            tenant_id=tenant_id,
            **kwargs,
        )


__all__ = ["EmailChannel"]
