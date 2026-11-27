<script lang="ts" setup>
/**
 * 企业端 SSL 证书管理抽屉
 * Tenant SSL cert management drawer
 *
 * 功能：查看证书状态与详情；平台证书签发/续期/自动续期；自定义证书上传/删除。
 * View status/detail; platform cert provision/renew/auto-renew; custom cert upload/delete.
 */

import type { ManagedHttpsInfo } from '#/types/domain';

import { computed, onScopeDispose, ref } from 'vue';

import { useVbenDrawer } from '@vben/common-ui';
import { IconifyIcon } from '@vben/icons';

import {
  Alert,
  Button,
  Descriptions,
  Divider,
  message,
  Modal,
  Spin,
  Switch,
  Tag,
  Textarea,
} from 'ant-design-vue';

import {
  deleteTenantSslCertApi,
  getTenantSslDetailApi,
  provisionTenantSslApi,
  renewTenantSslApi,
  updateTenantSslAutoRenewApi,
  uploadTenantSslCertApi,
} from '#/api/tenant/domain';
import DomainManagedHttps from '#/components/business/domain-managed-https.vue';
import { useDomainSslDetail } from '#/composables/use-domain-ssl-detail';
import { $t } from '#/locales';
import { useAccess } from '#/utils';
import { formatDate } from '#/utils/common';
import {
  getDomainSslPresentation,
  supportsCertificateRenewal,
} from '#/utils/domain-presentation';

/** SSL 抽屉打开数据 / SSL drawer open payload */
interface SslDrawerData {
  domainId: number;
  domain: string;
  isDefault?: boolean;
  managedHttps?: ManagedHttpsInfo | null;
}

/** Default domains show deployment evidence separately from DB certificates. */
const isDefaultDomain = computed(() => drawerData.value?.isDefault ?? false);

// State / 状态
const drawerData = ref<null | SslDrawerData>(null);
const actionLoading = ref(false);
const { sslDetail, loading, loadFailed, loadSslDetail, resetSslDetail } =
  useDomainSslDetail(() =>
    drawerData.value
      ? getTenantSslDetailApi(drawerData.value.domainId)
      : Promise.resolve(null),
  );
const showUploadForm = ref(false);
const uploadForm = ref({ certificate: '', chain: '', privateKey: '' });
let disposed = false;
let generation = 0;
type ViewOwner = SslDrawerData & { generation: number };
let deleteIntent: null | ViewOwner = null;
let deleteConfirmation: ReturnType<typeof Modal.confirm> | undefined;

function currentOwner(): null | ViewOwner {
  return drawerData.value && !disposed
    ? { ...drawerData.value, generation }
    : null;
}

function ownsView(owner: ViewOwner) {
  return (
    !disposed &&
    generation === owner.generation &&
    drawerData.value?.domainId === owner.domainId
  );
}

function beginAction(allowed: boolean) {
  const owner = currentOwner();
  if (!owner || !allowed || actionLoading.value) return null;
  actionLoading.value = true;
  return owner;
}

function resetView() {
  generation++;
  drawerData.value = null;
  resetSslDetail();
  actionLoading.value = false;
  showUploadForm.value = false;
  uploadForm.value = { certificate: '', chain: '', privateKey: '' };
  deleteIntent = null;
  const confirmation = deleteConfirmation;
  deleteConfirmation = undefined;
  confirmation?.destroy();
}

onScopeDispose(() => {
  disposed = true;
  resetView();
});

// Computed properties / 计算属性
const title = computed(() =>
  drawerData.value
    ? `${$t('tenant.system.domain.ssl.title')} - ${drawerData.value.domain}`
    : $t('tenant.system.domain.ssl.title'),
);
const { hasAccessByCodes } = useAccess();
const canProvisionSsl = hasAccessByCodes(['tenant_domain:ssl_provision']);
const canRenewSsl = hasAccessByCodes(['tenant_domain:ssl_renew']);
const canUploadSsl = hasAccessByCodes(['tenant_domain:ssl_upload']);
const canDeleteSsl = hasAccessByCodes(['tenant_domain:ssl_delete']);
const canToggleSslAutoRenew = hasAccessByCodes([
  'tenant_domain:ssl_auto_renew',
]);
const canRenewCurrentSsl = computed(
  () =>
    !isDefaultDomain.value &&
    canRenewSsl &&
    supportsCertificateRenewal(sslDetail.value),
);
const canToggleCurrentSslAutoRenew = computed(
  () =>
    !isDefaultDomain.value &&
    canToggleSslAutoRenew &&
    supportsCertificateRenewal(sslDetail.value),
);

