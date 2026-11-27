import type { SupportedLanguagesType } from '@vben/locales';

import { createApp, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import { $t, setupI18n } from '@vben/locales';

import { afterEach, describe, expect, it } from 'vitest';

import AuthenticationLogin from './login.vue';

const cleanups: Array<() => void> = [];

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
});

async function mountLogin(
  locale: SupportedLanguagesType,
  submitButtonText = '',
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ component: { render: () => null }, path: '/' }],
  });
  await router.push('/');

  const app = createApp({
    render: () =>
      h(AuthenticationLogin, {
        formSchema: [
          {
            component: 'VbenInput',
            fieldName: 'username',
            label: $t('authentication.username'),
          },
          {
            component: 'VbenInputPassword',
            fieldName: 'password',
            label: $t('authentication.password'),
          },
        ],
        showCodeLogin: false,
        showForgetPassword: false,
        showQrcodeLogin: false,
        showRegister: false,
        showRememberMe: false,
        showThirdPartyLogin: false,
        submitButtonText,
      }),
  });
  app.use(router);
  await setupI18n(app, { defaultLocale: locale });

  const root = document.createElement('div');
  app.mount(root);
  cleanups.push(() => app.unmount());
  await nextTick();
  return root;
}

describe('shared login accessibility', () => {
  it('[behavioral] names the native inputs even when their labels are hidden', async () => {
    const root = await mountLogin('zh-CN');

    expect(root.querySelector('label')).toBeNull();
    expect(
      [...root.querySelectorAll('input')].map((input) => [
        input.type,
        input.getAttribute('aria-label'),
      ]),
    ).toEqual([
      ['text', '账号'],
      ['password', '密码'],
    ]);
  });

  it.each([
    ['en-US', 'Login'],
    ['zh-CN', '登录'],
  ] as const)(
    '[behavioral] uses the visible submit caption as its name in %s',
    async (locale, text) => {
      const root = await mountLogin(locale);
      const submit = root.querySelector('button');

      expect(submit?.textContent?.trim()).toBe(text);
      expect(submit?.getAttribute('aria-label')).toBe(text);
    },
  );

  it('[behavioral] also uses an explicit submit caption as its name', async () => {
    const root = await mountLogin('en-US', 'Sign in to your account');
    const submit = root.querySelector('button');

    expect(submit?.textContent?.trim()).toBe('Sign in to your account');
    expect(submit?.getAttribute('aria-label')).toBe('Sign in to your account');
  });
});
