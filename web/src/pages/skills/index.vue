<script lang="ts" setup>
import type { SkillNode, SkillVersion } from "@@/apis/skills/type"
import { listSkillTreeApi, listSkillVersionsApi, patchSkillNodeApi, publishSkillVersionApi, saveSkillDraftApi } from "@@/apis/skills"
import SkillTree from "@@/components/SkillTree/index.vue"
import SkillVersionDrawer from "@@/components/SkillVersionDrawer/index.vue"
import { useSessionScope } from "@@/composables/useSessionScope"
import { renderMarkdown } from "@@/utils/markdown"

const sessionScope = useSessionScope()

const nodes = ref<SkillNode[]>([])
const scope = ref<"shared" | "news" | "all">("shared")
const search = ref("")
const mode = ref("split")
onMounted(() => {
  if (window.matchMedia("(max-width: 1023px)").matches) mode.value = "edit"
})
const expandAll = ref(true)
const filteredNodes = computed(() => {
  const scoped = nodes.value.filter(n => scope.value === "all" || n.scope === scope.value)
  if (!search.value.trim()) return scoped
  const ids = new Set(scoped.filter(n => n.path.toLowerCase().includes(search.value.toLowerCase())).map(n => n.id))
  for (const node of scoped) {
    if (!ids.has(node.id)) continue
    let parent = node.parent_id
    while (parent !== null) {
      ids.add(parent)
      parent = scoped.find(n => n.id === parent)?.parent_id ?? null
    }
  }
  return scoped.filter(n => ids.has(n.id))
})
async function changeScope(value: "shared" | "news" | "all") {
  if (dirty.value) await ElMessageBox.confirm("当前 Skill 有未保存的修改，切换后会丢失。继续？", "未保存", { type: "warning" })
  sessionScope.assertCurrent()
  scope.value = value
  selectedId.value = null
}
const selectedId = ref<number | null>(null)
const selected = computed(() => nodes.value.find(n => n.id === selectedId.value) ?? null)

// 编辑区：以最新版本为底稿
const latest = ref<SkillVersion | null>(null)
const content = ref("")
const dirty = computed(() => selected.value?.kind === "skill" && content.value !== (latest.value?.content ?? ""))
const saving = ref(false)
const loadingContent = ref(false)
let contentVersion = 0
const historyVisible = ref(false)
const preview = computed(() => renderMarkdown(content.value))

async function loadTree() {
  nodes.value = await listSkillTreeApi()
  if (selectedId.value !== null && !selected.value)
    selectedId.value = null
}

async function loadContent() {
  const version = ++contentVersion
  const skill = selected.value
  latest.value = null
  content.value = ""
  if (skill?.kind !== "skill") {
    loadingContent.value = false
    return
  }
  loadingContent.value = true
  try {
    const versions = await listSkillVersionsApi(skill.id)
    if (version !== contentVersion)
      return
    latest.value = versions[0] ?? null
    content.value = latest.value?.content ?? ""
  } finally {
    if (version === contentVersion)
      loadingContent.value = false
  }
}

async function select(node: SkillNode) {
  if (node.id === selectedId.value)
    return
  if (dirty.value)
    await ElMessageBox.confirm("当前 skill 有未保存的修改，切换后会丢失。继续？", "未保存", { type: "warning" })
  sessionScope.assertCurrent()
  selectedId.value = node.id
  await loadContent()
}

async function saveDraft(): Promise<SkillVersion | null> {
  if (loadingContent.value || saving.value)
    return null
  if (!selected.value || !dirty.value)
    return latest.value
  const id = selected.value.id
  const version = contentVersion
  saving.value = true
  try {
    const saved = await saveSkillDraftApi(id, content.value)
    if (version !== contentVersion)
      return null
    latest.value = saved
    ElMessage.success(`已保存草稿 v${latest.value.version_no}`)
    await loadTree()
    return latest.value
  } finally {
    saving.value = false
  }
}

async function publishLatest() {
  const skill = selected.value
  if (!skill)
    return
  const id = skill.id
  const contentRequest = contentVersion
  const version = await saveDraft()
  if (!version || selectedId.value !== id || contentRequest !== contentVersion)
    return
  await ElMessageBox.confirm(`发布 v${version.version_no}？发布后对应用途的下一次任务起使用此版本。`, "发布确认", { type: "warning" })
  sessionScope.assertCurrent()
  if (selectedId.value !== id || contentRequest !== contentVersion)
    return
  await publishSkillVersionApi(id, version.id)
  ElMessage.success(`已发布 v${version.version_no}`)
  await loadTree()
}

async function updateMeta(body: { priority?: number, enabled?: boolean }) {
  if (!selected.value)
    return
  await patchSkillNodeApi(selected.value.id, body)
  await loadTree()
}

async function onPublished() {
  historyVisible.value = false
  await loadTree()
}

// 离开页面前提醒未保存的修改
function beforeUnload(e: BeforeUnloadEvent) {
  if (dirty.value)
    e.preventDefault()
}
onMounted(() => window.addEventListener("beforeunload", beforeUnload))
onBeforeUnmount(() => window.removeEventListener("beforeunload", beforeUnload))
onBeforeRouteLeave(async () => {
  if (!sessionScope.isCurrent()) return true
  if (dirty.value)
    await ElMessageBox.confirm("当前 skill 有未保存的修改，离开后会丢失。继续？", "未保存", { type: "warning" })
  sessionScope.assertCurrent()
})

