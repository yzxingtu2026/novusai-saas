/**
 * Socket.IO connection composable
 * Socket.IO 连接 Composable
 *
 * Encapsulates socket.io-client connection management with reactive status tracking,
 * token refresh reconnection, and event listen/emit capabilities.
 * 封装 socket.io-client 的连接管理，提供响应式状态跟踪、Token 刷新重连、事件监听/发送等能力。
 */

import type { Socket } from 'socket.io-client';

import type { Ref, ShallowRef } from 'vue';

import {
  getCurrentScope,
  onScopeDispose,
  ref,
  shallowRef,
  toValue,
  watch,
} from 'vue';

import { io } from 'socket.io-client';

import { isDevErrorMode } from '#/utils/request';
import { ensureTraceIdHeader } from '#/utils/request/trace';

const SOCKET_TRACE_HEADERS: Record<string, unknown> = {};

export function getSocketTraceId(): string {
  return ensureTraceIdHeader(SOCKET_TRACE_HEADERS);
}

function isSocketPayloadObject(data: unknown): data is Record<string, unknown> {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(data);
  return prototype === null || prototype === Object.prototype;
}

export function withSocketTracePayload<T>(data: T): T {
  if (!isSocketPayloadObject(data)) {
    return data;
  }

  const traceId =
    typeof data.trace_id === 'string' && data.trace_id.trim()
      ? data.trace_id.trim()
      : getSocketTraceId();

  return traceId ? ({ ...data, trace_id: traceId } as T) : data;
}

interface SocketConnectErrorData {
  debug?: { detail?: string; message?: string };
  message?: string;
  reason?: string;
  trace_id?: string;
}

function getSocketConnectErrorInfo(
  err: Error & { data?: SocketConnectErrorData },
) {
  const reason = err.data?.reason || err.message;
  const traceId = err.data?.trace_id || '';
  const debugMessage =
    err.data?.debug?.detail || err.data?.debug?.message || '';
  return { debugMessage, reason, traceId };
}

/** 连接状态 / Connection status */
export type SocketIOStatus =
  | 'connected'
  | 'connecting'
  | 'disconnected'
  | 'reconnecting';

/** Composable options / Composable 选项 */
export interface UseSocketIOOptions {
  /** Socket.IO namespace, e.g. '/admin', '/tenant', '/user' / Socket.IO 命名空间 */
  namespace: string;
  /** JWT access token (reactive Ref, static string, or getter function) / JWT 访问令牌 */
  token: (() => string) | Ref<string> | string;
  /** Server URL, defaults to current origin / 服务器 URL */
  serverUrl?: string;
  /** Socket.IO path, default '/sio' / Socket.IO 路径 */
  path?: string;
  /** Whether to auto-connect, default false / 是否自动连接 */
  autoConnect?: boolean;
}

/** Composable return value / Composable 返回值 */
export interface UseSocketIOReturn {
  /** Socket.IO client instance / Socket.IO 客户端实例 */
  socket: ShallowRef<null | Socket>;
  /** Connection status / 连接状态 */
  status: Ref<SocketIOStatus>;
  /** Manual connect / 手动连接 */
  connect: () => void;
  /** Manual disconnect / 手动断开 */
  disconnect: () => void;
  /** Listen to event / 监听事件 */
  on: <T = unknown>(event: string, handler: (data: T) => void) => void;
  /** Remove event listener / 取消监听 */
  off: (event: string, handler?: (...args: unknown[]) => void) => void;
  /** Emit event / 发送事件 */
  emit: (event: string, data?: unknown) => void;
}

/**
 * Socket.IO connection composable / Socket.IO 连接 composable
 *
 * @example
 * ```ts
 * const { socket, status, connect, on } = useSocketIO({
 *   namespace: '/admin',
 *   token: accessTokenRef,
 * });
 * connect();
 * on('notification', (data) => { ... });
 * ```
 */
