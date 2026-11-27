<script lang="ts" setup>
/**
 * 用户角色权限分配抽屉 / User role permission assignment drawer
 * 加载权限树 + 勾选已分配权限 + 保存
 */
import type { TreeProps as AntTreeProps } from 'ant-design-vue';

import type { TenantPermissionNodeRaw } from '#/api/tenant/permission';
import type { TenantUserRoleInfo } from '#/api/tenant/tenant-user-roles';

import { computed, ref, shallowRef, watch } from 'vue';

import { IconifyIcon } from '@vben/icons';

import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  message,
  Skeleton,
  Space,
  Tag,
  Tree,
} from 'ant-design-vue';

import {
  assignTenantUserRolePermissionsApi,
  getTenantUserRoleAssignmentApi,
} from '#/api/tenant/tenant-user-roles';
import { $t } from '#/locales';

const props = defineProps<{
  open: boolean;
  role: null | TenantUserRoleInfo;
}>();

const emit = defineEmits<{
  saved: [];
  'update:open': [value: boolean];
}>();

// ============================================================
// State / 状态
// ============================================================

const loading = ref(false);
const loadFailed = ref(false);
const saving = ref(false);
const draft = shallowRef<null | { ineligibleGrants: string[]; roleId: number }>(
  null,
);
const removeIneligibleGrants = ref(false);
const permissionTree = ref<TreeNode[]>([]);
const checkedKeys = ref<number[]>([]);
const expandedKeys = ref<number[]>([]);
const parentKeyMap = ref<Map<number, null | number>>(new Map());
const descendantKeyMap = ref<Map<number, number[]>>(new Map());
const activeRoleId = computed(() => (props.open ? props.role?.id : undefined));
const canSave = computed(
  () =>
    !saving.value &&
    draft.value !== null &&
    draft.value.roleId === activeRoleId.value &&
    (draft.value.ineligibleGrants.length === 0 || removeIneligibleGrants.value),
);

// ============================================================
// Tree node type / 树节点类型
// ============================================================

interface TreeNode {
  key: number;
  title: string;
  code: string;
  type: string;
  icon?: string;
  children?: TreeNode[];
}

// ============================================================
// Computed / 计算属性
// ============================================================

const drawerTitle = computed(() => {
  if (!props.role) return $t('tenant.system.userRole.assignPermissions');
  return `${$t('tenant.system.userRole.assignPermissions')} - ${props.role.name}`;
});

// ============================================================
// Load data / 加载数据
// ============================================================

function transformToTreeNodes(options: TenantPermissionNodeRaw[]): TreeNode[] {
  const entries = options.map((option) => ({
    node: {
      children: [] as TreeNode[],
      code: option.code,
      icon: option.icon ?? undefined,
      key: option.id,
      title: option.name,
      type: option.type,
    },
    parentId: option.parent_id,
  }));
  const nodesById = new Map(entries.map(({ node }) => [node.key, node]));
  const roots: TreeNode[] = [];
  for (const { node, parentId } of entries) {
    const parent = parentId === null ? undefined : nodesById.get(parentId);
    // 中文: 父节点不可分配时，在顶层显示可分配的子权限。
    // EN: Show eligible child permissions at the root when their parent is ineligible.
    (parent?.children ?? roots).push(node);
  }
  return roots;
}

function getAllKeys(nodes: TreeNode[]): number[] {
  const keys: number[] = [];
  for (const node of nodes) {
    keys.push(node.key);
    if (node.children && node.children.length > 0) {
      keys.push(...getAllKeys(node.children));
    }
  }
  return keys;
}

function buildTreeRelations(nodes: TreeNode[], parentId: null | number = null) {
  for (const node of nodes) {
    parentKeyMap.value.set(node.key, parentId);
    const descendants: number[] = [];
    for (const child of node.children || []) {
      descendants.push(child.key);
      buildTreeRelations([child], node.key);
      descendants.push(...(descendantKeyMap.value.get(child.key) || []));
    }
    descendantKeyMap.value.set(node.key, descendants);
  }
}

