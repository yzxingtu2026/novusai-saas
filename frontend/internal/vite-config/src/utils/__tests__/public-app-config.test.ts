// Test type: behavioral
import { describe, expect, it } from 'vitest';

import { resolvePublicAppConfig } from '../public-app-config';

describe('public production app config', () => {
  it('uses deployment environment over file placeholders for API and platform hosts', () => {
    expect(
      resolvePublicAppConfig(
        {
          VITE_GLOB_API_URL: 'https://api.novusai.example.com',
          VITE_PLATFORM_DOMAINS: 'localhost',
        },
        {
          VITE_GLOB_API_URL: 'https://platform.example.test',
          VITE_PLATFORM_DOMAINS: 'platform.example.test',
          VITE_APP_STORE_SECURE_KEY: 'private-fixture-key',
        },
      ),
    ).toEqual({
      VITE_GLOB_API_URL: 'https://platform.example.test',
      VITE_PLATFORM_DOMAINS: 'platform.example.test',
    });
  });
  it('keeps file fallback and excludes non-public settings from either source', () => {
    expect(
      resolvePublicAppConfig(
        {
          VITE_GLOB_API_URL: '/api',
          VITE_APP_STORE_SECURE_KEY: 'file-fixture-key',
          OTHER_TOKEN: 'private',
        },
        {},
      ),
    ).toEqual({ VITE_GLOB_API_URL: '/api' });
  });
});
