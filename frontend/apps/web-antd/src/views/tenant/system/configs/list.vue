<script setup lang="ts">
import type { ConfigFormExpose } from '#/components/business/config-form/types';
import type { ConfigGroupRequest } from '#/composables/use-config-group-request-owner';
import type { ConfigGroupListItemMeta, ConfigItemMeta } from '#/types/config';

import {
  computed,
  nextTick,
  onActivated,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { Page } from '@vben/common-ui';
import { IconifyIcon } from '@vben/icons';

import { Button, Card, Empty, Modal, Spin } from 'ant-design-vue';

import {
  getTenantConfigGroupDetailApi,
  getTenantConfigGroupsApi,
  updateTenantConfigGroupApi,
} from '#/api/tenant/configs';
import { ConfigForm } from '#/components';
import ConfigGroupSidebar from '#/components/business/config-group-sidebar/index.vue';
import PluginSettingsTabs from '#/components/business/plugin-slots/PluginSettingsTabs.vue';
import { useConfigGroupRequestOwner } from '#/composables/use-config-group-request-owner';
import { $t as t } from '#/locales';

// Storage config dedicated page (lazy-loaded) / 存储配置专用页面
import TenantStoragePanel from '../storage/index.vue';

defineOptions({ name: 'TenantConfigList' });

const CONFIG_GROUP_QUERY_KEY = 'group';
const CONFIG_ITEM_QUERY_KEY = 'config';
const route = useRoute();
const router = useRouter();

const groups = ref<ConfigGroupListItemMeta[]>([]);
const activeGroup = ref<string>('');
const configs = ref<ConfigItemMeta[]>([]);
const groupLoading = ref(false);
const formRef = ref<ConfigFormExpose>();
const requestOwner = useConfigGroupRequestOwner({
  activeGroup: () => activeGroup.value,
  onInvalidate: () => {
    configs.value = [];
    groupLoading.value = false;
  },
});
const { loading, saving, ready: formReady } = requestOwner;

// Currently selected group data / 当前选中的分组数据
const activeGroupData = computed(() =>
  groups.value.find((g) => g.code === activeGroup.value),
);

function getQueryStringParam(key: string): string {
  const value = route.query[key];
  if (Array.isArray(value)) return value[0] ? String(value[0]) : '';
  return value ? String(value) : '';
}

function getRequestedGroupCode(): string {
  return getQueryStringParam(CONFIG_GROUP_QUERY_KEY);
}

function getRequestedConfigKey(): string {
  return getQueryStringParam(CONFIG_ITEM_QUERY_KEY);
}

function getResolvedRequestedGroupCode(): string | undefined {
  const requestedGroupCode = getRequestedGroupCode();
  if (!requestedGroupCode) return undefined;
  return groups.value.some((group) => group.code === requestedGroupCode)
    ? requestedGroupCode
    : undefined;
}

async function syncRouteSelection(groupCode: string, configKey?: string) {
  const currentGroupCode = getRequestedGroupCode();
  const currentConfigKey = getRequestedConfigKey();
  const nextConfigKey = configKey ?? '';
  if (currentGroupCode === groupCode && currentConfigKey === nextConfigKey) {
    return;
  }

  const query: Record<string, null | string | string[] | undefined> = {
    ...route.query,
    [CONFIG_GROUP_QUERY_KEY]: groupCode,
  };
  if (configKey) {
    query[CONFIG_ITEM_QUERY_KEY] = configKey;
  } else {
    Reflect.deleteProperty(query, CONFIG_ITEM_QUERY_KEY);
  }

  await router.replace({ hash: route.hash, path: route.path, query });
}

async function scrollToConfigItem(
  configKey: string,
  request: ConfigGroupRequest,
) {
  await nextTick();
  if (
    !requestOwner.isCurrent(request) ||
    !formReady.value ||
    request.group !== getRequestedGroupCode() ||
    configKey !== getRequestedConfigKey()
  )
    return;
  const target = document.querySelector<HTMLElement>(
    `#config-item-${CSS.escape(configKey)}`,
  );
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function focusRequestedConfig(request: ConfigGroupRequest) {
  const requestedConfigKey = getRequestedConfigKey();
  if (
    !requestOwner.isCurrent(request) ||
    !formReady.value ||
    !requestedConfigKey ||
    request.group !== getRequestedGroupCode() ||
    !configs.value.some((cfg) => cfg.key === requestedConfigKey)
  ) {
    return;
  }
  await scrollToConfigItem(requestedConfigKey, request);
}

async function applyRouteSelection() {
  if (!requestOwner.capturePage()) return;
  const requestedGroupCode = getResolvedRequestedGroupCode();
  if (!requestedGroupCode) return;
  const requestedConfigKey = getRequestedConfigKey() || undefined;
  if (requestedGroupCode !== activeGroup.value) {
    await activateGroup(requestedGroupCode, {
      configKey: requestedConfigKey,
      syncRoute: false,
    });
    return;
  }
  if (requestedConfigKey) {
    const request = requestOwner.current();
    if (request) await focusRequestedConfig(request);
  }
}

// Get group name (prefer name, then name_key translation, fallback to code) / 获取分组名称
function getGroupName(g: ConfigGroupListItemMeta): string {
  if (g.name) return g.name;
  if (g.name_key) {
    const translated = t(g.name_key);
    if (translated !== g.name_key) return translated;
  }
  const fallbackKey = `shared.config.group.${g.code}`;
  const fallbackTranslated = t(fallbackKey);
  if (fallbackTranslated !== fallbackKey) return fallbackTranslated;
  return g.code;
}

// Get group description / 获取分组描述
function getGroupDesc(g: ConfigGroupListItemMeta): string {
  if (g.description) return g.description;
  if (g.description_key) {
    const translated = t(g.description_key);
    if (translated !== g.description_key) return translated;
  }
  const fallbackKey = `shared.config.group.${g.code}.desc`;
  const fallbackTranslated = t(fallbackKey);
  if (fallbackTranslated !== fallbackKey) return fallbackTranslated;
  return '';
}

// Groups sorted by sort_order / 按 sort_order 排序的分组列表
const sortedGroups = computed(() =>
  groups.value.toSorted((a, b) => (a.sort_order || 0) - (b.sort_order || 0)),
);

const groupNavItems = computed(() =>
  sortedGroups.value.map((group) => ({
    ...group,
    displayDesc: getGroupDesc(group),
    displayName: getGroupName(group),
  })),
);

async function activateGroup(
  code: string,
  options?: { configKey?: string; syncRoute?: boolean },
) {
  const request = requestOwner.select(code, code !== 'tenant_storage');
  if (!request) return;
  activeGroup.value = code;
  configs.value = [];
  if (options?.syncRoute !== false) {
    await syncRouteSelection(code, options?.configKey);
  }
  if (!requestOwner.isCurrent(request)) return;
  if (code === 'tenant_storage') {
    return;
  }
  await loadGroupDetail(request);
}

async function loadGroups() {
  const owner = requestOwner.capturePage();
  if (!owner) return;
  groupLoading.value = true;
  try {
    const result = await getTenantConfigGroupsApi();
    if (!requestOwner.isPageCurrent(owner)) return;
    groups.value = result;
    if (groups.value.length > 0) {
      // Pick first group by sort_order / 按 sort_order 取第一个组
      const sorted = groups.value.toSorted(
        (a, b) => (a.sort_order || 0) - (b.sort_order || 0),
      );
      const firstGroup = sorted[0];
      if (!firstGroup) return;
      const requestedGroupCode = getResolvedRequestedGroupCode();
      const initialGroupCode = requestedGroupCode || firstGroup.code;
      await activateGroup(initialGroupCode, {
        configKey:
          requestedGroupCode === initialGroupCode
            ? getRequestedConfigKey() || undefined
            : undefined,
      });
    }
  } finally {
    if (requestOwner.isPageCurrent(owner)) groupLoading.value = false;
  }
}

async function loadGroupDetail(request: ConfigGroupRequest) {
  if (!requestOwner.startLoad(request)) return;
  configs.value = [];
  try {
    const detail = await getTenantConfigGroupDetailApi(request.group);
    if (!requestOwner.isCurrent(request)) return;
    configs.value = (detail.configs || []).toSorted(
      (a, b) => (a.sort_order || 0) - (b.sort_order || 0),
    );
    requestOwner.acceptLoad(request);
    await focusRequestedConfig(request);
  } finally {
    requestOwner.finishLoad(request);
  }
}

async function onSelectGroup(code: string) {
  const request = requestOwner.current();
  if (!request) return;
  if (code === activeGroup.value) return;
  const form = formRef.value;
  // Check if form has modifications / 检查表单是否有修改
  if (form?.isDirty?.()) {
    Modal.confirm({
      title: t('shared.config.page.unsaved_title'),
      content: t('shared.config.page.unsaved_content'),
      okText: t('shared.common.confirm'),
      cancelText: t('shared.common.cancel'),
      onOk: async () => {
        if (!requestOwner.isCurrent(request) || formRef.value !== form) return;
        await activateGroup(code);
      },
    });
  } else {
    await activateGroup(code);
  }
}

async function onSave() {
  const form = formRef.value;
  if (!form) return;
  const owner = requestOwner.beginSave();
  if (!owner) return;
  try {
    await form.validate();
    if (!requestOwner.ownsSave(owner) || formRef.value !== form) return;
    const payload = form.prepareSubmitData();
    await updateTenantConfigGroupApi(owner.request.group, payload, {
      showSuccessMessage: true,
    });
    if (!requestOwner.ownsSave(owner) || formRef.value !== form) return;
    await loadGroupDetail(owner.request);
  } catch {
    // Error already handled by request interceptor / 错误已由拦截器处理
  } finally {
    requestOwner.finishSave(owner);
  }
}

function beforeUnloadHandler(e: BeforeUnloadEvent) {
  if (formRef.value?.isDirty?.()) {
    e.preventDefault();
    e.returnValue = '';
  }
}
onMounted(() => {
  loadGroups();
  window.addEventListener('beforeunload', beforeUnloadHandler);
});
let isInitialMount = true;
onActivated(() => {
  if (isInitialMount) {
    isInitialMount = false;
    return;
  }
  if (groups.value.length === 0) {
    void loadGroups();
    return;
  }
  const requestedGroupCode = getResolvedRequestedGroupCode();
  if (requestedGroupCode) {
    activateGroup(requestedGroupCode, {
      configKey: getRequestedConfigKey() || undefined,
      syncRoute: false,
    });
    return;
  }
  if (activeGroup.value) {
    void activateGroup(activeGroup.value, { syncRoute: false });
  }
});
watch(
  () => [
    route.query[CONFIG_GROUP_QUERY_KEY],
    route.query[CONFIG_ITEM_QUERY_KEY],
  ],
  () => {
    void applyRouteSelection();
  },
);
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnloadHandler);
});
</script>

