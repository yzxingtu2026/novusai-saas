<script lang="ts" setup>
/**
 * 域名详情/编辑抽屉 / Domain detail/edit drawer
 * 查看域名详情，仅允许编辑备注
 */
import type { DomainDetailData, TenantDomainInfo } from './domains-types';

import { onScopeDispose, ref } from 'vue';

import { useVbenDrawer } from '@vben/common-ui';
import { IconifyIcon } from '@vben/icons';

import {
  Button,
  Descriptions,
  Input,
  message,
  Spin,
  Tag,
} from 'ant-design-vue';

import { getTenantDomainApi, updateTenantDomainApi } from '#/api/tenant/domain';
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
const { hasAccessByCodes } = useAccess();
const canUpdateDomain = hasAccessByCodes(['tenant_domain:update']);

// Child component refs / 子组件引用
const dnsGuideModalRef = ref<InstanceType<typeof DomainsDnsGuideModal>>();

// Drawer / 抽屉
const [Drawer, drawerApi] = useVbenDrawer({
  async onOpenChange(isOpen) {
    if (disposed) return;
    resetView();
    if (isOpen) {
      const data = drawerApi.getData<DomainDetailData>();
      if (data?.domainId) {
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
    const result = await getTenantDomainApi(owner.domainId);
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
    await updateTenantDomainApi(owner.domainId, { remark });
    if (disposed) return;
    // 中文：已提交写入仍通知父列表刷新，面板变化只撤销当前视图反馈。
    // EN: A committed write still refreshes the parent; panel changes revoke only view feedback.
    emits('success');
    if (!ownsView(owner)) return;
    message.success($t('common.saveSuccess'));
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
      return {
        color: 'error',
        text: $t('tenant.system.domain.verification.failed'),
      };
    }
    case 'verified': {
      return {
        color: 'success',
        text: $t('tenant.system.domain.verification.verified'),
      };
    }
    default: {
      return {
        color: 'warning',
        text: $t('tenant.system.domain.verification.pending'),
      };
    }
  }
}

/** 获取 SSL 状态标签配置 / Get SSL status tag config */
function getSslTagConfig(status?: string) {
  const presentation = getDomainSslPresentation(status);
  return {
    ...presentation,
    text: $t(`tenant.system.domain.ssl.${presentation.key}`),
  };
}

/** 打开 DNS 引导 / Open DNS guide */
function onOpenDnsGuide() {
  if (!domainDetail.value) return;
  const guideData = {
    domain: domainDetail.value.domain,
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
  <Drawer :title="$t('tenant.system.domain.edit')" class="w-[500px]">
    <DomainsDnsGuideModal ref="dnsGuideModalRef" />
    <Spin :spinning="loading">
      <template v-if="domainDetail">
        <!-- 基本信息 -->
        <Descriptions :column="1" bordered size="small" class="mb-4">
          <Descriptions.Item :label="$t('tenant.system.domain.domain')">
            <div class="flex items-center gap-2">
              <code class="font-mono">{{ domainDetail.domain }}</code>
              <Tag v-if="domainDetail.isPrimary" color="blue">
                {{ $t('common.primary') }}
              </Tag>
            </div>
          </Descriptions.Item>

          <Descriptions.Item :label="$t('tenant.system.domain.typeLabel')">
            <Tag
              :color="
                domainDetail.domainType === 'default' ? 'default' : 'blue'
              "
            >
              {{
                domainDetail.domainType === 'default'
                  ? $t('tenant.system.domain.type.default')
                  : $t('tenant.system.domain.type.custom')
              }}
            </Tag>
          </Descriptions.Item>

          <Descriptions.Item
            :label="$t('tenant.system.domain.verificationStatus')"
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
              {{ $t('tenant.system.domain.dnsGuide') }}
            </Button>
            <span
              v-if="domainDetail.verifiedAt"
              class="ml-2 text-xs text-muted-foreground"
            >
              {{ formatDate(domainDetail.verifiedAt) }}
            </span>
          </Descriptions.Item>

          <Descriptions.Item :label="$t('tenant.system.domain.sslStatus')">
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
                {{ $t('tenant.system.domain.ssl.expiresAt') }}:
                {{ formatDate(domainDetail.sslExpiresAt) }}
              </span>
            </template>
          </Descriptions.Item>

          <Descriptions.Item :label="$t('common.createdAt')">
            {{ formatDate(domainDetail.createdAt) }}
          </Descriptions.Item>

          <Descriptions.Item :label="$t('common.updatedAt')">
            {{ formatDate(domainDetail.updatedAt) }}
          </Descriptions.Item>
        </Descriptions>

        <!-- 备注编辑 -->
        <div class="rounded-lg border border-border p-4">
          <div class="mb-2 flex items-center justify-between">
            <h4 class="text-sm font-medium">
              {{ $t('tenant.system.domain.remark') }}
            </h4>
            <Button
              v-if="!editMode && canUpdateDomain"
              type="link"
              size="small"
              @click="onEnterEdit"
            >
              <IconifyIcon icon="lucide:edit" class="mr-1 size-3" />
              {{ $t('common.edit') }}
            </Button>
          </div>

          <template v-if="editMode">
            <div class="mb-3">
              <Input.TextArea
                v-model:value="editRemark"
                :rows="3"
                :max-length="500"
                show-count
                :placeholder="
                  $t('tenant.system.domain.placeholder.inputRemark')
                "
              />
            </div>
            <div class="flex gap-2">
              <Button
                type="primary"
                size="small"
                :loading="submitting"
                @click="onSaveRemark"
              >
                {{ $t('common.save') }}
              </Button>
              <Button size="small" @click="onCancelEdit">
                {{ $t('common.cancel') }}
              </Button>
            </div>
          </template>
          <template v-else>
            <p class="text-sm text-muted-foreground">
              {{ domainDetail.remark || $t('common.notSet') }}
            </p>
          </template>
        </div>
      </template>
    </Spin>
  </Drawer>
</template>
