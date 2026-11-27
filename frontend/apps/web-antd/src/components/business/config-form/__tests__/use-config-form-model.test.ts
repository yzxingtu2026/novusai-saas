// Test type: behavioral
// Verifies: config form model hydration, dirty tracking, and backend payload shaping.
// Mock strategy: Vue reactivity runs real; no business logic is mocked.

import type { ConfigItemMeta } from '#/types/config';

import { nextTick, shallowRef } from 'vue';

import { describe, expect, it } from 'vitest';

import { useConfigFormModel } from '../composables/use-config-form-model';

describe('useConfigFormModel', () => {
  it('displays runtime domain values but omits them from editable config payloads', () => {
    const model = useConfigFormModel({
      configs: () => [
        {
          key: 'tenant_domain_suffix',
          value: '.e2.dakkii.cn',
          default_value: '.e2.dakkii.cn',
          value_type: 'string',
          is_readonly: true,
        },
        { key: 'site_name', value: 'ERP', value_type: 'string' },
      ],
    });
    expect(model.getValues()).toEqual({
      tenant_domain_suffix: '.e2.dakkii.cn',
      site_name: 'ERP',
    });
    model.fieldApi.setStringValue('site_name', 'New ERP');
    model.fieldApi.setStringValue('tenant_domain_suffix', '.wrong.example.com');
    expect(model.prepareSubmitData()).toEqual({ site_name: 'New ERP' });
  });

  it('omits readonly JSON parents and preserves readonly children of editable JSON', () => {
    const model = useConfigFormModel({
      configs: () => [
        {
          key: 'runtime',
          is_readonly: true,
          value_type: 'json',
          value: { suffix: '.e2.dakkii.cn' },
          children: [
            {
              key: 'runtime_suffix',
              value_path: 'suffix',
              value_type: 'string',
            },
          ],
        },
        {
          key: 'editable',
          value_type: 'json',
          value: { suffix: '.e2.dakkii.cn', label: 'ERP' },
          children: [
            {
              key: 'suffix',
              value_path: 'suffix',
              value_type: 'string',
              is_readonly: true,
            },
            { key: 'label', value_path: 'label', value_type: 'string' },
          ],
        },
      ],
    });
    model.fieldApi.setStringValue('suffix', '.wrong.example.com');
    model.fieldApi.setStringValue('label', 'Updated');
    expect(model.prepareSubmitData()).toEqual({
      editable: { suffix: '.e2.dakkii.cn', label: 'Updated' },
    });
  });

  it('hydrates JSON child fields and merges them back into the parent payload', async () => {
    const configs = shallowRef<ConfigItemMeta[]>([
      {
        key: 'service_config',
        value_type: 'json',
        value: {
          mode: 'safe',
          retries: 3,
        },
        children: [
          {
            key: 'service_mode',
            value_path: 'mode',
            value_type: 'string',
          },
          {
            key: 'service_retries',
            value_path: 'retries',
            value_type: 'number',
          },
        ],
      },
    ]);

    const model = useConfigFormModel({
      configs: () => configs.value,
    });

    await nextTick();

    expect(model.formModel.service_mode).toBe('safe');
    expect(model.formModel.service_retries).toBe(3);

    model.fieldApi.setStringValue('service_mode', 'fast');
    model.fieldApi.setNumberValue('service_retries', 5);

    expect(model.prepareSubmitData()).toEqual({
      service_config: {
        mode: 'fast',
        retries: 5,
      },
    });
  });

  it('skips masked encrypted passwords until the user provides a new value', async () => {
    const configs = shallowRef<ConfigItemMeta[]>([
      {
        key: 'api_secret',
        is_encrypted: true,
        value: '******',
        value_type: 'password',
      },
    ]);

    const model = useConfigFormModel({
      configs: () => configs.value,
    });

    await nextTick();

    expect(model.prepareSubmitData()).toEqual({});

    model.fieldApi.setStringValue('api_secret', 'fresh-secret');

    expect(model.prepareSubmitData()).toEqual({
      api_secret: 'fresh-secret',
    });
  });

  it('resets the dirty snapshot when the config definition changes', async () => {
    const configs = shallowRef<ConfigItemMeta[]>([
      {
        key: 'feature_enabled',
        value: false,
        value_type: 'boolean',
      },
    ]);

    const model = useConfigFormModel({
      configs: () => configs.value,
    });

    await nextTick();

    expect(model.isDirty()).toBe(false);

    model.fieldApi.setBooleanValue('feature_enabled', true);

    expect(model.isDirty()).toBe(true);

    configs.value = [
      {
        key: 'feature_enabled',
        value: true,
        value_type: 'boolean',
      },
    ];

    await nextTick();

    expect(model.getValues()).toEqual({
      feature_enabled: true,
    });
    expect(model.isDirty()).toBe(false);
  });

  it('normalizes tag fields between Select tags and backend string payloads', async () => {
    const configs = shallowRef<ConfigItemMeta[]>([
      {
        key: 'tenant_storage_allowed_extensions',
        tag_separator: ',',
        value: 'jpg, png,',
        value_type: 'tag',
      },
    ]);

    const model = useConfigFormModel({
      configs: () => configs.value,
    });

    await nextTick();

    expect(
      model.fieldApi.getTagValue('tenant_storage_allowed_extensions', ','),
    ).toEqual(['jpg', 'png']);

    model.fieldApi.setTagValue('tenant_storage_allowed_extensions', [
      'pdf',
      ' docx ',
      '',
    ]);

    expect(model.prepareSubmitData()).toEqual({
      tenant_storage_allowed_extensions: 'pdf,docx',
    });
  });
});