<template>
  <Page auto-content-height>
    <div class="flex h-full flex-col gap-4 overflow-hidden md:flex-row">
      <ConfigGroupSidebar
        :groups="groupNavItems"
        :active-group="activeGroup"
        :loading="groupLoading"
        @select="onSelectGroup"
      />

      <!-- Right: Config form / 右侧配置表单 -->
      <Card
        class="min-w-0 flex-1 overflow-hidden"
        :body-style="{
          padding: '16px 24px',
          height: 'calc(100% - 57px)',
          overflow: 'auto',
        }"
      >
        <template #title>
          <span>{{
            activeGroupData ? getGroupName(activeGroupData) : ''
          }}</span>
        </template>
        <template #extra>
          <!-- Storage group save button managed inside storage component, hidden here / 存储配置组保存按钮在存储组件内部管理 -->
          <Button
            v-if="activeGroup !== 'tenant_storage'"
            type="primary"
            v-access:code="['tenant_config:update']"
            :loading="saving"
            :disabled="!formReady || loading || saving"
            @click="onSave"
          >
            <template #icon>
              <IconifyIcon icon="lucide:save" />
            </template>
            {{ t('shared.common.save') }}
          </Button>
        </template>

        <Spin :spinning="loading">
          <!-- tenant_storage group uses dedicated storage config component / 专用存储配置组件 -->
          <TenantStoragePanel v-if="activeGroup === 'tenant_storage'" />
          <div v-else-if="activeGroup && formReady" class="max-w-[800px]">
            <ConfigForm ref="formRef" :configs="configs" />
            <PluginSettingsTabs />
          </div>
          <Empty
            v-else-if="!activeGroup"
            :description="t('shared.config.page.select_group')"
            class="py-16"
          />
        </Spin>
      </Card>
    </div>
  </Page>
</template>
