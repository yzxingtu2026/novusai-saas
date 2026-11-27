// @vitest-environment happy-dom
// Test type: behavioral
// 中文: 真实 VXE 代理会在 loading 时丢弃 reload；验证适配器保留最新搜索且不在卸载后重启读取。
// EN: Real VXE drops reload while loading; verify the adapter retains the newest search and does not restart reads after unmount.
import type { VxeGridInstance } from 'vxe-table';

import { flushPromises, mount } from '@vue/test-utils';

import { afterEach, describe, expect, it } from 'vitest';
import { VxePager } from 'vxe-pc-ui';
import { VxeGrid, VxeUI } from 'vxe-table';

import { VxeGridApi } from './api';

VxeUI.component(VxePager);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((complete, fail) => {
    resolve = complete;
    reject = fail;
  });
  return { promise, reject, resolve };
}

const wrappers: Array<{ unmount: () => void }> = [];
afterEach(() => wrappers.splice(0).forEach((wrapper) => wrapper.unmount()));

function createGrid(
  query: (params: object, values: Record<string, unknown>) => Promise<unknown>,
  native = false,
) {
  const wrapper = mount(VxeGrid, {
    props: {
      columns: [{ field: 'title', sortable: native }],
      pagerConfig: native
        ? { currentPage: 1, pageSize: 10, pageSizes: [10, 50] }
        : undefined,
      proxyConfig: {
        autoLoad: false,
        ajax: { query },
        response: { list: 'items', result: 'items', total: 'total' },
        sort: native,
      },
      sortConfig: { allowBtn: true, remote: native },
    },
  });
  const grid = wrapper.vm as unknown as VxeGridInstance;
  const api = new VxeGridApi();
  api.mount(grid, {} as VxeGridApi['formApi']);
  wrappers.push({
    unmount: () => {
      api.unmount();
      wrapper.unmount();
    },
  });
  return { api, grid, wrapper };
}

function nativeResponse(page: number, pageSize = 10) {
  return {
    items: [{ id: page, title: `Fictional page ${page}, size ${pageSize}` }],
    total: 200,
  };
}

function nativeRead(params: object, values: Record<string, unknown> = {}) {
  const { code, page, sort } = params as {
    code: string;
    page: { currentPage: number; pageSize: number };
    sort: { order?: string };
  };
  return {
    code,
    keyword: values.keyword,
    page: page.currentPage,
    pageSize: page.pageSize,
    sort: sort.order,
  };
}

