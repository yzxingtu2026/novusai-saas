import {
  computed,
  onActivated,
  onDeactivated,
  onScopeDispose,
  ref,
  shallowReactive,
} from 'vue';

export interface ConfigGroupRequest {
  readonly group: string;
}

interface ConfigGroupSave {
  readonly request: ConfigGroupRequest;
}

interface ConfigGroupOwnerOptions {
  activeGroup: () => string;
  onInvalidate: () => void;
}

/** 中文: 跨导航和异步边界持有当前配置表单。 EN: Own the selected configuration form across navigation and async boundaries. */
export function useConfigGroupRequestOwner(options: ConfigGroupOwnerOptions) {
  const loading = ref(false);
  const pendingSaves = shallowReactive(new Map<string, ConfigGroupSave>());
  const saving = computed(() => pendingSaves.has(options.activeGroup()));
  const ready = ref(false);
  let active = true;
  let disposed = false;
  let pageOwner = Symbol('config-page');
  let requestOwner: ConfigGroupRequest | undefined;

  function isPageCurrent(owner: symbol | undefined) {
    return active && !disposed && owner === pageOwner;
  }

  function capturePage() {
    return active && !disposed ? pageOwner : undefined;
  }

  function isCurrent(
    request: ConfigGroupRequest | undefined,
  ): request is ConfigGroupRequest {
    return (
      active &&
      !disposed &&
      request !== undefined &&
      request === requestOwner &&
      request.group === options.activeGroup()
    );
  }

  function current() {
    return isCurrent(requestOwner) ? requestOwner : undefined;
  }

  function select(group: string, needsLoad: boolean) {
    if (!active || disposed) return;
    // 中文: 对象身份区分 A -> B -> A，并在路由等待前取得归属。
    // EN: Object identity distinguishes A -> B -> A, including before route awaits.
    requestOwner = { group };
    ready.value = false;
    loading.value = needsLoad;
    return requestOwner;
  }

  function startLoad(request: ConfigGroupRequest) {
    if (!isCurrent(request)) return false;
    ready.value = false;
    loading.value = true;
    return true;
  }

  function acceptLoad(request: ConfigGroupRequest) {
    if (isCurrent(request)) ready.value = true;
  }

  function finishLoad(request: ConfigGroupRequest) {
    if (isCurrent(request)) loading.value = false;
  }

  function beginSave() {
    const request = current();
    if (!request || !ready.value || loading.value || saving.value) return;
    const owner = { request };
    pendingSaves.set(request.group, owner);
    return owner;
  }

  function ownsSave(owner: ConfigGroupSave) {
    return (
      pendingSaves.get(owner.request.group) === owner &&
      isCurrent(owner.request)
    );
  }

  function finishSave(owner: ConfigGroupSave) {
    // 中文: 已派发写入独立于界面归属存活，只释放其精确分组锁。
    // EN: An admitted write outlives UI ownership; release only its exact group lock.
    if (pendingSaves.get(owner.request.group) === owner)
      pendingSaves.delete(owner.request.group);
  }

  function invalidate() {
    pageOwner = Symbol('config-page');
    requestOwner = undefined;
    ready.value = false;
    loading.value = false;
    options.onInvalidate();
  }

  onActivated(() => {
    active = !disposed;
  });
  onDeactivated(() => {
    active = false;
    invalidate();
  });
  onScopeDispose(() => {
    active = false;
    disposed = true;
    invalidate();
  });

  return {
    loading,
    saving,
    ready,
    acceptLoad,
    beginSave,
    capturePage,
    current,
    finishLoad,
    finishSave,
    isCurrent,
    isPageCurrent,
    ownsSave,
    select,
    startLoad,
  };
}
