/**
 * Global Socket.IO connection management store / 全局 Socket.IO 连接管理 Store
 *
 * Automatically connects after login, disconnects on logout.
 * Provides event handler registration/unregistration and dispatches events to registered handlers.
 * 登录后自动建立连接，登出时断开。提供事件 handler 注册/注销机制。
 */

import type { EffectScope } from 'vue';

import type { SocketIOStatus } from '#/composables/use-socketio';

import {
  computed,
  effectScope,
  getCurrentScope,
  onScopeDispose,
  ref,
  watch,
} from 'vue';

import { defineStore } from 'pinia';

import { useSocketIO } from '#/composables/use-socketio';
import { getEndpointFromPath } from '#/utils';
import { getAppApiUrl } from '#/utils/api-url';

import { TokenStorage } from './token-storage';

/** Namespace 映射：API endpoint → Socket.IO namespace / Namespace map */
const NAMESPACE_MAP: Record<string, string> = {
  admin: '/admin',
  tenant: '/tenant',
  user: '/user',
};

interface VerifiedSocketSession {
  principal: string;
  token: string;
}

export const useSocketIOStore = defineStore('socketio', () => {
  // 中文: 连接和状态监听由 Pinia 管理，生命周期独立于使用它的布局组件。
  // EN: Pinia owns the connection and status watcher across layout unmounts.
  const storeScope = getCurrentScope();

  // ============================================================
  // State / 状态
  // ============================================================

  /** Current connection status / 当前连接状态 */
  const status = ref<SocketIOStatus>('disconnected');

  /** Currently connected endpoint / 当前连接的 endpoint */
  const currentEndpoint = ref<string>('');

  /** Registered event handlers / 已注册的事件 handlers */
  const handlers = new Map<string, Set<(data: unknown) => void>>();

  /** Socket.IO composable instance (lazy-created) / Socket.IO composable 实例 */
  let sioInstance: null | ReturnType<typeof useSocketIO> = null;
  let connectionScope: EffectScope | null = null;
  let connectionToken: null | string = null;
  let connectionPrincipal: null | string = null;

  // ============================================================
  // Connection management / 连接管理
  // ============================================================

  /** Whether connected / 是否已连接 */
  const isConnected = computed(() => status.value === 'connected');

  /**
   * Establish connection / 建立连接
   *
   * @param endpoint - API endpoint type ('admin' | 'tenant' | 'user') / API 端类型
   */
  function connect(endpoint?: string, verifiedSession?: VerifiedSocketSession) {
    if (!storeScope?.active) return;

    const ep = endpoint || _detectEndpoint();
    if (!ep) return;

    const token = TokenStorage.getToken(ep as 'admin' | 'tenant' | 'user');
    if (!token || (verifiedSession && verifiedSession.token !== token)) return;

    const ns = NAMESPACE_MAP[ep];
    if (!ns) return;

    // 同一账号和租户的令牌轮换沿用连接，保留其他订阅。
    const samePrincipal =
      !verifiedSession ||
      (connectionPrincipal === null
        ? connectionToken === token
        : connectionPrincipal === verifiedSession.principal);
    if (
      sioInstance &&
      currentEndpoint.value === ep &&
      samePrincipal &&
      (verifiedSession || status.value === 'connected')
    ) {
      if (verifiedSession) connectionPrincipal = verifiedSession.principal;
      return;
    }

    releaseConnection();
    const scope = storeScope.run(() => effectScope());
    if (!scope) return;
    connectionScope = scope;
    connectionToken = token;
    connectionPrincipal = verifiedSession?.principal ?? null;

    currentEndpoint.value = ep;

    // Create new connection (use backend API URL; dev env can't use window.location.origin) / 创建新连接
    // Use getter function to read TokenStorage in real-time, ensuring Socket.IO gets the latest token after refresh / 实时读取 Token
    const tokenGetter = () =>
      TokenStorage.getToken(ep as 'admin' | 'tenant' | 'user') || '';
    scope.run(() => {
      sioInstance = useSocketIO({
        namespace: ns,
        token: tokenGetter,
        serverUrl: getAppApiUrl(),
        autoConnect: true,
      });
      watch(
        sioInstance.status,
        (newStatus) => {
          status.value = newStatus;
        },
        { immediate: true },
      );
    });

    // Bind all registered handlers / 绑定所有已注册的 handlers
    _bindHandlers();
  }

  function releaseConnection() {
    const scope = connectionScope;
    connectionScope = null;
    sioInstance = null;
    connectionToken = null;
    connectionPrincipal = null;
    scope?.stop();
  }

  /**
   * Disconnect / 断开连接
   *
   * Note: Does not clear handlers map; keeps registered handlers so that
   * _bindHandlers() can auto-rebind on reconnect. Call $reset() on logout to clear handlers.
   * 注意：不清 handlers map，保留已注册 handler 以便重连。登出时需调用 $reset()。
   */
  function disconnect() {
    releaseConnection();
    currentEndpoint.value = '';
    status.value = 'disconnected';
  }

  // ============================================================
  // Event dispatch / 事件分发
  // ============================================================

  /**
   * Register event handler / 注册事件 handler
   *
   * @param event - Event name (e.g. 'notification', 'presence:online') / 事件名
   * @param handler - Callback function / 回调函数
   */
  function registerHandler(event: string, handler: (data: unknown) => void) {
    if (!handlers.has(event)) {
      handlers.set(event, new Set());
    }
    const eventHandlers = handlers.get(event);
    if (eventHandlers) {
      eventHandlers.add(handler);
    }

    // If already connected, bind immediately / 如果已有连接，立即绑定
    if (sioInstance) {
      sioInstance.on(event, handler);
    }
  }

  /**
   * Unregister event handler / 注销事件 handler
   *
   * @param event - Event name / 事件名
   * @param handler - Callback to remove (omit to remove all handlers for this event) / 要移除的回调
   */
  function unregisterHandler(event: string, handler?: (data: unknown) => void) {
    const eventHandlers = handlers.get(event);
    if (!eventHandlers) return;

    if (handler) {
      eventHandlers.delete(handler);
      if (sioInstance) {
        sioInstance.off(event, handler as (...args: unknown[]) => void);
      }
      if (eventHandlers.size === 0) {
        handlers.delete(event);
      }
    } else {
      // Remove all handlers for this event / 移除该事件所有 handlers
      if (sioInstance) {
        sioInstance.off(event);
      }
      handlers.delete(event);
    }
  }

  /**
   * Emit event / 发送事件
   */
  function emit(event: string, data?: unknown) {
    sioInstance?.emit(event, data);
  }

  // ============================================================
  // Internal methods / 内部方法
  // ============================================================

  /** Bind all registered handlers to current socket (off first to prevent duplicates) / 绑定已注册 handlers */
  function _bindHandlers() {
    if (!sioInstance) return;
    for (const [event, eventHandlers] of handlers) {
      for (const handler of eventHandlers) {
        sioInstance.off(event, handler as (...args: unknown[]) => void);
        sioInstance.on(event, handler);
      }
    }
  }

  /** Detect current endpoint type / 检测当前端类型 */
  function _detectEndpoint(): string {
    try {
      const path = window.location.hash
        ? window.location.hash.slice(1)
        : window.location.pathname;
      return getEndpointFromPath(path);
    } catch {
      return 'admin';
    }
  }

  /**
   * Full reset (called on logout) / 完全重置（登出时调用）
   * Disconnects + clears handlers map to prevent duplicate registrations on re-login.
   */
  function $reset() {
    handlers.clear();
    disconnect();
  }

  onScopeDispose($reset);

  return {
    status,
    currentEndpoint,
    isConnected,
    connect,
    disconnect,
    registerHandler,
    unregisterHandler,
    emit,
    $reset,
  };
});
