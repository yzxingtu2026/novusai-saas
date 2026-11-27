// @vitest-environment happy-dom
// 中文: 使用真实权限抽屉和 API 适配器，替换 HTTP 与 Ant Design Vue 展示层。
// EN: Exercise the real permission drawer and API adapters with fixture HTTP and Ant Design Vue presentation.
/* eslint-disable vue/one-component-per-file -- Test-only presentation. */
import type { VueWrapper } from '@vue/test-utils';

import type { TenantUserRoleInfo } from '#/api/tenant/tenant-user-roles';

import { flushPromises, mount } from '@vue/test-utils';

import { Tree } from 'ant-design-vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import PermissionDrawer from '../modules/PermissionDrawer.vue';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));

vi.mock('#/utils/request', () => ({ requestClient: transport }));
vi.mock('#/locales', () => ({
  $t: (key: string, values?: { count: number }) =>
    values ? `${key} (${values.count})` : key,
}));
vi.mock('@vben/icons', () => ({ IconifyIcon: 'span' }));
vi.mock('ant-design-vue', async () => {
  const { defineComponent, h } = await import('vue');
  const Content = defineComponent({
    setup:
      (_, { slots }) =>
      () =>
        h('div', slots.default?.()),
  });

  return {
    Alert: defineComponent({
      props: {
        description: { default: undefined, type: String },
        message: { default: undefined, type: String },
      },
      setup:
        (props, { slots }) =>
        () =>
          h('div', { role: 'alert' }, [
            slots.message?.() ?? props.message,
            props.description,
          ]),
    }),
    Button: defineComponent({
      setup:
        (_, { attrs, slots }) =>
        () =>
          h('button', attrs, slots.default?.()),
    }),
    Checkbox: defineComponent({
      props: { checked: Boolean },
      emits: ['update:checked'],
      setup:
        (props, { emit, slots }) =>
        () =>
          h('label', [
            h('input', {
              checked: props.checked,
              type: 'checkbox',
              onChange: (event: Event) =>
                emit(
                  'update:checked',
                  (event.target as HTMLInputElement).checked,
                ),
            }),
            slots.default?.(),
          ]),
    }),
    Drawer: defineComponent({
      props: { open: Boolean },
      setup:
        (props, { slots }) =>
        () =>
          props.open
            ? h('section', [slots.default?.(), slots.footer?.()])
            : null,
    }),
    Empty: Content,
    Skeleton: Content,
    Space: Content,
    Tag: Content,
    Tree: defineComponent({
      props: {
        checkedKeys: { default: () => [], type: Array },
        checkStrictly: Boolean,
        treeData: { default: () => [], type: Array },
      },
      emits: ['check', 'expand', 'update:expandedKeys'],
      setup: () => () => h('div', { 'data-tree': '' }),
    }),
    message: { success: vi.fn() },
  };
});

const role: TenantUserRoleInfo = {
  code: 'fixture-role',
  createdAt: '2026-11-23T00:00:00Z',
  id: 10,
  isActive: true,
  isSystem: false,
  memberCount: 1,
  name: 'Fictional role',
  permissionsCount: 2,
  sortOrder: 0,
  tenantId: 77,
};

function option(id: number, name: string, parentId: null | number = null) {
  return {
    code: `fixture:${id}`,
    id,
    name,
    parent_id: parentId,
    sort_order: id,
    type: 'menu',
  };
}

const parent = option(101, 'Settings');
const child = option(102, 'Profile', parent.id);
const searchResult = option(103, 'Help');
const orphan = option(104, 'Existing orphan', 999);

function detail(permissionIds: number[], id = role.id) {
  return {
    code: role.code,
    id,
    permission_codes: permissionIds.map((key) => `fixture:${key}`),
    permission_ids: permissionIds,
    permission_options: [parent, child, searchResult, orphan],
  };
}

let mounted: undefined | VueWrapper;
let roleDetail: ReturnType<typeof detail>;

async function openDrawer() {
  const wrapper = mount(PermissionDrawer, { props: { open: false, role } });
  mounted = wrapper;
  await wrapper.setProps({ open: true });
  return wrapper;
}

function saveButton(wrapper: VueWrapper) {
  return wrapper.get('button[type="primary"]');
}

beforeEach(() => {
  vi.clearAllMocks();
  roleDetail = detail([parent.id, orphan.id]);
  transport.get.mockImplementation(async (path: string) => {
    if (path === '/tenant/permissions') {
      // 中文: 旧接口返回相同节点，单独验证树的选择逻辑。
      // EN: Keep legacy fixture nodes equivalent to isolate tree selection behavior.
      return [{ ...parent, children: [child] }, searchResult, orphan];
    }
    if (path === `/tenant/user-roles/${role.id}`) return roleDetail;
    throw new Error(`Unexpected fixture read: ${path}`);
  });
  transport.put.mockResolvedValue(roleDetail);
});

afterEach(() => {
  mounted?.unmount();
  mounted = undefined;
});

