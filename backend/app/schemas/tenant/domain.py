"""
企业域名 Schema / Tenant Domain Schema

定义域名管理相关的请求和响应数据结构
Defines domain management request and response data structures.
"""

import ipaddress
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.core.base_schema import BaseSchema
from app.core.domains import normalize_hostname, normalize_tenant_suffix
from app.core.i18n import _
from app.core.managed_https import get_managed_https


class TenantDomainSimpleResponse(BaseSchema):
    """域名简略响应（用于企业响应嵌套） / Domain brief response (for tenant response nesting)"""

    id: int = Field(..., description="域名 ID")
    domain: str = Field(..., description="域名")
    domain_type: str = Field("custom", description="域名类型: default/custom")
    is_primary: bool = Field(..., description="是否主域名")
    is_verified: bool = Field(..., description="是否已验证")
    ssl_status: str = Field(..., description="SSL 状态")


class TenantDomainVerificationInfo(BaseSchema):
    """域名验证 DNS 记录信息 / Domain verification DNS record info"""

    dns_type: str = Field("TXT", description="DNS 记录类型")
    dns_name: str = Field(..., description="DNS 记录名称")
    dns_value: str = Field(..., description="DNS 记录值")


class ManagedHttpsResponse(BaseSchema):
    """Configured shared public certificate, distinct from per-domain issuance."""

    configured: bool = False
    covered: bool = False
    status: Literal["active", "expiring", "expired", "unconfigured"] = "unconfigured"
    expires_at: datetime | None = None
    auto_renew: bool = False


class TenantDomainResponse(BaseSchema):
    """企业域名响应 / Tenant domain response"""

    id: int = Field(..., description="域名 ID")
    tenant_id: int = Field(..., description="企业 ID")
    domain: str = Field(..., description="域名")
    domain_type: str = Field("custom", description="域名类型: default/custom")
    is_verified: bool = Field(..., description="是否已验证")
    verified_at: datetime | None = Field(None, description="验证时间")
    is_primary: bool = Field(..., description="是否主域名")
    ssl_status: str = Field(..., description="SSL 状态")
    ssl_expires_at: datetime | None = Field(None, description="SSL 到期时间")
    managed_https: ManagedHttpsResponse | None = None
    verification_token: str | None = Field(None, description="验证 Token")
    verification_info: TenantDomainVerificationInfo | None = Field(
        None, description="DNS 验证记录信息"
    )
    remark: str | None = Field(None, description="备注")
    cname_target: str | None = Field(None, description="CNAME 解析目标")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    @model_validator(mode="after")
    def enrich_deployment_metadata(self) -> Self:
        if self.domain_type == "default":
            self.managed_https = ManagedHttpsResponse(**get_managed_https(self.domain))
        else:
            self.managed_https = None
        if self.remark == "tenant_domain.default_domain_remark":
            self.remark = _("tenant_domain.default_domain_remark")
        return self


class DevHostsRuntimeInfo(BaseSchema):
    """Dev Hosts 运行时信息 / Dev Hosts runtime information"""

    enabled: bool = Field(..., description="是否启用 Dev Hosts 管理")
    debug: bool = Field(..., description="当前是否为 DEBUG 模式")
    supported: bool = Field(..., description="当前系统是否支持 hosts 管理")
    os_name: str = Field(..., description="当前操作系统名称")
    hosts_path: str | None = Field(None, description="hosts 文件路径")
    requires_elevation: bool = Field(..., description="是否可能需要管理员或 sudo 权限")
    can_write_hint: bool = Field(..., description="当前进程是否看起来具备写入权限")


class DevHostDomainStatus(BaseSchema):
    """单个域名的 Dev Hosts 状态 / Dev Hosts status for a single domain"""

    domain_id: int = Field(..., description="域名 ID")
    domain: str = Field(..., description="域名")
    eligible: bool = Field(..., description="当前域名是否应参与 Dev Hosts 管理")
    status: str = Field(
        ...,
        description="hosts 状态：managed_present/manual_present/missing/not_required/unsupported",
    )
    managed: bool = Field(..., description="是否为系统托管条目")
    matched_ip: str | None = Field(None, description="匹配到的 IP")
    reason: str | None = Field(None, description="状态原因")


class DevHostsStatusResponse(BaseSchema):
    """企业全部域名的 Dev Hosts 状态总览 / Dev Hosts overview for all tenant domains"""

    runtime: DevHostsRuntimeInfo = Field(..., description="运行时信息")
    domains: list[DevHostDomainStatus] = Field(
        default_factory=list, description="域名状态列表"
    )


class DevHostMutationResponse(BaseSchema):
    """单域名 Dev Hosts 操作响应 / Dev Hosts mutation response for a single domain"""

    runtime: DevHostsRuntimeInfo = Field(..., description="运行时信息")
    domain: DevHostDomainStatus = Field(..., description="操作后的域名状态")


class DevHostsSyncAllResponse(BaseSchema):
    """批量同步 Dev Hosts 响应 / Batch Dev Hosts sync response"""

    runtime: DevHostsRuntimeInfo = Field(..., description="运行时信息")
    domains: list[DevHostDomainStatus] = Field(
        default_factory=list, description="域名状态列表"
    )
    synced: int = Field(..., description="本次同步的域名数量")
    skipped: int = Field(..., description="本次跳过的域名数量")


class TenantDomainCreateRequest(BaseSchema):
    """创建域名请求 / Create domain request"""

    domain: str = Field(
        ...,
        min_length=4,
        max_length=255,
        description="域名（如 app.example.com）",
    )
    is_primary: bool = Field(False, description="是否设为主域名")
    remark: str | None = Field(None, max_length=500, description="备注")

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        """验证域名格式 / Validate domain format"""
        try:
            v = normalize_hostname(v.strip())
        except ValueError as exc:
            raise ValueError(_("tenant_domain.invalid_format")) from exc
        try:
            ipaddress.ip_address(v)
        except ValueError:
            if "." not in v:
                raise ValueError(_("tenant_domain.invalid_format"))
        else:
            raise ValueError(_("tenant_domain.invalid_format"))

        # 禁止使用平台域名 / Forbid platform-reserved suffix
        from app.core.config import settings

        suffix = settings.TENANT_DOMAIN_SUFFIX
        suffix = normalize_tenant_suffix(suffix) if suffix else ""
        if v in settings.platform_domains_list:
            raise ValueError(_("tenant_domain.platform_suffix_forbidden", suffix=v))
        if suffix and (v == suffix[1:] or v.endswith(suffix)):
            raise ValueError(
                _("tenant_domain.platform_suffix_forbidden", suffix=suffix[1:])
            )

        return v


class TenantDomainUpdateRequest(BaseSchema):
    """更新域名请求 / Update domain request"""

    is_primary: bool | None = Field(None, description="是否设为主域名")
    remark: str | None = Field(None, max_length=500, description="备注")


class TenantDomainVerifyRequest(BaseSchema):
    """域名验证请求 / Domain verification request"""

    domain_id: int = Field(..., description="域名 ID")


__all__ = [
    "TenantDomainSimpleResponse",
    "TenantDomainVerificationInfo",
    "TenantDomainResponse",
    "DevHostsRuntimeInfo",
    "DevHostDomainStatus",
    "DevHostsStatusResponse",
    "DevHostMutationResponse",
    "DevHostsSyncAllResponse",
    "TenantDomainCreateRequest",
    "TenantDomainUpdateRequest",
    "TenantDomainVerifyRequest",
]
