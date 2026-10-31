<script lang="ts" setup>
import type { SkillNode, SkillVersion } from "@@/apis/skills/type"
import { listSkillVersionsApi, publishSkillVersionApi } from "@@/apis/skills"
import { useSessionScope } from "@@/composables/useSessionScope"
import { diffLines, formatTime } from "@@/utils/text"
import { CanceledError } from "axios"

const props = defineProps<{ skill: SkillNode | null }>()

const emit = defineEmits<{ published: [] }>()

/** 版本历史：与已发布版本逐行对比，可发布任意版本（发布旧版本即回退） */
const sessionScope = useSessionScope()

const visible = defineModel<boolean>({ required: true })

const versions = ref<SkillVersion[]>([])
const selectedId = ref<number | null>(null)
const publishing = ref(false)
const loading = ref(false)
const failed = ref(false)
let requestSeq = 0

const published = computed(() => versions.value.find(v => v.version_no === props.skill?.published_version_no) ?? null)
const selected = computed(() => versions.value.find(v => v.id === selectedId.value) ?? null)
const diff = computed(() => selected.value ? diffLines((published.value?.content ?? "").split("\n"), selected.value.content.split("\n")) : [])

async function load() {
  const seq = ++requestSeq
  const id = props.skill?.id
  versions.value = []
  selectedId.value = null
  failed.value = false
  loading.value = false
  if (!visible.value || id === undefined) return
  loading.value = true
  try {
    const result = await listSkillVersionsApi(id)
    if (seq !== requestSeq || !visible.value || id !== props.skill?.id) return
    versions.value = result
    // 默认选中最新版本，直接看到草稿相对已发布版本的改动
    selectedId.value = result[0]?.id ?? null
  } catch (error) {
    if (seq === requestSeq) failed.value = true
    throw error
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

watch([visible, () => props.skill?.id, () => props.skill?.latest_version_no], load)
onBeforeUnmount(() => ++requestSeq)

async function publish(version: SkillVersion) {
  const skillId = props.skill!.id
  const seq = requestSeq
  const current = props.skill?.published_version_no
  const action = current !== null && current !== undefined && version.version_no < current ? "回退到" : "发布"
  await ElMessageBox.confirm(`${action} v${version.version_no}？发布后对应用途的下一次任务起使用此版本。`, "发布确认", { type: "warning" })
  sessionScope.assertCurrent()
  if (seq !== requestSeq || !visible.value || skillId !== props.skill?.id)
    throw new CanceledError("查看的 Skill 已切换")
  publishing.value = true
  try {
    await publishSkillVersionApi(skillId, version.id)
    if (seq !== requestSeq) return
    ElMessage.success(`已${action} v${version.version_no}`)
    emit("published")
  } finally {
    publishing.value = false
  }
}
</script>

<template>
  <el-drawer v-model="visible" :title="`版本历史：${skill?.path ?? ''}`" size="60%">
    <el-empty v-if="failed" description="版本历史读取失败">
      <el-button type="primary" @click="load">
        重试
      </el-button>
    </el-empty>
    <div v-else v-loading="loading" class="h-full flex gap-3">
      <el-scrollbar class="w-60 shrink-0">
        <div
          v-for="v in versions" :key="v.id"
          class="mb-2 cursor-pointer border rounded-lg border-solid px-3 py-2 text-sm"
          :class="v.id === selectedId ? 'border-[var(--el-color-primary)] bg-[var(--el-color-primary-light-9)]' : 'border-slate-200 hover:border-slate-300'"
          @click="selectedId = v.id"
        >
          <div class="flex items-center gap-1">
            <b>v{{ v.version_no }}</b>
            <el-tag v-if="v.version_no === skill?.published_version_no" size="small" type="success">
              已发布
            </el-tag>
            <el-tag size="small" :type="v.source === 'chat' ? 'warning' : 'info'">
              {{ v.source === "chat" ? "Chat" : "我" }}
            </el-tag>
          </div>
          <div class="text-xs text-[var(--el-text-color-secondary)]">
            {{ formatTime(v.created_at, "MM-DD HH:mm:ss") }}
          </div>
          <div v-if="v.note" class="truncate text-xs" :title="v.note">
            {{ v.note }}
          </div>
        </div>
      </el-scrollbar>
      <div v-if="selected" class="min-w-0 flex flex-1 flex-col gap-2">
        <div class="flex items-center gap-2">
          <span class="text-sm">v{{ selected.version_no }} 相对已发布版本{{ published ? ` v${published.version_no}` : "（暂无）" }}的改动：</span>
          <el-button
            v-if="selected.version_no !== skill?.published_version_no" type="primary" size="small" class="ml-auto"
            :loading="publishing" @click="publish(selected)"
          >
            发布此版本
          </el-button>
        </div>
        <el-scrollbar class="min-h-0 flex-1 border border-slate-200 rounded-lg border-solid">
          <div class="p-2 text-xs font-mono">
            <div
              v-for="(line, i) in diff" :key="i" class="whitespace-pre-wrap"
              :class="{ 'bg-emerald-50 text-emerald-900': line.type === 'add', 'bg-rose-50 text-rose-900 line-through': line.type === 'del' }"
            >
              {{ line.type === "add" ? "+ " : line.type === "del" ? "- " : "  " }}{{ line.text }}
            </div>
          </div>
        </el-scrollbar>
      </div>
    </div>
  </el-drawer>
</template>