describe('tenant business-user role permission drawer', () => {
  it('echoes existing non-leaf grants and keeps eligible orphans visible', async () => {
    const wrapper = await openDrawer();
    await flushPromises();

    const tree = wrapper.getComponent(Tree);
    expect(tree.props('checkedKeys')).toEqual([parent.id, orphan.id]);
    expect(tree.props('checkStrictly')).toBe(true);
    expect(tree.props('treeData')).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          children: [expect.objectContaining({ key: child.id })],
          key: parent.id,
        }),
        expect.objectContaining({ key: orphan.id }),
      ]),
    );
    expect(transport.get).toHaveBeenCalledTimes(1);
    expect(transport.get).toHaveBeenCalledWith(
      '/tenant/user-roles/10',
      expect.objectContaining({ params: { include_permission_options: true } }),
    );
  });

  it('submits the exact selection without expanding half-checked ancestors', async () => {
    const wrapper = await openDrawer();
    await flushPromises();
    const tree = wrapper.getComponent(Tree);
    tree.vm.$emit(
      'check',
      { checked: [searchResult.id], halfChecked: [child.id] },
      { checked: true, node: { key: searchResult.id } },
    );
    await saveButton(wrapper).trigger('click');

    expect(transport.put).toHaveBeenCalledTimes(1);
    expect(transport.put).toHaveBeenCalledWith(
      '/tenant/user-roles/10/permissions',
      { permission_ids: [parent.id, orphan.id, searchResult.id] },
      undefined,
    );
  });

  it('blocks writes when the current role read fails', async () => {
    transport.get.mockRejectedValue(new Error('Fictional read failure'));
    const wrapper = await openDrawer();
    await flushPromises();
    const save = saveButton(wrapper);
    await save.trigger('click');

    expect(transport.put).not.toHaveBeenCalled();
    expect(save.attributes('disabled')).toBeDefined();
  });

  it('keeps a late previous role read out of the current role draft', async () => {
    let finishPrevious!: (value: ReturnType<typeof detail>) => void;
    const previous = new Promise<ReturnType<typeof detail>>((resolve) => {
      finishPrevious = resolve;
    });
    transport.get.mockImplementation(async (path: string) => {
      if (path === '/tenant/permissions')
        return [{ ...parent, children: [child] }];
      if (path === '/tenant/user-roles/10') return previous;
      if (path === '/tenant/user-roles/11') return detail([child.id], 11);
      throw new Error(`Unexpected fixture read: ${path}`);
    });

    const wrapper = await openDrawer();
    await wrapper.setProps({ role: { ...role, id: 11 } });
    await flushPromises();
    finishPrevious(detail([parent.id]));
    await flushPromises();

    expect(wrapper.getComponent(Tree).props('checkedKeys')).toEqual([child.id]);
    await saveButton(wrapper).trigger('click');
    expect(transport.put).toHaveBeenCalledExactlyOnceWith(
      '/tenant/user-roles/11/permissions',
      { permission_ids: [child.id] },
      undefined,
    );
  });
  it('refreshes the role list after an earlier save without closing the new draft', async () => {
    let finishSave!: (value: ReturnType<typeof detail>) => void;
    const pending = new Promise<ReturnType<typeof detail>>((resolve) => {
      finishSave = resolve;
    });
    transport.put.mockReturnValueOnce(pending);

    const wrapper = await openDrawer();
    await flushPromises();
    await saveButton(wrapper).trigger('click');
    expect(transport.put).toHaveBeenCalledExactlyOnceWith(
      '/tenant/user-roles/10/permissions',
      { permission_ids: [parent.id, orphan.id] },
      undefined,
    );

    await wrapper.setProps({ open: false });
    transport.get.mockResolvedValueOnce(detail([child.id], 11));
    await wrapper.setProps({ open: true, role: { ...role, id: 11 } });
    await flushPromises();
    finishSave(roleDetail);
    await flushPromises();

    expect(wrapper.emitted('saved')).toEqual([[]]);
    expect(wrapper.emitted('update:open')).toBeUndefined();
    expect(wrapper.getComponent(Tree).props('checkedKeys')).toEqual([child.id]);
  });

  it('requires explicit removal of historical ineligible grants and permits clearing', async () => {
    roleDetail = detail([888]);
    const wrapper = await openDrawer();
    await flushPromises();

    const save = saveButton(wrapper);
    expect(wrapper.text()).toContain('fixture:888');
    expect(save.attributes('disabled')).toBeDefined();
    await save.trigger('click');
    expect(transport.put).not.toHaveBeenCalled();

    await wrapper.get('input[type="checkbox"]').setValue(true);
    await saveButton(wrapper).trigger('click');
    expect(transport.put).toHaveBeenCalledTimes(1);
    expect(transport.put).toHaveBeenCalledWith(
      '/tenant/user-roles/10/permissions',
      { permission_ids: [] },
      undefined,
    );
  });
});
