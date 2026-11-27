// Test type: behavioral
// Replace transport and modal shell only; exercise real domain component gates.
import { flushPromises, shallowMount } from '@vue/test-utils';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import DomainManagedHttps from '#/components/business/domain-managed-https.vue';
import TenantDomains from '#/views/tenant/system-mgmt/domains/index.vue';
import TenantSslDrawer from '#/views/tenant/system-mgmt/domains/modules/DomainsSslDrawer.vue';

import DomainsModal from '../DomainsModal.vue';
import AdminSslDrawer from '../DomainsSslDrawer.vue';

const fixtures = vi.hoisted(() => ({
  modal: {} as { onOpenChange: (open: boolean) => Promise<void> },
  drawer: {} as { onOpenChange: (open: boolean) => Promise<void> },
  drawerData: {} as Record<string, unknown>,
  api: {
    getTenantDomainsApi: vi.fn(),
    getTenantDevHostsStatusApi: vi.fn(),
    getSslDetailApi: vi.fn(),
    getTenantSslDetailApi: vi.fn(),
  },
}));

vi.mock('#/api', () => ({ adminApi: fixtures.api }));
vi.mock('#/api/admin/tenant-domain', () => ({
  ...fixtures.api,
  deleteSslCertApi: vi.fn(),
  provisionSslApi: vi.fn(),
  renewSslApi: vi.fn(),
  updateSslAutoRenewApi: vi.fn(),
  uploadSslCertApi: vi.fn(),
}));
vi.mock('#/api/tenant/domain', () => ({
  ...fixtures.api,
  deleteTenantDomainApi: vi.fn(),
  setPrimaryDomainApi: vi.fn(),
  verifyTenantDomainApi: vi.fn(),
  deleteTenantSslCertApi: vi.fn(),
  provisionTenantSslApi: vi.fn(),
  renewTenantSslApi: vi.fn(),
  updateTenantSslAutoRenewApi: vi.fn(),
  uploadTenantSslCertApi: vi.fn(),
}));
vi.mock('#/locales', () => ({ $t: (key: string) => key }));
vi.mock('#/utils', () => ({
  useAccess: () => ({ hasAccessByCodes: () => true }),
}));
vi.mock('#/utils/common', () => ({
  formatDate: (date: string) => date,
  copyToClipboard: vi.fn(),
}));
vi.mock('@vben/icons', () => ({ IconifyIcon: { template: '<i />' } }));
vi.mock('@vben/common-ui', () => ({
  Page: { template: '<div><slot name="extra" /><slot /></div>' },
  useVbenModal: (options: typeof fixtures.modal) => {
    fixtures.modal = options;
    return [
      { template: '<div><slot /></div>' },
      {
        getData: () => ({
          tenantId: 8,
          tenantName: 'Audit',
          tenantCode: 'tc0llkrpw',
        }),
      },
    ];
  },
  useVbenDrawer: (options: typeof fixtures.drawer) => {
    fixtures.drawer = options;
    return [
      { template: '<div><slot /></div>' },
      { getData: () => fixtures.drawerData },
    ];
  },
}));
vi.mock('../DomainsAddDrawer.vue', () => ({ default: {} }));
vi.mock('../DomainsDetailDrawer.vue', () => ({ default: {} }));
vi.mock('../DomainsDnsGuideModal.vue', () => ({ default: {} }));
vi.mock(
  '#/views/tenant/system-mgmt/domains/modules/DomainsAddDrawer.vue',
  () => ({ default: {} }),
);
vi.mock(
  '#/views/tenant/system-mgmt/domains/modules/DomainsDetailDrawer.vue',
  () => ({ default: {} }),
);
vi.mock(
  '#/views/tenant/system-mgmt/domains/modules/DomainsDnsGuideModal.vue',
  () => ({ default: {} }),
);

const managedHttps = {
  configured: true,
  covered: true,
  status: 'active',
  expiresAt: '2027-01-10T00:00:00Z',
  autoRenew: false,
};
const defaultDomain = {
  id: 42,
  tenantId: 8,
  domain: 'tc0llkrpw.e2.dakkii.cn',
  domainType: 'default',
  verificationStatus: 'verified',
  sslStatus: 'active',
  isPrimary: true,
  createdAt: '2026-10-10',
  updatedAt: '2026-10-10',
  managedHttps,
};
const mountOptions = {
  global: {
    renderStubDefaultSlot: true,
    stubs: {
      DomainManagedHttps: false,
      AAlert: false,
      ATooltip: { inheritAttrs: false, template: '<span><slot /></span>' },
    },
    directives: { access: () => {} },
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  fixtures.api.getTenantDomainsApi.mockResolvedValue({
    items: [defaultDomain],
    total: 1,
  });
  fixtures.api.getSslDetailApi.mockResolvedValue(null);
  fixtures.api.getTenantSslDetailApi.mockResolvedValue(null);
  fixtures.api.getTenantDevHostsStatusApi.mockResolvedValue({
    runtime: {
      debug: true,
      enabled: true,
      supported: true,
      osName: 'Linux',
      hostsPath: '/etc/hosts',
      requiresElevation: true,
    },
    domains: [
      {
        domainId: 42,
        eligible: true,
        status: 'missing',
        managed: false,
        reason: 'missing',
        matchedIp: '127.0.0.1',
      },
    ],
  });
});
afterEach(() => vi.unstubAllEnvs());

