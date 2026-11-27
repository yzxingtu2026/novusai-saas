"""
通知投递服务 / Notification Delivery Service

统一入口：send() 查询用户偏好，分发到三个渠道（WS / 收件箱 / 邮件）。
Unified entry: send() queries user preferences and dispatches to three channels (WS / inbox / email).
提供通知查询、已读、删除等操作。
Provides notification query, read, delete operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.configs.service import PLATFORM_TENANT_ID
from app.core.base_model import utc_now
from app.core.logging import LogManager
from app.models.common.notification import Notification
from app.models.common.notification_delivery import NotificationDelivery
from app.models.common.notification_preference import NotificationPreference
from app.models.common.notification_template import NotificationTemplate

logger = LogManager.get_logger("app")

_SOFT_QUOTA_DEFAULT_COPY = (
    "AI 配额已超限提醒",
    "您的 AI 使用量已达 {current} Token，已超过软限制 {limit}（周期：{period}），"
    "请关注用量并及时续费或升级套餐。",
)
_SOFT_QUOTA_EMAIL_PERIOD_LABELS = {"daily": "每日", "monthly": "每月"}


@dataclass(frozen=True, slots=True)
class DefaultTemplateCopy:
    """A per-send replacement for an exact built-in copy, before interpolation."""

    original: tuple[str, str]
    replacement: tuple[str, str]


class NotificationService:
    """
    通知投递与管理服务 / Notification delivery and management service.

    使用方式：
        service = NotificationService(db)
        await service.send(
            template_code="ai.soft_quota_exceeded",
            recipients=[("tenant_admin", 5)],
            data={"current": 120000, "limit": 100000, "period": "monthly"},
        )
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ========================================
    # 投递 / Deliver
    # ========================================

    async def send(
        self,
        template_code: str,
        recipients: list[tuple[str, int]],
        data: dict[str, Any] | None = None,
        link: str | None = None,
        tenant_id: int | None = None,
        *,
        default_copy: DefaultTemplateCopy | None = None,
        fallback_template_code: str | None = None,
        task_failure_email_definition_id: int | None = None,
        **kwargs: Any,
    ) -> int:
        """
        投递通知（统一入口）/ Send notification (unified entry).

        注意：收件箱通知通过 db.add() 写入 session，但不会自动 commit。
        调用方需要在合适的时机执行 await db.commit() 来持久化收件箱记录。
        外部渠道只保存加密意图，外层提交后异步投递。
        External channels persist encrypted intentions and dispatch only after outer commit.

        Args:
            template_code: 通知模板编码（如 'ai.soft_quota_exceeded'）
            recipients: 接收人列表 [(user_type, user_id), ...]
            data: 业务数据（用于模板渲染和前端展示）
            link: 点击跳转链接
            tenant_id: 企业 ID（平台级通知为 None）
            default_copy: 文案变体，仅用于文案完全匹配的内置平台模板
            fallback_template_code: 仅在主模板未配置时使用的模板编码
            task_failure_email_definition_id: 管理员配置失败邮件的任务定义 ID
            **kwargs: 渠道扩展参数
                - email_html: 自定义 HTML 邮件正文
                - email_subject: 自定义邮件主题
                - email_text: 自定义纯文本邮件正文

        Returns:
            已处理的接收人数 / Number of recipients handled for delivery planning.
        """
        from app.repositories.common.notification_delivery_repository import (
            TASK_FAILURE_TEMPLATE,
            NotificationDeliveryRepository,
        )
        from app.services.common.channels import get_channel

        if task_failure_email_definition_id is not None and (
            template_code != TASK_FAILURE_TEMPLATE or tenant_id is not None
        ):
            raise ValueError("notification_recipient_scope_mismatch")

        # 检查通知系统总开关 / Check notification master switch
        if not await self._notifications_enabled():
            return 0

        # 查询模板 / Query template
        if fallback_template_code is None:
            template = await self._get_template(template_code, tenant_id=tenant_id)
        else:
            template = await self._get_template(
                template_code, tenant_id=tenant_id, fallback_code=fallback_template_code
            )
        if not template:
            logger.warning("Notification template not found: {}", template_code)
            return 0
        template_code = template.code

        # 渲染标题和正文 / Render title and body
        title, body = self._render_template_content(
            template, data, default_copy=default_copy
        )
        template_channels = sorted(
            template.channels or ["ws", "inbox"], key=lambda code: code != "inbox"
        )

        email_body = None
        if (
            "email" in template_channels
            and default_copy is None
            and not (kwargs.get("email_html") or kwargs.get("email_text"))
        ):
            email_body = self._render_soft_quota_email_body(template, data)

        # force_all_channels 模式：绕过偏好和渠道开关（用于测试发送）
        force = kwargs.pop("force_all_channels", False)
        external_created_notifications = kwargs.pop("created_notifications", None)
        dedupe_key = kwargs.pop("dedupe_key", None)

        # 预缓存渠道启用状态（避免在 recipients 循环内重复查询 DB）
        channel_enabled_cache: dict[str, bool] = {}
        if not force:
            for channel_code in template_channels:
                ch = get_channel(channel_code)
                if ch:
                    channel_enabled_cache[
                        channel_code
                    ] = await self._channel_is_enabled(channel_code)

        if task_failure_email_definition_id is not None:
            if template_code == TASK_FAILURE_TEMPLATE and channel_enabled_cache.get(
                "email"
            ):
                await self._plan_task_failure_emails(
                    template,
                    task_failure_email_definition_id,
                    title,
                    body,
                    data,
                    link,
                    kwargs,
                )
            # Account preferences and destinations still apply to the account channels.
            template_channels = [code for code in template_channels if code != "email"]

        sent = 0
        for user_type, user_id in recipients:
            inbox_notification_id = None
            try:
                await NotificationDeliveryRepository(self.db).recipient_email(
                    user_type, user_id, tenant_id
                )
            except ValueError:
                for channel_code in template_channels:
                    await self._record_delivery_status(
                        template=template,
                        channel_code=channel_code,
                        user_type=user_type,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        status="skipped",
                        last_error="notification_recipient_unavailable",
                        dedupe_key=dedupe_key,
                    )
                continue
            # 查询用户偏好（force 模式跳过）
            if not force:
                pref = await self._get_preference(
                    user_type,
                    user_id,
                    template.category,
                    tenant_id=tenant_id or PLATFORM_TENANT_ID,
                )
            else:
                pref = {}

            # 遍历模板定义的渠道 / Iterate channels defined on template
            for channel_code in template_channels:
                if channel_code not in {"inbox", "email", "ws", "webhook"}:
                    await self._record_delivery_status(
                        template=template,
                        channel_code=channel_code,
                        user_type=user_type,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        status="skipped",
                        last_error="channel_unsupported",
                        dedupe_key=dedupe_key,
                    )
                    continue
                # 用户偏好检查（force 模式全部允许）
                if not force:
                    default_enabled = channel_code in ("ws", "inbox")
                    pref_key = f"channel_{channel_code}"
                    if not pref.get(pref_key, default_enabled):
                        await self._record_delivery_status(
                            template=template,
                            channel_code=channel_code,
                            user_type=user_type,
                            user_id=user_id,
                            tenant_id=tenant_id,
                            status="skipped",
                            last_error="disabled_by_preference",
                            dedupe_key=dedupe_key,
                        )
                        continue

                channel = get_channel(channel_code)
                if not channel:
                    await self._record_delivery_status(
                        template=template,
                        channel_code=channel_code,
                        user_type=user_type,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        status="skipped",
                        last_error="channel_not_registered",
                        dedupe_key=dedupe_key,
                    )
                    continue

                # 渠道全局启用检查（force 模式跳过，使用预缓存结果）
                if not force and not channel_enabled_cache.get(channel_code, False):
                    await self._record_delivery_status(
                        template=template,
                        channel_code=channel_code,
                        user_type=user_type,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        status="skipped",
                        last_error="channel_disabled",
                        dedupe_key=dedupe_key,
                    )
                    continue

                # 投递 / Deliver
                delivery, should_deliver = await self._prepare_delivery_record(
                    template=template,
                    channel_code=channel_code,
                    user_type=user_type,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    dedupe_key=dedupe_key,
                )
                if not should_deliver:
                    if channel_code == "inbox":
                        inbox_notification_id = delivery.notification_id
                    continue
                created_notifications: list[Notification] = []
                arguments = {
                    "user_type": user_type,
                    "user_id": user_id,
                    "title": title,
                    "body": body,
                    "data": data,
                    "link": link,
                    "priority": template.priority,
                    "template_code": template_code,
                    "tenant_id": tenant_id,
                }
                if channel_code in {"ws", "webhook"}:
                    from app.services.common.notification_delivery_service import (
                        prepare_external_delivery,
                    )

                    if channel_code == "ws":
                        delivery.notification_id = inbox_notification_id
                    delivered = await prepare_external_delivery(
                        self.db,
                        delivery,
                        **arguments,
                        force=force,
                        category=template.category,
                        **kwargs,
                    )
                else:
                    options = (
                        {"force": force, "category": template.category}
                        if channel_code == "email"
                        else {}
                    )
                    if channel_code == "email" and email_body is not None:
                        options["email_render_body"] = email_body
                    delivered = await channel.deliver(
                        db=self.db,
                        **arguments,
                        delivery_record=delivery,
                        created_notifications=created_notifications,
                        dedupe_key=dedupe_key,
                        **options,
                        **kwargs,
                    )
                await self._finalize_delivery_record(
                    delivery,
                    delivered=delivered,
                    created_notifications=created_notifications,
                )
                if channel_code == "inbox":
                    inbox_notification_id = delivery.notification_id
                if isinstance(external_created_notifications, list):
                    external_created_notifications.extend(created_notifications)

            sent += 1

        # inbox 渠道的数量限制
        for user_type, user_id in recipients:
            if "inbox" in template_channels:
                await self._enforce_max_per_user(
                    user_type, user_id, tenant_id=tenant_id
                )

        logger.info(
            "Notification planning complete: template={} recipients={} handled_recipients={}",
            template_code,
            len(recipients),
            sent,
        )
        return sent

    async def _plan_task_failure_emails(
        self,
        template: NotificationTemplate,
        definition_id: int,
        title: str,
        body: str,
        data: dict[str, Any] | None,
        link: str | None,
        options: dict[str, Any],
    ) -> None:
        from app.repositories.common.notification_delivery_repository import (
            TASK_FAILURE_RECIPIENT,
            NotificationDeliveryRepository,
        )
        from app.services.common.email_service import _is_valid_email
        from app.services.common.notification_delivery_service import (
            prepare_external_delivery,
        )

        repo = NotificationDeliveryRepository(self.db)
        for address in await repo.task_failure_emails(definition_id):
            if not _is_valid_email(address):
                continue
            delivery = await self._create_delivery_record(
                template=template,
                channel_code="email",
                user_type=TASK_FAILURE_RECIPIENT,
                user_id=definition_id,
                tenant_id=None,
            )
            delivered = await prepare_external_delivery(
                self.db,
                delivery,
                user_type=TASK_FAILURE_RECIPIENT,
                user_id=definition_id,
                title=title,
                body=body,
                data=data,
                link=link,
                priority=template.priority,
                template_code=template.code,
                category=template.category,
                configured_task_email=address,
                email_html=options.get("email_html"),
                email_subject=options.get("email_subject"),
                email_text=options.get("email_text"),
            )
            await self._finalize_delivery_record(
                delivery, delivered=delivered, created_notifications=[]
            )

    async def send_test(
        self, *, template_code: str, admin_id: int, data: dict[str, Any]
    ) -> int:
        """中文: 返回当前管理员的受理数，不推断外部送达。 EN: Return current-admin acceptance without inferring external delivery."""
        from uuid import uuid4

        from app.repositories.common.notification_delivery_repository import (
            NotificationDeliveryRepository,
        )

        dedupe_key = "template-test:" + uuid4().hex
        await self.send(
            template_code=template_code,
            recipients=[("admin", admin_id)],
            data=data,
            tenant_id=None,
            force_all_channels=True,
            dedupe_key=dedupe_key,
        )
        await self.db.flush()
        return int(
            await NotificationDeliveryRepository(self.db).has_accepted_admin_test(
                dedupe_key=dedupe_key, admin_id=admin_id
            )
        )

    async def build_ws_payload(
        self,
        template_code: str,
        data: dict[str, Any] | None = None,
        link: str | None = None,
        *,
        fallback_category: str | None = None,
        fallback_title: str | None = None,
        fallback_body: str | None = None,
        fallback_priority: str = "normal",
        tenant_id: int | None = None,
        default_copy: DefaultTemplateCopy | None = None,
    ) -> dict[str, Any] | None:
        """
        构建实时 WS payload，优先使用通知模板渲染。
        Build a WS payload using notification template rendering when available.

        Returns None when notifications are globally disabled or the template
        explicitly disables the WS channel.
        """
        if not await self._notifications_enabled():
            return None

        template = await self._get_template(template_code, tenant_id=tenant_id)
        if template:
            template_channels = template.channels or ["ws", "inbox"]
            if "ws" not in template_channels:
                return None

            title, body = self._render_template_content(
                template, data, default_copy=default_copy
            )
            category = template.category
            priority = template.priority
        else:
            logger.warning(
                "Notification template not found for WS payload: {}", template_code
            )
            category = fallback_category or self._derive_category(template_code)
            title = fallback_title or template_code
            body = fallback_body
            priority = fallback_priority

        return {
            "type": template_code,
            "category": category,
            "title": title,
            "body": body,
            "data": data,
            "link": link,
            "priority": priority,
        }

    # ========================================
    # 查询 / Query
    # ========================================

    async def get_notifications(
        self,
        user_type: str,
        user_id: int,
        category: str | None = None,
        is_read: bool | None = None,
        page: int = 1,
        page_size: int = 20,
        tenant_id: int | None = None,
    ) -> tuple[list[Notification], int]:
        """
        查询通知列表（分页）/ Query notification list (paginated).

        Returns:
            (items, total)
        """
        conditions = [
            Notification.recipient_type == user_type,
            Notification.recipient_id == user_id,
            Notification.is_deleted.is_(False),
            self._notification_tenant_condition(tenant_id),
        ]
        if category:
            conditions.append(Notification.category == category)
        if is_read is not None:
            conditions.append(Notification.is_read == is_read)

        # 总数 / Total count
        count_q = select(func.count(Notification.id)).where(and_(*conditions))
        total = (await self.db.execute(count_q)).scalar() or 0

        # 列表 / List
        offset = (page - 1) * page_size
        q = (
            select(Notification)
            .where(and_(*conditions))
            .order_by(Notification.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        result = await self.db.execute(q)
        items = list(result.scalars().all())

        return items, total

    async def get_unread_count(
        self,
        user_type: str,
        user_id: int,
        tenant_id: int | None = None,
    ) -> int:
        """获取未读通知总数 / Get total unread notification count."""
        q = select(func.count(Notification.id)).where(
            Notification.recipient_type == user_type,
            Notification.recipient_id == user_id,
            self._notification_tenant_condition(tenant_id),
            Notification.is_read.is_(False),
            Notification.is_deleted.is_(False),
        )
        return (await self.db.execute(q)).scalar() or 0

    # ========================================
    # 操作 / Operations
    # ========================================

    async def mark_read(
        self,
        notification_id: int,
        user_type: str,
        user_id: int,
        tenant_id: int | None = None,
    ) -> bool:
        """标记单条通知已读 / Mark single notification as read."""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.recipient_type == user_type,
                Notification.recipient_id == user_id,
                self._notification_tenant_condition(tenant_id),
                Notification.is_deleted.is_(False),
            )
            .values(is_read=True, read_at=utc_now())
        )
        return result.rowcount > 0

    async def mark_all_read(
        self,
        user_type: str,
        user_id: int,
        category: str | None = None,
        tenant_id: int | None = None,
    ) -> int:
        """标记全部已读，返回更新数量 / Mark all as read, return updated count."""
        conditions = [
            Notification.recipient_type == user_type,
            Notification.recipient_id == user_id,
            self._notification_tenant_condition(tenant_id),
            Notification.is_read.is_(False),
            Notification.is_deleted.is_(False),
        ]
        if category:
            conditions.append(Notification.category == category)

        result = await self.db.execute(
            update(Notification)
            .where(and_(*conditions))
            .values(is_read=True, read_at=utc_now())
        )
        return result.rowcount

    async def delete_notification(
        self,
        notification_id: int,
        user_type: str,
        user_id: int,
        tenant_id: int | None = None,
    ) -> bool:
        """软删除通知 / Soft-delete notification."""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.recipient_type == user_type,
                Notification.recipient_id == user_id,
                self._notification_tenant_condition(tenant_id),
                Notification.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=utc_now())
        )
        return result.rowcount > 0

    # ========================================
    # 内部方法 / Internal methods
    # ========================================

    async def _get_template(
        self,
        code: str,
        tenant_id: int | None = None,
        *,
        fallback_code: str | None = None,
    ) -> NotificationTemplate | None:
        """按租户覆盖 -> 插件/source -> 平台默认查询模板 / Resolve template with tenant fallback."""
        from app.repositories.common.notification_template_repository import (
            NotificationTemplateRepository,
        )

        repo = NotificationTemplateRepository(self.db)
        return await repo.resolve_effective_template(
            code, tenant_id, fallback_code=fallback_code
        )

    @staticmethod
    def _notification_tenant_condition(tenant_id: int | None):
        if tenant_id == PLATFORM_TENANT_ID:
            return or_(
                Notification.tenant_id.is_(None),
                Notification.tenant_id == PLATFORM_TENANT_ID,
            )
        if tenant_id is None:
            return Notification.tenant_id.is_(None)
        return Notification.tenant_id == tenant_id

    @staticmethod
    def _model_int_id(model: object) -> int | None:
        value = getattr(model, "id", None)
        return value if isinstance(value, int) else None

    async def _record_delivery_status(
        self,
        *,
        template: NotificationTemplate,
        channel_code: str,
        user_type: str,
        user_id: int,
        tenant_id: int | None,
        status: str,
        last_error: str | None = None,
        dedupe_key: str | None = None,
    ) -> NotificationDelivery:
        if dedupe_key:
            existing = await self._find_delivery_by_dedupe(
                template=template,
                channel_code=channel_code,
                user_type=user_type,
                user_id=user_id,
                tenant_id=tenant_id,
                dedupe_key=dedupe_key,
            )
            if existing is not None:
                existing._notification_intent_new = False
                return existing
        delivery = NotificationDelivery(
            template_id=self._model_int_id(template),
            template_code=template.code,
            dedupe_key=dedupe_key,
            channel=channel_code,
            recipient_type=user_type,
            recipient_id=user_id,
            tenant_id=tenant_id,
            status=status,
            attempt=0,
            last_error=last_error,
            delivered_at=utc_now() if status in {"sent", "skipped"} else None,
        )
        if not dedupe_key:
            self.db.add(delivery)
            await self.db.flush()
            delivery._notification_intent_new = True
            return delivery

        # 中文: 在保存点前暴露调用方写入错误，不能将其误判为投递去重竞争。
        # EN: Surface caller write errors before the savepoint, never as a delivery dedupe race.
        await self.db.flush()
        try:
            async with self.db.begin_nested():
                self.db.add(delivery)
                await self.db.flush()
        except IntegrityError:
            existing = await self._find_delivery_by_dedupe(
                template=template,
                channel_code=channel_code,
                user_type=user_type,
                user_id=user_id,
                tenant_id=tenant_id,
                dedupe_key=dedupe_key,
            )
            if existing is None:
                raise
            existing._notification_intent_new = False
            return existing
        delivery._notification_intent_new = True
        return delivery

    async def _find_delivery_by_dedupe(
        self,
        *,
        template: NotificationTemplate,
        channel_code: str,
        user_type: str,
        user_id: int,
        tenant_id: int | None,
        dedupe_key: str,
    ) -> NotificationDelivery | None:
        conditions = [
            NotificationDelivery.template_code == template.code,
            NotificationDelivery.channel == channel_code,
            NotificationDelivery.recipient_type == user_type,
            NotificationDelivery.recipient_id == user_id,
            NotificationDelivery.dedupe_key == dedupe_key,
            NotificationDelivery.is_deleted.is_(False),
        ]
        if tenant_id is None:
            conditions.append(NotificationDelivery.tenant_id.is_(None))
        else:
            conditions.append(NotificationDelivery.tenant_id == tenant_id)
        result = await self.db.execute(select(NotificationDelivery).where(*conditions))
        return result.scalar_one_or_none()

    async def _prepare_delivery_record(
        self,
        *,
        template: NotificationTemplate,
        channel_code: str,
        user_type: str,
        user_id: int,
        tenant_id: int | None,
        dedupe_key: str | None = None,
    ) -> tuple[NotificationDelivery, bool]:
        if dedupe_key:
            existing = await self._find_delivery_by_dedupe(
                template=template,
                channel_code=channel_code,
                user_type=user_type,
                user_id=user_id,
                tenant_id=tenant_id,
                dedupe_key=dedupe_key,
            )
            if existing is not None:
                return existing, False
        delivery = await self._create_delivery_record(
            template=template,
            channel_code=channel_code,
            user_type=user_type,
            user_id=user_id,
            tenant_id=tenant_id,
            dedupe_key=dedupe_key,
        )
        return delivery, bool(getattr(delivery, "_notification_intent_new", False))

    async def _create_delivery_record(
        self,
        *,
        template: NotificationTemplate,
        channel_code: str,
        user_type: str,
        user_id: int,
        tenant_id: int | None,
        dedupe_key: str | None = None,
    ) -> NotificationDelivery:
        delivery = await self._record_delivery_status(
            template=template,
            channel_code=channel_code,
            user_type=user_type,
            user_id=user_id,
            tenant_id=tenant_id,
            status="pending",
            dedupe_key=dedupe_key,
        )
        await self.db.flush()
        return delivery

    async def _finalize_delivery_record(
        self,
        delivery: NotificationDelivery,
        *,
        delivered: bool,
        created_notifications: list[Notification],
    ) -> None:
        await self.db.flush()

        if created_notifications:
            delivery.notification_id = self._model_int_id(created_notifications[0])

        if delivery.status == "pending" and delivery.payload_ciphertext:
            return
        if delivery.status == "pending":
            delivery.attempt = (delivery.attempt or 0) + 1
            delivery.status = "sent" if delivered else "failed"
            delivery.last_error = None if delivered else "channel_deliver_failed"
            if delivered:
                delivery.delivered_at = utc_now()
        elif delivered and delivery.status == "queued":
            delivery.last_error = None
        elif delivery.status == "skipped" and delivery.delivered_at is None:
            delivery.delivered_at = utc_now()

        await self.db.flush()

    async def _notifications_enabled(self) -> bool:
        """检查通知系统总开关 / Check notification master switch."""
        from app.configs.service import ConfigService

        return bool(
            await ConfigService(self.db).get_platform_config(
                "notification_enabled", default=True
            )
        )

    async def _channel_is_enabled(self, code: str) -> bool:
        """中文: 配置读取使用当前会话，不借用其他事件循环的连接池。 EN: Read settings in the current session, never another loop's pool."""
        from app.configs.service import ConfigService

        config = ConfigService(self.db)
        if code in {"inbox", "ws"}:
            return await self._notifications_enabled()
        if code == "email":
            return bool(
                await config.get_platform_config("email_enabled", default=False)
            )
        if code == "webhook":
            enabled = await config.get_platform_config("webhook_enabled", default=False)
            url = await config.get_platform_config("webhook_url", default="")
            return (
                enabled is True
                and isinstance(url, str)
                and url.startswith(("http://", "https://"))
            )
        return False

    async def _get_preference(
        self,
        user_type: str,
        user_id: int,
        category: str,
        tenant_id: int = PLATFORM_TENANT_ID,
    ) -> dict[str, bool]:
        """查询用户通知偏好（个人 -> 全局 -> 硬编码默认） / Get user notification preference (user -> global -> default)."""
        result = await self.db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_type == user_type,
                NotificationPreference.user_id == user_id,
                NotificationPreference.tenant_id == tenant_id,
                NotificationPreference.category == category,
            )
        )
        pref = result.scalar_one_or_none()
        if pref:
            return {
                "channel_ws": pref.channel_ws,
                "channel_email": pref.channel_email,
                "channel_inbox": pref.channel_inbox,
            }

        global_type_map = {"admin": "platform_global", "tenant_admin": "tenant_global"}
        global_user_type = global_type_map.get(user_type)
        if global_user_type:
            gl_result = await self.db.execute(
                select(NotificationPreference).where(
                    NotificationPreference.user_type == global_user_type,
                    NotificationPreference.tenant_id == tenant_id,
                    NotificationPreference.user_id.is_(None),
                    NotificationPreference.category == category,
                )
            )
            gl_pref = gl_result.scalar_one_or_none()
            if gl_pref:
                return {
                    "channel_ws": gl_pref.channel_ws,
                    "channel_email": gl_pref.channel_email,
                    "channel_inbox": gl_pref.channel_inbox,
                }

        return {"channel_ws": True, "channel_email": False, "channel_inbox": True}

    async def _enforce_max_per_user(
        self,
        recipient_type: str,
        recipient_id: int,
        tenant_id: int | None = None,
    ) -> None:
        """
        执行每用户最大通知数限制 / Enforce max notifications per user.
        超出时物理删除最早的已读通知。 / When exceeded, physically delete oldest read notifications.
        """
        try:
            from app.configs.service import ConfigService

            max_per_user = await ConfigService(self.db).get_platform_config(
                "notification_max_per_user", default=500
            )
            if not max_per_user:
                return

            max_per_user = int(max_per_user)

            # 查询当前通知总数 / Query current notification total
            count_q = select(func.count(Notification.id)).where(
                Notification.recipient_type == recipient_type,
                Notification.recipient_id == recipient_id,
                self._notification_tenant_condition(tenant_id),
                Notification.is_deleted.is_(False),
            )
            total = (await self.db.execute(count_q)).scalar() or 0

            if total <= max_per_user:
                return

            # 超出限制，删除最早的已读通知（优先清理已读的） / Over limit: drop oldest read notifications first
            overflow = total - max_per_user
            oldest_q = (
                select(Notification.id)
                .where(
                    Notification.recipient_type == recipient_type,
                    Notification.recipient_id == recipient_id,
                    self._notification_tenant_condition(tenant_id),
                    Notification.is_deleted.is_(False),
                    Notification.is_read.is_(True),
                )
                .order_by(Notification.created_at.asc())
                .limit(overflow)
            )
            oldest_ids = [row[0] for row in (await self.db.execute(oldest_q)).all()]

            if oldest_ids:
                await self.db.execute(
                    update(Notification)
                    .where(Notification.id.in_(oldest_ids))
                    .values(is_deleted=True, deleted_at=utc_now())
                )
                logger.info(
                    "Notification overflow cleanup: type={} user_id={} deleted={}",
                    recipient_type,
                    recipient_id,
                    len(oldest_ids),
                )
        except Exception as e:
            logger.warning("_enforce_max_per_user failed: {}", str(e))

    @staticmethod
    def _render_template(template: str | None, data: dict[str, Any] | None) -> str:
        """渲染模板变量 / Render template variables."""
        if not template:
            return ""
        if not data:
            return template
        try:
            return template.format_map(data)
        except (KeyError, ValueError):
            return template

    @classmethod
    def _render_soft_quota_email_body(
        cls,
        template: NotificationTemplate,
        data: dict[str, Any] | None,
    ) -> str | None:
        """Use display labels in built-in quota email while preserving channel data."""
        if (
            template.code != "ai.soft_quota_exceeded"
            or template.category != "ai"
            or not data
            or not cls._matches_default_copy(template, _SOFT_QUOTA_DEFAULT_COPY)
        ):
            return None
        period = data.get("period")
        if not isinstance(period, str) or period not in _SOFT_QUOTA_EMAIL_PERIOD_LABELS:
            return None
        return cls._render_template(
            template.body_template,
            data | {"period": _SOFT_QUOTA_EMAIL_PERIOD_LABELS[period]},
        )

    @staticmethod
    def _matches_default_copy(
        template: NotificationTemplate, original: tuple[str, str]
    ) -> bool:
        return (
            template.scope == "platform"
            and template.tenant_id in (None, PLATFORM_TENANT_ID)
            and template.source == "core"
            and template.is_system
            and template.override_of is None
            and not template.plugin_name
            and (template.title_template, template.body_template) == original
        )

    @classmethod
    def _render_template_content(
        cls,
        template: NotificationTemplate,
        data: dict[str, Any] | None,
        *,
        default_copy: DefaultTemplateCopy | None = None,
    ) -> tuple[str, str | None]:
        """渲染模板标题和正文 / Render template title and body."""
        title_template, body_template = template.title_template, template.body_template
        if default_copy is not None and cls._matches_default_copy(
            template, default_copy.original
        ):
            title_template, body_template = default_copy.replacement
        title = cls._render_template(title_template, data)
        body = cls._render_template(body_template, data)
        return title, body or None

    @staticmethod
    def _derive_category(template_code: str) -> str:
        if "." in template_code:
            return template_code.split(".", 1)[0]
        return "system"


