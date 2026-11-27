// Test type: behavioral
import { describe, expect, it } from 'vitest';

import {
  getDomainSslPresentation,
  getManagedHttpsPresentation,
} from '../domain-presentation';

describe('shared domain SSL presentation', () => {
  it.each([
    ['active', 'success'],
    ['expired', 'error'],
    ['failed', 'error'],
    ['provisioning', 'processing'],
    ['pending', 'processing'],
    ['revoked', 'warning'],
    ['none', 'default'],
  ])('preserves %s as its own visible state', (status, color) => {
    expect(getDomainSslPresentation(status)).toMatchObject({
      key: status,
      color,
    });
  });

  it.each([
    ['active', 'success'],
    ['expiring', 'warning'],
    ['expired', 'error'],
    ['unconfigured', 'default'],
  ] as const)('shows actual managed HTTPS %s state', (status, color) => {
    expect(
      getManagedHttpsPresentation({
        configured: true,
        covered: true,
        status,
        expiresAt: null,
        autoRenew: false,
      }),
    ).toEqual({ key: status, color });
  });

  it('never treats a configured non-covering certificate as active', () => {
    expect(
      getManagedHttpsPresentation({
        configured: true,
        covered: false,
        status: 'unconfigured',
        expiresAt: '2027-01-10',
        autoRenew: false,
      }),
    ).toEqual({ key: 'uncovered', color: 'error' });
    expect(getManagedHttpsPresentation(null)).toEqual({
      key: 'unconfigured',
      color: 'default',
    });
  });
});
