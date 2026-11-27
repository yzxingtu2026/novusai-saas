"""
企业域名仓储 / Tenant Domain Repository

提供企业域名的数据访问操作（平台级，非企业隔离）
Provides tenant domain data access (platform-level, no tenant isolation).
"""

import secrets
from typing import Literal

from sqlalchemy import asc, column, func, select, table, update
from sqlalchemy.exc import DBAPIError

from app.core.base_repository import BaseRepository
from app.core.domains import normalize_hostname
from app.enums.domain import DomainSslStatus
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_domain import TenantDomain


class DomainPrimaryAdmissionError(Exception):
    """Expose only fixed safe reasons for failed primary-switch admission."""

    def __init__(self, reason: Literal["busy", "transaction_unsupported"]):
        self.reason = reason
        super().__init__(reason)


async def _lock_primary_domain_target(
    repository: BaseRepository[TenantDomain],
    tenant_id: int | None,
    domain_id: int,
) -> TenantDomain | None:
    """Serialize one tenant's primary writes and refresh its visible target.

    Both domain repositories pass their canonical tenant scope. NO KEY UPDATE
    retains the caller's transaction while allowing independent FK KEY SHARE
    checks; the following READ COMMITTED query sees a preceding switch commit.
    """
    db = repository.db
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    if (
        connection.dialect.name != "postgresql"
        or connection.sync_connection.get_execution_options().get("isolation_level")
        == "AUTOCOMMIT"
        or getattr(raw.dbapi_connection, "autocommit", None) is not False
        or await connection.get_isolation_level() != "READ COMMITTED"
    ):
        raise DomainPrimaryAdmissionError("transaction_unsupported")
    settings = table("pg_settings", column("name"), column("setting"))
    previous = dict(
        (
            await db.execute(
                select(settings.c.name, settings.c.setting).where(
                    settings.c.name.in_(["lock_timeout", "statement_timeout"])
                )
            )
        ).all()
    )
    for name, ceiling in (("lock_timeout", 2000), ("statement_timeout", 3000)):
        old = int(previous[name])
        limit = min(old, ceiling) if old > 0 else ceiling
        await db.execute(select(func.set_config(name, f"{limit}ms", True)))
    try:
        # Take the tenant lock before pending writes acquire domain locks.
        with db.no_autoflush:
            tenant = await db.scalar(
                select(Tenant.id)
                .where(Tenant.id == tenant_id, Tenant.is_deleted.is_(False))
                .with_for_update(key_share=True)
            )
        domain = None
        if tenant is not None:
            await db.flush()
            query = select(repository.model).where(
                repository.model.id == domain_id,
                repository.model.tenant_id == tenant_id,
                repository.model.is_deleted.is_(False),
            )
            query = repository._apply_data_permission_if_needed(query)
            domain = await db.scalar(
                query.with_for_update(
                    key_share=True, of=repository.model
                ).execution_options(populate_existing=True)
            )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "57014", "40P01"}:
            # A failed PostgreSQL transaction remains the caller's rollback.
            raise DomainPrimaryAdmissionError("busy") from None
        raise
    for name, value in previous.items():
        await db.execute(select(func.set_config(name, f"{value}ms", True)))
    return domain


