import { useAppConfig } from '@vben/hooks';

// Test type: behavioral
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAppApiUrl, resolveApiUrl } from '#/utils/api-url';

vi.mock('@vben/hooks', () => ({
  useAppConfig: vi.fn(() => ({
    apiURL: '',
    auth: {},
  })),
}));

describe('resolveApiUrl', () => {
  it('uses the tenant origin from initial bootstrap in production', () => {
    expect(
      resolveApiUrl('https://platform.example.test/', {
        currentHostname: 'tenant-a.platform.example.test',
        currentOrigin: 'https://tenant-a.platform.example.test',
        isProduction: true,
        platformDomains: 'platform.example.test',
      }),
    ).toBe('https://tenant-a.platform.example.test');
  });

  it('keeps the platform origin for the platform admin page', () => {
    expect(
      resolveApiUrl('https://platform.example.test/', {
        currentHostname: 'platform.example.test',
        currentOrigin: 'https://platform.example.test',
        isProduction: true,
        platformDomains: 'platform.example.test',
      }),
    ).toBe('https://platform.example.test');
  });

  it('keeps the platform origin for a tenant route hosted on the platform domain', () => {
    expect(
      resolveApiUrl('https://platform.example.test/', {
        currentHostname: 'platform.example.test',
        currentOrigin: 'https://platform.example.test',
        isProduction: true,
        platformDomains: 'platform.example.test',
      }),
    ).toBe('https://platform.example.test');
  });

  it('supports custom tenant domains outside the platform suffix', () => {
    expect(
      resolveApiUrl('https://platform.example.test', {
        currentHostname: 'shop.example.com',
        currentOrigin: 'https://shop.example.com:8443',
        isProduction: true,
        platformDomains: 'platform.example.test',
      }),
    ).toBe('https://shop.example.com:8443');
  });

  it('preserves the configured API path prefix on the tenant host', () => {
    expect(
      resolveApiUrl('https://platform.example.test/gateway/api/', {
        currentHostname: 'tenant-a.platform.example.test',
        currentOrigin: 'https://tenant-a.platform.example.test',
        isProduction: true,
        platformDomains: 'platform.example.test',
      }),
    ).toBe('https://tenant-a.platform.example.test/gateway/api');
  });

  it.each([undefined, '', 'another-platform.example.com', 'dakkii.cn'])(
    'keeps an independent API unless its exact host is configured as platform: %s',
    (platformDomains) => {
      expect(
        resolveApiUrl('https://platform.example.test', {
          currentHostname: 'tenant-a.platform.example.test',
          currentOrigin: 'https://tenant-a.platform.example.test',
          isProduction: true,
          platformDomains,
        }),
      ).toBe('https://platform.example.test');
    },
  );

  it.each([
    undefined,
    'https://different.example.com',
    'https://name:secret@tenant-a.platform.example.test',
    'https://tenant-a.platform.example.test/path',
    'https://tenant-a.platform.example.test?token=x',
    'https://tenant-a.platform.example.test#x',
    'file:///tenant-a.platform.example.test',
  ])(
    'preserves configuration when page origin is missing or invalid: %s',
    (currentOrigin) => {
      expect(
        resolveApiUrl('https://platform.example.test', {
          currentHostname: 'tenant-a.platform.example.test',
          currentOrigin,
          isProduction: true,
          platformDomains: 'platform.example.test',
        }),
      ).toBe('https://platform.example.test');
    },
  );

  it('normalizes domain case, whitespace and trailing dots for exact matching', () => {
    expect(
      resolveApiUrl('https://PLATFORM.EXAMPLE.TEST./api', {
        currentHostname: 'Shop.Example.Com.',
        currentOrigin: 'https://shop.example.com.',
        isProduction: true,
        platformDomains: ' other.example.com, PLATFORM.EXAMPLE.TEST. ',
      }),
    ).toBe('https://shop.example.com./api');
  });

  it.each(['localhost', '127.0.0.1', '[::1]'])(
    'keeps production API configuration on loopback page %s',
    (currentHostname) => {
      expect(
        resolveApiUrl('https://platform.example.test', {
          currentHostname,
          currentOrigin: `http://${currentHostname}:5173`,
          isProduction: true,
          platformDomains: 'platform.example.test',
        }),
      ).toBe('https://platform.example.test');
    },
  );

  it('uses a same-origin root without adding a second API prefix', () => {
    expect(resolveApiUrl('/', { isProduction: true })).toBe('');
  });

  it('accepts intentional relative proxy paths', () => {
    expect(resolveApiUrl('/api/', { isProduction: true })).toBe('/api');
  });

  it.each([
    'javascript:alert(1)',
    '//external.example.com',
    'https://name:secret@api.example.com',
    'https://api.example.com?token=x',
    'https://api.example.com#x',
    String.raw`https:\api.example.com`,
    'api',
    'https://api.example.com/path with space',
  ])('rejects invalid or ambiguous API configuration %s', (value) => {
    expect(() => resolveApiUrl(value, { isProduction: true })).toThrow(
      'Invalid API URL configuration',
    );
  });

  it('supports IPv6 loopback in development without changing an explicit remote host', () => {
    expect(
      resolveApiUrl('http://[::1]:8000', { currentHostname: 'tenant.local' }),
    ).toBe('http://tenant.local:8000');
    expect(
      resolveApiUrl('http://remote.local:8000', {
        currentHostname: 'tenant.local',
      }),
    ).toBe('http://remote.local:8000');
  });
  it('returns an empty string when api url config is missing', () => {
    expect(resolveApiUrl(undefined)).toBe('');
  });

  it('keeps explicit non-loopback api hosts in development', () => {
    expect(
      resolveApiUrl('http://192.168.31.129:8000/', {
        currentHostname: 'localhost',
      }),
    ).toBe('http://192.168.31.129:8000');
  });

  it('rewrites loopback api hosts to the current page hostname in development', () => {
    expect(
      resolveApiUrl('http://127.0.0.1:8000', {
        currentHostname: 'tenant.demo.local',
      }),
    ).toBe('http://tenant.demo.local:8000');
  });

  it('preserves loopback api hosts when the current page also uses loopback', () => {
    expect(
      resolveApiUrl('http://localhost:8000/', {
        currentHostname: 'localhost',
      }),
    ).toBe('http://localhost:8000');
  });

  it('preserves explicit ipv4 loopback in local browser development', () => {
    expect(
      resolveApiUrl('http://127.0.0.1:8000/', {
        currentHostname: 'localhost',
      }),
    ).toBe('http://127.0.0.1:8000');
  });

  it('does not rewrite api hosts in production mode', () => {
    expect(
      resolveApiUrl('http://127.0.0.1:8000/', {
        currentHostname: 'tenant.demo.local',
        isProduction: true,
      }),
    ).toBe('http://127.0.0.1:8000');
  });
});

