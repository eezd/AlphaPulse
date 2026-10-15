<script lang="ts" setup>
import type { ChatContext } from "@/apis/chat"
import { getChatContext, listChatContextVersions, saveChatContext } from "@/apis/chat"
import { renderMarkdown } from "@/utils/markdown"
import { formatTime } from "@/utils/text"

const SOURCE_LABELS = { user: "我", chat: "Chat", reset: "Reset 清空" } as const

const current = ref<ChatContext | null>(null)
const content = ref("")
const versions = ref<ChatContext[]>([])
const saving = ref(false)
const dirty = computed(() => content.value !== (current.value?.content ?? ""))
const preview = computed(() => renderMarkdown(content.value))

async function load() {
  current.value = await getChatContext()
  content.value = current.value.content
  versions.value = await listChatContextVersions()
}

async function save() {
  saving.value = true
  try {
    await saveChatContext(content.value)
    ElMessage.success("已保存")
    await load()
  } finally {
    saving.value = false
  }
}

async function restore(version: ChatContext) {
  if (dirty.value)
    await ElMessageBox.confirm("当前有未保存的修改，载入旧版本会覆盖编辑区。继续？", "未保存", { type: "warning" })
  // 只载入编辑区，保存后才成为新的当前版本
  content.value = version.content
}

/** Chat 更新了 CONTEXT.md：编辑区没有未保存修改时直接刷新 */
async function refreshFromChat() {
  if (dirty.value) {
    versions.value = await listChatContextVersions()
    ElMessage.warning("Chat 更新了 CONTEXT.md，你有未保存的修改，未自动刷新")
    return
  }
  await load()
}

defineExpose({ refreshFromChat, load, dirty })
load()
</script>

<template>
  <el-card shadow="never" class="h-full flex flex-col" body-class="min-h-0 flex-1 flex flex-col gap-3">
    <div class="flex items-center gap-2 text-sm">
      <span class="text-slate-500">
        跨会话的上下文持久化：每轮都会放进 Chat 的 prompt，Chat 也会用 update_context 更新它。
        {{ current?.created_at ? `当前版本：${formatTime(current.created_at)}（${SOURCE_LABELS[current.source!]}）` : "当前为空" }}
      </span>
      <el-tag v-if="dirty" type="danger" size="small">
        有未保存的修改
      </el-tag>
      <el-dropdown class="ml-auto" trigger="click" @command="restore">
        <el-button size="small">
          历史版本（{{ versions.length }}）
        </el-button>
        <template #dropdown>
          <el-dropdown-menu class="max-h-96 overflow-auto">
            <el-dropdown-item v-for="v in versions" :key="v.id!" :command="v">
              {{ formatTime(v.created_at, "MM-DD HH:mm:ss") }} · {{ SOURCE_LABELS[v.source!] }} · {{ v.content.length }} 字
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button size="small" type="primary" :disabled="!dirty" :loading="saving" @click="save">
        保存
      </el-button>
    </div>
    <div class="min-h-0 flex flex-1 gap-3">
      <textarea
        v-model="content" placeholder="目标、已确认的结论、待办…（Markdown）"
        class="box-border flex-1 resize-none border border-slate-300 rounded-lg border-solid p-3 text-sm text-slate-800 leading-6 font-mono outline-none focus:border-[var(--el-color-primary)]"
      />
      <el-scrollbar class="flex-1 border border-slate-200 rounded-lg border-solid bg-slate-50/50">
        <div class="markdown-body p-3" v-html="preview" />
      </el-scrollbar>
    </div>
  </el-card>
</template>
