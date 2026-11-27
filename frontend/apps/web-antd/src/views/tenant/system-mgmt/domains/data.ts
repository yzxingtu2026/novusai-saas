import type {
  DomainType,
  SslStatus,
  VerificationStatus,
} from './modules/domains-types';

import { $t } from '#/locales';
import { getDomainSslPresentation } from '#/utils/domain-presentation';

/**
 * 获取验证状态配置 (Tag 颜色与文本)
 */
export function getVerificationStatusConfig(status: VerificationStatus) {
  switch (status) {
    case 'failed': {
      return {
        color: 'error',
        text: $t('tenant.system.domain.verification.failed'),
      };
    }
    case 'verified': {
      return {
        color: 'success',
        text: $t('tenant.system.domain.verification.verified'),
      };
    }
    default: {
      return {
        color: 'warning',
        text: $t('tenant.system.domain.verification.pending'),
      };
    }
  }
}

/**
 * 获取 SSL 状态配置 (Badge 状态与文本)
 */
export function getSslStatusConfig(status: SslStatus) {
  const presentation = getDomainSslPresentation(status);
  return {
    ...presentation,
    text: $t(`tenant.system.domain.ssl.${presentation.key}`),
  };
}

/**
 * 获取域名类型配置 (Tag 颜色与文本)
 */
export function getDomainTypeConfig(type: DomainType) {
  if (type === 'default') {
    return {
      color: 'default', // 或 secondary/muted
      text: $t('tenant.system.domain.type.default'),
    };
  }
  return {
    color: 'blue',
    text: $t('tenant.system.domain.type.custom'),
  };
}
