import { useAppConfig } from '@vben/hooks';

const LOOPBACK_HOSTNAMES = new Set(['0.0.0.0', '127.0.0.1', 'localhost']);

function normalizeApiUrl(url: null | string | undefined): string {
  return (typeof url === 'string' ? url : '').trim().replace(/\/+$/, '');
}

function normalizeHostname(hostname: string): string {
  return hostname
    .trim()
    .replaceAll(/^\[|\]$/g, '')
    .replace(/\.$/, '')
    .toLowerCase();
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = normalizeHostname(hostname);
  return normalized === '::1' || LOOPBACK_HOSTNAMES.has(normalized);
}

function normalizeDomainSet(domains?: string): Set<string> {
  return new Set(
    (domains ?? '')
      .split(',')
      .map((domain) => normalizeHostname(domain))
      .filter(Boolean),
  );
}

function resolvePageOrigin(origin?: string, hostname?: string): null | string {
  if (!origin || !hostname) return null;
  try {
    const parsed = new URL(origin);
    if (
      !/^https?:$/.test(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== '/' ||
      parsed.search ||
      parsed.hash ||
      normalizeHostname(parsed.hostname) !== normalizeHostname(hostname)
    ) {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

export function resolveApiUrl(
  rawApiUrl: null | string | undefined,
  options: {
    currentHostname?: string;
    currentOrigin?: string;
    isProduction?: boolean;
    platformDomains?: string;
  } = {},
): string {
  const normalizedRawApiUrl = normalizeApiUrl(rawApiUrl);
  if (!normalizedRawApiUrl) {
    return normalizedRawApiUrl;
  }

  if (
    /^\/(?![/\\])/.test(normalizedRawApiUrl) &&
    !/[\\?#\s]/.test(normalizedRawApiUrl)
  ) {
    return normalizedRawApiUrl;
  }

  try {
    const parsed = new URL(normalizedRawApiUrl);
    if (
      !/^https?:$/.test(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      /[\\\s]/.test(normalizedRawApiUrl)
    ) {
      throw new Error('Invalid API URL configuration');
    }

    // 中文: 平台托管 API 在企业域名下走同源代理，覆盖首次首页初始化及后续导航。
    // EN: Use the tenant's same-origin proxy for a platform-hosted API, including bootstrap.
    if (options.isProduction && !isLoopbackHostname(parsed.hostname)) {
      const currentHostname = normalizeHostname(options.currentHostname ?? '');
      const pageOrigin = resolvePageOrigin(
        options.currentOrigin,
        options.currentHostname,
      );
      const configuredPlatformDomains = normalizeDomainSet(
        options.platformDomains,
      );
      const configuredApiHostname = normalizeHostname(parsed.hostname);
      if (
        pageOrigin &&
        !isLoopbackHostname(currentHostname) &&
        !configuredPlatformDomains.has(currentHostname) &&
        configuredPlatformDomains.has(configuredApiHostname)
      ) {
        return `${pageOrigin}${parsed.pathname.replace(/\/+$/, '')}`;
      }
    }

    if (options.isProduction || !isLoopbackHostname(parsed.hostname)) {
      return normalizedRawApiUrl;
    }

    const currentHostname = options.currentHostname?.trim();
    if (!currentHostname || isLoopbackHostname(currentHostname)) {
      return normalizedRawApiUrl;
    }

    parsed.hostname = currentHostname;
    return parsed.toString().replace(/\/+$/, '');
  } catch {
    throw new Error('Invalid API URL configuration');
  }
}

export function getAppApiUrl(): string {
  const { apiURL } = useAppConfig(import.meta.env, import.meta.env.PROD);
  const currentHostname =
    typeof window === 'undefined' ? undefined : window.location.hostname;
  const currentOrigin =
    typeof window === 'undefined' ? undefined : window.location.origin;
  const platformDomains =
    (import.meta.env.PROD && typeof window !== 'undefined'
      ? window._VBEN_ADMIN_PRO_APP_CONF_?.VITE_PLATFORM_DOMAINS
      : undefined) ?? import.meta.env.VITE_PLATFORM_DOMAINS;

  return resolveApiUrl(apiURL, {
    currentHostname,
    currentOrigin,
    isProduction: import.meta.env.PROD,
    platformDomains,
  });
}