// Drawer / 抽屉
const [Drawer, drawerApi] = useVbenDrawer({
  async onOpenChange(isOpen) {
    if (disposed) return;
    resetView();
    if (isOpen) {
      const data = drawerApi.getData<SslDrawerData>();
      if (data) {
        drawerData.value = data;
        if (!data.isDefault) await loadSslDetail();
      }
    }
  },
  footer: false,
});

/** 签发平台证书 / Provision platform cert */
async function onProvision() {
  const owner = beginAction(!isDefaultDomain.value && canProvisionSsl);
  if (!owner) return;
  try {
    await provisionTenantSslApi(owner.domainId);
    if (!ownsView(owner)) return;
    message.success($t('tenant.system.domain.ssl.provisionStarted'));
    await loadSslDetail();
  } catch {
  } finally {
    if (ownsView(owner)) actionLoading.value = false;
  }
}

/** 续期平台证书 / Renew platform cert */
async function onRenew() {
  const owner = beginAction(canRenewCurrentSsl.value);
  if (!owner) return;
  try {
    await renewTenantSslApi(owner.domainId);
    if (!ownsView(owner)) return;
    message.success($t('tenant.system.domain.ssl.renewStarted'));
    await loadSslDetail();
  } catch {
  } finally {
    if (ownsView(owner)) actionLoading.value = false;
  }
}

/** 切换自动续期 / Toggle auto-renew */
async function onToggleAutoRenew(checked: boolean) {
  const owner = beginAction(canToggleCurrentSslAutoRenew.value);
  if (!owner) return;
  try {
    const result = await updateTenantSslAutoRenewApi(owner.domainId, checked);
    if (!ownsView(owner)) return;
    sslDetail.value = result;
    message.success($t('tenant.system.domain.ssl.autoRenewUpdated'));
  } catch {
  } finally {
    if (ownsView(owner)) actionLoading.value = false;
  }
}

/** 上传自定义证书 / Upload custom cert */
async function onUpload() {
  if (disposed || actionLoading.value || isDefaultDomain.value || !canUploadSsl)
    return;
  if (!uploadForm.value.certificate || !uploadForm.value.privateKey) {
    message.warning($t('tenant.system.domain.ssl.uploadRequired'));
    return;
  }
  const owner = beginAction(true);
  if (!owner) return;
  try {
    await uploadTenantSslCertApi(owner.domainId, {
      certificate: uploadForm.value.certificate,
      certificate_chain: uploadForm.value.chain || undefined,
      private_key: uploadForm.value.privateKey,
    });
    if (!ownsView(owner)) return;
    message.success($t('tenant.system.domain.ssl.uploadSuccess'));
    showUploadForm.value = false;
    uploadForm.value = { certificate: '', chain: '', privateKey: '' };
    await loadSslDetail();
  } catch {
  } finally {
    if (ownsView(owner)) actionLoading.value = false;
  }
}

/** 删除证书 / Delete cert */
async function onDelete() {
  const owner = currentOwner();
  if (
    !owner ||
    isDefaultDomain.value ||
    !canDeleteSsl ||
    actionLoading.value ||
    deleteIntent
  )
    return;
  deleteIntent = owner;
  deleteConfirmation = Modal.confirm({
    title: $t('tenant.system.domain.ssl.deleteTitle'),
    content: $t('tenant.system.domain.ssl.deleteConfirm'),
    okType: 'danger',
    onCancel() {
      if (deleteIntent !== owner) return;
      deleteIntent = null;
      deleteConfirmation = undefined;
    },
    async onOk() {
      if (
        deleteIntent !== owner ||
        !ownsView(owner) ||
        !canDeleteSsl ||
        actionLoading.value
      )
        return;
      actionLoading.value = true;
      try {
        await deleteTenantSslCertApi(owner.domainId);
        if (!ownsView(owner)) return;
        message.success($t('tenant.system.domain.ssl.deleteSuccess'));
        sslDetail.value = null;
        await loadSslDetail();
      } catch {
      } finally {
        if (ownsView(owner)) actionLoading.value = false;
        if (deleteIntent === owner) {
          deleteIntent = null;
          deleteConfirmation = undefined;
        }
      }
    },
  });
}