function getBranchKeys(targetKey: number): number[] {
  const branch: number[] = [];
  let current: null | number = targetKey;

  while (typeof current === 'number') {
    branch.push(current);
    current = parentKeyMap.value.get(current) ?? null;
  }

  return branch.toReversed();
}

// ============================================================
// Actions / 操作
// ============================================================

const onCheck: NonNullable<AntTreeProps['onCheck']> = (
  _displayedKeys,
  { checked, node },
) => {
  const key = node.key;
  if (typeof key !== 'number') return;
  // 中文: 仅应用用户直接切换的权限，保留草稿中的其他授权。
  // EN: Apply only the toggled permission and retain the rest of the draft.
  const selection = new Set(checkedKeys.value);
  if (checked) {
    selection.add(key);
  } else {
    selection.delete(key);
  }
  checkedKeys.value = [...selection];
};

async function onSave() {
  const submittedDraft = draft.value;
  if (!canSave.value || !submittedDraft) return;
  saving.value = true;
  try {
    await assignTenantUserRolePermissionsApi(submittedDraft.roleId, {
      permission_ids: [...checkedKeys.value],
    });
    // 中文: 已完成的写入始终通知角色列表刷新，即使抽屉已切换到其他角色。
    // EN: Notify the role list of a committed save even after the drawer changes roles.
    emit('saved');
    if (
      draft.value === submittedDraft &&
      activeRoleId.value === submittedDraft.roleId
    ) {
      message.success($t('tenant.system.userRole.messages.permissionsSaved'));
      emit('update:open', false);
    }
  } finally {
    saving.value = false;
  }
}

function onClose() {
  emit('update:open', false);
}

function onExpandAll() {
  expandedKeys.value = getAllKeys(permissionTree.value);
}

function onCollapseAll() {
  expandedKeys.value = [];
}

const onExpand: NonNullable<AntTreeProps['onExpand']> = (
  nextExpandedKeys,
  info,
) => {
  const nodeKey = info.node.key;

  if (typeof nodeKey !== 'number') {
    expandedKeys.value = nextExpandedKeys.filter(
      (key): key is number => typeof key === 'number',
    );
    return;
  }

  if (info.expanded) {
    expandedKeys.value = getBranchKeys(nodeKey);
    return;
  }

  const removedKeys = new Set<number>([
    nodeKey,
    ...(descendantKeyMap.value.get(nodeKey) || []),
  ]);
  expandedKeys.value = nextExpandedKeys.filter(
    (key): key is number => typeof key === 'number' && !removedKeys.has(key),
  );
};

// ============================================================
// Watch / 监听
// ============================================================

watch(
  activeRoleId,
  async (roleId, _, onCleanup) => {
    draft.value = null;
    removeIneligibleGrants.value = false;
    permissionTree.value = [];
    checkedKeys.value = [];
    expandedKeys.value = [];
    loadFailed.value = false;
    loading.value = roleId !== undefined;
    if (roleId === undefined) return;

    let current = true;
    onCleanup(() => {
      current = false;
    });
    try {
      const assignment = await getTenantUserRoleAssignmentApi(roleId);
      if (!current) return;

      const eligibleIds = new Set(
        assignment.permissionOptions.map((option) => option.id),
      );
      const ineligibleGrants = assignment.permissionIds.flatMap((id, index) =>
        eligibleIds.has(id)
          ? []
          : [assignment.permissionCodes[index] ?? String(id)],
      );
      permissionTree.value = transformToTreeNodes(assignment.permissionOptions);
      parentKeyMap.value = new Map();
      descendantKeyMap.value = new Map();
      buildTreeRelations(permissionTree.value);
      checkedKeys.value = assignment.permissionIds.filter((id) =>
        eligibleIds.has(id),
      );
      draft.value = { ineligibleGrants, roleId };
    } catch {
      if (current) loadFailed.value = true;
    } finally {
      if (current) loading.value = false;
    }
  },
  { immediate: true },
);

