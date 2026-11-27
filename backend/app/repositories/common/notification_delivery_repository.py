"""中文: 通知的租户读取与短事务领取。 EN: Recipient boundaries and short delivery claims."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text

from app.core.base_repository import BaseRepository
from app.models.common.notification_delivery import NotificationDelivery
from app.models.system.admin import Admin
from app.models.system.task_definition import TaskDefinition
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_admin import TenantAdmin
from app.models.tenant.tenant_user import TenantUser

EXTERNAL_CHANNELS = frozenset({"email", "ws", "webhook"})
TASK_FAILURE_TEMPLATE = "system.task_failure"
TASK_FAILURE_RECIPIENT = "task_definition"


class NotificationDeliveryRepository(BaseRepository[NotificationDelivery]):
    """中文: 不从请求参数扩大通知接收人范围。 EN: Never expand recipient authority from worker arguments."""

    model = NotificationDelivery

    async def database_now(self):
        return await self.db.scalar(select(func.clock_timestamp()))

    async def has_accepted_admin_test(self, *, dedupe_key: str, admin_id: int) -> bool:
        """中文: 仅当前测试的可投递意图或已存收件箱算受理。 EN: Count only this test's deliverable intent or stored inbox receipt."""
        row = NotificationDelivery
        return bool(
            await self.db.scalar(
                select(
                    select(row.id)
                    .where(
                        row.dedupe_key == dedupe_key,
                        row.recipient_type == "admin",
                        row.recipient_id == admin_id,
                        row.tenant_id.is_(None),
                        row.is_deleted.is_(False),
                        or_(
                            and_(row.channel == "inbox", row.status == "sent"),
                            and_(
                                row.channel.in_(EXTERNAL_CHANNELS),
                                row.status == "pending",
                                row.payload_ciphertext.is_not(None),
                            ),
                        ),
                    )
                    .exists()
                )
            )
        )

    async def task_failure_emails(self, definition_id: int) -> tuple[str, ...]:
        """Read the current notification addresses from the task definition."""
        configured = await self.db.scalar(
            select(TaskDefinition.notify_emails).where(
                TaskDefinition.id == definition_id,
                TaskDefinition.is_deleted.is_(False),
                TaskDefinition.notify_on_failure.is_(True),
            )
        )
        if not configured:
            return ()
        return tuple(
            dict.fromkeys(
                address for address in map(str.strip, configured.split(",")) if address
            )
        )

    async def delivery_email(self, delivery, *, expected_email: str | None = None):
        if delivery.recipient_type == TASK_FAILURE_RECIPIENT:
            if (
                delivery.channel,
                delivery.template_code,
                delivery.tenant_id,
            ) != ("email", TASK_FAILURE_TEMPLATE, None):
                raise ValueError("notification_recipient_scope_mismatch")
            if (
                not expected_email
                or expected_email
                not in await self.task_failure_emails(delivery.recipient_id)
            ):
                raise ValueError("notification_recipient_changed")
            return expected_email

        email = await self.recipient_email(
            delivery.recipient_type, delivery.recipient_id, delivery.tenant_id
        )
        if expected_email is not None and email != expected_email:
            raise ValueError("notification_recipient_changed")
        return email

    async def recipient_email(self, user_type, user_id, tenant_id):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("notification_recipient_invalid")
        model = {
            "admin": Admin,
            "tenant_admin": TenantAdmin,
            "tenant_user": TenantUser,
        }.get(user_type)
        if model is None:
            raise ValueError("notification_recipient_invalid")
        conditions = [
            model.id == user_id,
            model.is_active.is_(True),
            model.is_deleted.is_(False),
        ]
        if user_type == "admin":
            if tenant_id not in (None, 0):
                raise ValueError("notification_recipient_scope_mismatch")
        else:
            if type(tenant_id) is not int or tenant_id <= 0:
                raise ValueError("notification_recipient_scope_mismatch")
            conditions.extend(
                [
                    model.tenant_id == tenant_id,
                    select(Tenant.id)
                    .where(
                        Tenant.id == tenant_id,
                        Tenant.is_active.is_(True),
                        Tenant.is_deleted.is_(False),
                    )
                    .exists(),
                ]
            )
        row = (await self.db.execute(select(model.email).where(*conditions))).first()
        if row is None:
            raise ValueError("notification_recipient_scope_mismatch")
        return row[0]


@dataclass(frozen=True)
class DeliveryClaim:
    """中文: 不携带 ORM 会话的领取快照。 EN: A claim snapshot detached from ORM sessions."""

    delivery_id: int
    token: str
    channel: str
    tenant_id: int | None
    recipient_type: str
    recipient_id: int
    template_code: str
    payload_ciphertext: str
    attempt: int


