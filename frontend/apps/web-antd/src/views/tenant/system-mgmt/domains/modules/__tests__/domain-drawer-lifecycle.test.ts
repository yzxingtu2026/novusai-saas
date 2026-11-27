// Test type: behavioral
// 中文：运行真实 SFC 和 API 转换，仅替换 HTTP 与展示外壳。
// EN: Exercise real SFCs and API adapters, replacing HTTP and presentation shells only.
import type { VueWrapper } from '@vue/test-utils';

import type {
  SslCertificateInfoRaw,
  TenantDomainInfoRaw,
} from '#/types/domain';

import { flushPromises, shallowMount } from '@vue/test-utils';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AdminDetailDrawer from '#/views/admin/tenant/list/modules/DomainsDetailDrawer.vue';
import AdminDnsGuideModal from '#/views/admin/tenant/list/modules/DomainsDnsGuideModal.vue';
import AdminSslDrawer from '#/views/admin/tenant/list/modules/DomainsSslDrawer.vue';

import DomainsDetailDrawer from '../DomainsDetailDrawer.vue';
import DomainsDnsGuideModal from '../DomainsDnsGuideModal.vue';
import DomainsSslDrawer from '../DomainsSslDrawer.vue';

const fixtures = vi.hoisted(() => ({
  drawer: {} as { onOpenChange: (open: boolean) => Promise<void> },
  modal: {} as { onOpenChange: (open: boolean) => void },
  data: {} as Record<string, unknown>,
  transport: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  success: vi.fn(),
  warning: vi.fn(),
  close: vi.fn(),
  confirm: vi.fn(),
  destroyConfirmation: vi.fn(),
}));

vi.mock('#/utils/request', () => ({ requestClient: fixtures.transport }));
vi.mock('#/api', async () => ({
  adminApi: await import('#/api/admin/tenant-domain'),
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
  useVbenDrawer: (options: typeof fixtures.drawer) => {
    fixtures.drawer = options;
    return [
      { template: '<section><slot /></section>' },
      { getData: () => fixtures.data },
    ];
  },
  useVbenModal: (options: typeof fixtures.modal) => {
    fixtures.modal = options;
    return [
      { template: '<section><slot /></section>' },
      { getData: () => fixtures.data, close: fixtures.close },
    ];
  },
}));
vi.mock('ant-design-vue', async (importOriginal) => ({
  ...(await importOriginal<typeof import('ant-design-vue')>()),
  message: { success: fixtures.success, warning: fixtures.warning },
  Modal: { confirm: fixtures.confirm },
}));

