// Test type: behavioral
// Exercise real drawer templates and API adapters; replace only HTTP and UI shells.
import type { SslCertificateInfoRaw } from '#/types/domain';

import { flushPromises, shallowMount } from '@vue/test-utils';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import TenantDetailDrawer from '#/views/tenant/system-mgmt/domains/modules/DomainsDetailDrawer.vue';
import TenantSslDrawer from '#/views/tenant/system-mgmt/domains/modules/DomainsSslDrawer.vue';

import AdminDetailDrawer from '../DomainsDetailDrawer.vue';
import AdminSslDrawer from '../DomainsSslDrawer.vue';

const fixtures = vi.hoisted(() => ({
  drawer: {} as { onOpenChange: (open: boolean) => Promise<void> },
  drawerData: {} as Record<string, unknown>,
  deniedPermissions: new Set<string>(),
  transport: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  success: vi.fn(),
}));

vi.mock('#/utils/request', () => ({ requestClient: fixtures.transport }));
vi.mock('#/api', async () => ({
  adminApi: await import('#/api/admin/tenant-domain'),
}));
vi.mock('#/locales', () => ({ $t: (key: string) => key }));
vi.mock('#/utils', () => ({
  useAccess: () => ({
    hasAccessByCodes: (codes: string[]) =>
      !codes.some((code) => fixtures.deniedPermissions.has(code)),
  }),
}));
vi.mock('#/utils/common', () => ({ formatDate: (date: string) => date }));
vi.mock('@vben/icons', () => ({ IconifyIcon: { template: '<i />' } }));
vi.mock('@vben/common-ui', () => ({
  useVbenDrawer: (options: typeof fixtures.drawer) => {
    fixtures.drawer = options;
    return [
      { template: '<div><slot /></div>' },
      { getData: () => fixtures.drawerData },
    ];
  },
}));
vi.mock('ant-design-vue', async (importOriginal) => ({
  ...(await importOriginal<typeof import('ant-design-vue')>()),
  message: { success: fixtures.success },
}));
vi.mock('../DomainsDnsGuideModal.vue', () => ({ default: {} }));
vi.mock(
  '#/views/tenant/system-mgmt/domains/modules/DomainsDnsGuideModal.vue',
  () => ({ default: {} }),
);

const managedHttps = {
  configured: true,
  covered: true,
  status: 'expired',
  expiresAt: '2026-09-01T00:00:00Z',
  autoRenew: false,
};
const defaultDomainRaw = {
  id: 42,
  tenant_id: 8,
  domain: 'tc0llkrpw.e2.dakkii.cn',
  domain_type: 'default',
  is_verified: true,
  is_primary: true,
  ssl_status: 'active',
  ssl_expires_at: '2030-01-01T00:00:00Z',
  created_at: '2026-01-01',
  updated_at: '2026-01-01',
  managed_https: {
    configured: true,
    covered: true,
    status: 'expired',
    expires_at: managedHttps.expiresAt,
    auto_renew: false,
  },
};
function certificate(
  status: SslCertificateInfoRaw['status'],
  certType: SslCertificateInfoRaw['cert_type'] = 'platform',
): SslCertificateInfoRaw {
  return {
    id: 51,
    domain_id: 42,
    tenant_id: 8,
    cert_type: certType,
    status,
    issuer: 'Test CA',
    serial_number: '51',
    issued_at: '2026-01-01T00:00:00Z',
    expires_at:
      status === 'expired' ? managedHttps.expiresAt : '2027-01-01T00:00:00Z',
    auto_renew: false,
    has_certificate: true,
    has_private_key: true,
    has_chain: true,
    last_renewal_attempt: null,
    renewal_error: null,
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
  };
}
const mountOptions = {
  global: {
    renderStubDefaultSlot: true,
    stubs: {
      DomainManagedHttps: false,
      AAlert: false,
      AButton: {
        emits: ['click'],
        template: '<button @click="$emit(\'click\')"><slot /></button>',
      },
      ASwitch: {
        name: 'ASwitch',
        emits: ['change'],
        props: {
          checked: { type: Boolean, default: false },
          loading: { type: Boolean, default: false },
        },
        template:
          '<button data-switch :data-checked="checked" @click="$emit(\'change\', !checked)">toggle</button>',
      },
    },
  },
};
const surfaces = [
  {
    name: 'admin',
    SslDrawer: AdminSslDrawer,
    DetailDrawer: AdminDetailDrawer,
    prefix: '/admin/tenants/8/domains/42',
    renewLabel: 'admin.tenant.domain.ssl.actions.renew',
  },
  {
    name: 'tenant',
    SslDrawer: TenantSslDrawer,
    DetailDrawer: TenantDetailDrawer,
    prefix: '/tenant/domains/42',
    renewLabel: 'tenant.system.domain.ssl.renew',
  },
] as const;

beforeEach(() => {
  vi.clearAllMocks();
  fixtures.deniedPermissions.clear();
  fixtures.drawerData = {
    domainId: 42,
    tenantId: 8,
    domain: 'custom.example.com',
    isDefault: false,
  };
  fixtures.transport.get.mockResolvedValue(certificate('active'));
  fixtures.transport.post.mockResolvedValue(undefined);
});

