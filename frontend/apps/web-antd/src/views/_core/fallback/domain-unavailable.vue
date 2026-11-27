<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { Button, Result } from 'ant-design-vue';

import { usePublicConfigStore } from '#/store/shared/public-config';
import { isDomainEntryUnavailable } from '#/utils/public-config-domain';

const store = usePublicConfigStore();
const route = useRoute();
const router = useRouter();
const retrying = ref(false);
const unknownHost = computed(() => store.isDomainDetected);

async function retry() {
  if (retrying.value) return;
  retrying.value = true;
  try {
    await store.retryDomainDetection();
    if (
      !isDomainEntryUnavailable(
        store.isDomainDetected,
        store.isDomainTenantDomain,
      )
    ) {
      const redirect = route.query.redirect;
      const target =
        typeof redirect === 'string' &&
        /^\/(?![/\\])/.test(redirect) &&
        router.resolve(redirect).name !== 'DomainUnavailable'
          ? redirect
          : '/';
      await router.replace(target);
    }
  } finally {
    retrying.value = false;
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center">
    <Result
      :status="unknownHost ? '404' : 'error'"
      :title="
        $t(unknownHost ? 'shared.domain.unknown' : 'shared.domain.unavailable')
      "
      :sub-title="
        $t(
          unknownHost
            ? 'shared.domain.unknownDescription'
            : 'shared.domain.unavailableDescription',
        )
      "
    >
      <template #extra>
        <Button type="primary" :loading="retrying" @click="retry">
          {{ $t('shared.domain.retry') }}
        </Button>
      </template>
    </Result>
  </div>
</template>
