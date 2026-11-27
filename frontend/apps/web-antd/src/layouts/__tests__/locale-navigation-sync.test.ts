import type { SyncLocaleNavigationOptions } from '../locale-navigation-sync';

import { flushPromises } from '@vue/test-utils';
import { shallowRef } from 'vue';

import { updatePreferences } from '@vben/preferences';

import { AxiosError } from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { i18n } from '#/locales';
import { resolveRuntimeLocale } from '#/locales/runtime-locale';
import {
  createRequestInterceptor,
  createResponseDataInterceptor,
} from '#/utils/request/interceptors';
import { RequestClient } from '#/utils/request/request-client';

import {
  buildCurrentRouteReloadTarget,
  syncLocaleNavigation,
} from '../locale-navigation-sync';

// Shared node_modules can resolve workspace packages to a sibling checkout.
// Exercise this checkout's real composer rather than a locale-shaped stub.
vi.mock(
  '@vben/locales',
  () => import('../../../../../packages/locales/src/index'),
);

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function createNavigationFixture(
  loadRuntimeLocale: (locale: string) => Promise<void>,
  pluginFailure?: { code: number; status: number },
) {
  const requests: string[] = [];
  const slotRequests: { locale: string; method: string; path: string }[] = [];
  const client = new RequestClient();
  client.addRequestInterceptor(
    createRequestInterceptor(
      client,
      { getRefreshToken: () => null, getToken: () => null },
      resolveRuntimeLocale,
      { showLoading: () => () => {}, showMessage: () => {}, t: (key) => key },
    ),
  );
  client.addResponseInterceptor(
    createResponseDataInterceptor(0, 'code', 'data'),
  );
  // Only the HTTP boundary is fictional: the real interceptor and live i18n
  // getter select the language, as they do for the compiled host menu request.
  client.instance.defaults.adapter = async (config) => {
    const locale = String(config.headers.get('Accept-Language'));
    if (pluginFailure && config.url?.endsWith('/plugins/slots')) {
      const { code, status } = pluginFailure;
      slotRequests.push({
        locale,
        method: String(config.method).toUpperCase(),
        path: config.url,
      });
      const message = `Request failed with status code ${status}`;
      throw new AxiosError(
        message,
        status >= 500
          ? AxiosError.ERR_BAD_RESPONSE
          : AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        {
          config,
          data: { code, message },
          headers: {},
          status,
          statusText: message,
        },
      );
    }
    requests.push(locale);
    return {
      config,
      data: {
        code: 0,
        data: [{ name: locale === 'zh-CN' ? '文档管理' : 'Documents' }],
        message: '',
      },
      headers: {},
      status: 200,
      statusText: 'OK',
    };
  };
  const accessStore = {
    setAccessMenus: vi.fn(),
    setAccessRoutes: vi.fn(),
  };
  const options: SyncLocaleNavigationOptions = {
    accessStore,
    endpoint: 'admin',
    generateAccess: vi.fn(async () => {
      const menus = await client.get<{ name: string }[]>(
        '/admin/permissions/menus',
      );
      return { accessibleMenus: menus, accessibleRoutes: [] };
    }),
    hasLocaleKey: () => false,
    locale: 'zh-CN',
    refreshPluginSlots: vi.fn(async (endpoint: string) => {
      if (pluginFailure) {
        await client.get(`/${endpoint}/plugins/slots`);
      }
    }),
    router: {
      currentRoute: shallowRef<
        SyncLocaleNavigationOptions['router']['currentRoute']['value']
      >({
        fullPath: '/admin/plugins/novusdoc',
        hash: '',
        matched: [],
        meta: { title: 'Documents' },
        name: 'documents',
        params: {},
        path: '/admin/plugins/novusdoc',
        query: {},
        redirectedFrom: undefined,
      }),
      getRoutes: () => [],
      replace: vi.fn(async () => {}),
    },
    routes: [],
    syncRuntimeLocale: loadRuntimeLocale,
    tabbarStore: { getTabs: [], setUpdateTime: vi.fn() },
    translate: (key: string) => key,
    userRoles: [],
  };
  return { accessStore, options, requests, slotRequests };
}