export function useSocketIO(options: UseSocketIOOptions): UseSocketIOReturn {
  const {
    namespace,
    token,
    serverUrl,
    path = '/sio',
    autoConnect = false,
  } = options;

  const socket: ShallowRef<null | Socket> = shallowRef(null);
  const status = ref<SocketIOStatus>('disconnected');

  let isManualDisconnect = false;
  let socketCleanup: (() => void) | null = null;

  /** Get current token value (supports Ref, getter function, static string) / 获取当前 token 值 */
  function getToken(): string {
    if (typeof token === 'function') {
      return token();
    }
    return toValue(token);
  }

  function createAuthPayload(currentToken: string): Record<string, string> {
    const traceId = getSocketTraceId();
    const authPayload: Record<string, string> = {
      token: currentToken ? `Bearer ${currentToken}` : '',
    };
    if (traceId) {
      authPayload.trace_id = traceId;
    }
    return authPayload;
  }

  function isCurrentSocket(sock: Socket): boolean {
    return socket.value === sock && !isManualDisconnect;
  }

  /** Create Socket.IO connection / 创建 Socket.IO 连接 */
  function createSocket(): { cleanup: () => void; socket: Socket } {
    const url = serverUrl || window.location.origin;
    let lastHandshakeToken = getToken();
    let lastRetriedToken = '';

    const sock = io(`${url}${namespace}`, {
      path,
      auth: (callback) => {
        lastHandshakeToken = getToken();
        callback(createAuthPayload(lastHandshakeToken));
      },
      autoConnect: false,
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      reconnectionAttempts: Number.POSITIVE_INFINITY,
      transports: ['websocket'],
    });

    // Connection status events / 连接状态事件
    sock.on('connect', () => {
      if (!isCurrentSocket(sock)) return;
      status.value = 'connected';
    });

    sock.on('disconnect', () => {
      if (isCurrentSocket(sock)) {
        status.value = 'disconnected';
      }
    });

    const onReconnectAttempt = () => {
      if (isCurrentSocket(sock) && sock.active) {
        status.value = 'reconnecting';
      }
    };

    const onReconnect = () => {
      if (isCurrentSocket(sock) && sock.active && !sock.connected) {
        // 中文: Manager 只恢复底层传输；命名空间仍需完成认证握手。
        // EN: The manager restored transport; the namespace still needs to authenticate.
        status.value = 'connecting';
      }
    };

    const onReconnectFailed = () => {
      if (isCurrentSocket(sock) && sock.active) {
        status.value = 'disconnected';
        console.error('[Socket.IO] Reconnection failed');
      }
    };

    sock.io.on('reconnect_attempt', onReconnectAttempt);
    sock.io.on('reconnect', onReconnect);
    sock.io.on('reconnect_failed', onReconnectFailed);

    // Token expired / auth failure handling / Token 过期认证失败处理
    sock.on(
      'connect_error',
      (err: Error & { data?: SocketConnectErrorData }) => {
        if (!isCurrentSocket(sock)) return;

        const { debugMessage, reason, traceId } =
          getSocketConnectErrorInfo(err);
        const traceSuffix = traceId ? ` (trace_id=${traceId})` : '';
        status.value = 'disconnected';

        if (reason === 'token_expired' || reason === 'authentication_failed') {
          const latestToken = getToken();

          if (
            latestToken &&
            latestToken !== lastHandshakeToken &&
            latestToken !== lastRetriedToken
          ) {
            // 中文: 仅在令牌确实更新时重试一次命名空间握手。
            // EN: Retry the namespace handshake once only when the token changed.
            lastRetriedToken = latestToken;
            lastHandshakeToken = latestToken;
            status.value = 'connecting';
            sock.connect();
            console.warn(
              `[Socket.IO] Token refreshed, reconnecting with new token${traceSuffix}`,
            );
          } else {
            console.warn(
              `[Socket.IO] Auth error: ${reason}; waiting for a new token or explicit retry${traceSuffix}`,
            );
          }
        } else {
          const debugSuffix =
            isDevErrorMode() && debugMessage ? ` | debug=${debugMessage}` : '';
          console.warn(
            `[Socket.IO] Connect error: ${reason}${traceSuffix}${debugSuffix}`,
          );
        }
      },
    );

    return {
      socket: sock,
      cleanup: () => {
        sock.io.off('reconnect_attempt', onReconnectAttempt);
        sock.io.off('reconnect', onReconnect);
        sock.io.off('reconnect_failed', onReconnectFailed);
        sock.removeAllListeners();
      },
    };
  }

  /** Connect / 连接 */
  function connect() {
    isManualDisconnect = false;

    const currentToken = getToken();
    if (!currentToken) {
      return;
    }

    if (socket.value) {
      if (socket.value.connected) {
        return;
      }

      status.value = 'connecting';
      socket.value.connect();
      return;
    }

    status.value = 'connecting';
    const { cleanup, socket: sock } = createSocket();
    socket.value = sock;
    socketCleanup = cleanup;
    sock.connect();
  }

  /** Disconnect / 断开连接 */
  function disconnect() {
    isManualDisconnect = true;
    if (socket.value) {
      const sock = socket.value;
      socketCleanup?.();
      socketCleanup = null;
      sock.disconnect();
      socket.value = null;
    }
    status.value = 'disconnected';
  }

  /** Listen to event / 监听事件 */
  function on<T = unknown>(event: string, handler: (data: T) => void) {
    socket.value?.on(event, handler as (...args: unknown[]) => void);
  }

  /** Remove event listener / 取消监听 */
  function off(event: string, handler?: (...args: unknown[]) => void) {
    if (handler) {
      socket.value?.off(event, handler);
    } else {
      socket.value?.off(event);
    }
  }

  /** Emit event / 发送事件 */
  function emit(event: string, data?: unknown) {
    if (socket.value?.connected) {
      socket.value.emit(event, withSocketTracePayload(data));
    }
  }

  // Disconnect and reconnect on token change (only for Ref type, getter functions don't need watch) / Token 变化时断开重连
  if (typeof token !== 'string' && typeof token !== 'function') {
    watch(token, (newToken, oldToken) => {
      if (newToken === oldToken) return;

      if (!newToken) {
        // Token cleared → disconnect / Token 清空 → 断开
        disconnect();
        return;
      }

      if (socket.value) {
        // 中文: 每次命名空间握手都通过 auth 回调读取最新令牌。
        // EN: The auth callback reads the latest token for every namespace handshake.
        if (socket.value.connected) {
          socket.value.disconnect();
        }
        status.value = 'connecting';
        socket.value.connect();
      } else if (oldToken === '') {
        // First token received → connect / 首次获得 token → 连接
        connect();
      }
    });
  }

  // Auto-connect / 自动连接
  if (autoConnect && getToken()) {
    connect();
  }

  if (getCurrentScope()) {
    onScopeDispose(disconnect);
  }

  return {
    socket,
    status,
    connect,
    disconnect,
    on,
    off,
    emit,
  };
}
