"""
企业端域名仓储 / Tenant Domain (Tenant-side) Repository
"""

from sqlalchemy import asc, func, select

from app.core.base_repository import TenantRepository
from app.core.domains import normalize_hostname
from app.enums.domain import DomainSslStatus
from app.models.tenant.tenant_domain import TenantDomain
from app.repositories.system.tenant_domain_repository import (
    TenantDomainRepository,
    _lock_primary_domain_target,
)


class TenantDomainTenantRepository(TenantRepository[TenantDomain]):
    model = TenantDomain

    async def get_primary_target_for_update(
        self, tenant_id: int, domain_id: int
    ) -> TenantDomain | None:
        # Caller-supplied ownership cannot override the repository's tenant.
        _ = tenant_id
        return await _lock_primary_domain_target(self, self.tenant_id, domain_id)

    _scope_fields: dict[str, set[str]] = {
        "tenant": {
            "id",
            "domain",
            "is_verified",
            "is_primary",
            "ssl_status",
            "created_at",
            "updated_at",
            "remark",
        },
    }

    async def get_by_domain(self, domain: str) -> TenantDomain | None:
        query = select(self.model).where(
            self.model.tenant_id == self.tenant_id,
            func.lower(func.rtrim(self.model.domain, "."))
            == normalize_hostname(domain),
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_primary_domain(
        self, tenant_id: int | None = None
    ) -> TenantDomain | None:
        """tenant_id is ignored — uses self.tenant_id from TenantRepository / tenant_id 忽略，使用 TenantRepository 的 self.tenant_id。"""
        _ = tenant_id
        query = select(self.model).where(
            self.model.tenant_id == self.tenant_id,
            self.model.is_primary.is_(True),
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_tenant_domains(
        self, tenant_id: int | None = None
    ) -> list[TenantDomain]:
        _ = tenant_id
        query = (
            select(self.model)
            .where(
                self.model.tenant_id == self.tenant_id,
                self.model.is_deleted.is_(False),
            )
            .order_by(
                self.model.is_primary.desc(),
                asc(self.model.created_at),
            )
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def domain_exists(
        self,
        domain: str,
        exclude_id: int | None = None,
    ) -> bool:
        query = select(self.model.id).where(
            # Reservation is global, including deleted rows until the service
            # explicitly releases them without restoring ownership proof.
            func.lower(func.rtrim(self.model.domain, "."))
            == normalize_hostname(domain),
        )
        if exclude_id:
            query = query.where(self.model.id != exclude_id)

        result = await self.db.execute(query)
        return result.first() is not None

    async def release_deleted_domain(self, domain: str) -> None:
        # This namespace-only operation deliberately crosses tenant scope, just
        # like domain_exists; active rows are never modified or returned.
        await TenantDomainRepository(self.db).release_deleted_domain(domain)

    async def get_ssl_provision_candidates(
        self, tenant_id: int | None = None
    ) -> list[TenantDomain]:
        _ = tenant_id
        query = select(self.model).where(
            self.model.tenant_id == self.tenant_id,
            self.model.is_verified.is_(True),
            self.model.ssl_status.in_(
                [
                    DomainSslStatus.NONE.value,
                    DomainSslStatus.FAILED.value,
                    DomainSslStatus.EXPIRED.value,
                ]
            ),
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_tenant_domains(self, tenant_id: int | None = None) -> int:
        """tenant_id is ignored — uses self.tenant_id from TenantRepository / tenant_id 忽略，使用 TenantRepository 的 self.tenant_id。"""
        _ = tenant_id
        return await self.count(tenant_id=self.tenant_id)

    async def has_primary_domain(self, tenant_id: int | None = None) -> bool:
        """tenant_id is ignored — uses self.tenant_id from TenantRepository / tenant_id 忽略，使用 TenantRepository 的 self.tenant_id。"""
        _ = tenant_id
        primary = await self.get_primary_domain()
        return primary is not None

    async def clear_primary_flag(self, tenant_id: int | None = None) -> None:
        """tenant_id is ignored — uses self.tenant_id from TenantRepository / tenant_id 忽略，使用 TenantRepository 的 self.tenant_id。"""
        _ = tenant_id
        from sqlalchemy import update

        stmt = (
            update(self.model)
            .where(
                self.model.tenant_id == self.tenant_id,
                self.model.is_primary.is_(True),
                self.model.is_deleted.is_(False),
            )
            .values(is_primary=False)
        )
        await self.db.execute(stmt)


__all__ = ["TenantDomainTenantRepository"]
