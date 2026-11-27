"""
SSL 证书服务 / SSL Certificate Service

提供 SSL 证书的业务逻辑（平台级）
Provides SSL certificate business logic (platform-level).
包括证书查询、上传、删除、自动续期开关、证书解析与验证
Includes cert query, upload, delete, auto-renewal toggle, cert parsing and validation.
"""

from datetime import datetime

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from app.core.base_model import utc_now
from app.core.base_service import GlobalService
from app.core.config import settings
from app.core.domains import normalize_hostname
from app.core.i18n import _
from app.enums import ErrorCode
from app.enums.domain import DomainSslStatus, SslCertStatus, SslCertType
from app.exceptions import BusinessException, NotFoundException
from app.models.tenant.domain_ssl_certificate import DomainSslCertificate
from app.models.tenant.tenant_domain import TenantDomain
from app.repositories.system.ssl_certificate_repository import SslCertificateRepository


class SslCertificateService(
    GlobalService[DomainSslCertificate, SslCertificateRepository]
):
    """
    SSL 证书服务 / SSL certificate service.

    提供证书管理的核心业务方法
    """

    model = DomainSslCertificate
    repository_class = SslCertificateRepository

    async def export_publishable_certificates(self) -> dict:
        """Privileged host publication snapshot. Fail closed on invalid material."""
        from cryptography.hazmat.primitives import hashes

        from app.services.system.tenant_domain_service import TenantDomainService

        domains = TenantDomainService(self.db)
        items = []
        rows = await self.repo.get_publishable_certificates()
        encryption_key = None
        suffix = await domains._get_domain_suffix()
        platform_hosts = {
            normalize_hostname(item.strip())
            for item in settings.PLATFORM_DOMAINS.split(",")
            if item.strip()
        }
        seen = set()
        for cert, domain in rows:
            host = normalize_hostname(domain.domain)
            if (
                host in platform_hosts
                or host == suffix.removeprefix(".")
                or host.endswith(suffix)
            ):
                continue
            try:
                await self.ensure_domain_ssl_entitled(domain.id, domain.tenant_id)
            except BusinessException as exc:
                if exc.code == ErrorCode.FORBIDDEN:
                    continue
                raise
            if cert.tenant_id != domain.tenant_id or host in seen:
                raise ValueError("invalid_certificate_publication_identity")
            seen.add(host)
            if encryption_key is None:
                encryption_key = await self._get_encryption_key()
            key_pem = self._decrypt_private_key(
                cert.private_key_encrypted, encryption_key
            )
            self._validate_certificate(cert.certificate, key_pem)
            self._validate_domain_in_cert(cert.certificate, host)
            parsed = x509.load_pem_x509_certificate(cert.certificate.encode())
            info = self._parse_cert_info(cert.certificate)
            if info["expires_at"] <= utc_now() or info["issued_at"] > utc_now():
                raise ValueError("certificate_publication_outside_validity")
            items.append(
                {
                    "domain_id": domain.id,
                    "tenant_id": domain.tenant_id,
                    "domain": host,
                    "certificate_id": cert.id,
                    "certificate_pem": cert.certificate,
                    "private_key_pem": key_pem,
                    "chain_pem": cert.certificate_chain,
                    "expires_at": info["expires_at"].isoformat() + "Z",
                    "fingerprint_sha256": parsed.fingerprint(hashes.SHA256()).hex(),
                }
            )
        return {"version": 1, "certificates": items}

    # ==================== 查询 ==================== / ==================== Query ====================

    async def get_cert_detail(self, domain_id: int) -> DomainSslCertificate | None:
        """获取域名当前证书（优先 active，否则最新） / Get domain cert (active first, else latest)."""
        cert = await self.repo.get_active_cert(domain_id)
        if not cert:
            cert = await self.repo.get_cert_by_domain(domain_id)
        return cert

    # ==================== 上传自定义证书 ==================== / ==================== Upload custom certificate ====================

    async def _check_custom_ssl_quota(self, tenant_id: int) -> None:
        """
        检查企业是否允许上传自定义 SSL 证书 / Check tenant allow_custom_ssl quota.

        通过企业套餐的 allow_custom_ssl 配额判断；不允许时抛出 BusinessException。
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.models.tenant.tenant import Tenant

        result = await self.db.execute(
            select(Tenant)
            .where(
                Tenant.id == tenant_id,
                Tenant.is_active.is_(True),
                Tenant.is_deleted.is_(False),
            )
            .options(selectinload(Tenant.tenant_plan))
        )
        tenant = result.scalar_one_or_none()
        if not tenant or not tenant.get_quota_value("allow_custom_ssl", False):
            raise BusinessException(
                message=_("ssl_certificate.custom_ssl_not_allowed"),
                code=ErrorCode.FORBIDDEN,
            )

    async def upload_custom_cert(
        self,
        domain_id: int,
        tenant_id: int,
        cert_pem: str,
        key_pem: str,
        chain_pem: str | None = None,
        *,
        check_quota: bool = False,
    ) -> DomainSslCertificate:
        """
        上传自定义证书 / Upload custom certificate.

        验证证书格式、私钥匹配、域名匹配后存储；自动设置 cert_type=custom, auto_renew=False。

        Args:
            check_quota: 是否检查套餐 allow_custom_ssl 配额（企业端传 True，管理端传 False） / Whether to check allow_custom_ssl quota.
        """
        await self._lock_domain(domain_id)
        domain = await self.ensure_domain_ssl_entitled(domain_id, tenant_id)

        # 套餐配额检查（仅企业端需要） / Plan quota check (tenant side only)
        if check_quota:
            await self._check_custom_ssl_quota(tenant_id)

        # 验证证书与私钥 / Validate cert matches private key
        cert_info = self._parse_cert_info(cert_pem)
        self._validate_certificate(cert_pem, key_pem)
        self._validate_domain_in_cert(cert_pem, domain.domain)

        self._validate_certificate_validity(cert_info)

        # 加密私钥 / Encrypt private key
        encryption_key = await self._get_encryption_key()
        encrypted_key = self._encrypt_private_key(key_pem, encryption_key)

        # Validate/encrypt before modifying the certificate currently in use.
        await self.repo.deactivate_domain_certs(domain_id)

        # 创建新证书记录 / Create new cert record
        cert = await self.create(
            {
                "domain_id": domain_id,
                "tenant_id": tenant_id,
                "cert_type": SslCertType.CUSTOM.value,
                "status": SslCertStatus.ACTIVE.value,
                "certificate": cert_pem,
                "private_key_encrypted": encrypted_key,
                "certificate_chain": chain_pem,
                "issuer": cert_info["issuer"],
                "serial_number": cert_info["serial_number"],
                "issued_at": cert_info["issued_at"],
                "expires_at": cert_info["expires_at"],
                "auto_renew": False,
            }
        )

        # 更新域名 SSL 状态
        await self._update_domain_ssl_status(
            domain_id,
            DomainSslStatus.ACTIVE.value,
            cert_info["expires_at"],
        )

        return cert

    # ==================== 删除证书 ==================== / ==================== Delete certificate ====================

    async def delete_cert(self, domain_id: int) -> bool:
        """删除域名证书并重置 SSL 状态 / Delete domain cert and reset SSL status."""
        await self._lock_domain(domain_id)
        cert = await self.repo.get_active_cert(domain_id)
        if not cert:
            cert = await self.repo.get_cert_by_domain(domain_id)
        if not cert:
            raise NotFoundException(message=_("ssl_certificate.not_found"))

        await self.delete(cert.id)
        await self._update_domain_ssl_status(
            domain_id, DomainSslStatus.NONE.value, None
        )
        return True

    # ==================== 自动续期开关 ==================== / ==================== Auto-renewal toggle ====================

    async def toggle_auto_renew(
        self, domain_id: int, enabled: bool
    ) -> DomainSslCertificate:
        """开关自动续期（仅 platform 类型可开启） / Toggle auto-renew (platform type only)."""
        if enabled:
            await self.ensure_domain_ssl_entitled(domain_id)
        await self._lock_domain(domain_id)
        cert = await self.get_cert_detail(domain_id)
        if not cert:
            raise NotFoundException(message=_("ssl_certificate.not_found"))
        if cert.status not in {SslCertStatus.ACTIVE.value, SslCertStatus.EXPIRED.value}:
            raise BusinessException(
                message=_("ssl_certificate.cert_not_current"), code=ErrorCode.CONFLICT
            )

        if enabled and cert.cert_type == SslCertType.CUSTOM.value:
            raise BusinessException(
                message=_("ssl_certificate.custom_cert_no_auto_renew"),
                code=ErrorCode.VALIDATION_ERROR,
            )

        result = await self.update(cert.id, {"auto_renew": enabled})
        if not result:
            raise NotFoundException(message=_("ssl_certificate.not_found"))
        return result

    # ==================== 存储平台证书（ACME 签发后调用） ====================

    async def store_platform_cert(
        self,
        domain_id: int,
        tenant_id: int,
        cert_pem: str,
        key_pem: str,
        chain_pem: str | None = None,
        acme_order_url: str | None = None,
        *,
        expected_certificate_id: int | None = None,
        require_auto_renew: bool = False,
        expected_domain: str | None = None,
    ) -> DomainSslCertificate | None:
        """存储 ACME 签发的平台证书（由 Celery 任务调用） / Store ACME-issued platform cert (Celery)."""
        await self._lock_domain(domain_id)
        domain = await self.ensure_domain_ssl_entitled(domain_id, tenant_id)
        if expected_domain is not None and normalize_hostname(
            domain.domain
        ) != normalize_hostname(expected_domain):
            return None
        current = await self.get_cert_detail(domain_id)
        if expected_certificate_id is not None:
            if (
                not current
                or current.id != expected_certificate_id
                or current.status
                not in {SslCertStatus.ACTIVE.value, SslCertStatus.EXPIRED.value}
            ):
                return None
            if require_auto_renew and not current.auto_renew:
                return None
        elif current and current.status in {
            SslCertStatus.ACTIVE.value,
            SslCertStatus.EXPIRED.value,
        }:
            # A concurrent upload/issuance won; provisioning is not replacement.
            return None
        cert_info = self._parse_cert_info(cert_pem)
        self._validate_certificate(cert_pem, key_pem)
        self._validate_domain_in_cert(cert_pem, domain.domain)
        self._validate_certificate_validity(cert_info)
        encryption_key = await self._get_encryption_key()
        encrypted_key = self._encrypt_private_key(key_pem, encryption_key)

        # 停用已有证书 / Deactivate existing certs
        await self.repo.deactivate_domain_certs(domain_id)

        cert = await self.create(
            {
                "domain_id": domain_id,
                "tenant_id": tenant_id,
                "cert_type": SslCertType.PLATFORM.value,
                "status": SslCertStatus.ACTIVE.value,
                "certificate": cert_pem,
                "private_key_encrypted": encrypted_key,
                "certificate_chain": chain_pem,
                "issuer": cert_info["issuer"],
                "serial_number": cert_info["serial_number"],
                "issued_at": cert_info["issued_at"],
                "expires_at": cert_info["expires_at"],
                "auto_renew": current.auto_renew
                if current and expected_certificate_id is not None
                else True,
                "acme_order_url": acme_order_url,
            }
        )

        await self._update_domain_ssl_status(
            domain_id,
            DomainSslStatus.ACTIVE.value,
            cert_info["expires_at"],
        )

        return cert

    # ==================== 记录签发失败 ==================== / ==================== Record renewal failure ====================

    async def mark_provision_failed(
        self,
        domain_id: int,
        error: str,
        *,
        expected_certificate_id: int | None = None,
    ) -> None:
        """标记签发失败 / Mark provision failed."""
        await self._lock_domain(domain_id)
        current = await self.get_cert_detail(domain_id)
        if expected_certificate_id is not None and (
            not current or current.id != expected_certificate_id
        ):
            return
        active_cert = await self.repo.get_active_cert(domain_id)
        if (
            active_cert
            and active_cert.expires_at
            and active_cert.expires_at > utc_now()
        ):
            await self.mark_renewal_failed(active_cert.id, error)
            await self._update_domain_ssl_status(
                domain_id, DomainSslStatus.ACTIVE.value, active_cert.expires_at
            )
            return
        cert = await self.repo.get_cert_by_domain(domain_id)
        if cert:
            # A failed replacement is still eligible for later expiry recovery.
            if cert.status == SslCertStatus.EXPIRED.value:
                await self.mark_renewal_failed(cert.id, error)
                await self._update_domain_ssl_status(
                    domain_id, DomainSslStatus.EXPIRED.value, cert.expires_at
                )
                return
            await self.update(
                cert.id,
                {
                    "status": SslCertStatus.FAILED.value,
                    "renewal_error": error,
                    "last_renewal_attempt": utc_now(),
                },
            )

        await self._update_domain_ssl_status(
            domain_id, DomainSslStatus.FAILED.value, None
        )

    # ==================== 记录续期失败 ==================== / ==================== Record expiry warning ====================

    async def mark_renewal_failed(
        self,
        cert_id: int,
        error: str,
    ) -> None:
        """标记续期失败（不改变 ssl_status，证书仍然有效直到过期） / Mark renewal failed (status unchanged)."""
        await self.update(
            cert_id,
            {
                "renewal_error": error,
                "last_renewal_attempt": utc_now(),
            },
        )

    # ==================== 记录到期预警 ==================== / ==================== Record issuance failure ====================

    async def mark_expiry_warning(
        self,
        cert_id: int,
        warning: str,
    ) -> None:
        """记录证书到期预警（用于自定义证书即将过期的提醒，不改变状态） / Record expiry warning."""
        await self.update(
            cert_id,
            {
                "renewal_error": warning,
                "last_renewal_attempt": utc_now(),
            },
        )

    # ==================== 标记过期 ==================== / ==================== Mark expired ====================

    async def mark_expired(self, cert_id: int, domain_id: int) -> None:
        """标记证书过期 / Mark cert expired."""
        await self._lock_domain(domain_id)
        current = await self.get_cert_detail(domain_id)
        if (
            not current
            or current.id != cert_id
            or not current.expires_at
            or current.expires_at > utc_now()
        ):
            return
        await self.update(cert_id, {"status": SslCertStatus.EXPIRED.value})
        await self._update_domain_ssl_status(
            domain_id, DomainSslStatus.EXPIRED.value, None
        )

    # ==================== 内部方法 ==================== / ==================== Internal methods ====================

    async def ensure_domain_ssl_entitled(
        self, domain_id: int, tenant_id: int | None = None
    ) -> TenantDomain:
        """Recheck identity, verification and current plan in every write/worker."""
        from app.services.system.tenant_domain_service import TenantDomainService

        domain = await self._get_domain(domain_id)
        if tenant_id is not None and tenant_id != domain.tenant_id:
            raise BusinessException(
                message=_("tenant_domain.not_found"), code=ErrorCode.FORBIDDEN
            )
        if not domain.is_verified:
            raise BusinessException(
                message=_("ssl_certificate.domain_not_verified"),
                code=ErrorCode.VALIDATION_ERROR,
            )
        await TenantDomainService(self.db).ensure_custom_domain_entitled(
            domain.tenant_id, domain
        )
        return domain

    async def _get_domain(self, domain_id: int) -> TenantDomain:
        """获取域名实例 / Get domain instance."""
        domain = await self.repo.get_domain(domain_id)
        if not domain:
            raise NotFoundException(message=_("tenant_domain.not_found"))
        return domain

    async def _lock_domain(self, domain_id: int) -> TenantDomain:
        domain = await self.repo.lock_domain(domain_id)
        if not domain:
            raise NotFoundException(message=_("tenant_domain.not_found"))
        return domain

    async def _update_domain_ssl_status(
        self,
        domain_id: int,
        status: str,
        expires_at: datetime | None,
    ) -> None:
        """更新域名 SSL 状态字段 / Update domain SSL status."""
        await self.repo.update_domain_ssl_status(domain_id, status, expires_at)

    # ==================== 证书工具方法 ==================== / ==================== Certificate helpers ====================

    async def _get_encryption_key(self) -> str:
        """从平台配置或环境变量获取 Fernet 加密密钥 / Get Fernet key from config or env."""
        from app.configs.service import ConfigService

        config_svc = ConfigService(self.db)
        key = await config_svc.get_platform_config(
            "ssl_private_key_encryption_key",
            default=None,
        )
        if key:
            return key
        if settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY:
            return settings.SSL_PRIVATE_KEY_ENCRYPTION_KEY
        raise BusinessException(
            message=_("ssl_certificate.encryption_key_not_configured"),
            code=ErrorCode.VALIDATION_ERROR,
        )

    @staticmethod
    def _encrypt_private_key(key_pem: str, encryption_key: str) -> str:
        """Fernet 加密私钥 / Fernet encrypt private key."""
        fernet = Fernet(encryption_key.encode())
        return fernet.encrypt(key_pem.encode()).decode()

    @staticmethod
    def _decrypt_private_key(encrypted: str, encryption_key: str) -> str:
        """Fernet 解密私钥 / Fernet decrypt private key."""
        fernet = Fernet(encryption_key.encode())
        return fernet.decrypt(encrypted.encode()).decode()

    @staticmethod
    def _parse_cert_info(cert_pem: str) -> dict:
        """解析 PEM 证书信息 / Parse PEM cert info."""
        cert = x509.load_pem_x509_certificate(cert_pem.encode())

        # 签发机构 / Certificate authority
        issuer_parts = []
        for attr in cert.issuer:
            if attr.oid == NameOID.ORGANIZATION_NAME or attr.oid == NameOID.COMMON_NAME:
                issuer_parts.append(attr.value)
        issuer = ", ".join(issuer_parts) if issuer_parts else str(cert.issuer)

        # Return naive UTC datetimes (TIMESTAMP WITHOUT TIME ZONE) / 返回无时区 UTC 时间 / naive UTC datetimes
        issued_at = (
            cert.not_valid_before_utc
            if hasattr(cert, "not_valid_before_utc")
            else cert.not_valid_before
        )
        expires_at = (
            cert.not_valid_after_utc
            if hasattr(cert, "not_valid_after_utc")
            else cert.not_valid_after
        )

        return {
            "issuer": issuer,
            "serial_number": format(cert.serial_number, "x"),
            "issued_at": issued_at.replace(tzinfo=None),
            "expires_at": expires_at.replace(tzinfo=None),
        }

    @staticmethod
    def _validate_certificate_validity(cert_info: dict) -> None:
        """Reject unusable material before replacing the certificate in use."""
        now = utc_now()
        if cert_info["issued_at"] > now:
            raise BusinessException(
                message=_("ssl_certificate.cert_not_yet_valid"),
                code=ErrorCode.VALIDATION_ERROR,
            )
        if cert_info["expires_at"] <= now:
            raise BusinessException(
                message=_("ssl_certificate.cert_expired"),
                code=ErrorCode.VALIDATION_ERROR,
            )

    @staticmethod
    def _validate_certificate(cert_pem: str, key_pem: str) -> None:
        """验证证书与私钥是否匹配 / Validate cert and key match."""
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
            key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        except Exception as e:
            raise BusinessException(
                message=_("ssl_certificate.cert_key_mismatch"),
                code=ErrorCode.VALIDATION_ERROR,
            ) from e

        # 比较公钥 / Compare public keys
        cert_pub = cert.public_key()
        key_pub = key.public_key()

        cert_pub_bytes = cert_pub.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_pub_bytes = key_pub.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        if cert_pub_bytes != key_pub_bytes:
            raise BusinessException(
                message=_("ssl_certificate.cert_key_mismatch"),
                code=ErrorCode.VALIDATION_ERROR,
            )

    @staticmethod
    def _validate_domain_in_cert(cert_pem: str, domain: str) -> None:
        """验证证书是否包含指定域名（检查 SAN 和 CN） / Validate domain in cert (SAN and CN)."""
        cert = x509.load_pem_x509_certificate(cert_pem.encode())

        host = normalize_hostname(domain)

        def matches(name: str) -> bool:
            try:
                if name.startswith("*."):
                    suffix = "." + normalize_hostname(name[2:])
                    label = host.removesuffix(suffix)
                    return host.endswith(suffix) and bool(label) and "." not in label
                return normalize_hostname(name) == host
            except ValueError:
                return False

        # SAN is authoritative when present; CN is only a legacy fallback.
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            dns_names = san.value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            dns_names = [
                attr.value for attr in cert.subject if attr.oid == NameOID.COMMON_NAME
            ]
        if any(matches(name) for name in dns_names):
            return

        raise BusinessException(
            message=_("ssl_certificate.domain_not_in_cert"),
            code=ErrorCode.VALIDATION_ERROR,
        )


__all__ = ["SslCertificateService"]
