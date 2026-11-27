// @vitest-environment happy-dom
// Test type: behavioral
// Real page SFCs, ConfigForm, Ant controls and memory router; fictional HTTP and
// unrelated presentation boundaries let requests settle in deliberate orders.
import type { Component, SetupContext } from 'vue';

import { flushPromises, mount } from '@vue/test-utils';
import { defineComponent, h, KeepAlive, nextTick, ref } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import { Button, Modal, Spin } from 'ant-design-vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ConfigForm from '#/components/business/config-form/index.vue';
import ConfigGroupSidebar from '#/components/business/config-group-sidebar/index.vue';
import AdminConfigPage from '#/views/admin/system/configs/list.vue';
import TenantConfigPage from '#/views/tenant/system/configs/list.vue';

vi.mock('@vben/common-ui', () => ({
  Page: { template: '<main><slot /></main>' },
}));
vi.mock('@vben/icons', () => ({ IconifyIcon: { render: () => null } }));
vi.mock('#/locales', () => ({ $t: (key: string) => key }));
vi.mock('#/components', async () => {
  const { default: ConfigForm } =
    await import('#/components/business/config-form/index.vue');
  return { ConfigForm };
});
vi.mock('#/components/business/config-html-editor', () => ({
  ConfigHtmlEditor: { render: () => null },
}));
vi.mock('#/components/business/config-image-picker', () => ({
  ConfigImagePicker: { render: () => null },
}));
vi.mock('#/components/business/plugin-slots/PluginSettingsTabs.vue', () => ({
  default: { render: () => null },
}));

const transport = vi.hoisted(() => ({
  tenant: { groups: vi.fn(), detail: vi.fn(), save: vi.fn() },
  admin: { groups: vi.fn(), detail: vi.fn(), save: vi.fn() },
  readiness: vi.fn(),
  storageSave: vi.fn(),
}));
vi.mock('#/api/tenant/configs', () => ({
  getTenantConfigGroupsApi: transport.tenant.groups,
  getTenantConfigGroupDetailApi: transport.tenant.detail,
  updateTenantConfigGroupApi: transport.tenant.save,
}));
vi.mock('#/api/admin/configs', () => ({
  generateFernetKeyApi: vi.fn(),
  getAdminConfigGroupsApi: transport.admin.groups,
  getAdminConfigGroupDetailApi: transport.admin.detail,
  updateAdminConfigGroupApi: transport.admin.save,
  getAdminSslDnsReadinessApi: transport.readiness,
}));
vi.mock('#/views/tenant/system/storage/index.vue', () => ({
  default: {
    name: 'TenantStoragePanel',
    template: '<div>Fictional tenant storage</div>',
  },
}));
vi.mock(
  '#/views/admin/system/configs/modules/PlatformStoragePanel.vue',
  () => ({
    default: {
      name: 'PlatformStoragePanel',
      setup(_: unknown, { expose }: SetupContext) {
        expose({ onSave: transport.storageSave, saving: ref(false) });
        return () => h('div', 'Fictional platform storage');
      },
    },
  }),
);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function detail(code: string, value: string) {
  return {
    configs: [
      {
        key: `${code}_field`,
        name: `${code} field`,
        value_type: 'string',
        value,
      },
    ],
  };
}

const surfaces = [
  {
    name: 'tenant',
    page: TenantConfigPage,
    a: 'tenant_features',
    b: 'tenant_general',
    storage: 'tenant_storage',
    api: transport.tenant,
  },
  {
    name: 'admin',
    page: AdminConfigPage,
    a: 'platform_general',
    b: 'platform_ssl',
    storage: 'platform_storage',
    api: transport.admin,
  },
];
type Surface = (typeof surfaces)[number];
const wrappers: Array<ReturnType<typeof mount>> = [];

beforeEach(() => {
  vi.resetAllMocks();
  transport.readiness.mockResolvedValue({
    ready: true,
    summary: 'Current SSL readiness',
    issues: [],
  });
  transport.storageSave.mockResolvedValue(undefined);
});
afterEach(() => {
  Modal.destroyAll();
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount());
  vi.restoreAllMocks();
});

