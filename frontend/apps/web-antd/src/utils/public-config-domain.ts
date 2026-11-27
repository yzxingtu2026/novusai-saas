export function normalizeDomainHostname(value: string): string {
  const hostname = value.trim().toLowerCase().replace(/\.$/, '');
  if (hostname.includes(':')) {
    try {
      const address = hostname.startsWith('[') ? hostname : `[${hostname}]`;
      return new URL(`http://${address}`).hostname;
    } catch {
      return '';
    }
  }
  return hostname.length <= 253 &&
    hostname
      .split('.')
      .every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
    ? hostname
    : '';
}

export function isCustomDomain(value: string): boolean {
  const hostname = normalizeDomainHostname(value);
  return Boolean(
    hostname && hostname.includes('.') && !/^[\d.]+$/.test(hostname),
  );
}

export function isUnknownTenantDomainError(error: unknown): boolean {
  const candidate = error as null | {
    code?: number;
    response?: { data?: { code?: number }; status?: number };
  };
  return (
    candidate?.code === 4040 ||
    candidate?.response?.data?.code === 4040 ||
    candidate?.response?.status === 404
  );
}

export function isDomainEntryUnavailable(
  isDomainDetected: boolean,
  isDomainTenantDomain: boolean | null,
): boolean {
  return !isDomainDetected || isDomainTenantDomain === null;
}

export function shouldRequestTenantPublicConfig(
  isDomainDetected: boolean,
  isDomainTenantDomain: boolean | null,
): boolean {
  return isDomainDetected && isDomainTenantDomain === true;
}