class TenantDomainRepository(BaseRepository[TenantDomain]):
    """
    企业域名仓储 / Tenant domain repository.

    提供域名特有的数据访问方法
    注意：域名管理是平台级操作，不做企业隔离
    """

    model = TenantDomain

    async def get_primary_target_for_update(
        self, tenant_id: int, domain_id: int
    ) -> TenantDomain | None:
        return await _lock_primary_domain_target(self, tenant_id, domain_id)

    # 按 scope 限制可过滤字段
    _scope_fields: dict[str, set[str]] = {
        "admin": {
            "id",
            "tenant_id",
            "domain",
            "is_verified",
            "is_primary",
            "ssl_status",
            "created_at",
            "updated_at",
        },
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
        """
        根据域名获取记录 / Get record by domain.

        Args:
            domain: 域名

        Returns:
            域名实例或 None
        """
        query = select(self.model).where(
            func.lower(func.rtrim(self.model.domain, "."))
            == normalize_hostname(domain),
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_primary_domain(self, tenant_id: int) -> TenantDomain | None:
        """
        获取企业的主域名 / Get tenant primary domain.

        Args:
            tenant_id: 企业 ID

        Returns:
            主域名实例或 None
        """
        query = select(self.model).where(
            self.model.tenant_id == tenant_id,
            self.model.is_primary.is_(True),
            self.model.is_deleted.is_(False),
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_tenant_domains(self, tenant_id: int) -> list[TenantDomain]:
        """
        获取企业所有域名 / Get all domains for tenant.

        Args:
            tenant_id: 企业 ID

        Returns:
            域名列表，主域名排在前面
        """
        query = (
            select(self.model)
            .where(
                self.model.tenant_id == tenant_id,
                self.model.is_deleted.is_(False),
            )
            .order_by(
                self.model.is_primary.desc(),  # 主域名排在前面 / Primary domain first
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
        """
        检查域名是否已存在 / Check if domain already exists.

        Args:
            domain: 域名
            exclude_id: 排除的 ID（用于更新时排除自身）

        Returns:
            是否存在
        """
        query = select(self.model.id).where(
            func.lower(func.rtrim(self.model.domain, "."))
            == normalize_hostname(domain),
        )
        if exclude_id:
            query = query.where(self.model.id != exclude_id)

        result = await self.db.execute(query)
        return result.first() is not None

    async def release_deleted_domain(self, domain: str) -> None:
        """Release soft-deleted reservations without restoring ownership proof."""
        query = (
            select(self.model.id)
            .where(
                func.lower(func.rtrim(self.model.domain, "."))
                == normalize_hostname(domain),
                self.model.is_deleted.is_(True),
            )
            .with_for_update()
        )
        result = await self.db.execute(query)
        for domain_id in result.scalars().all():
            await self.db.execute(
                update(self.model)
                .where(
                    self.model.id == domain_id,
                    self.model.is_deleted.is_(True),
                )
                .values(
                    domain=f"deleted-{domain_id}-{secrets.token_hex(8)}.invalid",
                    is_verified=False,
                    verified_at=None,
                    verification_token=None,
                    is_primary=False,
                    ssl_status=DomainSslStatus.NONE.value,
                    ssl_expires_at=None,
                )
            )
            await self.db.execute(
                update(DomainSslCertificate)
                .where(DomainSslCertificate.domain_id == domain_id)
                .values(status="revoked", auto_renew=False)
            )

    async def get_ssl_provision_candidates(self, tenant_id: int) -> list[TenantDomain]:
        query = select(self.model).where(
            self.model.tenant_id == tenant_id,
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

    async def count_tenant_domains(self, tenant_id: int) -> int:
        """
        统计企业域名数量 / Count tenant domains.

        Args:
            tenant_id: 企业 ID

        Returns:
            域名数量
        """
        return await self.count(tenant_id=tenant_id)

    async def has_primary_domain(self, tenant_id: int) -> bool:
        """
        检查企业是否已有主域名 / Check if tenant has primary domain.

        Args:
            tenant_id: 企业 ID

        Returns:
            是否有主域名
        """
        primary = await self.get_primary_domain(tenant_id)
        return primary is not None

    async def clear_primary_flag(self, tenant_id: int) -> None:
        """
        清除企业现有的主域名标记 / Clear tenant primary domain flag.

        在设置新的主域名之前调用，确保只有一个主域名

        Args:
            tenant_id: 企业 ID
        """
        from sqlalchemy import update

        stmt = (
            update(self.model)
            .where(
                self.model.tenant_id == tenant_id,
                self.model.is_primary.is_(True),
                self.model.is_deleted.is_(False),
            )
            .values(is_primary=False)
        )
        await self.db.execute(stmt)


__all__ = ["TenantDomainRepository"]
