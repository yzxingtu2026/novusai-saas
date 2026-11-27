import type { HttpResponse } from '#/utils/request';

import { onScopeDispose, ref, watch } from 'vue';

import { useAccessStore, useUserStore } from '@vben/stores';

import { defineStore } from 'pinia';

import { useNotificationToast } from '#/composables/use-notification-toast';
import { resolveEndpointByPath } from '#/constants/endpoints';
import { baseRequestClient, requestClient } from '#/utils/request';

import { useSocketIOStore } from './socketio';
import { TokenStorage } from './token-storage';

export interface NotificationItem {
  id: number;
  template_code?: null | string;
  category: string;
  title: string;
  body?: null | string;
  data?: null | Record<string, unknown>;
  link?: null | string;
  priority: string;
  is_read: boolean;
  read_at?: null | string;
  created_at?: null | string;
}

interface NotificationListResponse {
  items: NotificationItem[];
  total: number;
  page: number;
  page_size: number;
}

interface NotificationPushData {
  id?: null | number;
  type: string;
  category: string;
  title: string;
  body?: null | string;
  data?: null | Record<string, unknown>;
  link?: null | string;
  priority: string;
}

type NotificationEndpoint = 'admin' | 'tenant';

interface NotificationSession {
  cacheKey: null | string;
  controller: AbortController;
  endpoint: NotificationEndpoint;
  token: string;
  userId: string;
}

const LEGACY_CACHE_KEY = 'novus_local_notifications';

function loadLocalNotifications(key: string): NotificationItem[] {
  try {
    const raw = localStorage.getItem(key);
    const items: unknown = raw ? JSON.parse(raw) : null;
    return Array.isArray(items)
      ? items.filter(
          (item) =>
            item &&
            Number.isSafeInteger(item.id) &&
            item.id < 0 &&
            typeof item.title === 'string' &&
            typeof item.is_read === 'boolean',
        )
      : [];
  } catch {
    return [];
  }
}

function removeLocalNotifications(key: string) {
  try {
    localStorage.removeItem(key);
  } catch {
    // 浏览器可能禁用了本地缓存。
  }
}

function identityPart(value: unknown): null | string {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) {
    return String(value);
  }
  return typeof value === 'string' && /^[1-9]\d*$/.test(value) ? value : null;
}