describe('domain DevHosts capability', () => {
  it('production never requests hosts status or renders hosts controls/hints', async () => {
    vi.stubEnv('DEV', false);
    const wrapper = shallowMount(DomainsModal, mountOptions);
    await fixtures.modal.onOpenChange(true);
    await flushPromises();
    expect(fixtures.api.getTenantDomainsApi.mock.calls).toEqual([[8]]);
    expect(fixtures.api.getTenantDevHostsStatusApi.mock.calls).toEqual([]);
    expect(wrapper.text()).not.toContain('devHosts');
    expect(wrapper.text()).not.toContain('/etc/hosts');
    expect(wrapper.text()).toContain('shared.domain.managedHttps.label');
    expect(wrapper.text()).toContain('shared.domain.managedHttps.details');
    wrapper.unmount();
  });

  it.each([
    { debug: false, enabled: true },
    { debug: true, enabled: false },
  ])(
    'dEV hides hosts when backend capability is $debug/$enabled',
    async (runtime) => {
      vi.stubEnv('DEV', true);
      fixtures.api.getTenantDevHostsStatusApi.mockResolvedValue({
        runtime,
        domains: [
          {
            domainId: 42,
            status: 'missing',
            reason: 'missing',
            matchedIp: '127.0.0.1',
          },
        ],
      });
      const wrapper = shallowMount(DomainsModal, mountOptions);
      await fixtures.modal.onOpenChange(true);
      await flushPromises();
      expect(fixtures.api.getTenantDevHostsStatusApi.mock.calls).toEqual([[8]]);
      expect(wrapper.text()).not.toContain('devHosts');
      expect(wrapper.text()).not.toContain('127.0.0.1');
      wrapper.unmount();
    },
  );

  it('dEV with backend DEBUG and enabled capability exposes hosts controls', async () => {
    vi.stubEnv('DEV', true);
    const wrapper = shallowMount(DomainsModal, mountOptions);
    await fixtures.modal.onOpenChange(true);
    await flushPromises();
    expect(wrapper.text()).toContain('admin.tenant.domain.devHosts.title');
    expect(wrapper.text()).toContain('/etc/hosts');
    expect(wrapper.text()).toContain('admin.tenant.domain.devHosts.sync');
    wrapper.unmount();
  });
});

describe('default and custom domain HTTPS entries', () => {
  it('tenant card uses deployment metadata and exposes custom setup only after verification', async () => {
    fixtures.api.getTenantDomainsApi.mockResolvedValue({
      items: [
        {
          ...defaultDomain,
          managedHttps: {
            ...managedHttps,
            covered: false,
            status: 'unconfigured',
          },
        },
        {
          ...defaultDomain,
          id: 43,
          domainType: 'custom',
          domain: 'custom.example.com',
          managedHttps: null,
        },
        {
          ...defaultDomain,
          id: 44,
          domainType: 'custom',
          domain: 'pending.example.com',
          managedHttps: null,
          verificationStatus: 'pending',
        },
      ],
      total: 3,
    });
    const wrapper = shallowMount(TenantDomains, mountOptions);
    await flushPromises();
    expect(wrapper.text()).toContain('shared.domain.managedHttps.uncovered');
    expect(wrapper.text()).toContain('shared.domain.managedHttps.details');
    expect(
      wrapper.text().split('shared.domain.managedHttps.setup'),
    ).toHaveLength(2);
    expect(wrapper.text()).not.toContain('devHosts');
    expect(fixtures.api.getTenantDevHostsStatusApi.mock.calls).toEqual([]);
    wrapper.unmount();
  });

  it.each([
    ['admin', AdminSslDrawer],
    ['tenant', TenantSslDrawer],
  ] as const)(
    '%s default drawer shows managed expiry/manual renewal without a DB cert query',
    async (_name, Component) => {
      fixtures.drawerData = {
        domainId: 42,
        tenantId: 8,
        domain: defaultDomain.domain,
        isDefault: true,
        managedHttps,
      };
      const wrapper = shallowMount(Component, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(wrapper.findComponent(DomainManagedHttps).props('info')).toEqual(
        managedHttps,
      );
      expect(wrapper.text()).toContain(managedHttps.expiresAt);
      expect(wrapper.text()).toContain(
        'shared.domain.managedHttps.manualRenew',
      );
      expect(wrapper.text()).not.toContain('actions.provision');
      expect(wrapper.text()).not.toContain('certificateDeploymentHint');
      expect(fixtures.api.getSslDetailApi.mock.calls).toEqual([]);
      expect(fixtures.api.getTenantSslDetailApi.mock.calls).toEqual([]);
      wrapper.unmount();
    },
  );

  it.each([
    ['admin', AdminSslDrawer],
    ['tenant', TenantSslDrawer],
  ] as const)(
    '%s verified custom drawer exposes issuance with proxy deployment explanation',
    async (name, Component) => {
      fixtures.drawerData = {
        domainId: 43,
        tenantId: 8,
        domain: 'custom.example.com',
        isDefault: false,
      };
      const wrapper = shallowMount(Component, mountOptions);
      await fixtures.drawer.onOpenChange(true);
      await flushPromises();
      expect(wrapper.text()).toContain(
        'shared.domain.certificateDeploymentHint',
      );
      expect(wrapper.text()).toContain(
        name === 'admin'
          ? 'admin.tenant.domain.ssl.actions.provision'
          : 'tenant.system.domain.ssl.provision',
      );
      expect(wrapper.findComponent(DomainManagedHttps).exists()).toBe(false);
      expect(
        name === 'admin'
          ? fixtures.api.getSslDetailApi.mock.calls
          : fixtures.api.getTenantSslDetailApi.mock.calls,
      ).toEqual(name === 'admin' ? [[8, 43]] : [[43]]);
      wrapper.unmount();
    },
  );
});
