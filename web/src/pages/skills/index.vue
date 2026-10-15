<script lang="ts" setup>
import type { SkillNode, SkillVersion } from "@/apis/skills"
import { listSkillTree, listSkillVersions, patchSkillNode, publishSkillVersion, saveSkillDraft } from "@/apis/skills"
import SkillTree from "@/components/SkillTree.vue"
import SkillVersionDrawer from "@/components/SkillVersionDrawer.vue"
import { renderMarkdown } from "@/utils/markdown"

const nodes = ref<SkillNode[]>([])
const selectedId = ref<number | null>(null)
const selected = computed(() => nodes.value.find(n => n.id === selectedId.value) ?? null)

// 编辑区：以最新版本为底稿
const latest = ref<SkillVersion | null>(null)
const content = ref("")
const dirty = computed(() => selected.value?.kind === "skill" && content.value !== (latest.value?.content ?? ""))
const saving = ref(false)
const historyVisible = ref(false)
const preview = computed(() => renderMarkdown(content.value))

async function loadTree() {
  nodes.value = await listSkillTree()
  if (selectedId.value !== null && !selected.value)
    selectedId.value = null
}

async function loadContent() {
  if (selected.value?.kind !== "skill") {
    latest.value = null
    content.value = ""
    return
  }
  latest.value = (await listSkillVersions(selected.value.id))[0] ?? null
  content.value = latest.value?.content ?? ""
}

async function select(node: SkillNode) {
  if (node.id === selectedId.value)
    return
  if (dirty.value)
    await ElMessageBox.confirm("当前 skill 有未保存的修改，切换后会丢失。继续？", "未保存", { type: "warning" })
  selectedId.value = node.id
  await loadContent()
}

async function saveDraft(): Promise<SkillVersion | null> {
  if (!selected.value || !dirty.value)
    return latest.value
  saving.value = true
  try {
    latest.value = await saveSkillDraft(selected.value.id, content.value)
    ElMessage.success(`已保存草稿 v${latest.value.version_no}`)
    await loadTree()
    return latest.value
  } finally {
    saving.value = false
  }
}

async function publishLatest() {
  const version = await saveDraft()
  if (!version || !selected.value)
    return
  await ElMessageBox.confirm(`发布 v${version.version_no}？发布后 Chat 下一轮起按此版本判断。`, "发布确认", { type: "warning" })
  await publishSkillVersion(selected.value.id, version.id)
  ElMessage.success(`已发布 v${version.version_no}`)
  await loadTree()
}

async function updateMeta(body: { priority?: number, enabled?: boolean }) {
  if (!selected.value)
    return
  await patchSkillNode(selected.value.id, body)
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
  if (dirty.value)
    await ElMessageBox.confirm("当前 skill 有未保存的修改，离开后会丢失。继续？", "未保存", { type: "warning" })
})

loadTree()
</script>

<template>
  <div class="h-full flex gap-4">
    <el-card class="w-80 flex shrink-0 flex-col" body-class="min-h-0 flex-1" shadow="never">
      <template #header>
        Skills
      </template>
      <SkillTree :nodes="nodes" :selected-id="selectedId" editable @select="select" @changed="loadTree" />
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
          优先级越高越先读取，冲突时以优先级高的为准。只有「已发布」的版本会进入 Chat 的判断；保存只生成草稿。
        </div>

        <div class="min-h-0 flex flex-1 gap-3">
          <textarea
            v-model="content" placeholder="用 Markdown 写下投资经验、判断流程或输出规范"
            class="box-border flex-1 resize-none border border-slate-300 rounded-lg border-solid p-3 text-sm text-slate-800 leading-6 font-mono outline-none focus:border-[var(--el-color-primary)]"
          />
          <el-scrollbar class="flex-1 border border-slate-200 rounded-lg border-solid bg-slate-50/50">
            <!-- markdown-it 已关闭 html，内容中的标签按文本显示 -->
            <div class="markdown-body p-3" v-html="preview" />
          </el-scrollbar>
        </div>

        <div class="flex gap-2">
          <el-button :disabled="!dirty" :loading="saving" @click="saveDraft">
            保存草稿
          </el-button>
          <el-button type="primary" :disabled="!dirty && !selected.has_draft" :loading="saving" @click="publishLatest">
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
