import type {
  ManagedHttpsInfo,
  SslCertificateInfo,
  SslCertStatus,
  SslStatus,
} from '#/types/domain';

/** The detail API selects the current record; renewal manages active/expired platform certificates. */
export function supportsCertificateRenewal(
  cert?: null | Pick<SslCertificateInfo, 'certType' | 'status'>,
): boolean {
  return (
    cert?.certType === 'platform' &&
    (cert.status === 'active' || cert.status === 'expired')
  );
}

const SSL_PRESENTATION = {
  active: { color: 'success', icon: 'lucide:shield-check', status: 'success' },
  expired: { color: 'error', icon: 'lucide:shield-off', status: 'error' },
  failed: { color: 'error', icon: 'lucide:shield-x', status: 'error' },
  none: { color: 'default', icon: 'lucide:shield-x', status: 'default' },
  pending: { color: 'processing', icon: 'lucide:shield', status: 'processing' },
  provisioning: {
    color: 'processing',
    icon: 'lucide:shield',
    status: 'processing',
  },
  revoked: { color: 'warning', icon: 'lucide:shield-off', status: 'warning' },
} as const;

export function getDomainSslPresentation(
  status: SslCertStatus | SslStatus | string | undefined,
) {
  const key =
    status && Object.hasOwn(SSL_PRESENTATION, status)
      ? (status as keyof typeof SSL_PRESENTATION)
      : 'none';
  return { ...SSL_PRESENTATION[key], key };
}

export function getManagedHttpsPresentation(info?: ManagedHttpsInfo | null) {
  if (!info?.configured) {
    return { color: 'default', key: 'unconfigured' } as const;
  }
  if (!info.covered) return { color: 'error', key: 'uncovered' } as const;
  if (info.status === 'unconfigured')
    return { color: 'default', key: 'unconfigured' } as const;
  if (info.status === 'expired')
    return { color: 'error', key: 'expired' } as const;
  if (info.status === 'expiring')
    return { color: 'warning', key: 'expiring' } as const;
  return { color: 'success', key: 'active' } as const;
}