async function createPage(
  surface: Surface,
  pendingGroups?: ReturnType<typeof deferred<unknown>>,
) {
  const requests: Array<
    ReturnType<typeof deferred<ReturnType<typeof detail>>> & { code: string }
  > = [];
  const writes: Array<ReturnType<typeof deferred<undefined>>> = [];
  const groups = [surface.a, surface.b, surface.storage].map(
    (code, sort_order) => ({ code, sort_order, name: code }),
  );
  surface.api.groups.mockImplementation(
    () => pendingGroups?.promise ?? Promise.resolve(groups),
  );
  surface.api.detail.mockImplementation((code: string) => {
    const request = { code, ...deferred<ReturnType<typeof detail>>() };
    requests.push(request);
    return request.promise;
  });
  surface.api.save.mockImplementation(() => {
    const write = deferred<undefined>();
    writes.push(write);
    return write.promise;
  });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/fixture', component: { render: () => null } }],
  });
  await router.push({ path: '/fixture', query: { group: surface.a } });
  await router.isReady();
  const visible = ref(true);
  const Host = defineComponent({
    setup: () => () =>
      h(KeepAlive, null, {
        default: () => (visible.value ? h(surface.page as Component) : null),
      }),
  });
  const wrapper = mount(Host, {
    global: { plugins: [router], directives: { access: () => {} } },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return { wrapper, router, visible, requests, writes, groups };
}

type Page = Awaited<ReturnType<typeof createPage>>;
function saveButton(page: Page) {
  const button = page.wrapper
    .findAllComponents(Button)
    .find((item) => item.text() === 'shared.common.save');
  if (!button) throw new Error('Missing configuration save button');
  return button;
}
function renderedValue(page: Page) {
  return (
    page.wrapper.getComponent(ConfigForm).get('input')
      .element as HTMLInputElement
  ).value;
}
async function selectGroup(page: Page, code: string) {
  page.wrapper.getComponent(ConfigGroupSidebar).vm.$emit('select', code);
  await flushPromises();
}
async function settle(page: Page, index: number, value: string) {
  const request = page.requests[index];
  if (!request) throw new Error(`Missing configuration request ${index}`);
  request.resolve(detail(request.code, value));
  await flushPromises();
}
async function beginSave(page: Page) {
  saveButton(page).vm.$emit('click');
  await flushPromises();
}

async function settleWrite(page: Page, index: number) {
  const write = page.writes[index];
  if (!write) throw new Error(`Missing configuration write ${index}`);
  write.resolve(undefined);
  await flushPromises();
}

for (const surface of surfaces) {
  describe(`${surface.name} configuration request ownership`, () => {
    it('[behavioral] keeps B when the earlier A response arrives last', async () => {
      const page = await createPage(surface);
      await selectGroup(page, surface.b);
      await settle(page, 1, 'current B');
      await settle(page, 0, 'obsolete A');
      expect(renderedValue(page)).toBe('current B');
      expect(
        page.wrapper.getComponent(ConfigGroupSidebar).props('activeGroup'),
      ).toBe(surface.b);
    });

    it('[behavioral] rejects an older A response after A to B to A', async () => {
      const page = await createPage(surface);
      await selectGroup(page, surface.b);
      await selectGroup(page, surface.a);
      await settle(page, 2, 'latest A');
      await settle(page, 0, 'first A');
      await settle(page, 1, 'obsolete B');
      expect(renderedValue(page)).toBe('latest A');
    });

    it('[behavioral] keeps the new loading owner and disables saving an unloaded form', async () => {
      const page = await createPage(surface);
      await selectGroup(page, surface.b);
      await settle(page, 0, 'obsolete A');
      expect(
        page.wrapper.findAllComponents(Spin).at(-1)?.props('spinning'),
      ).toBe(true);
      expect(saveButton(page).props('disabled')).toBe(true);
      expect(page.wrapper.findComponent(ConfigForm).exists()).toBe(false);
      await settle(page, 1, 'current B');
      expect(saveButton(page).props('disabled')).toBe(false);
      expect(renderedValue(page)).toBe('current B');
    });

    it('[behavioral] owns selection before awaiting router replacement', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'initial A');
      const navigation = deferred<undefined>();
      vi.spyOn(page.router, 'replace').mockReturnValueOnce(navigation.promise);
      await selectGroup(page, surface.b);
      await selectGroup(page, surface.a);
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.a,
      ]);
      navigation.resolve(undefined);
      await flushPromises();
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.a,
      ]);
      await settle(page, 1, 'current A');
      expect(renderedValue(page)).toBe('current A');
    });

    it('[behavioral] cannot save before the selected group has loaded', async () => {
      const page = await createPage(surface);
      expect(saveButton(page).props('disabled')).toBe(true);
      await beginSave(page);
      expect(surface.api.save.mock.calls).toEqual([]);
      await settle(page, 0, 'loaded A');
      expect(saveButton(page).props('disabled')).toBe(false);
    });

    it('[behavioral] revokes a save while validation awaits a changed group', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'initial A');
      const validation = deferred<undefined>();
      const exposed = page.wrapper.getComponent(ConfigForm).vm.$.exposed as {
        validate: () => Promise<void>;
      };
      vi.spyOn(exposed, 'validate').mockReturnValue(validation.promise);
      await beginSave(page);
      await selectGroup(page, surface.b);
      await settle(page, 1, 'current B');
      validation.resolve(undefined);
      await flushPromises();
      expect(surface.api.save.mock.calls).toEqual([]);
      expect(renderedValue(page)).toBe('current B');
    });

    it('[behavioral] admits one save synchronously and preserves its payload contract', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'edited A');
      const validation = deferred<undefined>();
      const exposed = page.wrapper.getComponent(ConfigForm).vm.$.exposed as {
        validate: () => Promise<void>;
      };
      const validate = vi
        .spyOn(exposed, 'validate')
        .mockReturnValue(validation.promise);
      await beginSave(page);
      await beginSave(page);
      expect(validate).toHaveBeenCalledTimes(1);
      validation.resolve(undefined);
      await flushPromises();
      expect(surface.api.save.mock.calls).toEqual([
        [
          surface.a,
          { [`${surface.a}_field`]: 'edited A' },
          { showSuccessMessage: true },
        ],
      ]);
      await settleWrite(page, 0);
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.a,
      ]);
      await settle(page, 1, 'saved A');
      expect(renderedValue(page)).toBe('saved A');
    });

    it('[behavioral] cannot reload a new group or release its save when an old save completes', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'A to save');
      await beginSave(page);
      await selectGroup(page, surface.b);
      await settle(page, 1, 'B to save');
      await beginSave(page);
      expect(surface.api.save.mock.calls.map((call) => call[0])).toEqual([
        surface.a,
        surface.b,
      ]);
      await settleWrite(page, 0);
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.b,
      ]);
      expect(saveButton(page).props('loading')).toBe(true);
      expect(renderedValue(page)).toBe('B to save');
      await settleWrite(page, 1);
      await settle(page, 2, 'saved B');
      expect(renderedValue(page)).toBe('saved B');
    });

    it('[behavioral] retains the pending A write lock through A to B to A', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'first A');
      await beginSave(page);
      await selectGroup(page, surface.b);
      await settle(page, 1, 'current B');
      await selectGroup(page, surface.a);
      await settle(page, 2, 'returned A');
      expect(saveButton(page).props('disabled')).toBe(true);
      await beginSave(page);
      expect(surface.api.save.mock.calls).toEqual([
        [
          surface.a,
          { [`${surface.a}_field`]: 'first A' },
          { showSuccessMessage: true },
        ],
      ]);
      await settleWrite(page, 0);
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.b,
        surface.a,
      ]);
      expect(renderedValue(page)).toBe('returned A');
      expect(saveButton(page).props('disabled')).toBe(false);
      await beginSave(page);
      expect(surface.api.save.mock.calls.map((call) => call[0])).toEqual([
        surface.a,
        surface.a,
      ]);
    });

    it('[behavioral] retains an in-flight write lock while KeepAlive is inactive', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'A before deactivation');
      await beginSave(page);
      page.visible.value = false;
      await nextTick();
      page.visible.value = true;
      await flushPromises();
      await settle(page, 1, 'A after activation');
      expect(saveButton(page).props('disabled')).toBe(true);
      await beginSave(page);
      expect(surface.api.save.mock.calls.map((call) => call[0])).toEqual([
        surface.a,
      ]);
      await settleWrite(page, 0);
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.a,
      ]);
      expect(saveButton(page).props('disabled')).toBe(false);
      expect(renderedValue(page)).toBe('A after activation');
    });

    it('[behavioral] releases the exact group write lock after a failed save', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'A before failure');
      await beginSave(page);
      const write = page.writes[0];
      if (!write) throw new Error('Missing failed configuration write');
      write.reject(
        new Error(
          'Fictional HTTP failure already handled by the request client',
        ),
      );
      await flushPromises();
      expect(saveButton(page).props('disabled')).toBe(false);
      expect(page.requests.map((request) => request.code)).toEqual([surface.a]);
      await beginSave(page);
      expect(surface.api.save.mock.calls.map((call) => call[0])).toEqual([
        surface.a,
        surface.a,
      ]);
    });

    it('[behavioral] invalidates ordinary forms when selecting the dedicated storage panel', async () => {
      const page = await createPage(surface);
      await selectGroup(page, surface.storage);
      await settle(page, 0, 'obsolete A');
      expect(page.wrapper.findComponent(ConfigForm).exists()).toBe(false);
      expect(
        page.wrapper.findAllComponents(Spin).at(-1)?.props('spinning'),
      ).toBe(false);
      expect(surface.api.save.mock.calls).toEqual([]);
      if (surface.name === 'admin') {
        await beginSave(page);
        expect(transport.storageSave.mock.calls).toEqual([[]]);
      } else {
        expect(
          page.wrapper
            .findAllComponents(Button)
            .filter((item) => item.text() === 'shared.common.save'),
        ).toHaveLength(0);
      }
    });

    it('[behavioral] rejects a dirty-form confirmation after its group was left', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'initial A');
      await page.wrapper
        .getComponent(ConfigForm)
        .get('input')
        .setValue('unsaved A');
      await selectGroup(page, surface.b);
      await page.router.push({
        path: '/fixture',
        query: { group: surface.storage },
      });
      await flushPromises();
      const confirm = [
        ...document.querySelectorAll<HTMLButtonElement>(
          '.ant-modal-confirm-btns .ant-btn-primary',
        ),
      ].at(-1);
      if (!confirm) throw new Error('Missing dirty configuration confirmation');
      confirm.click();
      await flushPromises();
      expect(page.requests.map((request) => request.code)).toEqual([surface.a]);
      expect(
        page.wrapper.getComponent(ConfigGroupSidebar).props('activeGroup'),
      ).toBe(surface.storage);
    });

    it('[behavioral] revokes reads across KeepAlive deactivation and reloads on return', async () => {
      const page = await createPage(surface);
      page.visible.value = false;
      await nextTick();
      await settle(page, 0, 'received while inactive');
      page.visible.value = true;
      await flushPromises();
      expect(page.requests.map((request) => request.code)).toEqual([
        surface.a,
        surface.a,
      ]);
      expect(page.wrapper.findComponent(ConfigForm).exists()).toBe(false);
      expect(saveButton(page).props('disabled')).toBe(true);
      await settle(page, 1, 'fresh after activation');
      expect(renderedValue(page)).toBe('fresh after activation');
    });

    it('[behavioral] does not dispatch a group read after an unmounted group-list response', async () => {
      const groups = deferred<unknown>();
      const page = await createPage(surface, groups);
      page.wrapper.unmount();
      wrappers.splice(wrappers.indexOf(page.wrapper), 1);
      groups.resolve(page.groups);
      await flushPromises();
      expect(surface.api.detail.mock.calls).toEqual([]);
    });

    it('[behavioral] does not reload after an unmounted save completes', async () => {
      const page = await createPage(surface);
      await settle(page, 0, 'A to save');
      await beginSave(page);
      page.wrapper.unmount();
      wrappers.splice(wrappers.indexOf(page.wrapper), 1);
      await settleWrite(page, 0);
      expect(page.requests.map((request) => request.code)).toEqual([surface.a]);
    });
  });
}

describe('cached platform storage selection ownership', () => {
  it.each([{}, { group: 'missing-group' }])(
    '[behavioral] restores the storage save owner with query %j on activation',
    async (query) => {
      const surface = surfaces.find((item) => item.name === 'admin');
      if (!surface) throw new Error('Missing platform configuration surface');
      const page = await createPage(surface);
      await settle(page, 0, 'initial A');
      await selectGroup(page, surface.storage);
      page.visible.value = false;
      await nextTick();
      await page.router.replace({ path: '/fixture', query });
      await flushPromises();
      page.visible.value = true;
      await flushPromises();
      await beginSave(page);
      expect(transport.storageSave.mock.calls).toEqual([[]]);
      expect(surface.api.save.mock.calls).toEqual([]);
    },
  );
});
