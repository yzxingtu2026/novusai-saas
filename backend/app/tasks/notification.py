"""中文: 只按持久记录 ID 投递通知。 EN: Dispatch notifications by durable record IDs only."""

from app.services.common.notification_task_runtime import run_notification_task
from app.tasks.base import BaseTask, register_task


class LegacyNotificationTask(BaseTask):
    """中文: 旧任务的审计参数也只保留 ID。 EN: Legacy task audit arguments retain IDs only."""

    abstract = True

    def _record_task_run_start(self, task_id, args, kwargs):
        delivery_id = kwargs.get("delivery_id")
        if delivery_id is None and len(args) > 6:
            delivery_id = args[6]
        safe = (
            {"delivery_id": delivery_id}
            if type(delivery_id) is int and delivery_id > 0
            else {}
        )
        super()._record_task_run_start(task_id, (), safe)


@register_task(
    queue="notification",
    description="Durable notification dispatch / 持久通知投递",
    max_retries=0,
)
def dispatch_notification_delivery(self: BaseTask, delivery_id: int) -> dict:
    """中文: 重复 broker 消息仍需取得数据库租约。 EN: Duplicate broker hints still require a database lease."""
    from app.services.common.notification_delivery_service import (
        NotificationDeliveryService,
    )

    async def dispatch(sessions):
        return await NotificationDeliveryService(sessions).dispatch(delivery_id)

    return {"delivery_id": delivery_id, "status": run_notification_task(dispatch)}


@register_task(
    queue="notification",
    description="Recover durable notification hints / 恢复持久通知提示",
    max_retries=0,
)
def recover_notification_deliveries(self: BaseTask, limit: int = 20) -> dict:
    """中文: 有界补扫仅发布 ID，不重放历史邮件内容。 EN: Bounded recovery publishes IDs without replaying legacy mail content."""
    from app.repositories.common.notification_delivery_repository import (
        NotificationDeliveryClaimRepository,
    )
    from app.services.common.notification_delivery_dispatch import (
        publish_notification_delivery,
    )

    async def due(sessions):
        return await NotificationDeliveryClaimRepository(sessions).due_ids(limit)

    ids = run_notification_task(due)
    for delivery_id in ids:
        publish_notification_delivery(delivery_id)
    return {"hinted": len(ids)}


@register_task(
    queue="notification",
    description="Quarantine legacy notification email / 隔离旧通知邮件",
    max_retries=0,
    base=LegacyNotificationTask,
)
def send_notification_email(
    self: BaseTask,
    to: list[str],
    subject: str,
    html_body: str | None = None,
    text_body: str | None = None,
    triggered_by: str = "notification",
    tenant_id: int | None = None,
    delivery_id: int | None = None,
) -> dict:
    """中文: 保留旧任务签名但绝不重发未知历史提交。 EN: Preserve the legacy signature without replaying ambiguous historical submissions."""
    if type(delivery_id) is int and delivery_id > 0:
        from app.repositories.common.notification_delivery_repository import (
            NotificationDeliveryClaimRepository,
        )

        async def quarantine(sessions):
            return await NotificationDeliveryClaimRepository(
                sessions
            ).quarantine_legacy(delivery_id)

        run_notification_task(quarantine)
    return {"status": "legacy_quarantined"}


__all__ = [
    "dispatch_notification_delivery",
    "recover_notification_deliveries",
    "send_notification_email",
]