# ============================================
# 便捷函数 / Convenience functions
# ============================================


async def notify(
    db,
    template_code: str,
    recipients: list[tuple[str, int]],
    data: dict[str, Any] | None = None,
    link: str | None = None,
    tenant_id: int | None = None,
    **kwargs: Any,
) -> int:
    """
    异步便捷函数 — 一行投递通知

    用于 Controller / Service 等异步上下文。
    调用方仍需 await db.commit() 来持久化收件箱记录。

    示例::

        await notify(
            db,
            "ai.soft_quota_exceeded",
            [("tenant_admin", 5)],
            {"current": 120000, "limit": 100000, "period": "monthly"},
        )
    """
    service = NotificationService(db)
    return await service.send(
        template_code=template_code,
        recipients=recipients,
        data=data,
        link=link,
        tenant_id=tenant_id,
        **kwargs,
    )


def notify_sync(
    template_code: str,
    recipients: list[tuple[str, int]],
    data: dict[str, Any] | None = None,
    link: str | None = None,
    tenant_id: int | None = None,
    **kwargs: Any,
) -> int:
    """
    同步便捷函数 — 用于 Celery 任务等同步上下文 / Sync helper for Celery etc.

    内部创建同步事件循环和 DB session。

    示例::

        notify_sync("system.task_failure", [("admin", 1)], {"error": "timeout"})
    """
    import asyncio

    from app.services.common.notification_task_runtime import run_notification_task

    async def plan(sessions) -> int:
        async with sessions() as db:
            service = NotificationService(db)
            count = await service.send(
                template_code=template_code,
                recipients=recipients,
                data=data,
                link=link,
                tenant_id=tenant_id,
                **kwargs,
            )
            await db.commit()
            return count

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_notification_task(plan)

    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(copy_context().run, run_notification_task, plan).result()