// ============================================================
// Permission type helpers / 权限类型辅助
// ============================================================

function getTypeIcon(type: string): string {
  switch (type) {
    case 'api': {
      return 'lucide:route';
    }
    case 'button': {
      return 'lucide:square';
    }
    case 'menu': {
      return 'lucide:menu';
    }
    case 'operation': {
      return 'lucide:mouse-pointer-click';
    }
    default: {
      return 'lucide:folder';
    }
  }
}

function getTypeColor(type: string): string {
  switch (type) {
    case 'api': {
      return 'orange';
    }
    case 'button': {
      return 'cyan';
    }
    case 'menu': {
      return 'blue';
    }
    case 'operation': {
      return 'green';
    }
    default: {
      return 'default';
    }
  }
}

function getTypeLabel(type: string): string {
  const key = `tenant.system.userRole.permissionTypes.${type}`;
  const label = $t(key);
  return label === key ? type : label;
}
</script>

<template>
  <Drawer
    :open="open"
    :title="drawerTitle"
    :width="520"
    :destroy-on-close="true"
    @close="onClose"
  >
    <template #extra>
      <Space>
        <Button size="small" @click="onExpandAll">
          {{ $t('tenant.system.organization.expandAll') }}
        </Button>
        <Button size="small" @click="onCollapseAll">
          {{ $t('tenant.system.organization.collapseAll') }}
        </Button>
      </Space>
    </template>

    <Alert
      v-if="draft && draft.ineligibleGrants.length > 0"
      :description="draft.ineligibleGrants.join(', ')"
      class="mb-3"
      show-icon
      type="warning"
    >
      <template #message>
        <Checkbox v-model:checked="removeIneligibleGrants" :disabled="saving">
          {{
            $t('tenant.system.userRole.removeIneligibleGrants', {
              count: draft.ineligibleGrants.length,
            })
          }}
        </Checkbox>
      </template>
    </Alert>

    <!-- Loading -->
    <div v-if="loading" class="space-y-3 p-2">
      <Skeleton
        v-for="i in 6"
        :key="i"
        active
        :title="false"
        :paragraph="{ rows: 1, width: '80%' }"
        class="!mb-0"
      />
    </div>

    <Empty v-else-if="loadFailed" :description="$t('common.loadFailed')" />

    <!-- Empty -->
    <Empty
      v-else-if="permissionTree.length === 0"
      :description="$t('tenant.system.userRole.noPermissions')"
    />

    <!-- Permission Tree -->
    <Tree
      v-else
      v-model:expanded-keys="expandedKeys"
      :checked-keys="checkedKeys"
      :tree-data="permissionTree"
      check-strictly
      checkable
      :disabled="saving"
      :selectable="false"
      block-node
      @check="onCheck"
      @expand="onExpand"
    >
      <template #title="{ code, icon, title, type }">
        <div class="flex items-center gap-1.5">
          <IconifyIcon
            :icon="icon || getTypeIcon(type)"
            class="flex-shrink-0 text-sm"
          />
          <span class="text-sm">{{ title }}</span>
          <Tag :color="getTypeColor(type)" size="small">
            {{ getTypeLabel(type) }}
          </Tag>
          <span class="text-xs text-muted-foreground">
            {{ code }}
          </span>
        </div>
      </template>
    </Tree>

    <!-- Footer -->
    <template #footer>
      <div class="flex justify-end gap-2">
        <Button @click="onClose">
          {{ $t('shared.common.cancel') }}
        </Button>
        <Button
          type="primary"
          :loading="saving"
          :disabled="!canSave"
          @click="onSave"
        >
          {{ $t('shared.common.save') }}
        </Button>
      </div>
    </template>
  </Drawer>
</template>
