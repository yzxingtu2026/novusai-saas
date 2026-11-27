import type { DOMWrapper, VueWrapper } from '@vue/test-utils';

import type { TenantDomainInfoRaw } from '#/types/domain';

import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import DomainsPage from '../../index.vue';

const fixture = vi.hoisted(() => ({
  get: vi.fn(),
  hasAccessByCodes: vi.fn<(codes: string[]) => boolean>(),
  openAddDrawer: vi.fn(),
  put: vi.fn(),
  success: vi.fn(),
}));

vi.mock('#/utils/request', () => ({ requestClient: fixture }));
vi.mock('#/locales', () => ({ $t: (key: string) => key }));
vi.mock('#/utils', () => ({
  useAccess: () => ({ hasAccessByCodes: fixture.hasAccessByCodes }),
}));
vi.mock('#/utils/common', () => ({
  copyToClipboard: vi.fn(),
  formatDate: (value: string) => value,
}));
vi.mock('#/components/business/domain-managed-https.vue', () => ({
  default: { template: '<span />' },
}));
vi.mock('@vben/common-ui', () => ({
  Page: { template: '<main><slot name="extra" /><slot /></main>' },
}));
vi.mock('@vben/icons', () => ({ IconifyIcon: { template: '<i />' } }));
vi.mock('ant-design-vue', () => ({
  Badge: { template: '<span />' },
  Button: {
    emits: ['click'],
    props: ['loading'],
    template:
      '<button :data-loading="String(!!loading)" @click="$emit(\'click\')"><slot /></button>',
  },
  Card: { template: '<article><slot /></article>' },
  Empty: { template: '<span />' },
  message: { success: fixture.success },
  Popconfirm: { template: '<span><slot /></span>' },
  Space: { template: '<div><slot /></div>' },
  Spin: {
    props: ['spinning'],
    template:
      '<section :data-page-loading="String(!!spinning)"><slot /></section>',
  },
  Tag: { template: '<span><slot /></span>' },
  Tooltip: { template: '<span><slot /></span>' },
}));
vi.mock('../DomainsAddDrawer.vue', () => ({
  default: { methods: { open: fixture.openAddDrawer }, template: '<span />' },
}));
vi.mock('../DomainsDetailDrawer.vue', () => ({
  default: { template: '<span />' },
}));
vi.mock('../DomainsDnsGuideModal.vue', () => ({
  default: { template: '<span />' },
}));
vi.mock('../DomainsSslDrawer.vue', () => ({
  default: { template: '<span />' },
}));

enableAutoUnmount(afterEach);

function domain(id: number, isPrimary: boolean): TenantDomainInfoRaw {
  return {
    created_at: '2026-11-01T00:00:00Z',
    domain: `branch-${id}.example.test`,
    domain_type: 'custom',
    id,
    is_primary: isPrimary,
    is_verified: true,
    ssl_status: 'active',
    tenant_id: 5,
    updated_at: '2026-11-01T00:00:00Z',
  };
}

