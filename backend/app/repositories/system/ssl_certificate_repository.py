"""
SSL 证书仓储 / SSL Certificate Repository

提供 SSL 证书的数据访问操作（平台级，非企业隔离）
Provides SSL certificate data access (platform-level, no tenant isolation).
"""

from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from app.core.base_model import utc_now
from app.core.base_repository import BaseRepository
from app.enums.domain import SslCertStatus, SslCertType
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant_domain import TenantDomain


class SslCertificateRepository(BaseRepository[DomainSslCertificate]):
    """
    SSL 证书仓储 / SSL certificate repository.

    提供证书特有的数据访问方法
    """

    model = DomainSslCertificate

    async def get_publishable_certificates(
        self,
    ) -> list[tuple[DomainSslCertificate, TenantDomain]]:
        """Current live certificates only; entitlement is evaluated by the service."""
        from app.models.tenant.tenant import Tenant

        newer = aliased(self.model)
        query = (
            select(self.model, TenantDomain)
            .join(
                TenantDomain,
                TenantDomain.id == self.model.domain_id,
            )
            .join(Tenant, Tenant.id == TenantDomain.tenant_id)
            .where(
                self.model.is_deleted.is_(False),
                self.model.status == SslCertStatus.ACTIVE.value,
                self.model.expires_at > utc_now(),
                TenantDomain.is_deleted.is_(False),
                TenantDomain.is_verified.is_(True),
                Tenant.is_deleted.is_(False),
                Tenant.is_active.is_(True),
                (Tenant.expires_at.is_(None)) | (Tenant.expires_at > utc_now()),
                ~exists(
                    select(newer.id).where(
                        newer.domain_id == self.model.domain_id,
                        newer.id > self.model.id,
                        newer.is_deleted.is_(False),
                    )
                ),
            )
            .order_by(TenantDomain.id)
        )
        return list((await self.db.execute(query)).all())

    async def update_domain_ssl_status(self, domain_id, status, expires_at):
        from sqlalchemy import update

        await self.db.execute(
            update(TenantDomain)
            .where(
                TenantDomain.id == domain_id,
                TenantDomain.is_deleted.is_(False),
            )
            .values(ssl_status=status, ssl_expires_at=expires_at)
        )

    async def lock_domain(self, domain_id: int) -> TenantDomain | None:
        """Serialize short certificate writes for one live domain."""
        result = await self.db.execute(
            select(TenantDomain)
            .where(
                TenantDomain.id == domain_id,
                TenantDomain.is_deleted.is_(False),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_domain(self, domain_id: int) -> TenantDomain | None:
        result = await self.db.execute(
            select(TenantDomain)
            .where(
                TenantDomain.id == domain_id,
                TenantDomain.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_active_cert(self, domain_id: int) -> DomainSslCertificate | None:
        """获取域名当前有效证书 / Get current active cert for domain."""
        query = select(self.model).where(
            self.model.domain_id == domain_id,
            self.model.status == SslCertStatus.ACTIVE.value,
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def get_cert_by_domain(self, domain_id: int) -> DomainSslCertificate | None:
        """获取域名最新证书（不论状态） / Get latest cert for domain (any status)."""
        query = (
            select(self.model)
            .where(
                self.model.domain_id == domain_id,
                self.model.is_deleted.is_(False),
            )
            .order_by(self.model.created_at.desc(), self.model.id.desc())
            .limit(1)
        )
        result = await self.db.execute(query.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def get_expiring_platform_certs(
        self, days: int = 30
    ) -> list[DomainSslCertificate]:
        """查询即将过期的平台证书（auto_renew=True） / Get expiring platform certs (auto_renew)."""
        cutoff = utc_now() + timedelta(days=days)
        newer = aliased(self.model)
        superseded = exists(
            select(newer.id).where(
                newer.domain_id == self.model.domain_id,
                newer.id > self.model.id,
                newer.is_deleted.is_(False),
            )
        )
        query = select(self.model).where(
            self.model.cert_type == SslCertType.PLATFORM.value,
            self.model.status.in_(
                [SslCertStatus.ACTIVE.value, SslCertStatus.EXPIRED.value]
            ),
            self.model.auto_renew.is_(True),
            self.model.expires_at.is_not(None),
            self.model.expires_at < cutoff,
            self.model.is_deleted.is_(False),
            ~superseded,
            # A failed attempt backs off until the next daily inspection.
            (self.model.last_renewal_attempt.is_(None))
            | (self.model.last_renewal_attempt < utc_now() - timedelta(hours=12)),
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_expiring_custom_certs(
        self, days: int = 30
    ) -> list[DomainSslCertificate]:
        """查询即将过期的自定义证书（需通知，不自动续期） / Get expiring custom certs (notify only)."""
        cutoff = utc_now() + timedelta(days=days)
        query = select(self.model).where(
            self.model.cert_type == SslCertType.CUSTOM.value,
            self.model.status == SslCertStatus.ACTIVE.value,
            self.model.expires_at.is_not(None),
            self.model.expires_at < cutoff,
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_expired_certs(self) -> list[DomainSslCertificate]:
        """查询已过期但 status 未更新的证书 / Get certs expired but status not updated."""
        now = utc_now()
        query = select(self.model).where(
            self.model.status == SslCertStatus.ACTIVE.value,
            self.model.expires_at.is_not(None),
            self.model.expires_at < now,
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def deactivate_domain_certs(self, domain_id: int) -> None:
        """将域名所有有效证书标记为已吊销（上传新证书前调用） / Deactivate all active certs for domain (before upload)."""
        from sqlalchemy import update

        stmt = (
            update(self.model)
            .where(
                self.model.domain_id == domain_id,
                self.model.status.in_(
                    [SslCertStatus.ACTIVE.value, SslCertStatus.EXPIRED.value]
                ),
                self.model.is_deleted.is_(False),
            )
            .values(status=SslCertStatus.REVOKED.value)
        )
        await self.db.execute(stmt)


__all__ = ["SslCertificateRepository"]
