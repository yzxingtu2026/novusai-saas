// @vitest-environment happy-dom
// Test type: behavioral. Run the store and toast with captured transport calls.
import type { NotificationItem } from '../notification';

import { useAccessStore, useUserStore } from '@vben/stores';

import { createPinia, disposePinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useNotificationToast } from '#/composables/use-notification-toast';

import { useNotificationStore } from '../notification';
import { TokenStorage } from '../token-storage';

const {
  handlers,
  requestCount,
  requestDelete,
  requestGet,
  requestMe,
  requestPut,
  socket,
} = vi.hoisted(() => ({
  handlers: new Map<string, (data: unknown) => void>(),
  requestCount: vi.fn(),
  requestDelete: vi.fn(),
  requestGet: vi.fn(),
  requestMe: vi.fn(),
  requestPut: vi.fn(),
  socket: { currentEndpoint: '' },
}));

vi.mock('#/utils/request', () => ({
  baseRequestClient: { instance: { get: requestMe } },
  requestClient: {
    delete: requestDelete,
    get: (url: string) =>
      url.endsWith('/unread-count') ? requestCount(url) : requestGet(url),
    put: requestPut,
  },
}));

vi.mock('#/store/shared/socketio', () => ({
  useSocketIOStore: () => ({
    get currentEndpoint() {
      return socket.currentEndpoint;
    },
    connect(endpoint: string) {
      socket.currentEndpoint = endpoint;
    },
    disconnect() {
      socket.currentEndpoint = '';
    },
    registerHandler(event: string, handler: (data: unknown) => void) {
      handlers.set(event, handler);
    },
    unregisterHandler(event: string) {
      handlers.delete(event);
    },
  }),
}));

const message = {
  type: 'system.notice',
  category: 'system',
  title: 'Fixture notice',
  priority: 'normal',
  link: '/admin/system/announcements',
};
const row: NotificationItem = {
  id: 201,
  template_code: message.type,
  category: message.category,
  title: message.title,
  priority: message.priority,
  link: message.link,
  is_read: false,
};

function emit(data: unknown) {
  const handler = handlers.get('notification');
  if (!handler) throw new Error('Missing notification handler');
  handler(data);
}

function inbox(items: NotificationItem[]) {
  return {
    items: items.map((item) => ({ ...item })),
    total: items.length,
    page: 1,
    page_size: 20,
  };
}

function openSession(
  endpoint: 'admin' | 'tenant',
  userId: number,
  tenantId?: number,
  profileUserId = userId,
) {
  window.history.replaceState({}, '', `/#/${endpoint}/dashboard`);
  const token = `fixture-${endpoint}-${userId}-${tenantId}`;
  requestMe.mockResolvedValue({
    data: { code: 0, data: { id: profileUserId, tenant_id: tenantId } },
  });
  TokenStorage.setToken(endpoint, token);
  useAccessStore().setAccessToken(token);
  useUserStore().setUserInfo({
    avatar: '',
    realName: 'Test user',
    tenantId,
    userId: String(userId),
    username: 'test-user',
  });
  const store = useNotificationStore();
  store.setEndpoint(endpoint);
  store.initSocketHandlers();
  return store;
}