function domainResponse(primaryId: number) {
  return {
    items: [domain(41, primaryId === 41), domain(42, primaryId === 42)],
    total: 2,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function mountPage() {
  const errors = vi.fn();
  const wrapper = mount(DomainsPage, {
    global: {
      config: { errorHandler: errors },
      directives: { access: () => undefined },
    },
  });
  return { errors, wrapper };
}

function button(scope: DOMWrapper<Element> | VueWrapper, label: string) {
  const found = scope.findAll('button').find((item) => item.text() === label);
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}

function card(wrapper: VueWrapper, id: number) {
  const found = wrapper
    .findAll('article')
    .find((item) => item.find(`[title="branch-${id}.example.test"]`).exists());
  if (!found) throw new Error(`Missing domain ${id}`);
  return found;
}

function expectPrimary(wrapper: VueWrapper, id: number) {
  expect(card(wrapper, id).text()).toContain('common.primary');
  expect(card(wrapper, id === 41 ? 42 : 41).text()).not.toContain(
    'common.primary',
  );
}

describe('tenant domain list', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    fixture.hasAccessByCodes.mockReturnValue(true);
  });

  it('[behavioral] exposes the unavailable add action on a focusable button for a read-only role', async () => {
    fixture.hasAccessByCodes.mockImplementation((codes) =>
      codes.includes('tenant_domain:detail'),
    );
    fixture.get.mockResolvedValueOnce(domainResponse(41));
    const { wrapper } = mountPage();
    await flushPromises();

    const add = button(wrapper, 'tenant.system.domain.add');
    expect(add.element).toHaveProperty('tabIndex', 0);
    expect(add.attributes('disabled')).toBeUndefined();
    expect(add.attributes('aria-disabled')).toBe('true');
  });

  it('[behavioral] opens the add drawer for a role with domain creation access', async () => {
    fixture.hasAccessByCodes.mockImplementation((codes) =>
      codes.includes('tenant_domain:create'),
    );
    fixture.get.mockResolvedValueOnce(domainResponse(41));
    const { wrapper } = mountPage();
    await flushPromises();

    const add = button(wrapper, 'tenant.system.domain.add');
    expect(add.attributes('aria-disabled')).not.toBe('true');
    await add.trigger('click');
    expect(fixture.openAddDrawer).toHaveBeenCalledExactlyOnceWith(undefined);
  });

  it('[behavioral] an older refresh cannot restore the previous primary after the write and readback', async () => {
    const stale = deferred<ReturnType<typeof domainResponse>>();
    fixture.get
      .mockResolvedValueOnce(domainResponse(41))
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(domainResponse(42));
    fixture.put.mockResolvedValueOnce(domain(42, true));
    const { errors, wrapper } = mountPage();
    await flushPromises();
    expectPrimary(wrapper, 41);

    await button(wrapper, 'common.refresh').trigger('click');
    await button(card(wrapper, 42), 'tenant.system.domain.setPrimary').trigger(
      'click',
    );
    await flushPromises();
    expect(fixture.put).toHaveBeenCalledExactlyOnceWith(
      '/tenant/domains/42/primary',
      {},
      undefined,
    );
    expect(fixture.get.mock.calls).toEqual([
      ['/tenant/domains', undefined],
      ['/tenant/domains', undefined],
      ['/tenant/domains', undefined],
    ]);
    expectPrimary(wrapper, 42);

    stale.resolve(domainResponse(41));
    await flushPromises();
    expectPrimary(wrapper, 42);
    expect(errors).not.toHaveBeenCalled();
  });

  it('[behavioral] the mount response cannot replace rows or release loading owned by a newer refresh', async () => {
    const initial = deferred<ReturnType<typeof domainResponse>>();
    const refresh = deferred<ReturnType<typeof domainResponse>>();
    fixture.get
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(refresh.promise);
    const { wrapper } = mountPage();
    await flushPromises();
    expect(
      wrapper.get('[data-page-loading]').attributes('data-page-loading'),
    ).toBe('true');
    await button(wrapper, 'common.refresh').trigger('click');
    initial.resolve(domainResponse(41));
    await flushPromises();

    expect(wrapper.findAll('article')).toHaveLength(0);
    expect(button(wrapper, 'common.refresh').attributes('data-loading')).toBe(
      'true',
    );
    refresh.resolve(domainResponse(42));
    await flushPromises();
    expectPrimary(wrapper, 42);
    expect(button(wrapper, 'common.refresh').attributes('data-loading')).toBe(
      'false',
    );
  });

  it('[behavioral] a failed refresh retains the accepted cards and leaves the rejection to request handling', async () => {
    fixture.get
      .mockResolvedValueOnce(domainResponse(41))
      .mockRejectedValueOnce(new Error('Fictional domain refresh failure'));
    const { errors, wrapper } = mountPage();
    await flushPromises();
    expectPrimary(wrapper, 41);
    await button(wrapper, 'common.refresh').trigger('click');
    await flushPromises();

    expectPrimary(wrapper, 41);
    expect(button(wrapper, 'common.refresh').attributes('data-loading')).toBe(
      'false',
    );
    expect(errors).not.toHaveBeenCalled();
  });
});