describe('getAppApiUrl runtime configuration', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.mocked(useAppConfig).mockReturnValue({ apiURL: '', auth: {} });
  });

  function setPage(platformDomains?: string) {
    vi.stubEnv('PROD', true);
    vi.stubEnv('VITE_PLATFORM_DOMAINS', 'platform.example.test');
    vi.mocked(useAppConfig).mockReturnValue({
      apiURL: 'https://platform.example.test',
      auth: {},
    });
    vi.stubGlobal('window', {
      _VBEN_ADMIN_PRO_APP_CONF_: { VITE_PLATFORM_DOMAINS: platformDomains },
      location: {
        hash: '#/',
        hostname: 'tenant-a.platform.example.test',
        origin: 'https://tenant-a.platform.example.test',
        pathname: '/',
      },
    });
  }

  it('uses build platform domains when runtime has no override, including before hash navigation', () => {
    setPage();
    expect(getAppApiUrl()).toBe('https://tenant-a.platform.example.test');
    window.location.hash = '#/tenant/system/configs';
    expect(getAppApiUrl()).toBe('https://tenant-a.platform.example.test');
    window.location.hash = '#/user/login';
    expect(getAppApiUrl()).toBe('https://tenant-a.platform.example.test');
  });

  it('prefers runtime platform domains over build platform domains', () => {
    setPage('another-platform.example.com');
    expect(getAppApiUrl()).toBe('https://platform.example.test');
  });

  it('preserves an intentionally empty runtime platform domain override', () => {
    setPage('');
    expect(getAppApiUrl()).toBe('https://platform.example.test');
  });

  it('accepts a matching runtime platform domain list', () => {
    setPage('platform.example.test,other-platform.example.com');
    expect(getAppApiUrl()).toBe('https://tenant-a.platform.example.test');
  });
});
