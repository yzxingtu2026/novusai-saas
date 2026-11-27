// @vitest-environment happy-dom
// Test type: behavioral. Run the store and toast with captured transport calls.
import type { NotificationItem } from '../notification';

import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useNotificationToast } from '#/composables/use-notification-toast';

import { useNotificationStore } from '../notification';

const { handlers, requestDelete, requestGet, requestPut } = vi.hoisted(() => ({
  handlers: new Map<string, (data: unknown) => void>(),
  requestDelete: vi.fn(),
  requestGet: vi.fn(),
  requestPut: vi.fn(),
}));

vi.mock('#/utils/request', () => ({
  requestClient: { delete: requestDelete, get: requestGet, put: requestPut },
}));

vi.mock('#/store/shared/socketio', () => ({
  useSocketIOStore: () => ({
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
  return { items, total: items.length, page: 1, page_size: 20 };
}

describe('notification delivery identity', () => {
  beforeEach(() => {
    localStorage.clear();
    handlers.clear();
    vi.resetAllMocks();
    requestPut.mockResolvedValue(undefined);
    requestDelete.mockResolvedValue(undefined);
    setActivePinia(createPinia());
    window.history.replaceState({}, '', '/admin/dashboard');
    useNotificationStore().initSocketHandlers();
  });

  afterEach(() => {
    useNotificationStore().$reset();
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

    expect(requestPut.mock.calls).toEqual([['/admin/notifications/201/read']]);
    expect(requestDelete.mock.calls).toEqual([['/admin/notifications/201']]);
    expect(store.notifications).toEqual([]);
    expect(store.unreadCount).toBe(0);
  });

  it('keeps an already loaded row and its read status on late delivery', async () => {
    const store = useNotificationStore();
    const read = { ...row, is_read: true };
    requestGet.mockResolvedValueOnce(inbox([read]));
    await store.loadNotifications();
    requestGet.mockResolvedValueOnce({ count: 0 });
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
});
