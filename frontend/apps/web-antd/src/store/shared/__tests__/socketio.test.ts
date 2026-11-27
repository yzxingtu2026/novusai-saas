import type { Socket } from 'socket.io-client';

import { EventEmitter } from 'node:events';

import { createApp, defineComponent, h, nextTick, onMounted, ref } from 'vue';

import { createPinia, disposePinia } from 'pinia';
import { io } from 'socket.io-client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSocketIOStore } from '../socketio';
import { TokenStorage } from '../token-storage';

vi.mock('socket.io-client', () => ({ io: vi.fn() }));

// eslint-disable-next-line unicorn/prefer-event-target -- Socket.IO 客户端使用 on/off 事件接口。
class InertSocket extends EventEmitter {
  active = false;
  connected = false;

  readonly disconnect = vi.fn(() => {
    this.active = false;
    this.connected = false;
    this.emit('disconnect', 'io client disconnect');
  });

  // eslint-disable-next-line unicorn/prefer-event-target -- 重连事件使用同一套 on/off 接口。
  readonly io = new EventEmitter();

  connect() {
    this.active = true;
    this.connected = true;
    this.emit('connect');
  }

  loseTransport() {
    this.connected = false;
    this.emit('disconnect', 'transport close');
  }
}

const sockets: InertSocket[] = [];
const cleanups: Array<() => void> = [];

function socketAt(index: number) {
  const socket = sockets[index];
  if (!socket) throw new Error(`No socket created at index ${index}`);
  return socket;
}

function mountConsumer() {
  const pinia = createPinia();
  const showing = ref(true);
  const consumer = defineComponent({
    setup() {
      const store = useSocketIOStore();
      onMounted(() => store.connect('admin'));
      return () => h('span');
    },
  });
  const app = createApp({
    setup: () => () => (showing.value ? h(consumer) : null),
  });
  app.use(pinia);
  app.mount(document.createElement('div'));
  cleanups.push(() => {
    app.unmount();
    disposePinia(pinia);
  });

  return { pinia, showing, store: useSocketIOStore(pinia) };
}

beforeEach(() => {
  sockets.length = 0;
  TokenStorage.init('socket-store-lifecycle');
  TokenStorage.setToken('admin', 'fictional-admin-token');
  TokenStorage.setToken('tenant', 'fictional-tenant-token');
  vi.mocked(io).mockImplementation(() => {
    const socket = new InertSocket();
    sockets.push(socket);
    return socket as unknown as Socket;
  });
});

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  TokenStorage.clearAllTokens();
});

describe('shared Socket.IO store lifetime', () => {
  it('[behavioral] keeps the connection and status watcher across a consumer remount', async () => {
    const { showing, store } = mountConsumer();
    const handler = vi.fn();
    store.registerHandler('notification', handler);
    const socket = socketAt(0);

    expect([sockets.length, socket.connected, store.status]).toEqual([
      1,
      true,
      'connected',
    ]);

    showing.value = false;
    await nextTick();
    showing.value = true;
    await nextTick();

    expect([sockets.length, socket.connected, store.status]).toEqual([
      1,
      true,
      'connected',
    ]);
    socket.emit('notification', { id: 41 });
    expect(handler.mock.calls).toEqual([[{ id: 41 }]]);

    showing.value = false;
    await nextTick();
    socket.loseTransport();
    await nextTick();
    expect(store.status).toBe('disconnected');
    expect(store.isConnected).toBe(false);

    socket.io.emit('reconnect_attempt');
    await nextTick();
    expect(store.status).toBe('reconnecting');

    socket.connect();
    await nextTick();
    expect(store.status).toBe('connected');
  });

  it('[behavioral] releases switched and disconnected sockets and rebinds handlers once', () => {
    const { store } = mountConsumer();
    const handler = vi.fn();
    store.registerHandler('notification', handler);

    const admin = socketAt(0);
    store.connect('tenant');
    const tenant = socketAt(1);

    expect(admin.disconnect).toHaveBeenCalledTimes(1);
    expect(admin.eventNames()).toEqual([]);
    expect(admin.io.eventNames()).toEqual([]);
    expect([store.currentEndpoint, store.status]).toEqual([
      'tenant',
      'connected',
    ]);

    tenant.emit('notification', { id: 42 });
    store.disconnect();
    expect([store.currentEndpoint, store.status]).toEqual(['', 'disconnected']);
    expect(tenant.disconnect).toHaveBeenCalledTimes(1);
    expect(tenant.eventNames()).toEqual([]);
    expect(tenant.io.eventNames()).toEqual([]);

    tenant.emit('notification', { id: 43 });
    store.connect('tenant');
    socketAt(2).emit('notification', { id: 44 });

    expect(handler.mock.calls).toEqual([[{ id: 42 }], [{ id: 44 }]]);
  });

  it('[behavioral] preserves the verified room during token rotation and replaces it for another tenant', () => {
    const { store } = mountConsumer();
    const onPresence = vi.fn();
    store.registerHandler('presence:online', onPresence);
    store.connect('tenant');
    const firstTenant = socketAt(1);
    store.connect('tenant', {
      principal: 'tenant:17:user:42',
      token: 'fictional-tenant-token',
    });

    TokenStorage.setToken('tenant', 'rotated-tenant-token');
    store.connect('tenant', {
      principal: 'tenant:17:user:42',
      token: 'rotated-tenant-token',
    });
    firstTenant.emit('presence:online', { userId: 42 });

    expect(sockets).toHaveLength(2);
    expect(firstTenant.disconnect.mock.calls).toEqual([]);

    TokenStorage.setToken('tenant', 'another-tenant-token');
    store.connect('tenant', {
      principal: 'tenant:18:user:42',
      token: 'another-tenant-token',
    });
    firstTenant.emit('presence:online', { userId: 43 });
    socketAt(2).emit('presence:online', { userId: 44 });

    expect(firstTenant.disconnect).toHaveBeenCalledTimes(1);
    expect(onPresence.mock.calls).toEqual([[{ userId: 42 }], [{ userId: 44 }]]);
  });

  it('[behavioral] replaces the unverified socket when the verified token belongs to a later login', () => {
    const { store } = mountConsumer();
    const original = socketAt(0);
    TokenStorage.setToken('admin', 'another-admin-token');
    store.connect('admin', {
      principal: 'admin:user:41',
      token: 'fictional-admin-token',
    });
    expect(sockets).toHaveLength(1);

    store.connect('admin', {
      principal: 'admin:user:52',
      token: 'another-admin-token',
    });

    expect(sockets).toHaveLength(2);
    expect(original.disconnect).toHaveBeenCalledTimes(1);
    expect(socketAt(1).connected).toBe(true);
  });

  it('[behavioral] disposes the active transport and clears the retained Pinia state', () => {
    const { pinia, store } = mountConsumer();
    const handler = vi.fn();
    store.registerHandler('notification', handler);

    const socket = socketAt(0);
    store.$dispose();

    expect(socket.disconnect).toHaveBeenCalledTimes(1);
    expect(socket.eventNames()).toEqual([]);
    expect(socket.io.eventNames()).toEqual([]);

    const replacement = useSocketIOStore(pinia);
    expect([replacement.currentEndpoint, replacement.status]).toEqual([
      '',
      'disconnected',
    ]);
    socket.emit('notification', { id: 45 });
    replacement.connect('admin');
    socketAt(1).emit('notification', { id: 46 });

    expect([replacement.currentEndpoint, replacement.status]).toEqual([
      'admin',
      'connected',
    ]);
    expect(handler.mock.calls).toEqual([]);
  });
});