for (const surface of surfaces) {
  describe(`${surface.name} SSL renewal contract`, () => {
    it('default detail shows managed expiry instead of the stale active DB field', async () => {
      fixtures.transport.get.mockResolvedValue(defaultDomainRaw);
      const wrapper = shallowMount(surface.DetailDrawer, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(fixtures.transport.get.mock.calls).toEqual([
        [surface.prefix, undefined],
      ]);
      expect(wrapper.text()).toContain('shared.domain.managedHttps.expired');
      expect(wrapper.text()).toContain(managedHttps.expiresAt);
      expect(wrapper.text()).not.toContain('2030-01-01');
      expect(wrapper.text()).not.toContain(
        surface.name === 'admin'
          ? 'admin.tenant.domain.ssl.status.active'
          : 'tenant.system.domain.ssl.active',
      );
      wrapper.unmount();
    });

    it.each(['active', 'expired'] as const)(
      'current %s platform-issued certificate can renew and toggle auto renewal',
      async (status) => {
        const cert = certificate(status);
        fixtures.transport.get.mockResolvedValue(cert);
        fixtures.transport.put.mockResolvedValue({ ...cert, auto_renew: true });
        const wrapper = shallowMount(surface.SslDrawer, mountOptions);
        await fixtures.drawer.onOpenChange(true);
        await flushPromises();
        const renew = wrapper
          .findAll('button')
          .find((button) => button.text() === surface.renewLabel);
        expect(renew?.exists()).toBe(true);
        if (!renew)
          throw new Error('Expected the current certificate renewal button');
        await renew.trigger('click');
        await flushPromises();
        expect(fixtures.transport.post.mock.calls).toEqual([
          [`${surface.prefix}/ssl/renew`, {}, undefined],
        ]);
        expect(wrapper.find('[data-switch]').exists()).toBe(true);
        await wrapper.find('[data-switch]').trigger('click');
        await flushPromises();
        expect(fixtures.transport.put.mock.calls).toEqual([
          [`${surface.prefix}/ssl/auto-renew`, { auto_renew: true }, undefined],
        ]);
        expect(wrapper.find('[data-switch]').attributes('data-checked')).toBe(
          'true',
        );
        wrapper.unmount();
      },
    );

    it.each(['revoked', 'failed', 'pending'] as const)(
      '%s platform certificate exposes no renewal action or toggle',
      async (status) => {
        fixtures.transport.get.mockResolvedValue(certificate(status));
        const wrapper = shallowMount(surface.SslDrawer, mountOptions);
        await fixtures.drawer.onOpenChange(true);
        await flushPromises();
        expect(
          wrapper.findAll('button').map((button) => button.text()),
        ).not.toContain(surface.renewLabel);
        expect(wrapper.find('[data-switch]').exists()).toBe(false);
        expect(fixtures.transport.post.mock.calls).toEqual([]);
        expect(fixtures.transport.put.mock.calls).toEqual([]);
        wrapper.unmount();
      },
    );

    it('renewal permissions hide both controls without changing read access', async () => {
      fixtures.deniedPermissions.add('tenant_domain:ssl_renew');
      fixtures.deniedPermissions.add('tenant_domain:ssl_auto_renew');
      const wrapper = shallowMount(surface.SslDrawer, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(wrapper.text()).toContain('Test CA');
      expect(
        wrapper.findAll('button').map((button) => button.text()),
      ).not.toContain(surface.renewLabel);
      expect(wrapper.find('[data-switch]').exists()).toBe(false);
      expect(fixtures.transport.post.mock.calls).toEqual([]);
      expect(fixtures.transport.put.mock.calls).toEqual([]);
      wrapper.unmount();
    });

    it('uploaded certificates never expose application renewal controls', async () => {
      fixtures.transport.get.mockResolvedValue(certificate('active', 'custom'));
      const wrapper = shallowMount(surface.SslDrawer, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(
        wrapper.findAll('button').map((button) => button.text()),
      ).not.toContain(surface.renewLabel);
      expect(wrapper.find('[data-switch]').exists()).toBe(false);
      wrapper.unmount();
    });

    it('default shared HTTPS never loads or modifies a per-domain certificate', async () => {
      fixtures.drawerData = {
        ...fixtures.drawerData,
        isDefault: true,
        managedHttps,
      };
      const wrapper = shallowMount(surface.SslDrawer, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(wrapper.text()).toContain('shared.domain.managedHttps.expired');
      expect(wrapper.text()).toContain(
        'shared.domain.managedHttps.manualRenew',
      );
      expect(wrapper.findAll('button')).toHaveLength(0);
      expect(fixtures.transport.get.mock.calls).toEqual([]);
      expect(fixtures.transport.post.mock.calls).toEqual([]);
      expect(fixtures.transport.put.mock.calls).toEqual([]);
      wrapper.unmount();
    });

    it('current certificate explains publication is unconfirmed and gives release/check steps', async () => {
      const wrapper = shallowMount(surface.SslDrawer, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(wrapper.text()).toContain('shared.domain.publicationUnconfirmed');
      expect(wrapper.text()).toContain('shared.domain.publicationInstallStep');
      expect(wrapper.text()).toContain('shared.domain.publicationVerifyStep');
      wrapper.unmount();
    });

    it('a rejected toggle keeps the prior preference and emits no success', async () => {
      fixtures.transport.put.mockRejectedValueOnce(
        new Error('Rejected by server'),
      );
      const wrapper = shallowMount(surface.SslDrawer, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      await wrapper.find('[data-switch]').trigger('click');
      await flushPromises();
      expect(wrapper.find('[data-switch]').attributes('data-checked')).toBe(
        'false',
      );
      expect(fixtures.success.mock.calls).toEqual([]);
      wrapper.unmount();
    });
  });
}
