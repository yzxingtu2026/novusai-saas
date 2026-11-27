// Test type: behavioral
// 中文: 只替换 HTTP 传输，执行真实两端 API 转换。
// EN: Replace HTTP transport only; run both real API adapters.
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getTenantDomainsApi as getTenantDomains } from '../../tenant/domain';
import { getTenantDomainsApi as getAdminDomains } from '../tenant-domain';

const transport = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('#/utils/request', () => ({ requestClient: transport }));

describe('domain adapters share DNS verification contracts', () => {
  beforeEach(() => transport.get.mockReset());
  it('normalizes exact runtime DNS fields identically on both surfaces', async () => {
    const raw = {
      id: 42,
      tenant_id: 8,
      domain: 'erp.example.com',
      domain_type: 'custom',
      is_primary: false,
      is_verified: false,
      ssl_status: 'failed',
      verification_info: {
        dns_name: '_runtime.erp.example.com',
        dns_type: 'TXT',
        dns_value: 'novusai-domain-verification=token',
      },
      ssl_expires_at: null,
      remark: null,
      created_at: '2026-10-10',
      updated_at: '2026-10-10',
    };
    transport.get.mockResolvedValue({ items: [raw], total: 1 });
    const admin = await getAdminDomains(8);
    const tenant = await getTenantDomains();
    expect(admin).toEqual(tenant);
    expect(admin.items[0]).toMatchObject({
      sslStatus: 'failed',
      isVerified: false,
      verificationInfo: {
        host: '_runtime.erp.example.com',
        type: 'TXT',
        value: 'novusai-domain-verification=token',
      },
      sslExpiresAt: undefined,
      remark: undefined,
    });
    expect(transport.get.mock.calls.map(([url]) => url)).toEqual([
      '/admin/tenants/8/domains',
      '/tenant/domains',
    ]);
  });

  it('retains deployment HTTPS evidence independently of default DB SSL status', async () => {
    transport.get.mockResolvedValue({
      items: [
        {
          id: 42,
          tenant_id: 8,
          domain: 'tc0llkrpw.e2.dakkii.cn',
          domain_type: 'default',
          is_primary: true,
          is_verified: true,
          ssl_status: 'active',
          created_at: '2026-10-10',
          updated_at: '2026-10-10',
          managed_https: {
            configured: true,
            covered: false,
            status: 'unconfigured',
            expires_at: '2027-01-10T00:00:00Z',
            auto_renew: false,
          },
        },
      ],
      total: 1,
    });
    const admin = await getAdminDomains(8);
    const tenant = await getTenantDomains();
    expect(admin).toEqual(tenant);
    expect(admin.items[0]?.managedHttps).toEqual({
      configured: true,
      covered: false,
      status: 'unconfigured',
      expiresAt: '2027-01-10T00:00:00Z',
      autoRenew: false,
    });
    expect(admin.items[0]?.sslStatus).toBe('active');
  });
});
