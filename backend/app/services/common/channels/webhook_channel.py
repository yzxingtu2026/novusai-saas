"""
Webhook 通知渠道 / Webhook Notification Channel

用于对接企业微信、钉钉、Slack 等外部 Webhook 通知。
For integrating with WeCom, DingTalk, Slack and other external Webhook notifications.
通过 HTTP POST 将通知投递到配置的 URL。
Delivers notifications via HTTP POST to the configured URL.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress_url_policy import request_public_http
from app.core.logging import LogManager
from app.services.common.channels.base import NotificationChannel

logger = LogManager.get_logger("app")


class WebhookChannel(NotificationChannel):
    """Webhook 渠道 — 企业微信/钉钉/Slack 等 / Webhook channel — WeCom/DingTalk/Slack."""

    @property
    def channel_code(self) -> str:
        return "webhook"

    @property
    def channel_name(self) -> str:
        return "Webhook"

    async def is_enabled(self) -> bool:
        """检查 Webhook 是否启用且 URL 已配置 / Check if webhook is enabled and URL configured."""
        try:
            from app.sio.ws_config import get_ws_configs

            cfg = await get_ws_configs("webhook_enabled", "webhook_url")
            enabled = cfg.get("webhook_enabled") is True
            url = cfg.get("webhook_url") or ""
            if isinstance(url, str):
                url = url.strip()
            return bool(enabled and url.startswith(("http://", "https://")))
        except Exception as e:
            logger.warning("WebhookChannel is_enabled check failed: {}", str(e))
            return False

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
        """通过 HTTP POST 投递通知到 Webhook URL / Deliver notification via HTTP POST to webhook URL."""
        _ = db
        try:
            if kwargs.get("_worker_write_only"):
                url = kwargs.get("_prepared_webhook_url") or ""
            else:
                from app.sio.ws_config import get_ws_configs

                cfg = await get_ws_configs("webhook_url")
                url = (cfg.get("webhook_url") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                logger.debug("WebhookChannel: no valid URL configured, skip")
                return False

            payload = {
                "title": title,
                "body": body,
                "user_type": user_type,
                "user_id": user_id,
                "priority": priority,
                "template_code": template_code,
                "tenant_id": tenant_id,
                "link": link,
                "data": data or {},
            }

            resp = await request_public_http(
                "POST", url, json=payload, timeout=10.0, deadline_seconds=30.0
            )

            if resp.is_success:
                logger.debug(
                    "WebhookChannel delivered: template={} status={}",
                    template_code,
                    resp.status_code,
                )
                return True

            logger.warning(
                "WebhookChannel POST failed: status={}",
                resp.status_code,
            )
            return False
        except Exception as e:
            logger.warning("WebhookChannel deliver failed: type={}", type(e).__name__)
            return False


__all__ = ["WebhookChannel"]
