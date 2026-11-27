// Test type: behavioral
import { describe, expect, it } from 'vitest';

import { useDomainSslDetail } from '../use-domain-ssl-detail';

describe('domain SSL detail loading', () => {
  it('distinguishes request failure from an absent independent certificate and supports retry', async () => {
    let fail = true;
    const state = useDomainSslDetail(async () => {
      if (fail) throw new Error('TLS detail unavailable');
      return null;
    });
    await state.loadSslDetail();
    expect(state.loadFailed.value).toBe(true);
    expect(state.sslDetail.value).toBeNull();
    expect(state.loading.value).toBe(false);
    fail = false;
    await state.loadSslDetail();
    expect(state.loadFailed.value).toBe(false);
    expect(state.sslDetail.value).toBeNull();
    expect(state.loading.value).toBe(false);
  });

  it('ignores stale failures after the drawer closes or another domain opens', async () => {
    let reject: (reason: Error) => void = () => {};
    let requests = 0;
    const state = useDomainSslDetail(() => {
      if (requests++ === 0)
        return new Promise((_, onReject) => {
          reject = onReject;
        });
      return Promise.resolve(null);
    });
    const oldLoad = state.loadSslDetail();
    expect(state.loading.value).toBe(true);
    state.resetSslDetail();
    await state.loadSslDetail();
    reject(new Error('Old domain failed'));
    await oldLoad;
    expect(state.loadFailed.value).toBe(false);
    expect(state.loading.value).toBe(false);
    expect(state.sslDetail.value).toBeNull();
  });
});
