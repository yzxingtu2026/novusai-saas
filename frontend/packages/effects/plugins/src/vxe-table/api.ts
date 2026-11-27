import type { VxeGridInstance } from 'vxe-table';

import type { ExtendedFormApi } from '@vben-core/form-ui';

import type { VxeGridProps } from './types';

import { toRaw, watch } from 'vue';

import { Store } from '@vben-core/shared/store';
import {
  bindMethods,
  isBoolean,
  isFunction,
  mergeWithArrayOverride,
  StateHandler,
} from '@vben-core/shared/utils';

function getDefaultState(): VxeGridProps {
  return {
    class: '',
    gridClass: '',
    gridOptions: {},
    gridEvents: {},
    formOptions: undefined,
    searchPanelAnimation: false,
    showSearchForm: true,
  };
}

export class VxeGridApi<T extends Record<string, any> = any> {
  public formApi = {} as ExtendedFormApi;

  // private prevState: null | VxeGridProps = null;
  public grid = {} as VxeGridInstance<T>;
  public state: null | VxeGridProps<T> = null;

  public store: Store<VxeGridProps<T>>;

  private isMounted = false;

  private readSequence = 0;

  private readWaiters = new Set<() => void>();

  private stateHandler: StateHandler;

  private stopNativeReadWatcher: (() => void) | null = null;

  constructor(options: VxeGridProps = {}) {
    const storeState = { ...options };

    const defaultState = getDefaultState();
    this.store = new Store<VxeGridProps>(
      mergeWithArrayOverride(storeState, defaultState),
      {
        onUpdate: () => {
          // this.prevState = this.state;
          this.state = this.store.state;
        },
      },
    );

    this.state = this.store.state;
    this.stateHandler = new StateHandler();
    bindMethods(this);
  }

  mount(instance: null | VxeGridInstance, formApi: ExtendedFormApi) {
    if (!this.isMounted && instance) {
      this.grid = instance;
      this.formApi = formApi;
      this.stateHandler.setConditionTrue();
      this.isMounted = true;
      // 中文: 原生分页/排序先修改状态再发查询；仅在旧读取占用时排队，空闲操作仍由 VXE 单独发送。
      // EN: Native paging/sorting changes state before querying; queue only behind an older read so idle actions remain VXE-owned.
      // 中文: 稳定签名忽略 VXE 内部同值数组替换，避免旧查询再次排队。
      // EN: A stable signature ignores VXE's same-value array replacement and prevents requeuing the current read.
      this.stopNativeReadWatcher = watch(
        () =>
          JSON.stringify([
            this.grid.reactData?.tablePage?.currentPage,
            this.grid.reactData?.tablePage?.pageSize,
            this.grid.reactData?.sortData?.map(({ field, order }) => [
              field,
              order,
            ]),
          ]),
        () => {
          if (this.isMounted && this.grid.reactData?.tableLoading) {
            void this.runReadQuery('query', {});
          }
        },
        { flush: 'sync' },
      );
    }
  }

  async query(params: Record<string, any> = {}) {
    return this.runReadQuery('query', params);
  }

  async reload(params: Record<string, any> = {}) {
    return this.runReadQuery('reload', params);
  }

  setGridOptions(options: Partial<VxeGridProps['gridOptions']>) {
    this.setState({
      gridOptions: options,
    });
  }

  setLoading(isLoading: boolean) {
    this.setState({
      gridOptions: {
        loading: isLoading,
      },
    });
  }

  setState(
    stateOrFn:
      | ((prev: VxeGridProps<T>) => Partial<VxeGridProps<T>>)
      | Partial<VxeGridProps<T>>,
  ) {
    if (isFunction(stateOrFn)) {
      this.store.setState((prev) => {
        return mergeWithArrayOverride(stateOrFn(prev), prev);
      });
    } else {
      this.store.setState((prev) => mergeWithArrayOverride(stateOrFn, prev));
    }
  }

  toggleSearchForm(show?: boolean) {
    this.setState({
      showSearchForm: isBoolean(show) ? show : !this.state?.showSearchForm,
    });
    // nextTick(() => {
    //   this.grid.recalculate();
    // });
    return this.state?.showSearchForm;
  }

  unmount() {
    this.isMounted = false;
    this.stopNativeReadWatcher?.();
    this.stopNativeReadWatcher = null;
    this.readSequence += 1;
    this.readWaiters.forEach((finish) => finish());
    this.stateHandler.reset();
  }

  private async runReadQuery(
    code: 'query' | 'reload',
    params: Record<string, unknown>,
  ) {
    const sequence = ++this.readSequence;
    const requestParams = { ...toRaw(params) };
    this.readWaiters.forEach((finish) => finish());
    try {
      // 中文: VXE 会忽略 loading 期间的提交；等待原生分页或搜索读取结束，再仅运行最新读取。
      // EN: VXE ignores submissions while loading; wait for native paging or search reads and run only the newest read.
      while (
        this.isMounted &&
        sequence === this.readSequence &&
        this.grid.reactData?.tableLoading
      ) {
        await new Promise<void>((resolve) => {
          const finish = () => {
            stop();
            this.readWaiters.delete(finish);
            resolve();
          };
          const stop = watch(
            () => this.grid.reactData?.tableLoading,
            (loading) => {
              if (!loading) finish();
            },
            { flush: 'sync' },
          );
          this.readWaiters.add(finish);
        });
      }
      if (!this.isMounted || sequence !== this.readSequence) return;
      await this.grid.commitProxy(code, requestParams);
    } catch (error) {
      console.error(
        `Error occurred while ${code === 'query' ? 'querying' : 'reloading'}:`,
        error,
      );
    }
  }
}
