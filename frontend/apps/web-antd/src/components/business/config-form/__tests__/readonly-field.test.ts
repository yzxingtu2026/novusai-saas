// Test type: behavioral
import { shallowMount } from '@vue/test-utils';
import { defineComponent } from 'vue';

import { describe, expect, it, vi } from 'vitest';

import { useConfigFormModel } from '../composables/use-config-form-model';
import ConfigFormFieldSection from '../sections/ConfigFormFieldSection.vue';

vi.mock('#/locales', () => ({ $t: (key: string) => key }));
vi.mock('../../config-html-editor', () => ({ ConfigHtmlEditor: {} }));
vi.mock('../../config-image-picker', () => ({ ConfigImagePicker: {} }));

describe('readonly config field rendering', () => {
  it('renders runtime suffix read-only without update listeners', () => {
    const config = {
      key: 'tenant_domain_suffix',
      value: '.e2.dakkii.cn',
      value_type: 'string' as const,
      is_readonly: true,
    };
    const model = useConfigFormModel({ configs: () => [config] });
    const wrapper = shallowMount(ConfigFormFieldSection, {
      props: {
        config,
        fieldApi: model.fieldApi,
        formModel: model.formModel,
        formatJsonValue: model.formatJsonValue,
        getRuleNumber: () => undefined,
        getSelectOptions: () => [],
        fallbackRows: 4,
        textRows: 4,
      },
      global: {
        stubs: {
          AInput: defineComponent({
            name: 'AInput',
            props: { value: { type: String, default: '' } },
            emits: ['update:value'],
            template: '<input :value="value" />',
          }),
        },
      },
    });
    const input = wrapper.findComponent({ name: 'AInput' });
    expect(input.props('value')).toBe('.e2.dakkii.cn');
    expect(input.attributes('readonly')).toBeDefined();
    input.vm.$emit('update:value', '.wrong.example.com');
    expect(model.getValues().tenant_domain_suffix).toBe('.e2.dakkii.cn');
    expect(model.prepareSubmitData()).toEqual({});
  });
});
