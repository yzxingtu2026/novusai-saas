export function resolvePublicAppConfig(
  fileConfig: Record<string, string | undefined>,
  runtimeEnv: Record<string, string | undefined>,
): Record<string, string> {
  const publicConfig: Record<string, string> = {};
  for (const [key, value] of Object.entries({ ...fileConfig, ...runtimeEnv })) {
    // 中文: 仅公开运行配置进入浏览器，存储密钥等构建参数不在此名单。
    // EN: Emit only public runtime config; storage keys and other build parameters stay outside this list.
    if (
      (key.startsWith('VITE_GLOB_') || key === 'VITE_PLATFORM_DOMAINS') &&
      value !== undefined
    ) {
      publicConfig[key] = value;
    }
  }
  return publicConfig;
}
