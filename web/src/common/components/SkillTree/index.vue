<script lang="ts" setup>
import type { NodeKind, SkillNode } from "@@/apis/skills/type"
import type Node from "element-plus/es/components/tree/src/model/node"
import type { AllowDropType, NodeDropType } from "element-plus/es/components/tree/src/tree.type"
import { createSkillNodeApi, deleteSkillNodeApi, moveSkillNodeApi, patchSkillNodeApi } from "@@/apis/skills"
import { useSessionScope } from "@@/composables/useSessionScope"

const props = defineProps<{
  nodes: SkillNode[]
  editable?: boolean
  selectedId?: number | null
  /** 需要高亮的节点（如 Chat 刚生成草稿的 skill） */
  highlightIds?: number[]
}>()

const emit = defineEmits<{
  select: [node: SkillNode]
  changed: []
}>()

/** skills 树：Skills 页可编辑（拖拽、增删改），Chat 侧边栏只读 */
const sessionScope = useSessionScope()

interface TreeItem {
  id: number
  label: string
  data: SkillNode
  children: TreeItem[]
}

const treeData = computed(() => {
  const items = new Map<number, TreeItem>(props.nodes.map(n => [n.id, { id: n.id, label: n.name, data: n, children: [] }]))
  const roots: TreeItem[] = []
  // 后端已按 parent_id、sort_order 排好序
  for (const n of props.nodes) {
    const item = items.get(n.id)!
    if (n.parent_id === null)
      roots.push(item)
    else
      items.get(n.parent_id)?.children.push(item)
  }
  return roots
})

// 新出现的文件夹默认展开，保留用户手动折叠的状态
const expanded = ref<number[]>([])
const seenFolders = new Set<number>()
watch(() => props.nodes, (nodes) => {
  for (const n of nodes) {
    if (n.kind === "folder" && !seenFolders.has(n.id)) {
      seenFolders.add(n.id)
      expanded.value.push(n.id)
    }
  }
}, { immediate: true })

function allowDrop(_dragging: Node, drop: Node, type: AllowDropType) {
  return type !== "inner" || (drop.data as TreeItem).data.kind === "folder"
}

async function onDrop(dragging: Node, drop: Node, type: NodeDropType) {
  const dragged = (dragging.data as TreeItem).data
  const target = (drop.data as TreeItem).data
  const parentId = type === "inner" ? target.id : target.parent_id
  const siblings = props.nodes.filter(n => n.parent_id === parentId && n.id !== dragged.id)
  const index = type === "inner" ? siblings.length : siblings.findIndex(n => n.id === target.id) + (type === "after" ? 1 : 0)
  try {
    await moveSkillNodeApi(dragged.id, parentId, index)
  } finally {
    // 失败时也刷新，让树回到服务端的真实顺序
    emit("changed")
  }
}

const NAME_RULE = (v: string) => (!!v?.trim() && !v.includes("/")) || "名称不能为空，也不能包含 /"

async function create(parentId: number | null, kind: NodeKind) {
  const { value } = await ElMessageBox.prompt(kind === "folder" ? "文件夹名称" : "skill 名称", kind === "folder" ? "新建文件夹" : "新建 skill", {
    inputValidator: NAME_RULE
  })
  sessionScope.assertCurrent()
  const node = await createSkillNodeApi({ parent_id: parentId, kind, name: value.trim() })
  emit("changed")
  if (kind === "skill")
    emit("select", node)
}

async function rename(node: SkillNode) {
  const { value } = await ElMessageBox.prompt("新名称", "重命名", { inputValue: node.name, inputValidator: NAME_RULE })
  sessionScope.assertCurrent()
  await patchSkillNodeApi(node.id, { name: value.trim() })
  emit("changed")
}

async function remove(node: SkillNode) {
  const message = node.kind === "folder" ? `删除文件夹「${node.path}」？（必须为空）` : `删除 skill「${node.path}」及其全部版本？此操作不可恢复。`
  await ElMessageBox.confirm(message, "删除确认", { type: "warning" })
  sessionScope.assertCurrent()
  await deleteSkillNodeApi(node.id)
  ElMessage.success("已删除")
  emit("changed")
}

function onExpand(item: TreeItem) {
  if (!expanded.value.includes(item.id))
    expanded.value.push(item.id)
}

function onCollapse(item: TreeItem) {
  expanded.value = expanded.value.filter(id => id !== item.id)
}
</script>

<template>
  <div class="h-full flex flex-col">
    <div v-if="editable" class="mb-2 flex gap-2">
      <el-button size="small" @click="create(null, 'folder')">
        新建文件夹
      </el-button>
      <el-button size="small" @click="create(null, 'skill')">
        新建 skill
      </el-button>
    </div>
    <el-scrollbar class="min-h-0 flex-1">
      <el-empty v-if="nodes.length === 0" description="还没有 skill" :image-size="60" />
      <el-tree
        v-else
        :data="treeData" node-key="id" :props="{ label: 'label', children: 'children' }"
        :default-expanded-keys="expanded" :current-node-key="selectedId ?? undefined" highlight-current
        :draggable="editable" :allow-drop="allowDrop" :expand-on-click-node="false"
        @node-drop="onDrop" @node-click="(item: TreeItem) => emit('select', item.data)"
        @node-expand="onExpand" @node-collapse="onCollapse"
      >
        <template #default="{ data }">
          <div
            class="group w-full flex items-center gap-1 overflow-hidden rounded pr-1 text-sm"
            :class="{ 'opacity-50': !data.data.enabled, 'bg-amber-100': highlightIds?.includes(data.id) }"
          >
            <span>{{ data.data.kind === "folder" ? "📁" : "📄" }}</span>
            <span class="truncate">{{ data.data.name }}</span>
            <template v-if="data.data.kind === 'skill'">
              <el-tag size="small" effect="plain" class="shrink-0">
                P{{ data.data.priority }}
              </el-tag>
              <el-tag v-if="data.data.published_version_no === null" size="small" type="info" class="shrink-0">
                未发布
              </el-tag>
              <el-tag v-else-if="data.data.has_draft" size="small" type="warning" class="shrink-0">
                草稿
              </el-tag>
            </template>
            <div v-if="editable" class="ml-auto hidden shrink-0 gap-1 group-hover:flex" @click.stop>
              <template v-if="data.data.kind === 'folder'">
                <el-button link size="small" @click="create(data.id, 'skill')">
                  +skill
                </el-button>
                <el-button link size="small" @click="create(data.id, 'folder')">
                  +目录
                </el-button>
              </template>
              <el-button link size="small" @click="rename(data.data)">
                改名
              </el-button>
              <el-button link size="small" type="danger" @click="remove(data.data)">
                删除
              </el-button>
            </div>
          </div>
        </template>
      </el-tree>
    </el-scrollbar>
  </div>
</template>
