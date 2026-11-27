import type { SslCertificateInfo } from '#/types/domain';

import { ref } from 'vue';

export function useDomainSslDetail(
  fetchDetail: () => Promise<null | SslCertificateInfo>,
) {
  const sslDetail = ref<null | SslCertificateInfo>(null);
  const loading = ref(false);
  const loadFailed = ref(false);
  let generation = 0;

  function resetSslDetail() {
    generation++;
    sslDetail.value = null;
    loadFailed.value = false;
    loading.value = false;
  }

  async function loadSslDetail() {
    const request = ++generation;
    sslDetail.value = null;
    loading.value = true;
    loadFailed.value = false;
    try {
      const detail = await fetchDetail();
      if (request === generation) sslDetail.value = detail;
    } catch {
      if (request === generation) loadFailed.value = true;
    } finally {
      if (request === generation) loading.value = false;
    }
  }

  return { sslDetail, loading, loadFailed, loadSslDetail, resetSslDetail };
}
