/**
 * Domain management - shared type definitions
 * Unified domain-related types for both admin/tenant endpoints
 * 域名管理 - 共享类型定义
 * 统一 admin/tenant 两端的域名相关类型
 */

/** Domain type / 域名类型 */
export type DomainType = 'custom' | 'default';

/** Verification status / 验证状态 */
export type VerificationStatus = 'failed' | 'pending' | 'verified';

/** SSL status / SSL 状态 */
export type SslStatus =
  | 'active'
  | 'expired'
  | 'failed'
  | 'none'
  | 'pending'
  | 'provisioning';

/** SSL type / SSL 类型 */
export type SslType = 'custom' | 'platform';

/** SSL certificate status (certificate record level) / SSL 证书状态（证书记录级别） */
export type SslCertStatus =
  | 'active'
  | 'expired'
  | 'failed'
  | 'pending'
  | 'revoked';

/** DNS verification info (backend returns dns_name/dns_type/dns_value, frontend unifies to host/type/value) / DNS 验证信息（后端返回 dns_name/dns_type/dns_value，前端统一为 host/type/value） */
export interface VerificationInfo {
  dns_name?: string;
  dns_type?: string;
  dns_value?: string;
  host?: string;
  type?: string;
  value?: string;
}

/** Deployment certificate evidence; independent of domain DB SSL status. */
export interface ManagedHttpsInfoRaw {
  configured: boolean;
  covered: boolean;
  status: 'active' | 'expired' | 'expiring' | 'unconfigured';
  expires_at: null | string;
  auto_renew: boolean;
}

export interface ManagedHttpsInfo {
  configured: boolean;
  covered: boolean;
  status: ManagedHttpsInfoRaw['status'];
  expiresAt: null | string;
  autoRenew: boolean;
}

export interface TenantDomainInfoRaw {
  id: number;
  tenant_id: number;
  domain: string;
  domain_type: DomainType;
  is_verified: boolean;
  is_primary: boolean;
  ssl_status: SslStatus;
  managed_https?: ManagedHttpsInfoRaw | null;
  cname_target?: null | string;
  txt_record?: null | string;
  verification_token?: null | string;
  verification_info?: null | VerificationInfo;
  verified_at?: null | string;
  ssl_expires_at?: null | string;
  remark?: null | string;
  created_at: string;
  updated_at: string;
}

export interface TenantDomainInfo {
  id: number;
  tenantId: number;
  domain: string;
  domainType: DomainType;
  isPrimary: boolean;
  isVerified: boolean;
  verificationStatus: VerificationStatus;
  sslStatus: SslStatus;
  managedHttps?: ManagedHttpsInfo | null;
  cnameTarget?: string;
  txtRecord?: string;
  verificationToken?: null | string;
  verificationInfo?: null | VerificationInfo;
  verifiedAt?: string;
  sslExpiresAt?: string;
  remark?: string;
  createdAt: string;
  updatedAt: string;
}

// 中文: 两端使用相同 DNS 字段转换，保留原始字段兼容现有消费者。
// EN: Both surfaces normalize DNS fields together while retaining raw fields for compatibility.
export function transformDomainInfo(
  raw: TenantDomainInfoRaw,
): TenantDomainInfo {
  const info = raw.verification_info;
  return {
    id: raw.id,
    tenantId: raw.tenant_id,
    domain: raw.domain,
    domainType: raw.domain_type || 'custom',
    isPrimary: raw.is_primary,
    isVerified: raw.is_verified,
    verificationStatus: raw.is_verified ? 'verified' : 'pending',
    sslStatus: raw.ssl_status || 'none',
    managedHttps:
      raw.domain_type === 'default' && raw.managed_https
        ? {
            configured: raw.managed_https.configured,
            covered: raw.managed_https.covered,
            status: raw.managed_https.status,
            expiresAt: raw.managed_https.expires_at,
            autoRenew: raw.managed_https.auto_renew,
          }
        : null,
    cnameTarget: raw.cname_target ?? undefined,
    txtRecord: raw.txt_record ?? undefined,
    verificationToken: raw.verification_token,
    verificationInfo: info
      ? {
          ...info,
          host: info.dns_name ?? info.host,
          type: info.dns_type ?? info.type,
          value: info.dns_value ?? info.value,
        }
      : null,
    verifiedAt: raw.verified_at ?? undefined,
    sslExpiresAt: raw.ssl_expires_at ?? undefined,
    remark: raw.remark ?? undefined,
    createdAt: raw.created_at,
    updatedAt: raw.updated_at,
  };
}

/** SSL certificate details (frontend camelCase format) / SSL 证书详情（前端 camelCase 格式） */
export interface SslCertificateInfo {
  id: number;
  domainId: number;
  tenantId: number;
  certType: SslType;
  status: SslCertStatus;
  issuer: null | string;
  serialNumber: null | string;
  issuedAt: null | string;
  expiresAt: null | string;
  autoRenew: boolean;
  hasCertificate: boolean;
  hasPrivateKey: boolean;
  hasChain: boolean;
  lastRenewalAttempt: null | string;
  renewalError: null | string;
  createdAt: string;
  updatedAt: string;
}

/** SSL certificate details (backend snake_case raw format) / SSL 证书详情（后端 snake_case 原始格式） */
export interface SslCertificateInfoRaw {
  id: number;
  domain_id: number;
  tenant_id: number;
  cert_type: SslType;
  status: SslCertStatus;
  issuer: null | string;
  serial_number: null | string;
  issued_at: null | string;
  expires_at: null | string;
  auto_renew: boolean;
  has_certificate: boolean;
  has_private_key: boolean;
  has_chain: boolean;
  last_renewal_attempt: null | string;
  renewal_error: null | string;
  created_at: string;
  updated_at: string;
}

/** SSL certificate snake→camelCase transform / SSL 证书 snake→camelCase 转换 */
export function transformSslCertInfo(
  raw: SslCertificateInfoRaw,
): SslCertificateInfo {
  return {
    id: raw.id,
    domainId: raw.domain_id,
    tenantId: raw.tenant_id,
    certType: raw.cert_type,
    status: raw.status,
    issuer: raw.issuer,
    serialNumber: raw.serial_number,
    issuedAt: raw.issued_at,
    expiresAt: raw.expires_at,
    autoRenew: raw.auto_renew,
    hasCertificate: raw.has_certificate,
    hasPrivateKey: raw.has_private_key,
    hasChain: raw.has_chain,
    lastRenewalAttempt: raw.last_renewal_attempt,
    renewalError: raw.renewal_error,
    createdAt: raw.created_at,
    updatedAt: raw.updated_at,
  };
}
