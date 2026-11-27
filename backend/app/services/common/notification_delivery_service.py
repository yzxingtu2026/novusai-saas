"""中文: 加密通知意图及保守外发恢复。 EN: Encrypted notification intent and conservative dispatch recovery."""

import asyncio
import json

from sqlalchemy.exc import SQLAlchemyError

from app.configs.service import ConfigService
from app.core.i18n import get_locale, set_locale
from app.core.logging import LogManager
from app.core.security import decrypt_data, encrypt_data
from app.repositories.common.notification_delivery_repository import (
    EXTERNAL_CHANNELS,
    TASK_FAILURE_RECIPIENT,
    NotificationDeliveryClaimRepository,
    NotificationDeliveryRepository,
)
from app.services.common.notification_delivery_dispatch import (
    schedule_notification_delivery_after_commit,
)

logger = LogManager.get_logger("app")


async def prepare_external_delivery(
    db,
    delivery,
    *,
    user_type,
    user_id,
    title,
    body,
    data,
    link,
    priority,
    template_code,
    tenant_id=None,
    force=False,
    category=None,
    configured_task_email=None,
    **kwargs,
):
    """中文: 只在调用方事务保存意图，不接触 broker 或网络。 EN: Persist intent in the caller transaction without broker/network I/O."""
    if delivery.channel not in EXTERNAL_CHANNELS:
        raise ValueError("notification_channel_unsupported")
    if (
        delivery.recipient_type,
        delivery.recipient_id,
        delivery.tenant_id,
        delivery.template_code,
    ) != (user_type, user_id, tenant_id, template_code):
        raise ValueError("notification_delivery_scope_mismatch")
    if delivery.is_deleted is True or delivery.status != "pending":
        return False
    if delivery.payload_ciphertext:
        schedule_notification_delivery_after_commit(db, delivery.id)
        return True
    repo = NotificationDeliveryRepository(db)
    try:
        email = await repo.delivery_email(
            delivery, expected_email=configured_task_email
        )
    except ValueError as error:
        if (
            user_type != TASK_FAILURE_RECIPIENT
            or str(error) != "notification_recipient_changed"
        ):
            raise
        delivery.status = "skipped"
        delivery.last_error = "notification_recipient_changed"
        return False
    payload = {
        "version": 1,
        "channel": delivery.channel,
        "user_type": user_type,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "title": title,
        "body": body,
        "data": data,
        "link": link,
        "priority": priority,
        "template_code": template_code,
        "category": category or template_code.split(".")[0],
        "force": bool(force),
        "locale": get_locale(),
    }
    config = ConfigService(db)
    if delivery.channel == "email":
        from app.services.common.email_service import _is_valid_email
        from app.services.common.email_templates import render_notification_html

        if (
            tenant_id
            and await config.get_tenant_config(tenant_id, "tenant_email_notification")
            is False
        ):
            delivery.status = "skipped"
            delivery.last_error = "tenant_email_notification_disabled"
            return False
        if not isinstance(email, str) or not _is_valid_email(email):
            delivery.status = "skipped"
            delivery.last_error = "recipient_email_missing"
            return False
        html = kwargs.get("email_html")
        text = kwargs.get("email_text")
        if not html:
            html, text = render_notification_html(
                title=title,
                body=kwargs.get("email_render_body", body),
                priority=priority,
                link=link,
            )
        payload.update(
            email=email,
            email_subject=kwargs.get("email_subject") or title,
            email_html=html,
            email_text=text,
        )
    elif delivery.channel == "webhook":
        url = await config.get_platform_config("webhook_url", default="")
        if not isinstance(url, str) or not url.strip().startswith(
            ("http://", "https://")
        ):
            delivery.status = "skipped"
            delivery.last_error = "webhook_destination_missing"
            return False
        payload["webhook_url"] = url.strip()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise ValueError("notification_payload_too_large")
    delivery.payload_ciphertext = encrypt_data(encoded)
    delivery.status = "pending"
    delivery.available_at = await repo.database_now()
    await db.flush()
    schedule_notification_delivery_after_commit(db, delivery.id)
    return True


