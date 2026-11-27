"""中文: 通知提交后的非持久唤醒提示。 EN: Post-commit hints for durable notification rows."""

from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.logging import get_logger

logger = get_logger(__name__)
_KEY = "notification_outbox_after_commit"
_publisher = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="notification-publish"
)
_publisher_slots = BoundedSemaphore(16)


def publish_notification_delivery(delivery_id: int) -> None:
    """中文: 失败只丢提示，数据库仍是恢复依据。 EN: Lost hints leave recoverable database rows."""
    try:
        from app.celery_app import celery_app

        with celery_app.connection_for_write(
            connect_timeout=2,
            transport_options={
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
                "retry_on_timeout": False,
                "max_retries": 0,
            },
        ) as connection:
            celery_app.send_task(
                "app.tasks.notification.dispatch_notification_delivery",
                kwargs={"delivery_id": delivery_id},
                queue="notification",
                connection=connection,
                retry=False,
                ignore_result=True,
            )
    except Exception:
        logger.warning("Notification publish unavailable; committed intent retained")


def _committed(session):
    if session.in_nested_transaction():
        return
    for delivery_id in session.info.pop(_KEY, set()):
        if not _publisher_slots.acquire(blocking=False):
            logger.warning(
                "Notification hint capacity reached; committed intent retained"
            )
            continue
        try:
            future = _publisher.submit(publish_notification_delivery, delivery_id)
            future.add_done_callback(lambda _future: _publisher_slots.release())
        except Exception:
            _publisher_slots.release()
            logger.warning("Notification publisher stopped; committed intent retained")


def _snapshot(session, transaction):
    if transaction.nested:
        transaction.notification_hints = set(session.info.get(_KEY, set()))


def _rolled_back(session, previous_transaction):
    if previous_transaction.nested:
        session.info[_KEY] = getattr(previous_transaction, "notification_hints", set())
    elif previous_transaction.parent is None:
        session.info.pop(_KEY, None)


def schedule_notification_delivery_after_commit(db, delivery_id: int) -> None:
    """中文: 仅外层提交可发布；保存点回滚撤销其提示。 EN: Only outer commit publishes savepoint-safe hints."""
    session = getattr(db, "sync_session", db)
    if not isinstance(session, Session) or type(delivery_id) is not int:
        raise TypeError("notification_requires_transactional_session")
    if not session.info.get("notification_events_registered"):
        event.listen(session, "after_commit", _committed)
        event.listen(session, "after_soft_rollback", _rolled_back)
        event.listen(session, "after_transaction_create", _snapshot)
        session.info["notification_events_registered"] = True
    session.info.setdefault(_KEY, set()).add(delivery_id)