export const useNotificationStore = defineStore('notification', () => {
  const accessStore = useAccessStore();
  const userStore = useUserStore();
  const notifications = ref<NotificationItem[]>([]);
  const unreadCount = ref(0);
  const loading = ref(false);
  const confirmedReads = new Map<number, null | string | undefined>();
  const deletedIds = new Set<number>();
  const seenPushIds = new Set<number>();
  const pendingPushIds = new Set<number>();
  const pendingReads = new Set<number>();
  const pendingDeletes = new Set<number>();
  let pendingWrites = 0;
  let countMutation = 0;
  let countRequest = 0;
  let listRequest = 0;
  let temporaryId = -Date.now();
  let currentEndpoint: NotificationEndpoint | null = null;
  let verifiedPrincipal: null | string = null;
  let session: NotificationSession | null = null;
  let verification: null | Promise<void> = null;
  let wantsSocket = false;
  let socketHandler: ((data: unknown) => void) | null = null;

  function resolveNotificationEndpoint(): NotificationEndpoint | null {
    if (typeof window === 'undefined') return currentEndpoint;
    const { hash, hostname, pathname } = window.location;
    const hashRoute = hash.match(/^#(\/.*)$/)?.[1];
    const endpoint = resolveEndpointByPath(hashRoute ?? pathname, hostname);
    return endpoint === 'admin' || endpoint === 'tenant' ? endpoint : null;
  }

  function currentToken(endpoint: NotificationEndpoint) {
    const token = accessStore.accessToken;
    return !accessStore.loginExpired &&
      token &&
      TokenStorage.getToken(endpoint) === token
      ? token
      : null;
  }

  function isCurrent(context: NotificationSession) {
    return (
      session === context &&
      context.endpoint === resolveNotificationEndpoint() &&
      context.token === currentToken(context.endpoint) &&
      context.userId === identityPart(userStore.userInfo?.userId)
    );
  }

  function resetSession() {
    const previous = session;
    const previousHandler = socketHandler;
    session = null;
    verification = null;
    socketHandler = null;
    previous?.controller.abort();

    if (previousHandler) {
      useSocketIOStore().unregisterHandler('notification', previousHandler);
    }

    pendingPushIds.clear();
    pendingReads.clear();
    pendingDeletes.clear();
    pendingWrites = 0;
    countMutation++;
    notifications.value = [];
    unreadCount.value = 0;
    loading.value = false;
    useNotificationToast().clearAll();
  }

  function clearVerifiedPrincipal() {
    verifiedPrincipal = null;
    confirmedReads.clear();
    deletedIds.clear();
    seenPushIds.clear();
  }

  function setEndpoint(endpoint: NotificationEndpoint) {
    if (currentEndpoint === endpoint) return;
    currentEndpoint = endpoint;
    if (session && session.endpoint !== endpoint) resetSession();
  }

  async function verifyPrincipal(context: NotificationSession) {
    try {
      const { data: response } = await baseRequestClient.instance.get<
        HttpResponse<{ id: number | string; tenant_id?: number | string }>
      >(`/${context.endpoint}/auth/me`, {
        headers: { Authorization: `Bearer ${context.token}` },
        signal: context.controller.signal,
        timeout: 5000,
      });
      const userId = identityPart(response.data?.id);
      const tenantId = identityPart(response.data?.tenant_id);
      const expectedTenantId = identityPart(userStore.userInfo?.tenantId);
      if (
        !isCurrent(context) ||
        response.code !== 0 ||
        userId !== context.userId ||
        (context.endpoint === 'tenant' &&
          (!tenantId || (expectedTenantId && expectedTenantId !== tenantId)))
      ) {
        return;
      }

      const key = `${LEGACY_CACHE_KEY}:v2:${JSON.stringify([
        context.endpoint,
        userId,
        context.endpoint === 'tenant' ? tenantId : null,
      ])}`;
      if (verifiedPrincipal !== key) {
        clearVerifiedPrincipal();
        verifiedPrincipal = key;
      }
      context.cacheKey = key;
      removeLocalNotifications(LEGACY_CACHE_KEY);
      const local = loadLocalNotifications(key);
      notifications.value = local;
      unreadCount.value = local.filter((item) => !item.is_read).length;
      for (const item of local) temporaryId = Math.min(temporaryId, item.id);
      if (wantsSocket) bindSocket(context);
    } catch {
      // 下次请求或网络恢复时可重新核验会话。
    } finally {
      if (session === context) {
        verification = null;
        if (!context.cacheKey) session = null;
      }
    }
  }

  async function requireSession(): Promise<NotificationSession | null> {
    if (session && !isCurrent(session)) resetSession();
    const endpoint = resolveNotificationEndpoint();
    const userId = identityPart(userStore.userInfo?.userId);
    const token = endpoint && currentToken(endpoint);
    if (!endpoint || !userId || !token) return null;

    if (!session) {
      const context = {
        cacheKey: null,
        controller: new AbortController(),
        endpoint,
        token,
        userId,
      };
      session = context;
      verification = verifyPrincipal(context);
    }
    const context = session;
    if (verification) await verification;
    return context.cacheKey && isCurrent(context) ? context : null;
  }

  function saveLocalNotifications(context: NotificationSession) {
    if (!context.cacheKey) return;
    try {
      const local = notifications.value.filter((item) => item.id < 0);
      if (local.length > 0) {
        localStorage.setItem(context.cacheKey, JSON.stringify(local));
      } else {
        removeLocalNotifications(context.cacheKey);
      }
    } catch {
      // 浏览器可能禁用了本地缓存。
    }
  }

  function notify(item: NotificationItem) {
    if (item.category !== 'announcement')
      useNotificationToast().pushToast(item);
  }

  function addLocalNotification(
    item: Omit<NotificationItem, 'created_at' | 'id' | 'is_read'>,
  ) {
    const context = session;
    if (!context?.cacheKey || !isCurrent(context)) return;
    const local = {
      ...item,
      id: --temporaryId,
      is_read: false,
      created_at: new Date().toISOString(),
    };
    notifications.value.unshift(local);
    unreadCount.value++;
    countMutation++;
    saveLocalNotifications(context);
    notify(local);
  }

  function handlePush(raw: unknown) {
    const data = raw as NotificationPushData;
    if (!data?.title) return;
    const item = {
      template_code: data.type,
      category: data.category,
      title: data.title,
      body: data.body,
      data: data.data,
      link: data.link,
      priority: data.priority,
    };
    const id = data.id;
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id <= 0) {
      addLocalNotification(item);
      return;
    }
    if (seenPushIds.has(id) || deletedIds.has(id)) return;
    seenPushIds.add(id);
    if (notifications.value.some((row) => row.id === id)) {
      void loadUnreadCount();
      return;
    }

    const notification = {
      ...item,
      id,
      is_read: confirmedReads.has(id),
      created_at: new Date().toISOString(),
    };
    pendingPushIds.add(id);
    if (!notification.is_read) unreadCount.value++;
    countMutation++;
    notifications.value.unshift(notification);
    notify(notification);
    void loadUnreadCount();
  }

  function bindSocket(context: NotificationSession) {
    if (socketHandler || !context.cacheKey || !isCurrent(context)) return;
    const socket = useSocketIOStore();
    socket.connect(context.endpoint, {
      principal: context.cacheKey,
      token: context.token,
    });
    socketHandler = (data) => {
      if (isCurrent(context) && socket.currentEndpoint === context.endpoint) {
        handlePush(data);
      }
    };
    socket.registerHandler('notification', socketHandler);
  }

  function initSocketHandlers() {
    wantsSocket = true;
    void requireSession().then((context) => {
      if (context && wantsSocket) bindSocket(context);
    });
  }

  async function loadUnreadCount(): Promise<void> {
    const context = await requireSession();
    if (!context || !isCurrent(context) || pendingWrites > 0) return;
    const request = ++countRequest;
    const mutation = countMutation;
    try {
      const data = await requestClient.get<{ count: number }>(
        `/${context.endpoint}/notifications/unread-count`,
        { signal: context.controller.signal },
      );
      if (
        isCurrent(context) &&
        request === countRequest &&
        mutation === countMutation &&
        Number.isSafeInteger(data?.count) &&
        data.count >= 0
      ) {
        const localUnread = notifications.value.filter(
          (item) => item.id < 0 && !item.is_read,
        ).length;
        unreadCount.value = data.count + localUnread;
      }
    } catch {
      // 保留当前计数，后续读取时重试。
    }
  }

  async function loadNotifications(
    category?: string,
    page = 1,
    pageSize = 20,
  ): Promise<void> {
    const context = await requireSession();
    if (!context || !isCurrent(context)) return;
    const request = ++listRequest;
    loading.value = true;
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      });
      if (category) params.set('category', category);
      const data = await requestClient.get<NotificationListResponse>(
        `/${context.endpoint}/notifications?${params.toString()}`,
        { signal: context.controller.signal },
      );
      if (!isCurrent(context) || request !== listRequest || !data?.items)
        return;
      for (const item of data.items) {
        if (item.is_read) confirmedReads.set(item.id, item.read_at);
        pendingPushIds.delete(item.id);
      }

      // 保留列表请求发出后才投递的条目，去除已经被当前会话删除的条目。
      const retained = notifications.value.filter(
        (item) => page !== 1 || item.id < 0 || pendingPushIds.has(item.id),
      );
      const rows = new Map(
        [...retained, ...data.items].map((item) => [item.id, item]),
      );
      notifications.value = [...rows.values()]
        .filter((item) => !deletedIds.has(item.id))
        .map((item) => {
          const readAt = confirmedReads.get(item.id);
          if (item.is_read || !confirmedReads.has(item.id)) return item;
          return {
            ...item,
            is_read: true,
            ...(readAt ? { read_at: readAt } : {}),
          };
        });
    } catch {
      // 保留当前页，允许重试。
    } finally {
      if (isCurrent(context) && request === listRequest) loading.value = false;
    }
  }

  function confirmRead(id: number) {
    const item = notifications.value.find((row) => row.id === id);
    if (id > 0 && !confirmedReads.has(id))
      confirmedReads.set(id, item?.read_at);
    if (item && !item.is_read) {
      item.is_read = true;
      unreadCount.value = Math.max(0, unreadCount.value - 1);
    }
  }

  function finishWrite(context: NotificationSession) {
    if (!isCurrent(context)) return;
    pendingWrites--;
    countMutation++;
    if (pendingWrites === 0) return loadUnreadCount();
  }

  async function markRead(id: number): Promise<void> {
    const context = session;
    const item = notifications.value.find((row) => row.id === id);
    if (!context?.cacheKey || !isCurrent(context) || !item || item.is_read)
      return;
    if (id < 0) {
      confirmRead(id);
      countMutation++;
      saveLocalNotifications(context);
      return;
    }
    if (pendingReads.has(id) || pendingDeletes.has(id)) return;

    pendingReads.add(id);
    pendingWrites++;
    countMutation++;
    try {
      await requestClient.put(
        `/${context.endpoint}/notifications/${id}/read`,
        undefined,
        {
          signal: context.controller.signal,
          skipAuthRecovery: true,
        },
      );
      if (isCurrent(context)) confirmRead(id);
    } catch {
      // 保留原有已读状态；请求客户端会提示失败。
    } finally {
      if (isCurrent(context)) pendingReads.delete(id);
      await finishWrite(context);
    }
  }

  async function markAllRead(category?: string): Promise<void> {
    const context = await requireSession();
    if (!context || !isCurrent(context)) return;
    const eligibleIds = notifications.value
      .filter(
        (item) => !item.is_read && (!category || item.category === category),
      )
      .map((item) => item.id);
    pendingWrites++;
    countMutation++;
    try {
      const query = category ? `?category=${encodeURIComponent(category)}` : '';
      await requestClient.put(
        `/${context.endpoint}/notifications/read-all${query}`,
        undefined,
        {
          signal: context.controller.signal,
          skipAuthRecovery: true,
        },
      );
      if (isCurrent(context)) {
        for (const id of eligibleIds) confirmRead(id);
        saveLocalNotifications(context);
      }
    } catch {
      // 保留原有已读状态；请求客户端会提示失败。
    } finally {
      // 分类操作也会影响未加载的条目，需要读取服务器的全局计数。
      await finishWrite(context);
    }
  }

  function removeItem(id: number) {
    const index = notifications.value.findIndex((item) => item.id === id);
    if (index === -1) return;
    const [item] = notifications.value.splice(index, 1);
    if (item && !item.is_read)
      unreadCount.value = Math.max(0, unreadCount.value - 1);
  }

  async function deleteNotification(id: number): Promise<void> {
    const context = session;
    if (
      !context?.cacheKey ||
      !isCurrent(context) ||
      !notifications.value.some((item) => item.id === id)
    ) {
      return;
    }
    if (id < 0) {
      removeItem(id);
      countMutation++;
      saveLocalNotifications(context);
      return;
    }
    if (pendingDeletes.has(id)) return;

    pendingDeletes.add(id);
    pendingWrites++;
    countMutation++;
    try {
      await requestClient.delete(`/${context.endpoint}/notifications/${id}`, {
        signal: context.controller.signal,
        skipAuthRecovery: true,
      });
      if (isCurrent(context)) {
        deletedIds.add(id);
        pendingPushIds.delete(id);
        removeItem(id);
      }
    } catch {
      // 删除失败保留条目，允许重试。
    } finally {
      if (isCurrent(context)) pendingDeletes.delete(id);
      await finishWrite(context);
    }
  }

  function $reset() {
    if (verifiedPrincipal) removeLocalNotifications(verifiedPrincipal);
    wantsSocket = false;
    currentEndpoint = null;
    resetSession();
    clearVerifiedPrincipal();
  }

  function onStorageChange() {
    if (session && !isCurrent(session)) resetSession();
  }

  function retryUnreadCount() {
    if (wantsSocket) void loadUnreadCount();
  }

  watch(
    [
      () => userStore.userInfo?.userId,
      () => userStore.userInfo?.tenantId,
      () => accessStore.accessToken,
      () => accessStore.loginExpired,
    ],
    () => {
      if (session) resetSession();
      retryUnreadCount();
    },
    { flush: 'sync' },
  );

  if (typeof window !== 'undefined') {
    window.addEventListener('storage', onStorageChange);
    window.addEventListener('online', retryUnreadCount);
  }
  onScopeDispose(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('storage', onStorageChange);
      window.removeEventListener('online', retryUnreadCount);
    }
    resetSession();
    clearVerifiedPrincipal();
  });

  return {
    notifications,
    unreadCount,
    loading,
    setEndpoint,
    loadUnreadCount,
    loadNotifications,
    addLocalNotification,
    markRead,
    markAllRead,
    deleteNotification,
    initSocketHandlers,
    $reset,
  };
});
