<script lang="ts" setup>
/**
 * 域名详情/编辑抽屉；查看详情，仅允许编辑备注。
 * Domain detail/edit drawer; view detail, remark editable only.
 */
import type { DomainDetailData, TenantDomainInfo } from './domains-types';

import { onScopeDispose, ref } from 'vue';

import { useVbenDrawer } from '@vben/common-ui';
import { IconifyIcon } from '@vben/icons';

import {
  Button,
  Descriptions,
  Form,
  FormItem,
  Input,
  message,
  Spin,
  Tag,
} from 'ant-design-vue';

import { adminApi as admin } from '#/api';
import DomainManagedHttps from '#/components/business/domain-managed-https.vue';
import { $t } from '#/locales';
import { useAccess } from '#/utils';
import { formatDate } from '#/utils/common';
import { getDomainSslPresentation } from '#/utils/domain-presentation';

import DomainsDnsGuideModal from './DomainsDnsGuideModal.vue';

// Emits / 组件事件
const emits = defineEmits<{
  success: [];
}>();

// State / 状态
const detailData = ref<DomainDetailData | null>(null);
const domainDetail = ref<null | TenantDomainInfo>(null);
const loading = ref(false);
const submitting = ref(false);
const editMode = ref(false);
const editRemark = ref('');
let disposed = false;
let generation = 0;
let readGeneration = 0;

function currentOwner() {
  return detailData.value && !disposed
    ? { ...detailData.value, generation }
    : null;
}

function ownsView(owner: NonNullable<ReturnType<typeof currentOwner>>) {
  return (
    !disposed &&
    generation === owner.generation &&
    detailData.value?.tenantId === owner.tenantId &&
    detailData.value?.domainId === owner.domainId
  );
}

function resetView() {
  generation++;
  readGeneration++;
  detailData.value = null;
  domainDetail.value = null;
  loading.value = false;
  submitting.value = false;
  editMode.value = false;
  editRemark.value = '';
}

onScopeDispose(() => {
  disposed = true;
  resetView();
});

// Child component refs / 子组件引用
const dnsGuideModalRef = ref<InstanceType<typeof DomainsDnsGuideModal>>();
const { hasAccessByCodes } = useAccess();
const canUpdateDomain = hasAccessByCodes(['tenant_domain:update']);

// Drawer / 抽屉
const [Drawer, drawerApi] = useVbenDrawer({
  async onOpenChange(isOpen) {
    if (disposed) return;
    resetView();
    if (isOpen) {
      const data = drawerApi.getData<DomainDetailData>();
      if (data?.domainId && data?.tenantId) {
        detailData.value = data;
        await loadDetail();
      }
    }
  },
  footer: false,
});

/** 加载域名详情 / Load domain detail */
async function loadDetail() {
  const owner = currentOwner();
  if (!owner) return;
  const read = ++readGeneration;

  loading.value = true;
  try {
    const result = await admin.getTenantDomainApi(
      owner.tenantId,
      owner.domainId,
    );
    if (!ownsView(owner) || read !== readGeneration) return;
    domainDetail.value = result as TenantDomainInfo;
    editRemark.value = result.remark || '';
  } catch {
  } finally {
    if (ownsView(owner) && read === readGeneration) loading.value = false;
  }
}

/** 进入编辑模式 / Enter edit mode */
function onEnterEdit() {
  if (disposed || loading.value || submitting.value || !canUpdateDomain) return;
  editMode.value = true;
  editRemark.value = domainDetail.value?.remark || '';
}

/** 取消编辑 / Cancel edit */
function onCancelEdit() {
  if (disposed || submitting.value) return;
  editMode.value = false;
  editRemark.value = domainDetail.value?.remark || '';
}

/** 保存备注 / Save remark */
async function onSaveRemark() {
  const owner = currentOwner();
  if (!owner || submitting.value || loading.value || !canUpdateDomain) return;
  const remark = editRemark.value.trim();

  submitting.value = true;
  try {
    await admin.updateTenantDomainApi(owner.tenantId, owner.domainId, {
      remark,
    });
    if (disposed) return;
    // 中文：已提交写入仍通知父列表刷新，面板变化只撤销当前视图反馈。
    // EN: A committed write still refreshes the parent; panel changes revoke only view feedback.
    emits('success');
    if (!ownsView(owner)) return;
    message.success($t('admin.tenant.domain.updateSuccess'));
    editMode.value = false;
    await loadDetail();
  } catch {
  } finally {
    if (ownsView(owner)) submitting.value = false;
  }
}

/** 获取验证状态标签配置 / Get verification status tag config */
function getVerificationTagConfig(status: string) {
  switch (status) {
    case 'failed': {
      return { color: 'error', text: $t('admin.tenant.domain.verifyFailed') };
    }
    case 'verified': {
      return { color: 'success', text: $t('admin.tenant.domain.verified') };
    }
    default: {
      return { color: 'warning', text: $t('admin.tenant.domain.pending') };
    }
  }
}

/** 获取 SSL 状态标签配置 / Get SSL status tag config */
function getSslTagConfig(status?: string) {
  const presentation = getDomainSslPresentation(status);
  return {
    ...presentation,
    text: $t(`admin.tenant.domain.ssl.status.${presentation.key}`),
  };
}