const mountOptions = {
  global: {
    renderStubDefaultSlot: true,
    directives: { access: () => {} },
    stubs: {
      ASpin: {
        props: ['spinning'],
        template:
          '<div data-spin :data-spinning="String(!!spinning)"><slot /></div>',
      },
      AButton: {
        emits: ['click'],
        props: ['loading', 'disabled'],
        template:
          '<button :data-loading="String(!!loading)" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
      },
      ATextarea: {
        props: ['value'],
        emits: ['update:value'],
        template:
          '<textarea :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
      },
      ASwitch: {
        props: ['checked', 'loading'],
        emits: ['change'],
        template:
          '<button data-switch :data-checked="String(!!checked)" :data-loading="String(!!loading)" @click="$emit(\'change\', !checked)">toggle</button>',
      },
    },
  },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function domain(id: number): TenantDomainInfoRaw {
  return {
    id,
    tenant_id: id === 41 ? 8 : 9,
    domain: `domain-${id}.example.com`,
    domain_type: 'custom',
    is_verified: true,
    is_primary: false,
    ssl_status: 'active',
    remark: `remark-${id}`,
    created_at: '2026-11-08T00:00:00Z',
    updated_at: '2026-11-08T00:00:00Z',
  };
}

function certificate(id: number): SslCertificateInfoRaw {
  return {
    id: id + 100,
    domain_id: id,
    tenant_id: id === 41 ? 8 : 9,
    cert_type: 'platform',
    status: 'active',
    issuer: `issuer-${id}`,
    serial_number: `${id}`,
    issued_at: '2026-11-01T00:00:00Z',
    expires_at: '2027-11-01T00:00:00Z',
    auto_renew: false,
    has_certificate: true,
    has_private_key: true,
    has_chain: true,
    last_renewal_attempt: null,
    renewal_error: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  };
}

function setOwner(id: number) {
  fixtures.data = {
    domainId: id,
    tenantId: id === 41 ? 8 : 9,
    domain: `domain-${id}.example.com`,
    isDefault: false,
  };
}

function button(wrapper: VueWrapper, label: string) {
  const result = wrapper
    .findAll('button')
    .find((item) => item.text() === label);
  if (!result) throw new Error(`Missing button: ${label}`);
  return result;
}

async function openDrawer(id: number) {
  setOwner(id);
  await fixtures.drawer.onOpenChange(true);
  await flushPromises();
}

async function changeDrawer(id: number) {
  await fixtures.drawer.onOpenChange(false);
  await openDrawer(id);
}

async function openUploadDraft(
  wrapper: VueWrapper,
  owner: number,
  label: string,
) {
  await button(wrapper, label).trigger('click');
  const inputs = wrapper.findAll('textarea');
  expect(inputs).toHaveLength(3);
  const [certificateInput, privateKeyInput, chainInput] = inputs;
  if (!certificateInput || !privateKeyInput || !chainInput)
    throw new Error('Missing certificate upload fields');
  await certificateInput.setValue(`certificate-${owner}`);
  await privateKeyInput.setValue(`private-key-${owner}`);
  await chainInput.setValue(`chain-${owner}`);
}

async function reachVerify(wrapper: VueWrapper, id: number, next: string) {
  setOwner(id);
  fixtures.modal.onOpenChange(true);
  await flushPromises();
  await button(wrapper, next).trigger('click');
  await button(wrapper, next).trigger('click');
}

beforeEach(() => {
  vi.clearAllMocks();
  fixtures.transport.get.mockImplementation(async (url: string) => {
    const id = Number(url.match(/\/domains\/(\d+)/)?.[1]);
    return url.endsWith('/ssl') ? certificate(id) : domain(id);
  });
  fixtures.transport.put.mockResolvedValue(domain(42));
  fixtures.transport.post.mockResolvedValue(domain(42));
  fixtures.transport.delete.mockResolvedValue(undefined);
  fixtures.close.mockImplementation(() => fixtures.modal.onOpenChange(false));
  fixtures.confirm.mockReturnValue({ destroy: fixtures.destroyConfirmation });
});

afterEach(() => vi.restoreAllMocks());

const surfaces = [
  {
    name: 'tenant',
    Detail: DomainsDetailDrawer,
    Ssl: DomainsSslDrawer,
    Dns: DomainsDnsGuideModal,
    path: (id: number) => `/tenant/domains/${id}`,
    edit: 'common.edit',
    save: 'common.save',
    confirm: 'common.confirm',
    verify: 'tenant.system.domain.verify',
    next: 'common.nextStep',
    ssl: (action: string) =>
      `tenant.system.domain.ssl.${action === 'delete' ? 'deleteCert' : action}`,
  },
  {
    name: 'admin',
    Detail: AdminDetailDrawer,
    Ssl: AdminSslDrawer,
    Dns: AdminDnsGuideModal,
    path: (id: number) => `/admin/tenants/${id === 41 ? 8 : 9}/domains/${id}`,
    edit: 'shared.common.edit',
    save: 'shared.common.save',
    confirm: 'shared.common.confirm',
    verify: 'admin.tenant.domain.verifyDomain',
    next: 'admin.tenant.domain.dnsGuide.nextStep',
    ssl: (action: string) => `admin.tenant.domain.ssl.actions.${action}`,
  },
] as const;

for (const surface of surfaces) {
  describe(`${surface.name} domain detail ownership`, () => {
    it('a current save refreshes both detail and parent once', async () => {
      const wrapper = shallowMount(surface.Detail, mountOptions);
      await openDrawer(42);
      await button(wrapper, surface.edit).trigger('click');
      await wrapper.find('textarea').setValue('  updated remark  ');
      fixtures.transport.get.mockResolvedValue({
        ...domain(42),
        remark: 'updated remark',
      });
      await button(wrapper, surface.save).trigger('click');
      await flushPromises();
      expect(fixtures.transport.put.mock.calls).toEqual([
        [surface.path(42), { remark: 'updated remark' }, undefined],
      ]);
      expect(fixtures.transport.get.mock.calls).toEqual([
        [surface.path(42), undefined],
        [surface.path(42), undefined],
      ]);
      expect(wrapper.text()).toContain('updated remark');
      expect(wrapper.find('textarea').exists()).toBe(false);
      expect(wrapper.emitted('success')).toEqual([[]]);
      expect(fixtures.success.mock.calls).toHaveLength(1);
      wrapper.unmount();
    });

    it('a rejected save retains the edited draft and never retries automatically', async () => {
      fixtures.transport.put.mockRejectedValueOnce(
        new Error('fictional transport failure'),
      );
      const wrapper = shallowMount(surface.Detail, mountOptions);
      await openDrawer(42);
      await button(wrapper, surface.edit).trigger('click');
      await wrapper.find('textarea').setValue('retain this draft');
      await button(wrapper, surface.save).trigger('click');
      await flushPromises();
      expect(wrapper.find('textarea').element.value).toBe('retain this draft');
      expect(button(wrapper, surface.save).attributes('data-loading')).toBe(
        'false',
      );
      expect(fixtures.transport.put.mock.calls).toEqual([
        [surface.path(42), { remark: 'retain this draft' }, undefined],
      ]);
      expect(fixtures.transport.get.mock.calls).toEqual([
        [surface.path(42), undefined],
      ]);
      expect(fixtures.success.mock.calls).toEqual([]);
      expect(wrapper.emitted('success')).toBeUndefined();
      wrapper.unmount();
    });

    it('a late A read cannot release the loading owner of a pending B read', async () => {
      const first = deferred<TenantDomainInfoRaw>();
      const second = deferred<TenantDomainInfoRaw>();
      fixtures.transport.get
        .mockImplementationOnce(() => first.promise)
        .mockImplementationOnce(() => second.promise);
      const wrapper = shallowMount(surface.Detail, mountOptions);
      setOwner(41);
      const firstOpen = fixtures.drawer.onOpenChange(true);
      await fixtures.drawer.onOpenChange(false);
      setOwner(42);
      const secondOpen = fixtures.drawer.onOpenChange(true);
      first.resolve(domain(41));
      await firstOpen;
      await flushPromises();
      expect(wrapper.find('[data-spin]').attributes('data-spinning')).toBe(
        'true',
      );
      expect(wrapper.text()).not.toContain('domain-41.example.com');
      second.resolve(domain(42));
      await secondOpen;
      await flushPromises();
      expect(wrapper.find('[data-spin]').attributes('data-spinning')).toBe(
        'false',
      );
      expect(wrapper.text()).toContain('domain-42.example.com');
      wrapper.unmount();
    });
    it('a late A detail cannot replace B or pair A remark with B domain ID', async () => {
      const pending = deferred<TenantDomainInfoRaw>();
      fixtures.transport.get.mockImplementationOnce(() => pending.promise);
      const wrapper = shallowMount(surface.Detail, mountOptions);
      setOwner(41);
      const firstOpen = fixtures.drawer.onOpenChange(true);
      await changeDrawer(42);
      pending.resolve(domain(41));
      await firstOpen;
      await flushPromises();
      await button(wrapper, surface.edit).trigger('click');
      await button(wrapper, surface.save).trigger('click');
      await flushPromises();
      expect(fixtures.transport.put.mock.calls).toEqual([
        [surface.path(42), { remark: 'remark-42' }, undefined],
      ]);
      expect(wrapper.text()).toContain('domain-42.example.com');
      expect(wrapper.text()).not.toContain('domain-41.example.com');
      wrapper.unmount();
    });

    it('clearing a remark submits an explicit empty string', async () => {
      const wrapper = shallowMount(surface.Detail, mountOptions);
      await openDrawer(42);
      await button(wrapper, surface.edit).trigger('click');
      await wrapper.find('textarea').setValue('   ');
      await button(wrapper, surface.save).trigger('click');
      await flushPromises();
      expect(fixtures.transport.put.mock.calls).toEqual([
        [surface.path(42), { remark: '' }, undefined],
      ]);
      wrapper.unmount();
    });

    it('two same-tick saves dispatch only one mutation', async () => {
      const pending = deferred<TenantDomainInfoRaw>();
      fixtures.transport.put.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Detail, mountOptions);
      await openDrawer(42);
      await button(wrapper, surface.edit).trigger('click');
      const save = button(wrapper, surface.save);
      await Promise.all([save.trigger('click'), save.trigger('click')]);
      expect(fixtures.transport.put.mock.calls).toEqual([
        [surface.path(42), { remark: 'remark-42' }, undefined],
      ]);
      pending.resolve(domain(42));
      await flushPromises();
      wrapper.unmount();
    });

    it.each(['close-reopen', 'unmount'] as const)(
      'a committed save preserves parent refresh without stale panel effects after %s',
      async (transition) => {
        const pending = deferred<TenantDomainInfoRaw>();
        fixtures.transport.put.mockImplementation(() => pending.promise);
        const wrapper = shallowMount(surface.Detail, mountOptions);
        await openDrawer(41);
        await button(wrapper, surface.edit).trigger('click');
        await button(wrapper, surface.save).trigger('click');
        if (transition === 'unmount') wrapper.unmount();
        else await changeDrawer(42);
        const readCount = fixtures.transport.get.mock.calls.length;
        pending.resolve(domain(41));
        await flushPromises();
        expect(fixtures.transport.get.mock.calls).toHaveLength(readCount);
        expect(fixtures.success.mock.calls).toEqual([]);
        expect(wrapper.emitted('success')).toEqual(
          transition === 'unmount' ? undefined : [[]],
        );
        expect(fixtures.transport.put.mock.calls).toEqual([
          [surface.path(41), { remark: 'remark-41' }, undefined],
        ]);
        if (transition !== 'unmount') wrapper.unmount();
      },
    );
  });

  describe(`${surface.name} SSL mutation ownership`, () => {
    it('a successful upload refreshes its certificate and clears private material', async () => {
      fixtures.transport.post.mockResolvedValue(certificate(42));
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(42);
      await openUploadDraft(wrapper, 42, surface.ssl('upload'));
      await button(wrapper, surface.confirm).trigger('click');
      await flushPromises();
      expect(fixtures.transport.post.mock.calls).toEqual([
        [
          `${surface.path(42)}/ssl/upload`,
          {
            certificate: 'certificate-42',
            private_key: 'private-key-42',
            certificate_chain: 'chain-42',
          },
          undefined,
        ],
      ]);
      expect(fixtures.transport.get.mock.calls).toEqual([
        [`${surface.path(42)}/ssl`, undefined],
        [`${surface.path(42)}/ssl`, undefined],
      ]);
      expect(fixtures.success.mock.calls).toHaveLength(1);
      await button(wrapper, surface.ssl('upload')).trigger('click');
      expect(
        wrapper.findAll('textarea').map((input) => input.element.value),
      ).toEqual(['', '', '']);
      wrapper.unmount();
    });

    it('a failed upload retains the draft without replaying the request', async () => {
      fixtures.transport.post.mockRejectedValueOnce(
        new Error('fictional upload failure'),
      );
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(42);
      await openUploadDraft(wrapper, 42, surface.ssl('upload'));
      await button(wrapper, surface.confirm).trigger('click');
      await flushPromises();
      expect(
        wrapper.findAll('textarea').map((input) => input.element.value),
      ).toEqual(['certificate-42', 'private-key-42', 'chain-42']);
      expect(button(wrapper, surface.confirm).attributes('data-loading')).toBe(
        'false',
      );
      expect(fixtures.transport.post.mock.calls).toHaveLength(1);
      expect(fixtures.transport.get.mock.calls).toEqual([
        [`${surface.path(42)}/ssl`, undefined],
      ]);
      expect(fixtures.success.mock.calls).toEqual([]);
      wrapper.unmount();
    });

    it('closing clears uploaded private material even without submission', async () => {
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(42);
      await openUploadDraft(wrapper, 42, surface.ssl('upload'));
      await changeDrawer(42);
      await button(wrapper, surface.ssl('upload')).trigger('click');
      expect(
        wrapper.findAll('textarea').map((input) => input.element.value),
      ).toEqual(['', '', '']);
      expect(fixtures.transport.post.mock.calls).toEqual([]);
      wrapper.unmount();
    });

    it('a late A completion does not release the loading owner of B mutation', async () => {
      const first = deferred<SslCertificateInfoRaw>();
      const second = deferred<SslCertificateInfoRaw>();
      fixtures.transport.put
        .mockImplementationOnce(() => first.promise)
        .mockImplementationOnce(() => second.promise);
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(41);
      await wrapper.find('[data-switch]').trigger('click');
      await changeDrawer(42);
      await wrapper.find('[data-switch]').trigger('click');
      first.resolve(certificate(41));
      await flushPromises();
      expect(wrapper.find('[data-switch]').attributes('data-loading')).toBe(
        'true',
      );
      expect(wrapper.text()).toContain('issuer-42');
      expect(fixtures.success.mock.calls).toEqual([]);
      second.resolve({ ...certificate(42), auto_renew: true });
      await flushPromises();
      expect(wrapper.find('[data-switch]').attributes('data-loading')).toBe(
        'false',
      );
      expect(wrapper.find('[data-switch]').attributes('data-checked')).toBe(
        'true',
      );
      expect(fixtures.success.mock.calls).toHaveLength(1);
      wrapper.unmount();
    });

    it('a deletion owns one confirmation and one write before resolving', async () => {
      const pending = deferred<undefined>();
      fixtures.transport.delete.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(42);
      const remove = button(wrapper, surface.ssl('delete'));
      await Promise.all([remove.trigger('click'), remove.trigger('click')]);
      expect(fixtures.confirm.mock.calls).toHaveLength(1);
      const confirmation = fixtures.confirm.mock.calls[0]?.[0] as {
        onOk: () => Promise<void>;
      };
      const completion = confirmation.onOk();
      await confirmation.onOk();
      expect(fixtures.transport.delete.mock.calls).toEqual([
        [`${surface.path(42)}/ssl`, undefined],
      ]);
      fixtures.transport.get.mockResolvedValue(null);
      pending.resolve(undefined);
      await completion;
      await flushPromises();
      expect(wrapper.text()).not.toContain('issuer-42');
      expect(fixtures.success.mock.calls).toHaveLength(1);
      wrapper.unmount();
    });
    it('a late toggle for A cannot replace B certificate', async () => {
      const pending = deferred<SslCertificateInfoRaw>();
      fixtures.transport.put.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(41);
      await wrapper.find('[data-switch]').trigger('click');
      await changeDrawer(42);
      pending.resolve({ ...certificate(41), auto_renew: true });
      await flushPromises();
      expect(wrapper.text()).toContain('issuer-42');
      expect(wrapper.text()).not.toContain('issuer-41');
      expect(wrapper.find('[data-switch]').attributes('data-checked')).toBe(
        'false',
      );
      expect(fixtures.success.mock.calls).toEqual([]);
      expect(fixtures.transport.put.mock.calls).toEqual([
        [`${surface.path(41)}/ssl/auto-renew`, { auto_renew: true }, undefined],
      ]);
      wrapper.unmount();
    });

    it('two same-tick toggles dispatch only one mutation', async () => {
      const pending = deferred<SslCertificateInfoRaw>();
      fixtures.transport.put.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(41);
      const toggle = wrapper.find('[data-switch]');
      await Promise.all([toggle.trigger('click'), toggle.trigger('click')]);
      expect(fixtures.transport.put.mock.calls).toEqual([
        [`${surface.path(41)}/ssl/auto-renew`, { auto_renew: true }, undefined],
      ]);
      pending.resolve(certificate(41));
      await flushPromises();
      wrapper.unmount();
    });

    it('a completed A upload preserves B certificate/private-key draft', async () => {
      const pending = deferred<SslCertificateInfoRaw>();
      fixtures.transport.post.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(41);
      await openUploadDraft(wrapper, 41, surface.ssl('upload'));
      await button(wrapper, surface.confirm).trigger('click');
      await changeDrawer(42);
      await openUploadDraft(wrapper, 42, surface.ssl('upload'));
      const readCount = fixtures.transport.get.mock.calls.length;
      pending.resolve(certificate(41));
      await flushPromises();
      expect(
        wrapper.findAll('textarea').map((input) => input.element.value),
      ).toEqual(['certificate-42', 'private-key-42', 'chain-42']);
      expect(fixtures.transport.get.mock.calls).toHaveLength(readCount);
      expect(fixtures.success.mock.calls).toEqual([]);
      expect(fixtures.transport.post.mock.calls).toEqual([
        [
          `${surface.path(41)}/ssl/upload`,
          {
            certificate: 'certificate-41',
            private_key: 'private-key-41',
            certificate_chain: 'chain-41',
          },
          undefined,
        ],
      ]);
      wrapper.unmount();
    });

    it.each(['renew', 'provision', 'upload', 'toggle'] as const)(
      'a completed %s after disposal cannot toast or reload',
      async (action) => {
        const pending = deferred<SslCertificateInfoRaw>();
        if (action === 'provision')
          fixtures.transport.get.mockResolvedValue(null);
        fixtures.transport.post.mockImplementation(() => pending.promise);
        fixtures.transport.put.mockImplementation(() => pending.promise);
        const wrapper = shallowMount(surface.Ssl, mountOptions);
        await openDrawer(41);
        if (action === 'toggle')
          await wrapper.find('[data-switch]').trigger('click');
        else if (action === 'upload') {
          await openUploadDraft(wrapper, 41, surface.ssl('upload'));
          await button(wrapper, surface.confirm).trigger('click');
        } else await button(wrapper, surface.ssl(action)).trigger('click');
        wrapper.unmount();
        const readCount = fixtures.transport.get.mock.calls.length;
        pending.resolve(certificate(41));
        await flushPromises();
        expect(fixtures.success.mock.calls).toEqual([]);
        expect(fixtures.transport.get.mock.calls).toHaveLength(readCount);
        expect(
          fixtures.transport.post.mock.calls.length +
            fixtures.transport.put.mock.calls.length,
        ).toBe(1);
      },
    );

    it('an A delete confirmation is invalid once B opens', async () => {
      const wrapper = shallowMount(surface.Ssl, mountOptions);
      await openDrawer(41);
      await button(wrapper, surface.ssl('delete')).trigger('click');
      const confirmation = fixtures.confirm.mock.calls[0]?.[0] as {
        onOk: () => Promise<void>;
      };
      await changeDrawer(42);
      expect(fixtures.destroyConfirmation.mock.calls).toEqual([[]]);
      await confirmation.onOk();
      expect(fixtures.transport.delete.mock.calls).toEqual([]);
      expect(wrapper.text()).toContain('issuer-42');
      expect(fixtures.success.mock.calls).toEqual([]);
      wrapper.unmount();
    });
  });

  describe(`${surface.name} DNS verification ownership`, () => {
    it('a current verified response refreshes the parent and closes once', async () => {
      const wrapper = shallowMount(surface.Dns, mountOptions);
      await reachVerify(wrapper, 42, surface.next);
      await button(wrapper, surface.verify).trigger('click');
      await flushPromises();
      expect(fixtures.transport.post.mock.calls).toEqual([
        [`${surface.path(42)}/verify`, {}, undefined],
      ]);
      expect(wrapper.emitted('success')).toEqual([[]]);
      expect(fixtures.close.mock.calls).toEqual([[]]);
      expect(fixtures.success.mock.calls).toHaveLength(1);
      wrapper.unmount();
    });

    it('a pending verification remains open and available for an explicit later retry', async () => {
      fixtures.transport.post.mockResolvedValue({
        ...domain(42),
        is_verified: false,
      });
      const wrapper = shallowMount(surface.Dns, mountOptions);
      await reachVerify(wrapper, 42, surface.next);
      await button(wrapper, surface.verify).trigger('click');
      await flushPromises();
      expect(fixtures.transport.post.mock.calls).toEqual([
        [`${surface.path(42)}/verify`, {}, undefined],
      ]);
      expect(fixtures.close.mock.calls).toEqual([]);
      expect(fixtures.success.mock.calls).toEqual([]);
      expect(fixtures.warning.mock.calls).toHaveLength(1);
      expect(wrapper.emitted('success')).toBeUndefined();
      expect(button(wrapper, surface.verify).attributes('data-loading')).toBe(
        'false',
      );
      wrapper.unmount();
    });
    it('a late A verification refreshes the parent without closing or toasting B', async () => {
      const pending = deferred<TenantDomainInfoRaw>();
      fixtures.transport.post.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Dns, mountOptions);
      await reachVerify(wrapper, 41, surface.next);
      await button(wrapper, surface.verify).trigger('click');
      fixtures.modal.onOpenChange(false);
      await reachVerify(wrapper, 42, surface.next);
      pending.resolve(domain(41));
      await flushPromises();
      expect(fixtures.close.mock.calls).toEqual([]);
      expect(fixtures.success.mock.calls).toEqual([]);
      expect(wrapper.emitted('success')).toEqual([[]]);
      expect(button(wrapper, surface.verify).exists()).toBe(true);
      expect(fixtures.transport.post.mock.calls).toEqual([
        [`${surface.path(41)}/verify`, {}, undefined],
      ]);
      wrapper.unmount();
    });

    it('two same-tick verifies dispatch only one request', async () => {
      const pending = deferred<TenantDomainInfoRaw>();
      fixtures.transport.post.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Dns, mountOptions);
      await reachVerify(wrapper, 41, surface.next);
      const verify = button(wrapper, surface.verify);
      await Promise.all([verify.trigger('click'), verify.trigger('click')]);
      expect(fixtures.transport.post.mock.calls).toEqual([
        [`${surface.path(41)}/verify`, {}, undefined],
      ]);
      pending.resolve(domain(41));
      await flushPromises();
      wrapper.unmount();
    });

    it('an already sent verification cannot emit success after disposal', async () => {
      const pending = deferred<TenantDomainInfoRaw>();
      fixtures.transport.post.mockImplementation(() => pending.promise);
      const wrapper = shallowMount(surface.Dns, mountOptions);
      await reachVerify(wrapper, 41, surface.next);
      await button(wrapper, surface.verify).trigger('click');
      wrapper.unmount();
      pending.resolve(domain(41));
      await flushPromises();
      expect(fixtures.close.mock.calls).toEqual([]);
      expect(fixtures.success.mock.calls).toEqual([]);
      expect(wrapper.emitted('success')).toBeUndefined();
      expect(fixtures.transport.post.mock.calls).toEqual([
        [`${surface.path(41)}/verify`, {}, undefined],
      ]);
    });
  });
}