describe('vxe API latest pending read', () => {
  it('[behavioral] sends idle native page and sort reads exactly once', async () => {
    const calls: Array<ReturnType<typeof nativeRead>> = [];
    const { api, grid, wrapper } = createGrid(async (params, values) => {
      const read = nativeRead(params, values);
      calls.push(read);
      return nativeResponse(read.page, read.pageSize);
    }, true);
    await api.query();
    await flushPromises();

    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    await flushPromises();
    await wrapper.get('.vxe-sort--asc-btn').trigger('click');
    await flushPromises();

    expect(calls.map(({ page, sort }) => ({ page, sort }))).toEqual([
      { page: 1, sort: undefined },
      { page: 2, sort: undefined },
      { page: 1, sort: 'asc' },
    ]);
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual(nativeResponse(1).items);
  });

  it('[behavioral] coalesces native page and page-size changes into the latest read', async () => {
    const slow = deferred<ReturnType<typeof nativeResponse>>();
    const calls: Array<ReturnType<typeof nativeRead>> = [];
    const { api, grid, wrapper } = createGrid(async (params, values) => {
      const read = nativeRead(params, values);
      calls.push(read);
      return read.page === 2 && read.pageSize === 10
        ? slow.promise
        : nativeResponse(read.page, read.pageSize);
    }, true);
    await api.query();
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    wrapper
      .getComponent(VxePager)
      .vm.setPageSizeByEvent(new Event('change'), 50);
    await flushPromises();
    expect(calls.map(({ page, pageSize }) => ({ page, pageSize }))).toEqual([
      { page: 1, pageSize: 10 },
      { page: 2, pageSize: 10 },
    ]);

    slow.resolve(nativeResponse(2));
    await flushPromises();

    expect(calls.map(({ page, pageSize }) => ({ page, pageSize }))).toEqual([
      { page: 1, pageSize: 10 },
      { page: 2, pageSize: 10 },
      { page: 4, pageSize: 50 },
    ]);
    expect(grid.reactData.tablePage).toMatchObject({
      currentPage: 4,
      pageSize: 50,
    });
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual(nativeResponse(4, 50).items);
  });

  it('[behavioral] lets the latest filter reload supersede queued native paging and reset the page', async () => {
    const slow = deferred<ReturnType<typeof nativeResponse>>();
    const calls: Array<ReturnType<typeof nativeRead>> = [];
    const { api, grid, wrapper } = createGrid(async (params, values) => {
      const read = nativeRead(params, values);
      calls.push(read);
      return read.page === 2
        ? slow.promise
        : {
            items: [{ id: 9, title: `Fictional ${read.keyword} filter` }],
            total: 200,
          };
    }, true);
    await api.query();
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    const middle = api.reload({ keyword: 'middle' });
    const latest = api.reload({ keyword: 'latest' });
    slow.resolve(nativeResponse(2));
    await Promise.all([middle, latest]);
    await flushPromises();

    expect(
      calls.map(({ code, keyword, page }) => ({ code, keyword, page })),
    ).toEqual([
      { code: 'query', keyword: undefined, page: 1 },
      { code: 'query', keyword: undefined, page: 2 },
      { code: 'reload', keyword: 'latest', page: 1 },
    ]);
    expect(grid.reactData.tablePage.currentPage).toBe(1);
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual([{ id: 9, title: 'Fictional latest filter' }]);
  });

  it('[behavioral] drops queued native paging and its watcher when the owner unmounts', async () => {
    const slow = deferred<ReturnType<typeof nativeResponse>>();
    const calls: number[] = [];
    const { api, grid, wrapper } = createGrid(async (params) => {
      const { page } = nativeRead(params);
      calls.push(page);
      return page === 2 ? slow.promise : nativeResponse(page);
    }, true);
    await api.query();
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    api.unmount();
    wrapper.unmount();
    grid.reactData.tablePage.currentPage = 4;
    slow.resolve(nativeResponse(2));
    await flushPromises();

    expect(calls).toEqual([1, 2]);
  });

  it('[behavioral] follows a rejected native page with the newest native sort', async () => {
    const slow = deferred<ReturnType<typeof nativeResponse>>();
    const calls: Array<ReturnType<typeof nativeRead>> = [];
    const { api, grid, wrapper } = createGrid(async (params, values) => {
      const read = nativeRead(params, values);
      calls.push(read);
      return read.page === 2
        ? slow.promise
        : nativeResponse(read.page, read.pageSize);
    }, true);
    await api.query();
    await flushPromises();
    await wrapper.get('.vxe-pager--next-btn').trigger('click');
    await flushPromises();
    await wrapper.get('.vxe-sort--asc-btn').trigger('click');
    await wrapper.get('.vxe-sort--desc-btn').trigger('click');
    slow.reject(new Error('Fictional native page failure'));
    await flushPromises();

    expect(calls.map(({ page, sort }) => ({ page, sort }))).toEqual([
      { page: 1, sort: undefined },
      { page: 2, sort: undefined },
      { page: 1, sort: 'desc' },
    ]);
    expect(grid.reactData.tablePage.currentPage).toBe(1);
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual(nativeResponse(1).items);
  });

  it('[behavioral] follows an in-flight search with the latest search instead of dropping it', async () => {
    const slow = deferred<{ items: Array<{ id: number; title: string }> }>();
    const calls: string[] = [];
    const { api, grid } = createGrid(async (_params, values) => {
      const keyword = String(values.keyword);
      calls.push(keyword);
      return keyword === 'slow'
        ? slow.promise
        : { items: [{ id: 2, title: `Fictional ${keyword} result` }] };
    });
    const first = api.reload({ keyword: 'slow' });
    await flushPromises();
    const next = api.reload({ keyword: 'latest' });
    await flushPromises();
    slow.resolve({ items: [{ id: 1, title: 'Fictional slow result' }] });
    await Promise.all([first, next]);
    await flushPromises();
    expect(calls).toEqual(['slow', 'latest']);
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual([{ id: 2, title: 'Fictional latest result' }]);
  });

  it('[behavioral] coalesces intermediate searches behind a native page query and preserves reload semantics', async () => {
    const slow = deferred<{ items: Array<{ id: number; title: string }> }>();
    const calls: Array<{ code: string; keyword: string }> = [];
    const { api, grid } = createGrid(async (params, values) => {
      const keyword = String(values.keyword);
      calls.push({ code: String(Reflect.get(params, 'code')), keyword });
      return keyword === 'native-page'
        ? slow.promise
        : { items: [{ id: 3, title: `Fictional ${keyword} result` }] };
    });
    const native = grid.commitProxy('query', { keyword: 'native-page' });
    await flushPromises();
    const middle = api.reload({ keyword: 'middle' });
    const latest = api.reload({ keyword: 'latest' });
    await flushPromises();
    expect(calls).toEqual([{ code: 'query', keyword: 'native-page' }]);
    slow.resolve({ items: [{ id: 1, title: 'Fictional native page result' }] });
    await Promise.all([native, middle, latest]);
    await flushPromises();
    expect(calls).toEqual([
      { code: 'query', keyword: 'native-page' },
      { code: 'reload', keyword: 'latest' },
    ]);
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual([{ id: 3, title: 'Fictional latest result' }]);
  });

  it('[behavioral] drops a pending latest search when the grid owner unmounts', async () => {
    const slow = deferred<{ items: never[] }>();
    const calls: string[] = [];
    const { api, grid, wrapper } = createGrid(async (_params, values) => {
      calls.push(String(values.keyword));
      return slow.promise;
    });
    const native = grid.commitProxy('query', { keyword: 'native-page' });
    await flushPromises();
    const pending = api.reload({ keyword: 'latest' });
    api.unmount();
    wrapper.unmount();
    await pending;
    slow.resolve({ items: [] });
    await Promise.all([native, pending]);
    await flushPromises();
    expect(calls).toEqual(['native-page']);
  });

  it('[behavioral] runs the latest pending search after the older read rejects', async () => {
    const slow = deferred<{ items: Array<{ id: number; title: string }> }>();
    const calls: string[] = [];
    const { api, grid } = createGrid(async (_params, values) => {
      const keyword = String(values.keyword);
      calls.push(keyword);
      return keyword === 'slow'
        ? slow.promise
        : { items: [{ id: 2, title: 'Fictional latest result' }] };
    });
    await grid.loadData([{ id: 1, title: 'Fictional older result' }]);
    const first = api.reload({ keyword: 'slow' });
    await flushPromises();
    const latest = api.reload({ keyword: 'latest' });
    await flushPromises();
    expect(calls).toEqual(['slow']);
    slow.reject(new Error('Fictional read failed'));
    await Promise.all([first, latest]);
    await flushPromises();
    expect(calls).toEqual(['slow', 'latest']);
    expect(
      grid.getTableData().fullData.map(({ id, title }) => ({ id, title })),
    ).toEqual([{ id: 2, title: 'Fictional latest result' }]);
  });
});