class NotificationDeliveryClaimRepository:
    """中文: 每次操作独立提交且不持锁等待外发。 EN: Commit each short operation before network work."""

    def __init__(self, sessions):
        self.sessions = sessions

    async def quarantine_legacy(self, delivery_id):
        """中文: 不推断旧排队记录是否已发送。 EN: Never infer whether old queued records were submitted."""
        async with self.sessions() as db, db.begin():
            await self._bounded(db)
            row = await db.scalar(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.id == delivery_id,
                    NotificationDelivery.payload_ciphertext.is_(None),
                    NotificationDelivery.status.in_(["pending", "queued"]),
                    NotificationDelivery.is_deleted.is_(False),
                )
                .with_for_update(skip_locked=True)
            )
            if row is not None:
                row.status = "uncertain"
                row.last_error = "legacy_submission_unknown"

    @staticmethod
    def _due():
        row = NotificationDelivery
        now = func.clock_timestamp()
        return and_(
            row.is_deleted.is_(False),
            row.payload_ciphertext.is_not(None),
            or_(
                and_(
                    row.status == "pending",
                    or_(row.available_at.is_(None), row.available_at <= now),
                ),
                and_(
                    row.status.in_(["claimed", "submitting"]),
                    row.lease_expires_at <= now,
                ),
            ),
        )

    async def due_ids(self, limit=20):
        async with self.sessions() as db, db.begin():
            await self._bounded(db)
            return list(
                (
                    await db.scalars(
                        select(NotificationDelivery.id)
                        .where(self._due())
                        .order_by(NotificationDelivery.id)
                        .limit(max(1, min(limit, 100)))
                    )
                ).all()
            )

    @staticmethod
    async def _bounded(db):
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        await db.execute(text("SET LOCAL statement_timeout = '5s'"))

    async def claim(self, delivery_id):
        async with self.sessions() as db, db.begin():
            await self._bounded(db)
            row = await db.scalar(
                select(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id, self._due())
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            if row.status == "submitting":
                row.status = "uncertain"
                row.last_error = "submission_lease_expired"
                return None
            if row.attempt >= 3:
                row.status = "failed"
                row.last_error = "notification_attempts_exhausted"
                row.lease_expires_at = None
                return None
            now = await db.scalar(select(func.clock_timestamp()))
            row.status = "claimed"
            row.lease_token = str(uuid4())
            row.lease_expires_at = now + timedelta(seconds=120)
            row.attempt += 1
            return DeliveryClaim(
                row.id,
                row.lease_token,
                row.channel,
                row.tenant_id,
                row.recipient_type,
                row.recipient_id,
                row.template_code,
                row.payload_ciphertext,
                row.attempt,
            )

    async def mark_submitting(self, claim, *, email=None):
        async with self.sessions() as db, db.begin():
            await self._bounded(db)
            row = await db.scalar(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.id == claim.delivery_id,
                    NotificationDelivery.lease_token == claim.token,
                    NotificationDelivery.status == "claimed",
                    NotificationDelivery.lease_expires_at > func.clock_timestamp(),
                    NotificationDelivery.is_deleted.is_(False),
                )
                .with_for_update(skip_locked=True)
            )
            if row is None:
                raise ValueError("notification_lease_lost")
            await NotificationDeliveryRepository(db).delivery_email(
                row, expected_email=email
            )
            now = await db.scalar(select(func.clock_timestamp()))
            row.status = "submitting"
            row.submission_started_at = now
            row.lease_expires_at = now + timedelta(seconds=120)
        # 中文: begin 上下文已成功提交后才允许调用 SMTP。 EN: Permit SMTP only after begin has successfully committed.

    async def finish(self, claim, status, error=None, *, retryable=False):
        async with self.sessions() as db, db.begin():
            await self._bounded(db)
            row = await db.scalar(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.id == claim.delivery_id,
                    NotificationDelivery.lease_token == claim.token,
                    NotificationDelivery.is_deleted.is_(False),
                )
                # 中文: 已知外发结果要等待短锁，超时由调用方明确报告未落库。
                # EN: Wait for short locks on known results; callers report bounded timeout.
                .with_for_update()
            )
            if row is None or row.status not in {"claimed", "submitting", "uncertain"}:
                return False
            if row.status == "uncertain" and status != "sent":
                return False
            now = await db.scalar(select(func.clock_timestamp()))
            if retryable and claim.attempt < 3:
                row.status = "pending"
                row.available_at = now + timedelta(seconds=min(300, 30 * claim.attempt))
                row.lease_token = None
                row.submission_started_at = None
            else:
                row.status = status
                if status in {"sent", "skipped"}:
                    row.delivered_at = now.replace(tzinfo=None)
            row.lease_expires_at = None
            row.last_error = error
            return True
