// @vitest-environment happy-dom
/* eslint-disable vue/one-component-per-file */
import { mount } from '@vue/test-utils';
import { defineComponent, h, markRaw } from 'vue';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import CrudGrid from '../components/crud-grid.vue';

const accessState = vi.hoisted(() => {
  const state = {
    allowed: false,
    lastCodes: [] as string[],
  };
  return {
    state,
    hasAccessByCodes: vi.fn((codes: string[]) => {
      state.lastCodes = codes;
      return state.allowed;
    }),
  };
});

vi.mock('#/utils/access', () => ({
  useAccess: () => ({
    hasAccessByCodes: accessState.hasAccessByCodes,
  }),
}));

vi.mock('#/locales', () => ({
  $t: (key: string) => key,
}));

vi.mock('@vben/icons', () => {
  const IconStub = defineComponent({
    name: 'IconStub',
    setup(_, { slots }) {
      return () => h('span', slots.default?.());
    },
  });

  return {
    Download: IconStub,
    IconifyIcon: IconStub,
    Plus: IconStub,
  };
});

vi.mock('ant-design-vue', () => {
  const PassthroughStub = defineComponent({
    name: 'PassthroughStub',
    setup(_, { slots }) {
      return () => h('span', slots.default?.());
    },
  });
  const ButtonStub = defineComponent({
    name: 'ButtonStub',
    props: {
      disabled: {
        default: false,
        type: Boolean,
      },
    },
    setup(props, { slots }) {
      return () => h('button', { disabled: props.disabled }, slots.default?.());
    },
  });

  return {
    Badge: PassthroughStub,
    Button: ButtonStub,
    Input: PassthroughStub,
    Select: PassthroughStub,
    Tooltip: PassthroughStub,
  };
});

const GridStub = defineComponent({
  name: 'GridStub',
  setup(_, { slots }) {
    return () => h('div', slots['toolbar-actions']?.());
  },
});

const accessDirective = {
  mounted: () => undefined,
  updated: () => undefined,
};

function mountGrid(createPermission = 'item:create') {
  const onCreate = vi.fn();
  const wrapper = mount(CrudGrid, {
    global: {
      directives: {
        access: accessDirective,
      },
    },
    props: {
      createLabel: 'Create item',
      createPermission,
      grid: markRaw(GridStub),
      onCreate,
      showExport: false,
    },
  });

  return { onCreate, wrapper };
}

describe('crud grid create action permission state', () => {
  beforeEach(() => {
    accessState.state.allowed = false;
    accessState.state.lastCodes = [];
    accessState.hasAccessByCodes.mockClear();
  });

  it('uses native disabled when a configured permission is denied', () => {
    const { onCreate, wrapper } = mountGrid();
    const button = wrapper.get('button');

    expect(accessState.state.lastCodes).toEqual(['item:create']);
    expect(button.element.disabled).toBe(true);
    button.trigger('click');
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('keeps the create button enabled for an authorized user', async () => {
    accessState.state.allowed = true;
    const { onCreate, wrapper } = mountGrid();
    const button = wrapper.get('button');

    expect(button.element.disabled).toBe(false);
    await button.trigger('click');
    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it('does not add a permission restriction when no code is configured', () => {
    const { wrapper } = mountGrid('');

    expect(accessState.hasAccessByCodes).not.toHaveBeenCalled();
    expect(wrapper.get('button').element.disabled).toBe(false);
  });
});