function saveShortcut(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault()
    if (dirty.value && !saving.value && !loadingContent.value) void saveDraft()
  }
}
onMounted(() => window.addEventListener("keydown", saveShortcut))
onBeforeUnmount(() => window.removeEventListener("keydown", saveShortcut))
onMounted(loadTree)
</script>

<template>
  <div class="skills-workspace h-full flex flex-col gap-3 lg:flex-row">
    <el-card class="skills-tree max-h-80 w-full flex shrink-0 flex-col lg:max-h-none lg:w-72" body-class="min-h-0 flex-1" shadow="never">
      <template #header>
        Skills（新闻专用规则独立生效）
      </template>
      <el-radio-group :model-value="scope" @update:model-value="value => changeScope(value as 'shared' | 'news' | 'all')" class="mb-2">
        <el-radio-button value="all">
          全部
        </el-radio-button><el-radio-button value="shared">
          Chat／交易
        </el-radio-button><el-radio-button value="news">
          新闻
        </el-radio-button>
      </el-radio-group>
      <el-input v-model="search" clearable placeholder="搜索规则名称或路径" class="my-2" /><el-button size="small" @click="expandAll = !expandAll">
        {{ expandAll ? '收起全部' : '展开全部' }}
      </el-button>
      <p v-if="scope === 'all'" class="text-xs text-slate-500">
        新建或整理目录时，请先选择用途。
      </p>
      <p v-if="search.trim()" class="text-xs text-slate-500">
        搜索期间暂停拖拽排序，清空搜索后恢复。
      </p>
      <SkillTree :nodes="filteredNodes" :expand-all="expandAll" :scope="scope === 'all' ? 'news' : scope" :selected-id="selectedId" :editable="scope !== 'all'" :draggable="!search.trim()" @select="select" @changed="loadTree" />
    </el-card>

    <el-card class="min-w-0 flex flex-1 flex-col" body-class="min-h-0 flex-1 flex flex-col gap-3" shadow="never">
      <el-empty v-if="!selected" description="在左侧选择或新建一个 skill" />

      <template v-else-if="selected.kind === 'folder'">
        <div class="text-lg text-slate-900 font-bold">
          📁 {{ selected.path }}
        </div>
        <div class="flex items-center gap-2 text-sm">
          启用
          <el-switch :model-value="selected.enabled" @change="v => updateMeta({ enabled: Boolean(v) })" />
          <span class="text-xs text-[var(--el-text-color-secondary)]">停用文件夹后，其中所有 skill 都不会进入 Chat 的判断</span>
        </div>
      </template>

      <template v-else>
        <div class="flex flex-wrap items-center gap-3">
          <span class="text-lg text-slate-900 font-bold">📄 {{ selected.path }}</span>
          <el-tag v-if="selected.published_version_no === null" type="info">
            未发布
          </el-tag>
          <el-tag v-else type="success">
            已发布 v{{ selected.published_version_no }}
          </el-tag>
          <el-tag v-if="selected.has_draft" type="warning">
            最新 v{{ selected.latest_version_no }} 未发布
          </el-tag>
          <el-tag v-if="dirty" type="danger">
            有未保存的修改
          </el-tag>
          <div class="ml-auto flex items-center gap-2 text-sm">
            优先级
            <el-input-number
              :model-value="selected.priority" :min="-100" :max="100" size="small" controls-position="right" class="w-24"
              @change="v => v !== undefined && v !== null && updateMeta({ priority: v })"
            />
            启用
            <el-switch :model-value="selected.enabled" @change="v => updateMeta({ enabled: Boolean(v) })" />
          </div>
        </div>
        <div class="text-xs text-[var(--el-text-color-secondary)]">
          优先级越高越先读取，冲突时以优先级高的为准。只有「已发布」的版本会用于对应用途的 AI 处理；保存只生成草稿。
        </div>

        <el-radio-group v-model="mode">
          <el-radio-button value="edit">
            Markdown 编辑
          </el-radio-button><el-radio-button value="preview">
            实时预览
          </el-radio-button><el-radio-button value="split">
            分栏对照
          </el-radio-button>
        </el-radio-group>
        <div class="skills-editor min-h-0 flex flex-1 flex-col gap-3 lg:flex-row">
          <textarea
            v-show="mode !== 'preview'"
            v-model="content" placeholder="用 Markdown 写下投资经验、判断流程或输出规范"
            :disabled="loadingContent || saving"
            class="box-border min-w-0 flex-1 resize-none border border-slate-300 rounded-lg border-solid p-3 text-sm text-slate-800 leading-6 font-mono outline-none focus:border-[var(--el-color-primary)]"
          />
          <el-scrollbar v-show="mode !== 'edit'" class="flex-1 border border-slate-200 rounded-lg border-solid bg-slate-50/50">
            <!-- markdown-it 已关闭 html，内容中的标签按文本显示 -->
            <div class="markdown-body p-3" v-html="preview" />
          </el-scrollbar>
        </div>

        <div class="flex flex-wrap gap-2">
          <el-button :disabled="loadingContent || !dirty" :loading="saving" @click="saveDraft">
            保存草稿
          </el-button>
          <el-button type="primary" :disabled="loadingContent || (!dirty && !selected.has_draft)" :loading="saving" @click="publishLatest">
            {{ dirty ? "保存并发布" : "发布最新版本" }}
          </el-button>
          <el-button @click="historyVisible = true">
            版本历史 / 回退
          </el-button>
        </div>
      </template>
    </el-card>

    <SkillVersionDrawer v-model="historyVisible" :skill="selected" @published="onPublished" />
  </div>
</template>
