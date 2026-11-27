import type { RouteMeta } from 'vue-router';

import { createApp, defineComponent, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import { i18n } from '@vben/locales';
import { getTabKey, initStores, useTabbarStore } from '@vben/stores';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { useTabbar } from './use-tabbar';

const path = '/admin/plugins/novusdoc';
const unmounts: Array<() => void> = [];

async function mountTabbar(
  meta: Partial<RouteMeta> = {},
  parentMeta: Partial<RouteMeta> = {},
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        children: [
          {
            component: { render: () => null },
            meta: { title: 'Documents', ...meta },
            name: 'PluginDocuments',
            path: 'plugins/novusdoc',
          },
        ],
        meta: { title: 'Admin', ...parentMeta },
        path: '/admin',
      },
    ],
  });
  await router.push(`${path}?fixture_entry=menu`);
  let tabbar!: ReturnType<typeof useTabbar>;
  const app = createApp(
    defineComponent({
      setup() {
        tabbar = useTabbar();
        return () => h('div');
      },
    }),
  );
  const pinia = await initStores(app, { namespace: 'tabbar-regression' });
  const store = useTabbarStore(pinia);
  store.$reset();
  app.use(router).use(i18n);
  app.mount(document.createElement('div'));
  unmounts.push(() => app.unmount());
  await nextTick();
  return { router, store, tabbar };
}

beforeEach(() => localStorage.clear());

afterEach(() => {
  for (const unmount of unmounts.splice(0)) unmount();
});

describe('useTabbar route key consistency', () => {
  it('[behavioral] activates and reuses the default tab across query changes', async () => {
    const { router, store, tabbar } = await mountTabbar();

    expect(store.getTabs.map((tab) => tab.key)).toEqual([path]);
    expect(tabbar.currentActive.value).toBe(path);
    expect(getTabKey(store.getTabByKey(path))).toBe(tabbar.currentActive.value);

    await router.push(`${path}?fixture_entry=changed#details`);
    await nextTick();

    expect(store.getTabs.map((tab) => tab.key)).toEqual([path]);
    expect(store.getTabs[0]?.fullPath).toBe(
      `${path}?fixture_entry=changed#details`,
    );
    expect(tabbar.currentActive.value).toBe(path);
    expect(tabbar.currentTabs.value?.map((tab) => tab.meta.title)).toEqual([
      'Documents',
    ]);
  });

  it.each([false, true])(
    '[behavioral] preserves explicit fullPathKey=%s for active and stored tabs',
    async (fullPathKey) => {
      const { router, store, tabbar } = await mountTabbar({ fullPathKey });
      const firstKey = fullPathKey ? `${path}?fixture_entry=menu` : path;
      expect(store.getTabs.map((tab) => tab.key)).toEqual([firstKey]);
      expect(tabbar.currentActive.value).toBe(firstKey);

      await router.push(`${path}?fixture_entry=changed#details`);
      await nextTick();

      const nextKey = fullPathKey
        ? `${path}?fixture_entry=changed#details`
        : path;
      expect(store.getTabs.map((tab) => tab.key)).toEqual(
        fullPathKey ? [firstKey, nextKey] : [path],
      );
      expect(tabbar.currentActive.value).toBe(nextKey);
      expect(store.getTabByKey(tabbar.currentActive.value)?.fullPath).toBe(
        `${path}?fixture_entry=changed#details`,
      );
    },
  );

  it.each([undefined, false, true])(
    '[behavioral] preserves scalar and array pageKey with fullPathKey=%s',
    async (fullPathKey) => {
      const { router, store, tabbar } = await mountTabbar({ fullPathKey });
      await router.push({ path, query: { pageKey: 'document-42' } });
      await nextTick();
      expect(tabbar.currentActive.value).toBe('document-42');
      expect(store.getTabByKey('document-42')?.key).toBe('document-42');

      await router.push({
        path,
        query: { pageKey: ['document-51', 'ignored'], q: 'changed' },
      });
      await nextTick();
      expect(tabbar.currentActive.value).toBe('document-51');
      expect(store.getTabByKey('document-51')?.query.pageKey).toEqual([
        'document-51',
        'ignored',
      ]);
      expect(getTabKey(store.getTabByKey('document-51'))).toBe('document-51');
    },
  );

  it('[behavioral] uses the matched leaf policy consistently without mutating route metadata', async () => {
    const { router, store, tabbar } = await mountTabbar(
      {},
      { fullPathKey: true },
    );
    expect(router.currentRoute.value.meta.fullPathKey).toBe(true);
    expect(store.getTabs.map((tab) => tab.key)).toEqual([path]);
    expect(tabbar.currentActive.value).toBe(path);
    expect(router.currentRoute.value.meta.fullPathKey).toBe(true);
    expect(router.currentRoute.value.matched.at(-1)?.meta.fullPathKey).toBe(
      undefined,
    );
  });
});
