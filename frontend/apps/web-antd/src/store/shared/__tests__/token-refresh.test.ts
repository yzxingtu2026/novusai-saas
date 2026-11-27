// Test type: behavioral
import { AxiosError } from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const accessStore = vi.hoisted(() => ({
  accessToken: null as null | string,
  refreshToken: null as null | string,
  setAccessToken: vi.fn(),
  setRefreshToken: vi.fn(),
}));

vi.mock('@vben/stores', () => ({ useAccessStore: () => accessStore }));
vi.mock('@vben/preferences', () => ({
  preferences: { app: { enableRefreshToken: true } },
}));

const namespace = 'vitest_refresh';
const endpoint = 'tenant';
const lockName = 'novusai:refresh:vitest_refresh_tenant_admin_refresh_token';

function tokenPair(sequence: string, family?: string) {
  const token = (type: string) => {
    const payload = btoa(
      JSON.stringify({
        jti: `${sequence}-${type}`,
        refresh_family_id: family,
        scope: 'tenant_admin',
        sub: '7',
        type,
      }),
    )
      .replaceAll('+', '-')
      .replaceAll('/', '_')
      .replaceAll('=', '');
    return `header.${payload}.signature`;
  };
  return { accessToken: token('access'), refreshToken: token('refresh') };
}

const original = tokenPair('first', 'original-family');
const rotated = tokenPair('second', 'original-family');
const otherLogin = tokenPair('new-login', 'new-family');

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function loadPage() {
  vi.resetModules();
  const { TokenStorage } = await import('../token-storage');
  const { refreshEndpointTokens } = await import('../token-refresh');
  TokenStorage.init(namespace);
  return {
    exchange: (
      post: (token: string) => Promise<typeof original>,
      isCurrent = () => true,
      failedAccessToken?: string,
    ) => refreshEndpointTokens(endpoint, post, isCurrent, failedAccessToken),
    read: () => TokenStorage.getTokenData(endpoint),
    write: ({ accessToken, refreshToken }: typeof original) => {
      TokenStorage.setToken(endpoint, accessToken);
      TokenStorage.setRefreshToken(endpoint, refreshToken);
    },
  };
}

function simulateWebLocks() {
  const tails = new Map<string, Promise<unknown>>();
  type Handler = (lock: null | { name: string }) => unknown;
  const request = vi.fn(
    (
      name: string,
      options: Handler | { ifAvailable: boolean },
      callback?: Handler,
    ) => {
      const run = typeof options === 'function' ? options : callback;
      if (!run) throw new Error('A lock callback is required');
      if (
        typeof options !== 'function' &&
        options.ifAvailable &&
        tails.has(name)
      ) {
        return Promise.resolve(run(null));
      }
      const result = (tails.get(name) ?? Promise.resolve()).then(() =>
        run({ name }),
      );
      const tail = result
        .catch(() => undefined)
        .finally(() => {
          if (tails.get(name) === tail) tails.delete(name);
        });
      tails.set(name, tail);
      return result;
    },
  );
  vi.stubGlobal('navigator', { locks: { request } });
  return request;
}