/** 打开 DNS 引导 / Open DNS guide */
function onOpenDnsGuide() {
  if (!domainDetail.value || !detailData.value?.tenantId) return;
  const guideData = {
    domain: domainDetail.value.domain,
    tenantId: detailData.value.tenantId,
    domainId: domainDetail.value.id,
    verificationInfo: domainDetail.value.verificationInfo,
    verificationToken: domainDetail.value.verificationToken,
    cnameTarget: domainDetail.value.cnameTarget,
  };
  dnsGuideModalRef.value?.open(guideData);
}

/** 打开抽屉 / Open drawer */
function open(data: DomainDetailData) {
  drawerApi.setData(data).open();
}

defineExpose({ open });
</script>

<template>
  <Drawer :title="$t('admin.tenant.domain.detail')" class="w-[500px]">
    <DomainsDnsGuideModal ref="dnsGuideModalRef" />
    <Spin :spinning="loading">
      <template v-if="domainDetail">
        <!-- 基本信息 -->
        <Descriptions :column="1" bordered size="small" class="mb-4">
          <Descriptions.Item :label="$t('admin.tenant.domain.domain')">
            <div class="flex items-center gap-2">
              <code class="font-mono">{{ domainDetail.domain }}</code>
              <Tag v-if="domainDetail.isPrimary" color="blue">
                {{ $t('admin.tenant.domain.primaryDomain') }}
              </Tag>
            </div>
          </Descriptions.Item>

          <Descriptions.Item :label="$t('admin.tenant.domain.type')">
            <Tag
              :color="
                domainDetail.domainType === 'default' ? 'default' : 'blue'
              "
            >
              {{
                domainDetail.domainType === 'default'
                  ? $t('admin.tenant.domain.defaultDomain')
                  : $t('admin.tenant.domain.customDomain')
              }}
            </Tag>
          </Descriptions.Item>

          <Descriptions.Item
            :label="$t('admin.tenant.domain.verificationStatus')"
          >
            <Tag
              :color="
                getVerificationTagConfig(domainDetail.verificationStatus).color
              "
            >
              {{
                getVerificationTagConfig(domainDetail.verificationStatus).text
              }}
            </Tag>
            <!-- DNS 配置按钮 (待验证才显示) -->
            <Button
              v-if="domainDetail.verificationStatus === 'pending'"
              type="link"
              size="small"
              class="ml-2 !p-0"
              @click="onOpenDnsGuide"
            >
              <IconifyIcon icon="lucide:info" class="mr-1 size-3" />
              {{ $t('admin.tenant.domain.dnsGuide.title') }}
            </Button>
            <span
              v-if="domainDetail.verifiedAt"
              class="ml-2 text-xs text-muted-foreground"
            >
              {{ formatDate(domainDetail.verifiedAt) }}
            </span>
          </Descriptions.Item>

          <Descriptions.Item label="SSL">
            <DomainManagedHttps
              v-if="domainDetail.domainType === 'default'"
              :info="domainDetail.managedHttps"
              detail
            />
            <template v-else>
              <Tag :color="getSslTagConfig(domainDetail.sslStatus).color">
                {{ getSslTagConfig(domainDetail.sslStatus).text }}
              </Tag>
              <span
                v-if="domainDetail.sslExpiresAt"
                class="ml-2 text-xs text-muted-foreground"
              >
                {{ $t('admin.tenant.domain.ssl.info.validTo') }}:
                {{ formatDate(domainDetail.sslExpiresAt) }}
              </span>
            </template>
          </Descriptions.Item>

          <Descriptions.Item :label="$t('admin.tenant.domain.createdAt')">
            {{ formatDate(domainDetail.createdAt) }}
          </Descriptions.Item>

          <Descriptions.Item :label="$t('admin.tenant.domain.updatedAt')">
            {{ formatDate(domainDetail.updatedAt) }}
          </Descriptions.Item>
        </Descriptions>

        <!-- 备注编辑 -->
        <div class="rounded-lg border border-border p-4">
          <div class="mb-2 flex items-center justify-between">
            <h4 class="text-sm font-medium">
              {{ $t('admin.tenant.domain.remark') }}
            </h4>
            <Button
              v-if="!editMode && canUpdateDomain"
              type="link"
              size="small"
              @click="onEnterEdit"
            >
              <IconifyIcon icon="lucide:edit" class="mr-1 size-3" />
              {{ $t('shared.common.edit') }}
            </Button>
          </div>

          <template v-if="editMode">
            <Form layout="vertical">
              <FormItem class="mb-2">
                <Input.TextArea
                  v-model:value="editRemark"
                  :rows="3"
                  :max-length="500"
                  show-count
                  :placeholder="$t('admin.tenant.domain.remarkPlaceholder')"
                />
              </FormItem>
              <div class="flex gap-2">
                <Button
                  type="primary"
                  size="small"
                  :loading="submitting"
                  @click="onSaveRemark"
                >
                  {{ $t('shared.common.save') }}
                </Button>
                <Button size="small" @click="onCancelEdit">
                  {{ $t('shared.common.cancel') }}
                </Button>
              </div>
            </Form>
          </template>
          <template v-else>
            <p class="text-sm text-muted-foreground">
              {{ domainDetail.remark || $t('shared.common.notAssigned') }}
            </p>
          </template>
        </div>
      </template>
    </Spin>
  </Drawer>
</template>
