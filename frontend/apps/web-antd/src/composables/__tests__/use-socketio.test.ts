// @vitest-environment happy-dom
import type { Socket } from 'socket.io-client';

import { nextTick, ref } from 'vue';

import { io } from 'socket.io-client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSocketIO, withSocketTracePayload } from '../use-socketio';

vi.mock('socket.io-client', () => ({
  io: vi.fn(),
}));

type FakeListener = (...args: unknown[]) => void;

class FakeEmitter {
  private readonly listeners = new Map<string, Set<FakeListener>>();

  emit(event: string, ...args: unknown[]) {
    for (const listener of this.listeners.get(event) ?? []) {
      listener(...args);
    }
    return this;
  }

  listenerCount(event: string) {
    return this.listeners.get(event)?.size ?? 0;
  }

  off(event: string, listener?: FakeListener) {
    if (!listener) {
      this.listeners.delete(event);
      return this;
    }
    this.listeners.get(event)?.delete(listener);
    return this;
  }

  on(event: string, listener: FakeListener) {
    const listeners = this.listeners.get(event) ?? new Set<FakeListener>();
    listeners.add(listener);
    this.listeners.set(event, listeners);
    return this;
  }

  removeAllListeners() {
    this.listeners.clear();
  }
}

class FakeSocket extends FakeEmitter {
  active = false;
  auth: Socket['auth'];
  readonly connect = vi.fn(() => {
    this.active = true;
    if (typeof this.auth === 'function') {
      this.auth((data) =>
        this.handshakes.push(data as Record<string, unknown>),
      );
    } else if (this.auth) {
      this.handshakes.push(this.auth);
    }
  });
  connected = false;
  readonly disconnect = vi.fn(() => {
    this.connected = false;
    this.active = false;
    this.emit('disconnect', 'io client disconnect');
  });
  readonly handshakes: Record<string, unknown>[] = [];
  readonly io = new FakeEmitter();

  constructor(auth: Socket['auth']) {
    super();
    this.auth = auth;
  }

  setConnected() {
    this.connected = true;
    this.active = true;
    this.emit('connect');
  }
}

const sockets: FakeSocket[] = [];

function latestSocket(): FakeSocket {
  const socket = sockets.at(-1);
  if (!socket) {
    throw new Error('Socket fixture was not created');
  }
  return socket;
}