describe('browser refresh coordination', () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState({}, '', '/tenant/dashboard');
    vi.clearAllMocks();
    accessStore.accessToken = null;
    accessStore.refreshToken = null;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it.each([
    [original, rotated],
    [tokenPair('legacy'), tokenPair('replacement', 'legacy-refresh')],
  ])(
    'reuses the same session after a separate page rotates it',
    async (source, child) => {
      const lock = simulateWebLocks();
      const first = await loadPage();
      const second = await loadPage();
      first.write(source);
      const response = deferred<typeof original>();
      const post = vi.fn(() => response.promise);

      const firstPage = first.exchange(post);
      const secondPage = second.exchange(post);
      response.resolve(child);

      await expect(Promise.all([firstPage, secondPage])).resolves.toEqual([
        child,
        child,
      ]);
      expect(lock.mock.calls.map(([name]) => name)).toEqual([
        lockName,
        lockName,
        lockName,
      ]);
      expect(post.mock.calls).toEqual([[source.refreshToken]]);
      expect(second.read()).toEqual(child);
    },
  );

  it('shares one active exchange in a page without Web Locks', async () => {
    vi.stubGlobal('navigator', {});
    const page = await loadPage();
    page.write(original);
    const response = deferred<typeof original>();
    const post = vi.fn(() => response.promise);

    const automatic = page.exchange(post, () => true, original.accessToken);
    const manual = page.exchange(post);
    response.resolve(rotated);

    await expect(Promise.all([automatic, manual])).resolves.toEqual([
      rotated,
      rotated,
    ]);
    expect(post.mock.calls).toEqual([[original.refreshToken]]);
    expect(page.read()).toEqual(rotated);
  });

  it('does not submit the old token while storage propagation trails lock handoff', async () => {
    const lock = simulateWebLocks();
    const page = await loadPage();
    const unlocked = deferred<undefined>();
    lock(lockName, () => unlocked.promise);
    page.write(original);
    const addListener = vi.spyOn(window, 'addEventListener');
    const post = vi.fn(async () => rotated);
    const attempt = page.exchange(post);
    unlocked.resolve(undefined);

    await vi.waitFor(() => {
      expect(
        addListener.mock.calls.some(([event]) => event === 'storage'),
      ).toBe(true);
    });
    expect(post.mock.calls).toEqual([]);

    page.write(rotated);
    window.dispatchEvent(
      new StorageEvent('storage', { storageArea: localStorage }),
    );
    await expect(attempt).resolves.toEqual(rotated);
    expect(post.mock.calls).toEqual([]);
  });

  it('rejects another login by the same account while waiting for the lock', async () => {
    const lock = simulateWebLocks();
    const page = await loadPage();
    const unlocked = deferred<undefined>();
    lock(lockName, () => unlocked.promise);
    page.write(original);
    const post = vi.fn(async () => rotated);
    const attempt = page.exchange(post);
    const rejected = expect(attempt).rejects.toThrow(
      'Authentication session changed',
    );
    page.write(otherLogin);
    unlocked.resolve(undefined);

    await rejected;
    expect(post.mock.calls).toEqual([]);
    expect(page.read()).toEqual(otherLogin);
  });

  it('leaves a newer login intact when an older exchange finishes', async () => {
    vi.stubGlobal('navigator', {});
    const page = await loadPage();
    page.write(original);
    const response = deferred<typeof original>();
    const post = vi.fn(() => response.promise);
    const attempt = page.exchange(post);
    const rejected = expect(attempt).rejects.toThrow(
      'Authentication session changed',
    );
    page.write(otherLogin);
    response.resolve(rotated);

    await rejected;
    expect(post.mock.calls).toEqual([[original.refreshToken]]);
    expect(page.read()).toEqual(otherLogin);
  });

  it('skips a second exchange for a delayed 401 and rejects a departed session', async () => {
    const page = await loadPage();
    const post = vi.fn(async () => rotated);
    page.write(rotated);

    await expect(
      page.exchange(post, () => true, original.accessToken),
    ).resolves.toEqual(rotated);
    page.write(otherLogin);
    await expect(
      page.exchange(post, () => true, original.accessToken),
    ).rejects.toThrow('Authentication session changed');
    expect(post.mock.calls).toEqual([]);
    expect(page.read()).toEqual(otherLogin);
  });

  it('preserves the exchange error when the login is unchanged', async () => {
    const page = await loadPage();
    page.write(original);
    const transportError = new Error('Rejected by the refresh endpoint');

    await expect(
      page.exchange(async () => {
        throw transportError;
      }),
    ).rejects.toBe(transportError);
    expect(page.read()).toEqual(original);
  });

  it(
    'coalesces HTTP, SSE and manual refresh and retries with the new access token',
    { timeout: 15_000 },
    async () => {
      vi.stubGlobal('navigator', {});
      vi.stubEnv('VITE_ROUTER_HISTORY', 'hash');
      window.history.replaceState({}, '', '/#/tenant/dashboard?view=open');
      const page = await loadPage();
      const { baseRequestClient, refreshEndpointToken, requestClient } =
        await import('#/utils/request/instance');
      page.write(original);

      const started = deferred<undefined>();
      const release = deferred<undefined>();
      const refreshPosts: unknown[] = [];
      baseRequestClient.instance.defaults.adapter = async (config) => {
        refreshPosts.push([config.url, JSON.parse(config.data)]);
        started.resolve(undefined);
        await release.promise;
        return {
          config,
          data: {
            code: 0,
            data: {
              access_token: rotated.accessToken,
              refresh_token: rotated.refreshToken,
            },
          },
          headers: {},
          status: 200,
          statusText: 'OK',
        };
      };
      const httpTokens: unknown[] = [];
      requestClient.instance.defaults.adapter = async (config) => {
        const token = config.headers.get('Authorization');
        httpTokens.push(token);
        if (httpTokens.length === 1) {
          throw new AxiosError(
            'Expired',
            'ERR_BAD_REQUEST',
            config,
            undefined,
            {
              config,
              data: { code: 4011, message: 'Expired' },
              headers: {},
              status: 401,
              statusText: 'Unauthorized',
            },
          );
        }
        return {
          config,
          data: { code: 0, data: { account: '7' } },
          headers: {},
          status: 200,
          statusText: 'OK',
        };
      };
      const sseTokens: unknown[] = [];
      vi.spyOn(globalThis, 'fetch').mockImplementation(async (_, options) => {
        sseTokens.push(new Headers(options?.headers).get('Authorization'));
        return sseTokens.length === 1
          ? Response.json({ code: 4011, message: 'Expired' }, { status: 401 })
          : new Response('data: ready\n\n');
      });

      const messages: string[] = [];
      const http = requestClient.get('/tenant/fixture');
      await started.promise;
      const manual = refreshEndpointToken(endpoint);
      const sse = requestClient.requestSSE(
        '/tenant/fixture-stream',
        undefined,
        {
          onMessage: (chunk) => {
            messages.push(chunk);
          },
        },
      );
      release.resolve(undefined);

      await expect(Promise.all([http, manual, sse])).resolves.toEqual([
        { account: '7' },
        rotated.accessToken,
        undefined,
      ]);
      expect(refreshPosts).toEqual([
        ['/tenant/auth/refresh', { refresh_token: original.refreshToken }],
      ]);
      const expected = [
        `Bearer ${original.accessToken}`,
        `Bearer ${rotated.accessToken}`,
      ];
      expect(httpTokens).toEqual(expected);
      expect(sseTokens).toEqual(expected);
      expect(messages).toEqual(['data: ready\n\n']);
      expect(page.read()).toEqual(rotated);
      expect(accessStore.setAccessToken).toHaveBeenLastCalledWith(
        rotated.accessToken,
      );
    },
  );

  it('synchronizes only a complete successor with the current hash route store', async () => {
    vi.stubEnv('VITE_ROUTER_HISTORY', 'hash');
    window.history.replaceState({}, '', '/#/tenant/dashboard');
    const page = await loadPage();
    const { TokenStorage } = await import('../token-storage');
    const { trackSharedSessionRefresh } =
      await import('#/utils/request/instance');
    page.write(original);
    accessStore.accessToken = original.accessToken;
    accessStore.refreshToken = original.refreshToken;
    const emitStorage = () => {
      window.dispatchEvent(
        new StorageEvent('storage', { storageArea: localStorage }),
      );
    };
    const dispose = trackSharedSessionRefresh();
    try {
      page.write(otherLogin);
      emitStorage();
      expect(accessStore.setAccessToken).not.toHaveBeenCalled();

      TokenStorage.setToken(endpoint, rotated.accessToken);
      emitStorage();
      expect(accessStore.setAccessToken).not.toHaveBeenCalled();

      TokenStorage.setRefreshToken(endpoint, rotated.refreshToken);
      emitStorage();
      expect(accessStore.setAccessToken.mock.calls).toEqual([
        [rotated.accessToken],
      ]);
      expect(accessStore.setRefreshToken.mock.calls).toEqual([
        [rotated.refreshToken],
      ]);
    } finally {
      dispose();
    }
  });
});