def build_ws_payload_sync(
    template_code: str,
    data: dict[str, Any] | None = None,
    link: str | None = None,
    *,
    fallback_category: str | None = None,
    fallback_title: str | None = None,
    fallback_body: str | None = None,
    fallback_priority: str = "normal",
    tenant_id: int | None = None,
) -> dict[str, Any] | None:
    """
    同步便捷函数 — 用于 Celery 等同步上下文构建 WS payload。
    Sync helper for building WS payloads from synchronous contexts.
    """
    import asyncio
    import contextvars

    from app.core.database import async_session_factory

    async def _run() -> dict[str, Any] | None:
        async with async_session_factory() as db:
            service = NotificationService(db)
            return await service.build_ws_payload(
                template_code=template_code,
                data=data,
                link=link,
                fallback_category=fallback_category,
                fallback_title=fallback_title,
                fallback_body=fallback_body,
                fallback_priority=fallback_priority,
                tenant_id=tenant_id,
            )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            ctx = contextvars.copy_context()

            def _run_in_thread() -> dict[str, Any] | None:
                return ctx.run(lambda: asyncio.run(_run()))

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_run_in_thread)
                return future.result(timeout=30)
        return loop.run_until_complete(_run())
    except RuntimeError:
        return asyncio.run(_run())


__all__ = [
    "NotificationService",
    "notify",
    "notify_sync",
    "build_ws_payload_sync",
]
