import type { EndpointTokenData } from './token-storage';

import type { ApiEndpoint } from '#/types/endpoint';

import { TokenStorage } from './token-storage';

type TokenPair = { accessToken: string; refreshToken?: string };
type Exchange = (refreshToken: string) => Promise<TokenPair>;

const endpointScopes: Record<ApiEndpoint, string> = {
  admin: 'admin',
  tenant: 'tenant_admin',
  user: 'tenant_user',
};

const pendingRefreshes = new Map<
  string,
  { result: Promise<TokenPair>; source: EndpointTokenData }
>();
const storageSyncTimeoutMs = 1000;

export class RefreshSessionChangedError extends Error {
  constructor() {
    super('Authentication session changed during refresh');
  }
}

export function readBearerToken(value: unknown): null | string {
  return typeof value === 'string'
    ? (/^Bearer (\S+)$/i.exec(value)?.[1] ?? null)
    : null;
}

// JWT claims here only correlate an existing browser session. The server verifies tokens.
function sessionFamily(
  token: null | string,
  type: 'access' | 'refresh',
  endpoint: ApiEndpoint,
): null | string {
  const parts = token?.split('.');
  const payload = parts?.[1];
  if (parts?.length !== 3 || !payload) return null;
  try {
    const claims: unknown = JSON.parse(
      atob(payload.replaceAll('-', '+').replaceAll('_', '/')),
    );
    if (!claims || typeof claims !== 'object') return null;
    const fields = claims as Record<string, unknown>;
    const family =
      fields.refresh_family_id ?? (type === 'refresh' ? fields.jti : undefined);
    if (
      fields.type === type &&
      fields.scope === endpointScopes[endpoint] &&
      typeof fields.sub === 'string' &&
      /^\d+$/.test(fields.sub) &&
      typeof family === 'string' &&
      /^[\w.-]{1,128}$/.test(family)
    ) {
      return JSON.stringify([fields.sub, family]);
    }
  } catch {
    // Opaque or malformed tokens cannot identify a successor in another page.
  }
  return null;
}

function samePair(left: EndpointTokenData, right: EndpointTokenData) {
  return (
    left.accessToken === right.accessToken &&
    left.refreshToken === right.refreshToken
  );
}

function matchingFamily(
  tokens: EndpointTokenData,
  endpoint: ApiEndpoint,
  family: null | string,
): TokenPair | undefined {
  if (
    family &&
    tokens.accessToken &&
    tokens.refreshToken &&
    sessionFamily(tokens.accessToken, 'access', endpoint) === family &&
    sessionFamily(tokens.refreshToken, 'refresh', endpoint) === family
  ) {
    return {
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
    };
  }
}

export function readSharedRefreshSuccessor(
  endpoint: ApiEndpoint,
  source: EndpointTokenData,
): TokenPair | undefined {
  const current = TokenStorage.getTokenData(endpoint);
  if (
    current.refreshToken === source.refreshToken ||
    current.accessToken === source.accessToken
  ) {
    return;
  }
  const accessFamily = sessionFamily(source.accessToken, 'access', endpoint);
  const refreshFamily = sessionFamily(source.refreshToken, 'refresh', endpoint);
  if (accessFamily && refreshFamily && accessFamily !== refreshFamily) return;
  return matchingFamily(current, endpoint, accessFamily ?? refreshFamily);
}