describe('notification delivery identity', () => {
  let pinia: ReturnType<typeof createPinia>;

  beforeEach(async () => {
    localStorage.clear();
    handlers.clear();
    vi.resetAllMocks();
    requestPut.mockResolvedValue(undefined);
    requestDelete.mockResolvedValue(undefined);
    pinia = createPinia();
    setActivePinia(pinia);
    TokenStorage.init('notification-delivery-test');
    socket.currentEndpoint = '';
    await openSession('admin', 1).loadUnreadCount();
  });

  afterEach(() => {
    disposePinia(pinia);
  });

  it('merges persisted pushes with the inbox, keeping exact read and delete IDs', async () => {
    const store = useNotificationStore();
    let resolveSnapshot!: (value: ReturnType<typeof inbox>) => void;
    requestGet.mockReturnValueOnce(
      new Promise<ReturnType<typeof inbox>>((resolve) => {
        resolveSnapshot = resolve;
      }),
    );
    const list = store.loadNotifications();
    await vi.waitFor(() => expect(requestGet).toHaveBeenCalledOnce());
    const push = { ...message, id: row.id };
    emit(push);
    emit(push);
    resolveSnapshot(inbox([]));
    await list;

    expect(store.notifications.map(({ id, link }) => [id, link])).toEqual([
      [row.id, row.link],
    ]);
    expect(store.unreadCount).toBe(1);
    expect(
      useNotificationToast().toasts.value.map(({ title }) => title),
    ).toEqual([row.title]);
    expect(localStorage.getItem('novus_local_notifications')).toBeNull();

    requestGet.mockResolvedValueOnce(inbox([row]));
    await store.loadNotifications();
    expect(store.notifications).toEqual([row]);
    await store.markRead(row.id);
    await store.deleteNotification(row.id);
    emit(push);

    expect(requestPut.mock.calls.map(([url]) => url)).toEqual([
      '/admin/notifications/201/read',
    ]);
    expect(requestDelete.mock.calls.map(([url]) => url)).toEqual([
      '/admin/notifications/201',
    ]);
    expect(store.notifications).toEqual([]);
    expect(store.unreadCount).toBe(0);
  });

  it('keeps an already loaded row and its read status on late delivery', async () => {
    const store = useNotificationStore();
    const read = { ...row, is_read: true };
    requestGet.mockResolvedValueOnce(inbox([read]));
    await store.loadNotifications();
    requestCount.mockResolvedValueOnce({ count: 0 });
    emit({ ...message, id: row.id });
    await Promise.resolve();

    expect(store.notifications).toEqual([read]);
    expect(store.unreadCount).toBe(0);
    expect(useNotificationToast().toasts.value).toEqual([]);
    expect(requestPut.mock.calls).toEqual([]);
  });

  it('drops an older first-page response after the newer list has observed the push', async () => {
    const store = useNotificationStore();
    let resolveSnapshot!: (value: ReturnType<typeof inbox>) => void;
    requestGet.mockReturnValueOnce(
      new Promise<ReturnType<typeof inbox>>((resolve) => {
        resolveSnapshot = resolve;
      }),
    );
    const older = store.loadNotifications();
    await vi.waitFor(() => expect(requestGet).toHaveBeenCalledOnce());
    emit({ ...message, id: row.id });
    requestGet.mockResolvedValueOnce(inbox([row]));
    await store.loadNotifications();
    resolveSnapshot(inbox([]));
    await older;

    expect(store.notifications).toEqual([row]);
    expect(store.unreadCount).toBe(1);
  });

  it.each([undefined, '201', 0, 1.5, Number.MAX_SAFE_INTEGER + 1])(
    'keeps a push with missing or invalid ID %s local',
    async (id) => {
      const store = useNotificationStore();
      emit({ ...message, id });
      const [item] = store.notifications;
      expect(item?.id).toBeLessThan(0);
      if (!item) throw new Error('Missing local notification');
      await store.markRead(item.id);
      await store.deleteNotification(item.id);

      expect(requestPut.mock.calls).toEqual([]);
      expect(requestDelete.mock.calls).toEqual([]);
      expect(store.notifications).toEqual([]);
    },
  );

  it('loads only an authenticated tenant inbox on a platform Hash route', async () => {
    const store = openSession('tenant', 2, 17);
    requestGet.mockResolvedValueOnce(inbox([row]));
    await store.loadNotifications();

    expect(requestMe.mock.calls.at(-1)?.[0]).toBe('/tenant/auth/me');
    expect(requestGet.mock.calls.map(([url]) => url)).toEqual([
      '/tenant/notifications?page=1&page_size=20',
    ]);
    expect(store.notifications).toEqual([row]);
  });

  it('restores only the verified endpoint, account, and tenant cache', async () => {
    const cachePrefix = 'novus_local_notifications';
    const cacheKey = (endpoint: string, id: string, tenant: null | string) =>
      `${cachePrefix}:v2:${JSON.stringify([endpoint, id, tenant])}`;
    const store = useNotificationStore();
    emit({ ...message, title: 'Admin session only' });
    const lateAdminPush = handlers.get('notification');
    expect(lateAdminPush).toBeTypeOf('function');
    const safeTenantNotice = { ...row, id: -3, title: 'Tenant 18 only' };
    const otherTenantNotice = { ...row, id: -4, title: 'Tenant 17 only' };
    localStorage.setItem(
      cacheKey('tenant', '1', '17'),
      JSON.stringify([otherTenantNotice]),
    );
    localStorage.setItem(
      cacheKey('tenant', '1', '18'),
      JSON.stringify([safeTenantNotice]),
    );
    localStorage.setItem(
      cacheKey('tenant', '9', '18'),
      JSON.stringify([otherTenantNotice]),
    );
    localStorage.setItem(cachePrefix, JSON.stringify([otherTenantNotice]));

    openSession('tenant', 1, 18);
    await store.loadUnreadCount();
    lateAdminPush?.({ ...message, id: 999, title: 'Late admin push' });

    expect(store.notifications).toEqual([safeTenantNotice]);
    expect(store.unreadCount).toBe(1);
    expect(localStorage.getItem(cachePrefix)).toBeNull();
    expect(localStorage.getItem(cacheKey('admin', '1', null))).toContain(
      'Admin session only',
    );
    expect(useNotificationToast().toasts.value).toEqual([]);
  });

  it('drops old tenant list results and pushes after the tenant changes', async () => {
    const store = openSession('tenant', 2, 17);
    await store.loadUnreadCount();
    const lateTenantPush = handlers.get('notification');
    expect(lateTenantPush).toBeTypeOf('function');
    let deliverOldSnapshot!: (value: ReturnType<typeof inbox>) => void;
    requestGet.mockReturnValueOnce(
      new Promise<ReturnType<typeof inbox>>((resolve) => {
        deliverOldSnapshot = resolve;
      }),
    );
    const older = store.loadNotifications();
    await vi.waitFor(() => expect(requestGet).toHaveBeenCalledOnce());

    openSession('tenant', 2, 18);
    const newRow = { ...row, id: 301, title: 'New tenant only' };
    requestGet.mockResolvedValueOnce(inbox([newRow]));
    await store.loadNotifications();
    deliverOldSnapshot(inbox([row]));
    await older;
    lateTenantPush?.({ ...message, id: 999 });

    expect(store.notifications).toEqual([newRow]);
    expect(useNotificationToast().toasts.value).toEqual([]);
  });

  it('invalidates rows and registered callbacks after another tab changes credentials', async () => {
    const store = useNotificationStore();
    emit({ ...message, id: row.id });
    const oldPush = handlers.get('notification');
    expect(oldPush).toBeTypeOf('function');
    TokenStorage.setToken('admin', 'different-admin-token');
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'notification-delivery-test_admin_token',
      }),
    );
    oldPush?.({ ...message, id: row.id + 1 });
    await store.markRead(row.id);

    expect(store.notifications).toEqual([]);
    expect(store.unreadCount).toBe(0);
    expect(requestPut.mock.calls).toEqual([]);
  });

  it('keeps the shared socket available when profile verification fails after token rotation', async () => {
    const store = useNotificationStore();
    const onPresence = vi.fn();
    handlers.set('presence:online', onPresence);
    emit({ ...message, id: row.id });
    const oldPush = handlers.get('notification');
    requestMe.mockRejectedValueOnce(new Error('Temporary profile failure'));
    TokenStorage.setToken('admin', 'rotated-admin-token');
    useAccessStore().setAccessToken('rotated-admin-token');
    await store.loadUnreadCount();
    oldPush?.({ ...message, id: row.id + 1 });
    handlers.get('presence:online')?.({ userId: 1 });

    expect(socket.currentEndpoint).toBe('admin');
    expect(onPresence.mock.calls).toEqual([[{ userId: 1 }]]);
    expect(handlers.get('notification')).toBeUndefined();
    expect(store.notifications).toEqual([]);

    window.dispatchEvent(new Event('online'));
    await vi.waitFor(() =>
      expect(handlers.get('notification')).toBeTypeOf('function'),
    );
    emit({ ...message, id: row.id + 2 });
    expect(store.notifications.map(({ id }) => id)).toEqual([row.id + 2]);
  });

  it('keeps confirmed deletions out of later inbox and socket snapshots after verified token rotation', async () => {
    const store = useNotificationStore();
    requestGet.mockResolvedValue(inbox([row]));
    await store.loadNotifications();
    await store.deleteNotification(row.id);

    TokenStorage.setToken('admin', 'rotated-admin-token');
    useAccessStore().setAccessToken('rotated-admin-token');
    await store.loadNotifications();
    emit({ ...message, id: row.id });

    expect(store.notifications).toEqual([]);
    expect(useNotificationToast().toasts.value).toEqual([]);
  });

  it('does not expose stored notifications when the profile no longer matches the active account', async () => {
    const store = openSession('tenant', 2, 17, 3);
    const key = `novus_local_notifications:v2:${JSON.stringify(['tenant', '3', '17'])}`;
    localStorage.setItem(
      key,
      JSON.stringify([{ ...row, id: -3, title: 'A different account' }]),
    );
    await store.loadNotifications();

    expect(store.notifications).toEqual([]);
    expect(store.unreadCount).toBe(0);
    expect(requestGet.mock.calls).toEqual([]);
    expect(handlers.get('notification')).toBeUndefined();
  });

  it('preserves the inbox row and global count when a read or delete fails', async () => {
    const store = useNotificationStore();
    requestGet.mockResolvedValueOnce(inbox([row]));
    await store.loadNotifications();
    requestCount.mockResolvedValue({ count: 4 });
    await store.loadUnreadCount();
    requestPut.mockRejectedValueOnce(new Error('Read failed'));
    requestDelete.mockRejectedValueOnce(new Error('Delete failed'));
    await store.markRead(row.id);
    await store.deleteNotification(row.id);

    expect(store.notifications).toEqual([row]);
    expect(store.unreadCount).toBe(4);
    expect(requestPut.mock.calls[0]?.[2]).toMatchObject({
      skipAuthRecovery: true,
    });
    expect(requestDelete.mock.calls[0]?.[1]).toMatchObject({
      skipAuthRecovery: true,
    });
  });

  it('keeps a confirmed deletion out of subsequent lists and queued pushes', async () => {
    const store = useNotificationStore();
    requestGet.mockResolvedValue(inbox([row]));
    await store.loadNotifications();
    await store.deleteNotification(row.id);
    emit({ ...message, id: row.id });
    await store.loadNotifications();

    expect(store.notifications).toEqual([]);
    expect(store.unreadCount).toBe(0);
    expect(useNotificationToast().toasts.value).toEqual([]);
  });

  it('uses the global server count after marking only one category read', async () => {
    const store = useNotificationStore();
    const task = { ...row, category: 'task', id: 202 };
    requestGet.mockResolvedValueOnce(inbox([row, task]));
    await store.loadNotifications();
    store.addLocalNotification({
      ...message,
      category: 'task',
      title: 'Local task',
    });
    requestCount.mockResolvedValueOnce({ count: 8 });
    await store.loadUnreadCount();
    expect(store.unreadCount).toBe(9);
    requestCount.mockResolvedValueOnce({ count: 5 });
    await store.markAllRead('system');

    expect(requestPut.mock.calls.map(([url]) => url)).toEqual([
      '/admin/notifications/read-all?category=system',
    ]);
    expect(
      store.notifications.map(({ category, is_read }) => [category, is_read]),
    ).toEqual([
      ['task', false],
      ['system', true],
      ['task', false],
    ]);
    expect(store.unreadCount).toBe(6);
  });
});
