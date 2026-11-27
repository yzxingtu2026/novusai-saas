<script setup lang="ts">
import type { ManagedHttpsInfo } from '#/types/domain';

import { computed } from 'vue';

import { Tag } from 'ant-design-vue';

import { $t } from '#/locales';
import { formatDate } from '#/utils/common';
import { getManagedHttpsPresentation } from '#/utils/domain-presentation';

const props = defineProps<{
  detail?: boolean;
  info?: ManagedHttpsInfo | null;
}>();
const presentation = computed(() => getManagedHttpsPresentation(props.info));
</script>

<template>
  <div :class="detail ? 'space-y-3' : 'flex flex-wrap items-center gap-1'">
    <span class="text-sm text-muted-foreground">
      {{ $t('shared.domain.managedHttps.label') }}:
    </span>
    <Tag :color="presentation.color">
      {{ $t(`shared.domain.managedHttps.${presentation.key}`) }}
    </Tag>
    <template v-if="detail">
      <p class="text-sm text-muted-foreground">
        {{ $t('shared.domain.managedHttps.description') }}
      </p>
      <p v-if="info?.expiresAt" class="text-sm">
        {{ $t('shared.domain.managedHttps.expiresAt') }}:
        {{ formatDate(info.expiresAt) }}
      </p>
      <p v-if="info?.configured" class="text-sm text-muted-foreground">
        {{
          $t(
            info.autoRenew
              ? 'shared.domain.managedHttps.autoRenew'
              : 'shared.domain.managedHttps.manualRenew',
          )
        }}
      </p>
    </template>
  </div>
</template>