/** Serialize refresh across same-origin windows and keep late results scoped to their login. */
export async function refreshEndpointTokens(
  endpoint: ApiEndpoint,
  exchange: Exchange,
  isCurrentEndpoint: () => boolean,
  failedAccessToken?: null | string,
): Promise<TokenPair> {
  const storageKey = TokenStorage.getRefreshTokenKey(endpoint);
  const source = TokenStorage.getTokenData(endpoint);
  const isCurrent = () =>
    isCurrentEndpoint() &&
    TokenStorage.getRefreshTokenKey(endpoint) === storageKey;

  if (!isCurrent()) throw new RefreshSessionChangedError();
  if (
    failedAccessToken !== undefined &&
    failedAccessToken !== source.accessToken
  ) {
    const successor = matchingFamily(
      source,
      endpoint,
      sessionFamily(failedAccessToken, 'access', endpoint),
    );
    if (successor) return successor;
    throw new RefreshSessionChangedError();
  }
  if (!storageKey || !source.refreshToken) {
    throw new Error('No refresh token available');
  }

  const refreshToken = source.refreshToken;
  const family = sessionFamily(refreshToken, 'refresh', endpoint);
  const accessFamily = sessionFamily(source.accessToken, 'access', endpoint);
  if (accessFamily && accessFamily !== family) {
    throw new RefreshSessionChangedError();
  }

  const readCurrent = () => TokenStorage.getTokenData(endpoint);
  const readSuccessor = (current: EndpointTokenData) =>
    current.refreshToken !== refreshToken &&
    current.accessToken !== source.accessToken
      ? matchingFamily(current, endpoint, family)
      : undefined;
  const assertSourceUnchanged = () => {
    if (!isCurrent() || !samePair(source, readCurrent())) {
      throw new RefreshSessionChangedError();
    }
  };
  const run = async () => {
    if (!isCurrent()) throw new RefreshSessionChangedError();
    const current = readCurrent();
    if (!samePair(source, current)) {
      const successor = readSuccessor(current);
      if (successor) return successor;
      throw new RefreshSessionChangedError();
    }
    let result: TokenPair;
    try {
      result = await exchange(refreshToken);
    } catch (error) {
      assertSourceUnchanged();
      throw error;
    }
    assertSourceUnchanged();
    if (!result.accessToken) throw new Error('Failed to refresh token');
    TokenStorage.setToken(endpoint, result.accessToken);
    if (result.refreshToken)
      TokenStorage.setRefreshToken(endpoint, result.refreshToken);
    return result;
  };

  // Web Locks can hand off before another renderer observes the storage writes.
  // A contending page waits for the successor without sending the spent token.
  const waitForSuccessor = () =>
    new Promise<TokenPair>((resolve, reject) => {
      const checkStorage = () => {
        const current = readCurrent();
        const successor = readSuccessor(current);
        if (!isCurrent() || !current.refreshToken || !family) {
          stop();
          reject(new RefreshSessionChangedError());
        } else if (successor) {
          stop();
          resolve(successor);
        } else if (
          (current.refreshToken !== refreshToken &&
            sessionFamily(current.refreshToken, 'refresh', endpoint) !==
              family) ||
          (current.accessToken !== source.accessToken &&
            sessionFamily(current.accessToken, 'access', endpoint) !== family)
        ) {
          stop();
          reject(new RefreshSessionChangedError());
        }
      };
      const timeout = setTimeout(() => {
        stop();
        reject(new RefreshSessionChangedError());
      }, storageSyncTimeoutMs);
      const stop = () => {
        clearTimeout(timeout);
        window.removeEventListener('storage', checkStorage);
      };
      window.addEventListener('storage', checkStorage);
      checkStorage();
    });

  const lockName = `novusai:refresh:${storageKey}`;
  let pending = pendingRefreshes.get(lockName);
  if (!pending || !samePair(source, pending.source)) {
    const locks = globalThis.navigator?.locks;
    const result = locks
      ? (async () => {
          const tokens = await locks.request(
            lockName,
            { ifAvailable: true },
            (lock) => (lock ? run() : undefined),
          );
          return tokens ?? locks.request(lockName, waitForSuccessor);
        })()
      : run();
    pending = { result, source };
    pendingRefreshes.set(lockName, pending);
  }
  try {
    const result = await pending.result;
    if (
      !isCurrent() ||
      !samePair(readCurrent(), {
        accessToken: result.accessToken,
        refreshToken: result.refreshToken ?? refreshToken,
      })
    ) {
      throw new RefreshSessionChangedError();
    }
    return result;
  } finally {
    if (pendingRefreshes.get(lockName) === pending) {
      pendingRefreshes.delete(lockName);
    }
  }
}