beforeEach(() => {
  sockets.length = 0;
  vi.mocked(io).mockImplementation((_url, options) => {
    const socket = new FakeSocket(options?.auth ?? {});
    sockets.push(socket);
    return socket as unknown as Socket;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('withSocketTracePayload', () => {
  it('injects trace_id into plain object payloads', () => {
    const payload = withSocketTracePayload<{
      action: string;
      trace_id?: string;
    }>({ action: 'ping' });

    expect(payload).toMatchObject({
      action: 'ping',
      trace_id: expect.any(String),
    });
    expect(payload.trace_id).toHaveLength(36);
  });

  it('preserves explicit trace_id on payload', () => {
    const payload = withSocketTracePayload<{
      action: string;
      trace_id?: string;
    }>({
      action: 'ping',
      trace_id: 'trace-explicit',
    });

    expect(payload).toEqual({
      action: 'ping',
      trace_id: 'trace-explicit',
    });
  });

  it('leaves non-object payloads unchanged', () => {
    expect(withSocketTracePayload('ping')).toBe('ping');
    expect(withSocketTracePayload(123)).toBe(123);
    expect(withSocketTracePayload(['ping'])).toEqual(['ping']);
  });
});

describe('useSocketIO recovery', () => {
  it('reconnects a Ref token change with the new handshake identity', async () => {
    const token = ref('token-old');
    useSocketIO({
      namespace: '/tenant',
      token,
      autoConnect: true,
    });
    const socket = latestSocket();
    socket.setConnected();

    token.value = 'token-new';
    await nextTick();

    expect(
      socket.handshakes.map(({ token: handshakeToken }) => handshakeToken),
    ).toEqual(['Bearer token-old', 'Bearer token-new']);
    expect(socket.disconnect).toHaveBeenCalledTimes(1);
  });

  it('uses the current getter token for every namespace handshake', () => {
    let currentToken = 'token-old';
    const connection = useSocketIO({
      namespace: '/tenant',
      token: () => currentToken,
      autoConnect: true,
    });
    const socket = latestSocket();

    expect(socket.handshakes.map(({ token }) => token)).toEqual([
      'Bearer token-old',
    ]);

    currentToken = 'token-new';
    connection.connect();

    expect(socket.handshakes.map(({ token }) => token)).toEqual([
      'Bearer token-old',
      'Bearer token-new',
    ]);
  });

  it('retries one auth rejection only after the handshake token changes', () => {
    let currentToken = 'token-old';
    const connection = useSocketIO({
      namespace: '/tenant',
      token: () => currentToken,
      autoConnect: true,
    });
    const socket = latestSocket();

    currentToken = 'token-new';
    socket.emit(
      'connect_error',
      Object.assign(new Error('unauthorized'), {
        data: { reason: 'token_expired' },
      }),
    );

    expect(socket.handshakes.map(({ token }) => token)).toEqual([
      'Bearer token-old',
      'Bearer token-new',
    ]);
    expect(socket.connect).toHaveBeenCalledTimes(2);
    expect(connection.status.value).toBe('connecting');

    socket.emit(
      'connect_error',
      Object.assign(new Error('unauthorized'), {
        data: { reason: 'authentication_failed' },
      }),
    );

    expect(socket.connect).toHaveBeenCalledTimes(2);
    expect(connection.status.value).toBe('disconnected');
  });

  it('does not retry an auth rejection with the same token', () => {
    const connection = useSocketIO({
      namespace: '/tenant',
      token: 'token-same',
      autoConnect: true,
    });
    const socket = latestSocket();

    socket.emit(
      'connect_error',
      Object.assign(new Error('unauthorized'), {
        data: { reason: 'authentication_failed' },
      }),
    );

    expect(socket.handshakes.map(({ token }) => token)).toEqual([
      'Bearer token-same',
    ]);
    expect(socket.connect).toHaveBeenCalledTimes(1);
    expect(connection.status.value).toBe('disconnected');
  });

  it('waits for namespace connect before reporting connected after manager recovery', () => {
    const connection = useSocketIO({
      namespace: '/tenant',
      token: ref('token-valid'),
      autoConnect: true,
    });
    const socket = latestSocket();

    socket.io.emit('reconnect_attempt');
    expect(connection.status.value).toBe('reconnecting');

    socket.io.emit('reconnect');
    expect(connection.status.value).toBe('connecting');

    socket.setConnected();
    expect(connection.status.value).toBe('connected');
  });

  it('does not automatically reconnect after a server namespace disconnect', () => {
    const connection = useSocketIO({
      namespace: '/tenant',
      token: ref('token-valid'),
      autoConnect: true,
    });
    const socket = latestSocket();
    socket.setConnected();

    socket.emit('disconnect', 'io server disconnect');

    expect(connection.status.value).toBe('disconnected');
    expect(socket.connect).toHaveBeenCalledTimes(1);
  });

  it('ignores manager recovery events after this namespace was deactivated', () => {
    const connection = useSocketIO({
      namespace: '/tenant',
      token: ref('token-valid'),
      autoConnect: true,
    });
    const socket = latestSocket();
    socket.active = false;
    socket.emit('connect_error', new Error('fixture_namespace_rejected'));

    socket.io.emit('reconnect_attempt');
    socket.io.emit('reconnect');
    socket.io.emit('reconnect_failed');

    expect(connection.status.value).toBe('disconnected');
    expect(socket.connect).toHaveBeenCalledTimes(1);
  });

  it('removes manager listeners and ignores late events from a replaced socket', () => {
    const connection = useSocketIO({
      namespace: '/tenant',
      token: ref('token-valid'),
      autoConnect: true,
    });
    const staleSocket = latestSocket();
    connection.disconnect();
    connection.connect();

    expect(staleSocket.io.listenerCount('reconnect_attempt')).toBe(0);
    expect(staleSocket.io.listenerCount('reconnect')).toBe(0);
    expect(staleSocket.io.listenerCount('reconnect_failed')).toBe(0);

    staleSocket.io.emit('reconnect_failed');
    staleSocket.emit('connect');

    expect(connection.status.value).toBe('connecting');
  });
});