describe('locale-navigation-sync', () => {
  beforeEach(() => {
    i18n.global.locale.value = 'en-US';
    updatePreferences({ app: { locale: 'en-US' } });
  });

  it('[behavioral] rebuilds access, plugin routes, tabs, and current route title from the active locale', async () => {
    const accessStore = {
      setAccessMenus: vi.fn(),
      setAccessRoutes: vi.fn(),
    };
    const tabbarStore = {
      getTabs: [
        {
          meta: {} as Record<string, unknown>,
          path: '/admin/plugins/demo-plugin',
        },
        { meta: {} as Record<string, unknown>, path: '/admin/system/codegen' },
      ],
      touchTabs: vi.fn(),
      setUpdateTime: vi.fn(),
    };
    const router = {
      currentRoute: {
        value: {
          hash: '#details',
          name: 'plugin-demo-plugin-home',
          params: { id: '42' },
          path: '/admin/plugins/demo-plugin',
          query: { view: 'summary' },
        },
      },
      getRoutes: vi.fn(() => [
        {
          meta: {
            title: 'plugin.demo.menu',
            titleLocaleMap: { 'zh-CN': '演示插件', en: 'Demo Plugin' },
          },
          path: '/admin/plugins/demo-plugin',
        },
        {
          meta: {
            title: 'admin.system.codegen.name',
            titleLocaleMap: { 'zh-CN': '代码生成', en: 'Codegen' },
          },
          path: '/admin/system/codegen',
        },
      ]),
      replace: vi.fn(() => Promise.resolve()),
    };
    const generateAccess = vi.fn(async () => ({
      accessibleMenus: [{ name: 'demo-menu' }],
      accessibleRoutes: [{ name: 'demo-route' }],
    }));
    const refreshPluginSlots = vi.fn(async () => {});

    await syncLocaleNavigation({
      accessStore,
      endpoint: 'admin',
      generateAccess,
      hasLocaleKey: () => false,
      locale: 'en',
      refreshPluginSlots,
      router: router as never,
      routes: [{ component: {} as never, name: 'AdminRoot', path: '/admin' }],
      syncRuntimeLocale: async () => {},
      tabbarStore,
      translate: (key: string) => key,
      userRoles: ['super_admin'],
    });

    expect(generateAccess).toHaveBeenCalledWith(
      {
        roles: ['super_admin'],
        router,
        routes: [{ component: {} as never, name: 'AdminRoot', path: '/admin' }],
      },
      'admin',
    );
    expect(accessStore.setAccessMenus).toHaveBeenCalledWith([
      { name: 'demo-menu' },
    ]);
    expect(accessStore.setAccessRoutes).toHaveBeenCalledWith([
      { name: 'demo-route' },
    ]);
    expect(refreshPluginSlots).toHaveBeenCalledWith('admin', router, {
      reloadAssets: false,
    });
    expect(tabbarStore.getTabs[0]?.meta?.title).toBe('Demo Plugin');
    expect(tabbarStore.getTabs[1]?.meta?.title).toBe('Codegen');
    expect(tabbarStore.touchTabs).toHaveBeenCalledTimes(1);
    expect(tabbarStore.setUpdateTime).toHaveBeenCalledTimes(1);
    expect(router.replace).toHaveBeenCalledWith({
      force: true,
      hash: '#details',
      name: 'plugin-demo-plugin-home',
      params: { id: '42' },
      query: { view: 'summary' },
    });
  });

  it('[behavioral] waits for runtime locale before issuing the actual intercepted menu request', async () => {
    const loading = deferred();
    const fixture = createNavigationFixture(async (locale) => {
      await loading.promise;
      i18n.global.locale.value = locale;
    });
    updatePreferences({ app: { locale: 'zh-CN' } });
    expect(resolveRuntimeLocale()).toBe('en-US');

    const syncing = syncLocaleNavigation(fixture.options);
    try {
      await flushPromises();
      expect(fixture.requests).toEqual([]);
      expect(fixture.options.generateAccess).not.toHaveBeenCalled();
      expect(fixture.accessStore.setAccessMenus).not.toHaveBeenCalled();
    } finally {
      loading.resolve();
      await syncing;
    }

    expect(resolveRuntimeLocale()).toBe('zh-CN');
    expect(fixture.requests).toEqual(['zh-CN']);
    expect(fixture.accessStore.setAccessMenus).toHaveBeenCalledWith([
      { name: '文档管理' },
    ]);
  });

  it('[behavioral] leaves navigation untouched when runtime locale loading fails', async () => {
    const failure = new Error('language bundle unavailable');
    const fixture = createNavigationFixture(async () => {
      throw failure;
    });
    await expect(syncLocaleNavigation(fixture.options)).rejects.toBe(failure);

    expect(fixture.requests).toEqual([]);
    expect(fixture.accessStore.setAccessMenus).not.toHaveBeenCalled();
    expect(fixture.accessStore.setAccessRoutes).not.toHaveBeenCalled();
    expect(fixture.options.refreshPluginSlots).not.toHaveBeenCalled();
    expect(fixture.options.router.replace).not.toHaveBeenCalled();
    expect(resolveRuntimeLocale()).toBe('en-US');
  });

  it('[behavioral] reads the live runtime locale for each serialized language change', async () => {
    const fixture = createNavigationFixture(async (locale) => {
      await Promise.resolve();
      i18n.global.locale.value = locale;
    });
    updatePreferences({ app: { locale: 'zh-CN' } });
    await syncLocaleNavigation(fixture.options);
    updatePreferences({ app: { locale: 'en-US' } });
    await syncLocaleNavigation({ ...fixture.options, locale: 'en-US' });

    expect(fixture.requests).toEqual(['zh-CN', 'en-US']);
    expect(fixture.accessStore.setAccessMenus.mock.calls).toEqual([
      [[{ name: '文档管理' }]],
      [[{ name: 'Documents' }]],
    ]);
    expect(resolveRuntimeLocale()).toBe('en-US');
  });

  it('[behavioral] keeps the admin task page and its tab usable after a denied plugin-slot read', async () => {
    const fixture = createNavigationFixture(
      async (locale) => {
        i18n.global.locale.value = locale;
      },
      { code: 4030, status: 403 },
    );
    const path = '/admin/system/task-logs';
    const name = 'menu:admin.task_log';
    const tab = { meta: { title: '任务日志' }, path };
    const setUpdateTime = vi.fn();
    const { router } = fixture.options;
    router.currentRoute.value = {
      ...router.currentRoute.value,
      fullPath: `${path}?view=internal`,
      name,
      path,
      query: { view: 'internal' },
    };
    router.getRoutes = () =>
      [{ meta: { title: 'Task Logs' }, path }] as ReturnType<
        typeof router.getRoutes
      >;
    i18n.global.locale.value = 'zh-CN';

    await syncLocaleNavigation({
      ...fixture.options,
      locale: 'en-US',
      tabbarStore: { getTabs: [tab], setUpdateTime },
    });

    expect(fixture.requests).toEqual(['en-US']);
    expect(fixture.slotRequests).toEqual([
      { locale: 'en-US', method: 'GET', path: '/admin/plugins/slots' },
    ]);
    expect(fixture.accessStore.setAccessMenus).toHaveBeenCalledWith([
      { name: 'Documents' },
    ]);
    expect(tab.meta.title).toBe('Task Logs');
    expect(setUpdateTime).toHaveBeenCalledTimes(1);
    expect(router.replace).toHaveBeenCalledExactlyOnceWith({
      force: true,
      hash: '',
      name,
      params: {},
      query: { view: 'internal' },
    });
  });

  it.each([
    { code: 4030, endpoint: 'tenant' as const, status: 403 },
    { code: 4031, endpoint: 'admin' as const, status: 403 },
    { code: 5000, endpoint: 'admin' as const, status: 500 },
  ])(
    '[behavioral] propagates $endpoint slot errors with HTTP $status/code $code',
    async ({ code, endpoint, status }) => {
      const fixture = createNavigationFixture(async () => {}, { code, status });
      await expect(
        syncLocaleNavigation({ ...fixture.options, endpoint, locale: 'en-US' }),
      ).rejects.toMatchObject({
        isAxiosError: true,
        response: { data: { code }, status },
      });

      expect(fixture.slotRequests).toEqual([
        { locale: 'en-US', method: 'GET', path: `/${endpoint}/plugins/slots` },
      ]);
      expect(fixture.options.router.replace).not.toHaveBeenCalled();
    },
  );

  it('[structural] falls back to path reload targets when the current route has no name', () => {
    expect(
      buildCurrentRouteReloadTarget({
        hash: '#intro',
        params: {},
        path: '/tenant/plugins/example',
        query: { a: '1' },
      } as never),
    ).toEqual({
      hash: '#intro',
      path: '/tenant/plugins/example',
      query: { a: '1' },
    });
  });
});