class NotificationDeliveryService:
    """中文: 外部调用期间不持有数据库事务。 EN: No database transaction stays open across external I/O."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.claims = NotificationDeliveryClaimRepository(sessions)

    async def _validate(self, claim, payload):
        from app.services.common.email_service import EmailService
        from app.services.common.notification_service import NotificationService

        task_recipient = claim.recipient_type == TASK_FAILURE_RECIPIENT
        async with self.sessions() as db:
            email = await NotificationDeliveryRepository(db).delivery_email(
                claim, expected_email=payload.get("email") if task_recipient else None
            )
            config = ConfigService(db)
            if not await config.get_platform_config(
                "notification_enabled", default=True
            ):
                raise ValueError("notification_disabled")
            planner = NotificationService(db)
            template = await planner._get_template(claim.template_code, claim.tenant_id)
            if template is None:
                raise ValueError("notification_template_unavailable")
            if task_recipient and "email" not in (template.channels or ()):
                raise ValueError("notification_channel_disabled")
            if task_recipient or not payload["force"]:
                if not task_recipient:
                    pref = await planner._get_preference(
                        claim.recipient_type,
                        claim.recipient_id,
                        payload["category"],
                        tenant_id=claim.tenant_id or 0,
                    )
                    if not pref.get("channel_" + claim.channel, claim.channel == "ws"):
                        raise ValueError("notification_preference_disabled")
                if not await planner._channel_is_enabled(claim.channel):
                    raise ValueError("notification_channel_disabled")
            if claim.channel == "email":
                if email != payload.get("email"):
                    raise ValueError("notification_recipient_changed")
                if (
                    claim.tenant_id
                    and await config.get_tenant_config(
                        claim.tenant_id, "tenant_email_notification"
                    )
                    is False
                ):
                    raise ValueError("tenant_email_notification_disabled")
                smtp = await EmailService(db)._load_smtp_config()
                if not smtp.enabled:
                    raise ValueError("email_disabled")
                if not smtp.host or not smtp.from_address:
                    raise ValueError("email_config_incomplete")
                return smtp
            if claim.channel == "webhook":
                url = await config.get_platform_config("webhook_url", default="")
                if not isinstance(url, str) or url.strip() != payload.get(
                    "webhook_url"
                ):
                    raise ValueError("notification_destination_changed")
        return None

    async def _prepare(self, claim, payload):
        from app.services.common.email_service import EmailMessage, EmailService

        smtp = await self._validate(claim, payload)
        if claim.channel != "email":
            return None
        message = EmailMessage(
            to=[payload["email"]],
            subject=payload["email_subject"],
            html_body=payload.get("email_html"),
            text_body=payload.get("email_text"),
        )
        return smtp, EmailService._build_mime_message(message, smtp)

    async def _mark_submitting(self, claim, payload, prepared):
        # 中文: SMTP 握手可能耗时；提交前重新读取边界且不持锁等待网络。
        # EN: SMTP negotiation may be slow; re-read boundaries before committing the fence.
        smtp = await self._validate(claim, payload)
        if prepared is not None and smtp != prepared[0]:
            raise ValueError("notification_destination_changed")
        await self.claims.mark_submitting(claim, email=payload.get("email"))

    async def _finish(self, claim, status, error=None, *, retryable=False):
        try:
            recorded = await self.claims.finish(
                claim, status, error, retryable=retryable
            )
        except SQLAlchemyError:
            recorded = False
        if not recorded:
            # 中文: 仅任务诊断结果；不创造投递状态，也不重发可能已受理的内容。
            # EN: Task diagnostics only, never a delivery state or authorization to resend.
            logger.warning(
                "Notification finalization unconfirmed: delivery_id={} outcome={}",
                claim.delivery_id,
                status,
            )
            return "sent_unrecorded" if status == "sent" else "finalization_unconfirmed"
        return "pending" if retryable and claim.attempt < 3 else status

    @staticmethod
    def _payload(claim):
        payload = json.loads(decrypt_data(claim.payload_ciphertext))
        expected = {
            "version": 1,
            "channel": claim.channel,
            "tenant_id": claim.tenant_id,
            "user_type": claim.recipient_type,
            "user_id": claim.recipient_id,
            "template_code": claim.template_code,
        }
        if (
            not isinstance(payload, dict)
            or any(payload.get(key) != value for key, value in expected.items())
            or type(payload.get("force")) is not bool
        ):
            raise ValueError("notification_payload_invalid")
        if claim.channel not in EXTERNAL_CHANNELS:
            raise ValueError("notification_channel_unsupported")
        return payload

    async def dispatch(self, delivery_id):
        """中文: 只接受持久记录 ID；失联提交不得自动重发。 EN: Accept durable IDs only; never retry uncertain submissions."""
        claim = await self.claims.claim(delivery_id)
        if claim is None:
            return "not_claimed"
        try:
            payload = self._payload(claim)
        except Exception:
            return await self._finish(claim, "failed", "notification_payload_invalid")
        previous_locale = get_locale()
        set_locale(payload.get("locale", "zh_CN"))
        submitted = False
        try:
            prepared = await self._prepare(claim, payload)
            if claim.channel == "email":
                from app.services.common.email_service import send_prepared_email

                loop = asyncio.get_running_loop()
                submission_rejection = None

                def before_submit():
                    nonlocal submitted, submission_rejection
                    # 中文: 此 future 返回前提交必须完成；异常会阻止 sendmail。
                    # EN: Commit must complete before this future returns; errors stop sendmail.
                    future = asyncio.run_coroutine_threadsafe(
                        self._mark_submitting(claim, payload, prepared), loop
                    )
                    try:
                        future.result(timeout=10)
                        submitted = True
                    except ValueError as error:
                        submission_rejection = error
                        future.cancel()
                        raise
                    except BaseException:
                        future.cancel()
                        raise

                result = await asyncio.to_thread(
                    send_prepared_email,
                    prepared[0],
                    prepared[1],
                    [payload["email"]],
                    before_submit,
                )
                if submission_rejection is not None:
                    raise submission_rejection
                state = (
                    "sent"
                    if result.success
                    else (
                        "uncertain"
                        if result.submission_state == "uncertain"
                        else "failed"
                    )
                )
                return await self._finish(
                    claim,
                    state,
                    None if result.success else result.message,
                    retryable=result.retryable,
                )
            from app.services.common.channels import get_channel

            await self._mark_submitting(claim, payload, prepared)
            submitted = True
            channel = get_channel(claim.channel)
            if channel is None:
                raise ValueError("notification_channel_unsupported")
            delivered = await channel.deliver(
                db=None,
                user_type=claim.recipient_type,
                user_id=claim.recipient_id,
                title=payload["title"],
                body=payload.get("body"),
                data=payload.get("data"),
                link=payload.get("link"),
                priority=payload["priority"],
                template_code=claim.template_code,
                tenant_id=claim.tenant_id,
                _worker_write_only=True,
                _prepared_webhook_url=payload.get("webhook_url"),
            )
            state = "sent" if delivered else "uncertain"
            return await self._finish(
                claim, state, None if delivered else "external_acceptance_unknown"
            )
        except ValueError as error:
            safe_errors = {
                "notification_recipient_invalid",
                "notification_recipient_scope_mismatch",
                "notification_recipient_changed",
                "notification_disabled",
                "notification_template_unavailable",
                "notification_preference_disabled",
                "notification_channel_disabled",
                "tenant_email_notification_disabled",
                "email_disabled",
                "email_config_incomplete",
                "notification_destination_changed",
                "notification_lease_lost",
                "notification_channel_unsupported",
            }
            code = (
                str(error)
                if str(error) in safe_errors
                else "notification_preparation_invalid"
            )
            return await self._finish(
                claim, "uncertain" if submitted else "skipped", code
            )
        except Exception:
            return await self._finish(
                claim,
                "uncertain" if submitted else "failed",
                "external_acceptance_unknown"
                if submitted
                else "notification_preparation_unavailable",
                retryable=not submitted,
            )
        finally:
            set_locale(previous_locale)

    async def recover(self, limit=20):
        ids = await self.claims.due_ids(limit)
        outcomes = {}
        for delivery_id in ids:
            status = await self.dispatch(delivery_id)
            outcomes[status] = outcomes.get(status, 0) + 1
        return {"processed": len(ids), "outcomes": outcomes}
