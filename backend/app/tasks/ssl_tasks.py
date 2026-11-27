"""
SSL certificate Celery tasks / SSL 证书 Celery 任务

Contains 3 tasks: / 包含 3 个任务：
1. task_provision_ssl - Auto-trigger ACME issuance after domain verification (default queue, one-time)
   域名验证后自动触发 ACME 签发（default 队列，一次性）
2. task_check_ssl_renewals - Daily inspection of expiring certificates (scheduled queue, Beat cron)
   每日巡检即将过期证书（scheduled 队列，Beat cron）
3. task_renew_ssl - Single certificate renewal (default queue, triggered by inspection)
   单个证书续期（default 队列，由巡检触发）

Note: In Celery Worker (Windows --pool=solo), asyncio.new_event_loop() between retries
invalidates the module-level async_session_factory's event loop.
Therefore, an independent async engine + session must be created on each task call.
注意：Celery Worker (Windows --pool=solo) 中，asyncio.new_event_loop() 在 retries 之间
会导致模块级 async_session_factory 绑定的 event loop 失效。
因此必须在每次任务调用时创建独立的 async engine + session。
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_model import utc_now
from app.core.config import settings
from app.core.logging import LogManager
from app.enums.error_code import ErrorCode
from app.exceptions import BusinessException
from app.tasks.async_db import task_async_session as _task_async_session
from app.tasks.base import BaseTask, register_task

logger = LogManager.get_logger("ssl")


class SslInspectionError(RuntimeError):
    """A partially blocked inspection must activate failure monitoring."""

    def __init__(self, stats: dict):
        self.stats = stats
        super().__init__(f"SSL inspection incomplete: {stats['errors']} error(s)")

    def __reduce__(self):
        return (type(self), (self.stats,))


async def _build_acme_client(db, ssl_service):
    from app.configs.service import ConfigService
    from app.services.system.acme_client import AcmeClient

    config = ConfigService(db)
    staging = await config.get_platform_config(
        "acme_use_staging", default=settings.ACME_USE_STAGING
    )
    url_key = "acme_staging_url" if staging else "acme_directory_url"
    default_url = settings.ACME_STAGING_URL if staging else settings.ACME_DIRECTORY_URL
    url = await config.get_platform_config(url_key, default=default_url)
    email = await config.get_platform_config(
        "acme_account_email", default=settings.ACME_ACCOUNT_EMAIL
    )
    key = (
        await ssl_service._get_encryption_key()
        if settings.ACME_ACCOUNT_KEY_DIRECTORY
        else None
    )
    return AcmeClient(
        directory_url=url or default_url,
        account_email=email or None,
        use_staging=staging,
        account_encryption_key=key,
    )


@register_task(
    queue="default",
    description="ACME SSL certificate issuance (auto-triggered after domain verification) / ACME SSL 证书签发（域名验证后自动触发）",
    max_retries=5,
    default_retry_delay=120,
)
def task_provision_ssl(
    self: BaseTask, domain_id: int, *, expected_certificate_id: int | None = None
) -> dict:
    """
    Async SSL certificate issuance / 异步签发 SSL 证书

    Triggered by celery_app.send_task() after domain verification succeeds.
    由域名验证成功后 celery_app.send_task() 触发
    Uses default queue, one-time immediate task.
    走 default 队列，一次性即时任务
    """

    async def _provision() -> dict:
        from sqlalchemy import select

        from app.models.tenant.tenant_domain import TenantDomain
        from app.services.system.ssl_certificate_service import SslCertificateService
        from app.services.system.tenant_domain_service import TenantDomainService

        async with _task_async_session() as db:
            try:
                # 1. Get domain info / 获取域名信息
                result = await db.execute(
                    select(TenantDomain).where(
                        TenantDomain.id == domain_id,
                        TenantDomain.is_deleted.is_(False),
                    )
                )
                domain = result.scalar_one_or_none()

                if not domain:
                    logger.error("Domain {} not found for SSL provisioning", domain_id)
                    return {"error": "domain_not_found", "domain_id": domain_id}

                if not domain.is_verified:
                    logger.warning("Domain {} not verified, skipping SSL", domain_id)
                    return {"error": "domain_not_verified", "domain_id": domain_id}

                domain_service = TenantDomainService(db)
                if await domain_service._is_default_domain(domain):
                    await domain_service.ensure_custom_domain_entitled(
                        domain.tenant_id, domain
                    )
                    return {"status": "managed_wildcard", "domain_id": domain_id}
                await domain_service.ensure_custom_domain_entitled(
                    domain.tenant_id, domain
                )

                logger.info(
                    "Starting SSL provisioning for domain {} (id={})",
                    domain.domain,
                    domain_id,
                )

                # 3. Build DNS provider + ACME client issuance / 构建 DNS 提供商 + ACME 客户端签发
                from app.services.system.dns_provider import get_dns_provider

                dns_provider = await get_dns_provider(db)
                ssl_service = SslCertificateService(db)
                acme = await _build_acme_client(db, ssl_service)
                # End preflight reads before waiting on DNS/CA network traffic.
                domain_name, tenant_id = domain.domain, domain.tenant_id
                await db.commit()
                cert_pem, key_pem, chain_pem = await acme.provision_certificate(
                    domain_name,
                    dns_setter=dns_provider.set_txt_record,
                    dns_deleter=dns_provider.delete_txt_record,
                )

                # 4. Store certificate / 存储证书
                ssl_service = SslCertificateService(db)
                cert = await ssl_service.store_platform_cert(
                    domain_id=domain_id,
                    tenant_id=tenant_id,
                    cert_pem=cert_pem,
                    key_pem=key_pem,
                    chain_pem=chain_pem,
                    expected_domain=domain_name,
                    expected_certificate_id=expected_certificate_id,
                )

                await db.commit()
                if cert is None:
                    return {"domain_id": domain_id, "status": "superseded"}

                logger.info(
                    "SSL certificate issued for {}, expires {}",
                    domain.domain,
                    cert.expires_at,
                )
                return {
                    "domain_id": domain_id,
                    "domain": domain.domain,
                    "cert_id": cert.id,
                    "expires_at": cert.expires_at.isoformat()
                    if cert.expires_at
                    else None,
                    "status": "active",
                }

            except Exception as exc:
                await db.rollback()

                # dns_setter missing: config issue, no retry, WARNING only / dns_setter 缺失：配置问题，不重试，仅 WARNING
                from app.services.system.acme_client import AcmeDnsSetterMissingError

                if isinstance(exc, AcmeDnsSetterMissingError):
                    logger.warning(
                        "SSL provisioning skipped for domain {}: {}",
                        domain_id,
                        str(exc),
                    )
                    async with _task_async_session() as db2:
                        await SslCertificateService(db2).mark_provision_failed(
                            domain_id, str(exc)
                        )
                        await db2.commit()
                    return {
                        "error": "dns_setter_missing",
                        "domain_id": domain_id,
                        "message": str(exc),
                    }

                if (
                    isinstance(exc, BusinessException)
                    and exc.code == ErrorCode.FORBIDDEN
                ):
                    async with _task_async_session() as db2:
                        await SslCertificateService(db2).mark_provision_failed(
                            domain_id, str(exc)
                        )
                        await db2.commit()
                    return {"error": "domain_not_entitled", "domain_id": domain_id}

                if (
                    isinstance(exc, BusinessException)
                    and exc.code == ErrorCode.CONFIG_VALIDATION_FAILED
                ):
                    logger.warning(
                        "SSL provisioning blocked by DNS config for domain {}: {}",
                        domain_id,
                        str(exc),
                    )
                    try:
                        async with _task_async_session() as db2:
                            ssl_service = SslCertificateService(db2)
                            await ssl_service.mark_provision_failed(
                                domain_id,
                                str(exc),
                            )
                            await db2.commit()
                    except Exception as state_exc:
                        logger.error(
                            "Failed to record provisioning failure for {}: {}",
                            domain_id,
                            str(state_exc),
                        )
                    return {
                        "error": "dns_provider_not_ready",
                        "domain_id": domain_id,
                        "message": str(exc),
                    }

                # Other errors: log ERROR + mark failed + retry / 其他错误：记录 ERROR + 标记失败 + 重试
                logger.error(
                    "SSL provisioning failed for domain {}: {}",
                    domain_id,
                    str(exc),
                    exc_info=True,
                )
                try:
                    async with _task_async_session() as db2:
                        ssl_service = SslCertificateService(db2)
                        await ssl_service.mark_provision_failed(
                            domain_id,
                            str(exc),
                        )
                        await db2.commit()
                except Exception as state_exc:
                    logger.error(
                        "Failed to record provisioning failure for {}: {}",
                        domain_id,
                        str(state_exc),
                    )

                raise self.retry(
                    exc=exc,
                    countdown=self.get_retry_countdown() * (self.request.retries + 1),
                ) from exc

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_provision())
    finally:
        loop.close()


@register_task(
    queue="scheduled",
    description="SSL certificate renewal check (daily at 3:00 AM) / SSL 证书续期检查（每日凌晨 3:00 执行）",
    max_retries=1,
)
def task_check_ssl_renewals(self: BaseTask) -> dict:
    """
    Periodically inspect expiring certificates / 定期巡检即将过期的证书

    Uses scheduled queue, triggered by Beat cron.
    走 scheduled 队列，由 Beat cron 触发
    - platform certs: auto_renew=True → trigger task_renew_ssl
    - platform 证书：auto_renew=True → 触发 task_renew_ssl
    - custom certs: record reminder only (no auto-renewal)
    - custom 证书：仅记录提醒（不自动续期）
    - expired certs: mark ssl_status=expired
    - 已过期证书：标记 ssl_status=expired
    """

    async def _check() -> dict:
        from app.repositories.system.ssl_certificate_repository import (
            SslCertificateRepository,
        )
        from app.services.system.ssl_certificate_service import SslCertificateService

        stats = {
            "platform_renewals_triggered": 0,
            "custom_expiring_notified": 0,
            "expired_marked": 0,
            "errors": 0,
        }

        async with _task_async_session() as db:
            try:
                repo = SslCertificateRepository(db)
                ssl_service = SslCertificateService(db)

                # 1. Query expiring platform certs → trigger renewal / 查询即将过期的平台证书 → 触发续期
                from app.configs.service import ConfigService
                from app.services.system.dns_provider import audit_dns_provider_config

                config_svc = ConfigService(db)
                readiness = await audit_dns_provider_config(db)
                renew_days_raw = await config_svc.get_platform_config(
                    "ssl_auto_renew_days",
                    default=30,
                )
                try:
                    renew_days = int(renew_days_raw)
                except (TypeError, ValueError):
                    renew_days = 30

                renew_days = min(60, max(1, renew_days))
                platform_certs = await repo.get_expiring_platform_certs(days=renew_days)
                if readiness["ready"]:
                    for cert in platform_certs:
                        try:
                            domain = await ssl_service.ensure_domain_ssl_entitled(
                                cert.domain_id, cert.tenant_id
                            )
                            from app.services.system.tenant_domain_service import (
                                TenantDomainService,
                            )

                            if await TenantDomainService(db)._is_default_domain(domain):
                                continue
                            task_renew_ssl.delay(cert_id=cert.id, automatic=True)
                            stats["platform_renewals_triggered"] += 1
                            logger.info(
                                "Renewal triggered for cert {} (domain_id={}, expires={})",
                                cert.id,
                                cert.domain_id,
                                cert.expires_at,
                            )
                        except BusinessException as e:
                            stats["errors"] += 1
                            logger.warning(
                                "Skipping ineligible SSL renewal for {}: {}",
                                cert.id,
                                str(e),
                            )
                        except Exception as e:
                            stats["errors"] += 1
                            logger.error(
                                "Failed to trigger renewal for cert {}: {}",
                                cert.id,
                                str(e),
                            )
                elif platform_certs:
                    stats["errors"] += 1
                    logger.warning(
                        "Skipping platform SSL renewal trigger because DNS automation is not ready: {}",
                        readiness["summary"],
                    )

                # 2. Query expiring custom certs → record reminder + email notification / 查询即将过期的自定义证书 → 记录提醒 + 邮件通知
                custom_certs = await repo.get_expiring_custom_certs(days=30)
                for cert in custom_certs:
                    await ssl_service.mark_expiry_warning(
                        cert.id,
                        "Custom certificate expiring soon, manual upload required",
                    )
                    stats["custom_expiring_notified"] += 1
                    logger.info(
                        "Custom cert {} expiring soon (domain_id={}, expires={})",
                        cert.id,
                        cert.domain_id,
                        cert.expires_at,
                    )
                    # Send SSL expiry reminder email (extract associated data in async context) / 发送 SSL 到期提醒邮件（在 async 上下文中提取关联数据）
                    notify_email = await _get_cert_notify_email(db, cert.domain_id)
                    if notify_email:
                        _send_ssl_expiry_email(
                            domain_name=notify_email["domain"],
                            expires_at=cert.expires_at,
                            contact_email=notify_email["email"],
                            tenant_id=notify_email["tenant_id"],
                        )

                # 3. Query expired certs → mark expired / 查询已过期证书 → 标记 expired
                expired_certs = await repo.get_expired_certs()
                for cert in expired_certs:
                    try:
                        await ssl_service.mark_expired(cert.id, cert.domain_id)
                        stats["expired_marked"] += 1
                        logger.info(
                            "Cert {} marked expired (domain_id={})",
                            cert.id,
                            cert.domain_id,
                        )
                    except Exception as e:
                        stats["errors"] += 1
                        logger.error(
                            "Failed to mark cert {} expired: {}",
                            cert.id,
                            str(e),
                        )

                await db.commit()

            except Exception as e:
                await db.rollback()
                logger.error("SSL renewal check failed: {}", str(e), exc_info=True)
                stats["errors"] += 1

        logger.info(
            "SSL renewal check completed: {} renewals, {} custom warnings, "
            "{} expired, {} errors",
            stats["platform_renewals_triggered"],
            stats["custom_expiring_notified"],
            stats["expired_marked"],
            stats["errors"],
        )
        if stats["errors"]:
            raise SslInspectionError(stats)
        return stats

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_check())
    finally:
        loop.close()


@register_task(
    queue="default",
    description="Single SSL certificate renewal (triggered by inspection task or manually) / 单个 SSL 证书续期（由巡检任务或手动触发）",
    max_retries=3,
    default_retry_delay=120,
)
def task_renew_ssl(self: BaseTask, cert_id: int, *, automatic: bool = False) -> dict:
    """
    Single platform certificate renewal / 单个平台证书续期

    Uses default queue, triggered by task_check_ssl_renewals or manually from admin/tenant side.
    走 default 队列，由 task_check_ssl_renewals 或管理端/企业端手动触发
    Only platform type certificates can be renewed.
    仅 platform 类型可续期
    """

    async def _renew() -> dict:
        from app.enums.domain import SslCertType
        from app.services.system.ssl_certificate_service import SslCertificateService
        from app.services.system.tenant_domain_service import TenantDomainService

        async with _task_async_session() as db:
            try:
                ssl_service = SslCertificateService(db)

                # 1. Get certificate info / 获取证书信息
                cert = await ssl_service.get_by_id(cert_id)
                if not cert:
                    logger.error("Cert {} not found for renewal", cert_id)
                    return {"error": "cert_not_found", "cert_id": cert_id}
                if automatic and not cert.auto_renew:
                    return {"error": "auto_renew_disabled", "cert_id": cert_id}

                if cert.cert_type != SslCertType.PLATFORM.value:
                    logger.warning(
                        "Cert {} is custom type, cannot auto-renew",
                        cert_id,
                    )
                    return {"error": "custom_cert_no_renew", "cert_id": cert_id}

                # 2. Get domain info / 获取域名信息
                domain = await ssl_service.ensure_domain_ssl_entitled(
                    cert.domain_id, cert.tenant_id
                )
                if await TenantDomainService(db)._is_default_domain(domain):
                    return {"status": "managed_wildcard", "cert_id": cert_id}
                from app.enums.domain import SslCertStatus

                if cert.status not in {
                    SslCertStatus.ACTIVE.value,
                    SslCertStatus.EXPIRED.value,
                }:
                    return {"error": "cert_not_current", "cert_id": cert_id}
                current_cert = await ssl_service.get_cert_detail(cert.domain_id)
                if current_cert and current_cert.id != cert.id:
                    return {"error": "cert_not_current", "cert_id": cert_id}

                logger.info(
                    "Starting SSL renewal for cert {} (domain={})",
                    cert_id,
                    domain.domain,
                )

                # 4. Build DNS provider + ACME re-issuance / 构建 DNS 提供商 + ACME 重新签发
                from app.services.system.dns_provider import get_dns_provider

                dns_provider = await get_dns_provider(db)

                acme = await _build_acme_client(db, ssl_service)
                domain_name, domain_id, tenant_id = (
                    domain.domain,
                    cert.domain_id,
                    cert.tenant_id,
                )
                await db.commit()
                cert_pem, key_pem, chain_pem = await acme.provision_certificate(
                    domain_name,
                    dns_setter=dns_provider.set_txt_record,
                    dns_deleter=dns_provider.delete_txt_record,
                )

                # 5. Store new certificate (auto-deactivates old one) / 存储新证书（会自动停用旧证书）
                new_cert = await ssl_service.store_platform_cert(
                    domain_id=domain_id,
                    tenant_id=tenant_id,
                    cert_pem=cert_pem,
                    key_pem=key_pem,
                    chain_pem=chain_pem,
                    expected_certificate_id=cert_id,
                    require_auto_renew=automatic,
                    expected_domain=domain_name,
                )

                await db.commit()
                if new_cert is None:
                    return {"cert_id": cert_id, "status": "superseded"}

                logger.info(
                    "SSL certificate renewed for {}, new cert {} expires {}",
                    domain.domain,
                    new_cert.id,
                    new_cert.expires_at,
                )
                return {
                    "cert_id": cert_id,
                    "new_cert_id": new_cert.id,
                    "domain": domain.domain,
                    "expires_at": new_cert.expires_at.isoformat()
                    if new_cert.expires_at
                    else None,
                    "status": "renewed",
                }

            except Exception as exc:
                await db.rollback()
                logger.error(
                    "SSL renewal failed for cert {}: {}",
                    cert_id,
                    str(exc),
                    exc_info=True,
                )

                # Record renewal failure (does not change cert status, still valid until expiry) / 记录续期失败（不改变证书状态，仍有效直到过期）
                try:
                    async with _task_async_session() as db2:
                        ssl_service2 = SslCertificateService(db2)
                        await ssl_service2.mark_renewal_failed(cert_id, str(exc))
                        await db2.commit()
                except Exception as state_exc:
                    logger.error(
                        "Failed to record renewal failure for {}: {}",
                        cert_id,
                        str(state_exc),
                    )

                if (
                    isinstance(exc, BusinessException)
                    and exc.code == ErrorCode.FORBIDDEN
                ):
                    return {"error": "domain_not_entitled", "cert_id": cert_id}

                if (
                    isinstance(exc, BusinessException)
                    and exc.code == ErrorCode.CONFIG_VALIDATION_FAILED
                ):
                    logger.warning(
                        "SSL renewal blocked by DNS config for cert {}: {}",
                        cert_id,
                        str(exc),
                    )
                    return {
                        "error": "dns_provider_not_ready",
                        "cert_id": cert_id,
                        "message": str(exc),
                    }

                raise self.retry(
                    exc=exc,
                    countdown=self.get_retry_countdown() * (self.request.retries + 1),
                ) from exc

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_renew())
    finally:
        loop.close()


async def _get_cert_notify_email(db: AsyncSession, domain_id: int) -> dict | None:
    """
    从 domain_id 查询域名与企业联系邮箱（显式查询，避免 ORM 懒加载）/ Query domain + tenant contact email from domain_id (explicit query, avoid ORM lazy loading).

    Returns:
        {"domain": str, "email": str, "tenant_id": int} or None
        {"domain": str, "email": str, "tenant_id": int} 或 None
    """
    from sqlalchemy import select

    from app.models import Tenant
    from app.models.tenant.tenant_domain import TenantDomain

    try:
        result = await db.execute(
            select(
                TenantDomain.domain,
                Tenant.id.label("tenant_id"),
                Tenant.contact_email,
            )
            .join(Tenant, Tenant.id == TenantDomain.tenant_id)
            .where(TenantDomain.id == domain_id)
        )
        row = result.first()
        if row and row.contact_email:
            return {
                "domain": row.domain,
                "email": row.contact_email,
                "tenant_id": row.tenant_id,
            }
    except Exception as e:
        logger.warning("Failed to query cert notify email: {}", str(e))
    return None


def _send_ssl_expiry_email(
    domain_name: str,
    expires_at,
    contact_email: str,
    tenant_id: int,
) -> None:
    """
    发送 SSL 证书到期提醒（通过统一通知系统）/ Send SSL certificate expiry reminder (via unified notification system).

    Sent asynchronously via notification queue; silent failure does not affect inspection flow.
    走 notification 队列异步发送，静默失败不影响巡检主流程。
    """
    try:
        from app.services.common.email_templates import render_ssl_expiry_email
        from app.services.common.notification_service import notify_sync

        days_remaining = (expires_at - utc_now()).days if expires_at else 0
        subject, html_body, text_body = render_ssl_expiry_email(
            domain=domain_name,
            expires_at=str(expires_at.date()) if expires_at else "-",
            days_remaining=max(days_remaining, 0),
        )
        notify_sync(
            template_code="system.ssl_expiry",
            recipients=[("admin", 1)],
            data={"domain": domain_name, "days_remaining": max(days_remaining, 0)},
            tenant_id=tenant_id,
            email_html=html_body,
            email_subject=subject,
            email_text=text_body,
        )
    except Exception as e:
        logger.warning("Failed to send SSL expiry notification: {}", str(e))


__all__ = ["task_provision_ssl", "task_check_ssl_renewals", "task_renew_ssl"]