/** 获取 SSL 状态标签配置 / Get SSL status tag config */
function getSslStatusConfig(status?: string) {
  const presentation = getDomainSslPresentation(status);
  return {
    ...presentation,
    text: $t(`tenant.system.domain.ssl.${presentation.key}`),
  };
}

/** 打开抽屉 / Open drawer */
function open(data: SslDrawerData) {
  drawerApi.setData(data).open();
}

defineExpose({ open });
</script>

<template>
  <Drawer :title="title" class="w-[550px]">
    <Alert
      v-if="!isDefaultDomain"
      type="info"
      class="mb-4"
      :message="$t('shared.domain.publicationUnconfirmed')"
      show-icon
    >
      <template #description>
        <p>{{ $t('shared.domain.certificateDeploymentHint') }}</p>
        <ol class="mt-2 list-decimal space-y-1 pl-4">
          <li>{{ $t('shared.domain.publicationInstallStep') }}</li>
          <li>{{ $t('shared.domain.publicationVerifyStep') }}</li>
        </ol>
      </template>
    </Alert>
    <Spin :spinning="loading">
      <DomainManagedHttps
        v-if="isDefaultDomain"
        :info="drawerData?.managedHttps"
        detail
      />
      <Alert
        v-else-if="loadFailed"
        type="error"
        :message="$t('common.loadFailed')"
        show-icon
      >
        <template #action>
          <Button :loading="loading" @click="loadSslDetail">
            {{ $t('shared.domain.retry') }}
          </Button>
        </template>
      </Alert>
      <template v-else-if="!loading && sslDetail">
        <!-- 证书详情 -->
        <div class="mb-4">
          <h4 class="mb-3 text-sm font-medium">
            {{ $t('tenant.system.domain.sslStatus') }}
          </h4>
          <Descriptions :column="1" bordered size="small">
            <Descriptions.Item :label="$t('common.status')">
              <Tag :color="getSslStatusConfig(sslDetail.status).color">
                {{ getSslStatusConfig(sslDetail.status).text }}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item
              :label="$t('tenant.system.domain.ssl.typeLabel')"
            >
              {{
                sslDetail.certType === 'platform'
                  ? $t('tenant.system.domain.ssl.typePlatform')
                  : $t('tenant.system.domain.ssl.typeCustom')
              }}
            </Descriptions.Item>
            <Descriptions.Item
              v-if="sslDetail.issuer"
              :label="$t('tenant.system.domain.ssl.issuer')"
            >
              {{ sslDetail.issuer }}
            </Descriptions.Item>
            <Descriptions.Item
              v-if="sslDetail.issuedAt"
              :label="$t('tenant.system.domain.ssl.issuedAt')"
            >
              {{ formatDate(sslDetail.issuedAt) }}
            </Descriptions.Item>
            <Descriptions.Item
              v-if="sslDetail.expiresAt"
              :label="$t('tenant.system.domain.ssl.expiresAt')"
            >
              {{ formatDate(sslDetail.expiresAt) }}
            </Descriptions.Item>
            <Descriptions.Item
              v-if="canToggleCurrentSslAutoRenew"
              :label="$t('tenant.system.domain.ssl.autoRenewLabel')"
            >
              <Switch
                :checked="sslDetail.autoRenew"
                :loading="actionLoading"
                size="small"
                @change="
                  (val: boolean | string | number) => onToggleAutoRenew(!!val)
                "
              />
              <span class="ml-2 text-xs text-muted-foreground">
                {{
                  sslDetail.autoRenew
                    ? $t('tenant.system.domain.ssl.autoRenewEnabled')
                    : $t('tenant.system.domain.ssl.autoRenewDisabled')
                }}
              </span>
            </Descriptions.Item>
            <Descriptions.Item
              v-if="sslDetail.renewalError"
              :label="$t('tenant.system.domain.ssl.renewalError')"
            >
              <span class="text-xs text-destructive">{{
                sslDetail.renewalError
              }}</span>
            </Descriptions.Item>
          </Descriptions>
        </div>

        <!-- 操作按钮 -->
        <div class="mb-4 flex flex-wrap gap-2">
          <Button
            v-if="canRenewCurrentSsl"
            type="primary"
            size="small"
            :loading="actionLoading"
            @click="onRenew"
          >
            <IconifyIcon icon="lucide:refresh-cw" class="mr-1 size-3" />
            {{ $t('tenant.system.domain.ssl.renew') }}
          </Button>
          <Button
            v-if="
              (!sslDetail || sslDetail.status !== 'active') && canProvisionSsl
            "
            type="primary"
            size="small"
            :loading="actionLoading"
            @click="onProvision"
          >
            <IconifyIcon icon="lucide:shield-check" class="mr-1 size-3" />
            {{ $t('tenant.system.domain.ssl.provision') }}
          </Button>
          <Button
            v-if="canUploadSsl"
            size="small"
            @click="showUploadForm = !showUploadForm"
          >
            <IconifyIcon icon="lucide:upload" class="mr-1 size-3" />
            {{ $t('tenant.system.domain.ssl.upload') }}
          </Button>
          <Button
            v-if="canDeleteSsl"
            danger
            size="small"
            :loading="actionLoading"
            @click="onDelete"
          >
            <IconifyIcon icon="lucide:trash-2" class="mr-1 size-3" />
            {{ $t('tenant.system.domain.ssl.deleteCert') }}
          </Button>
        </div>
      </template>

      <!-- 无证书状态 -->
      <template v-else-if="!loading">
        <div
          class="flex flex-col items-center justify-center py-12 text-muted-foreground"
        >
          <IconifyIcon icon="lucide:shield-off" class="mb-3 size-12" />
          <p class="mb-4 text-sm">{{ $t('tenant.system.domain.ssl.none') }}</p>
          <div class="flex gap-2">
            <Button
              v-if="canProvisionSsl"
              type="primary"
              size="small"
              :loading="actionLoading"
              @click="onProvision"
            >
              <IconifyIcon icon="lucide:shield-check" class="mr-1 size-3" />
              {{ $t('tenant.system.domain.ssl.provision') }}
            </Button>
            <Button
              v-if="canUploadSsl"
              size="small"
              @click="showUploadForm = !showUploadForm"
            >
              <IconifyIcon icon="lucide:upload" class="mr-1 size-3" />
              {{ $t('tenant.system.domain.ssl.upload') }}
            </Button>
          </div>
        </div>
      </template>

      <!-- 上传自定义证书表单 -->
      <template
        v-if="
          !isDefaultDomain &&
          showUploadForm &&
          canUploadSsl &&
          !loadFailed &&
          !loading
        "
      >
        <Divider />
        <div>
          <h4 class="mb-3 text-sm font-medium">
            {{ $t('tenant.system.domain.ssl.upload') }}
          </h4>
          <div class="flex flex-col gap-3">
            <div>
              <label class="mb-1 block text-xs font-medium">
                {{ $t('tenant.system.domain.ssl.certLabel') }}
              </label>
              <Textarea
                v-model:value="uploadForm.certificate"
                :rows="4"
                placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium">
                {{ $t('tenant.system.domain.ssl.keyLabel') }}
              </label>
              <Textarea
                v-model:value="uploadForm.privateKey"
                :rows="4"
                placeholder="-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium">
                {{ $t('tenant.system.domain.ssl.chainLabel') }}
                <span class="text-muted-foreground"
                  >({{ $t('common.optional') }})</span
                >
              </label>
              <Textarea
                v-model:value="uploadForm.chain"
                :rows="3"
                placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
              />
            </div>
            <div class="flex gap-2">
              <Button
                type="primary"
                size="small"
                :loading="actionLoading"
                @click="onUpload"
              >
                {{ $t('common.confirm') }}
              </Button>
              <Button size="small" @click="showUploadForm = false">
                {{ $t('common.cancel') }}
              </Button>
            </div>
          </div>
        </div>
      </template>
    </Spin>
  </Drawer>
</template>
