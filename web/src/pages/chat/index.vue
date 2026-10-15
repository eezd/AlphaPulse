<script lang="ts" setup>
import type { ChatMessage } from "@/apis/chat"
import type { Effort, Provider } from "@/apis/settings"
import type { SkillNode } from "@/apis/skills"
import type { DisplayItem } from "@/pages/chat/components/MessageList.vue"
import { ElNotification } from "element-plus"
import { getChatSession, resetChat } from "@/apis/chat"
import { EFFORTS, listProviders } from "@/apis/settings"
import { listSkillTree } from "@/apis/skills"
import SkillTree from "@/components/SkillTree.vue"
import SkillVersionDrawer from "@/components/SkillVersionDrawer.vue"
import { useChatStream } from "@/composables/useChatStream"
import ContextTab from "@/pages/chat/components/ContextTab.vue"
import MessageList from "@/pages/chat/components/MessageList.vue"
import { usePersisted } from "@/utils/storage"

const router = useRouter()
const activeTab = ref<"chat" | "context">("chat")
const contextTab = ref<InstanceType<typeof ContextTab>>()

// ---------- 模型与会话 ----------
const providers = ref<Provider[]>([])
const providerId = usePersisted<number | null>("chat.providerId", null)
const provider = computed(() => providers.value.find(p => p.id === providerId.value))
// 为空表示跟随模型配置里的默认值
const effort = usePersisted<Effort | null>("chat.effort", null)

const messages = ref<ChatMessage[]>([])
const estimatedTokens = ref(0)
// 正在生成的这一轮，完成后以服务端会话为准重新加载
const liveItems = ref<DisplayItem[]>([])
const input = ref("")
const listBox = ref<HTMLElement>()
const { streaming, send, stop } = useChatStream()

/** 把会话消息转成展示项：工具结果挂到对应的工具调用上 */
function toItems(list: ChatMessage[]): DisplayItem[] {
  const items: DisplayItem[] = []
  const calls = new Map<string, DisplayItem["toolCalls"][number]>()
  for (const m of list) {
    if (m.role === "user") {
      items.push({ key: `m${m.id}`, kind: "user", text: m.content.text, toolCalls: [] })
    } else if (m.role === "assistant") {
      const toolCalls = (m.content.tool_calls ?? []).map((c: any) => ({ ...c, result: null }))
      toolCalls.forEach((c: any) => calls.set(c.id, c))
      items.push({ key: `m${m.id}`, kind: "assistant", text: m.content.text, toolCalls, stopped: m.content.stopped, error: m.content.error })
    } else {
      const call = calls.get(m.content.tool_call_id)
      if (call)
        call.result = m.content.result
    }
  }
  return items
}

const items = computed(() => [...toItems(messages.value), ...liveItems.value])
const tokenRatio = computed(() => provider.value ? estimatedTokens.value / provider.value.context_window : 0)

async function scrollToBottom() {
  await nextTick()
  listBox.value?.scrollTo({ top: listBox.value.scrollHeight })
}

const RELOAD_RETRY_MS = 300
const RELOAD_RETRIES = 10

async function loadSession() {
  let session = await getChatSession()
  // 停止生成后服务端还要片刻才落库部分回复：等它结束再以服务端为准
  for (let i = 0; session.generating && i < RELOAD_RETRIES; i++) {
    await new Promise(resolve => setTimeout(resolve, RELOAD_RETRY_MS))
    session = await getChatSession()
  }
  messages.value = session.messages
  estimatedTokens.value = session.estimated_tokens
  liveItems.value = []
  scrollToBottom()
}

async function loadProviders() {
  providers.value = await listProviders()
  if (!provider.value)
    providerId.value = providers.value.find(p => p.is_default)?.id ?? providers.value[0]?.id ?? null
}

// ---------- 发送与流式事件 ----------
function currentAssistant(): DisplayItem {
  const last = liveItems.value.at(-1)
  if (last?.kind === "assistant" && !last.closed)
    return last
  const item: DisplayItem = { key: `live${Date.now()}${liveItems.value.length}`, kind: "assistant", text: "", toolCalls: [] }
  liveItems.value.push(item)
  return liveItems.value.at(-1)!
}

function onEvent(name: string, data: any) {
  if (name === "prompt") {
    estimatedTokens.value = data.estimated_tokens
    // OpenAI 格式推理期间没有任何推送，先显示等待状态直到出字
    currentAssistant().thinking = true
  } else if (name === "thinking") {
    currentAssistant().thinking = true
  } else if (name === "delta") {
    const item = currentAssistant()
    item.thinking = false
    item.text += data.text
  } else if (name === "usage") {
    // 一次模型调用结束；之后的工具调用挂在这条回复上，新的文字另起一条
    const item = currentAssistant()
    item.closed = true
    item.thinking = false
  } else if (name === "tool_call") {
    const last = [...liveItems.value].reverse().find(i => i.kind === "assistant") ?? currentAssistant()
    last.toolCalls.push({ id: data.id, name: data.name, arguments: data.arguments, result: null })
  } else if (name === "tool_result") {
    for (const item of liveItems.value) {
      const call = item.toolCalls.find(c => c.id === data.id)
      if (call)
        call.result = data.preview
    }
    // 工具都返回后进入下一次模型调用
    const last = liveItems.value.at(-1)
    if (last?.closed && last.toolCalls.every(c => c.result !== null))
      currentAssistant().thinking = true
  } else if (name === "skill_draft") {
    highlightIds.value = [...new Set([...highlightIds.value, data.skill_id])]
    loadSkills()
    ElNotification({ title: "Chat 生成了 skill 草稿", message: `${data.path} v${data.version}，需审阅并发布后才生效`, type: "warning" })
  } else if (name === "context_updated") {
    contextTab.value?.refreshFromChat()
  } else if (name === "error") {
    const item = currentAssistant()
    item.thinking = false
    item.error = data.message
  }
  scrollToBottom()
}

async function submit() {
  const text = input.value.trim()
  if (!text || streaming.value)
    return
  if (!provider.value) {
    ElMessage.warning("请先到「环境变量」页面添加模型配置")
    return
  }
  input.value = ""
  liveItems.value = [{ key: `live-user${Date.now()}`, kind: "user", text, toolCalls: [] }]
  scrollToBottom()
  await send({ text, provider_id: provider.value.id, reasoning_effort: effort.value }, onEvent)
  // 无论完成、出错还是停止，都以服务端保存的会话为准；保留本地的错误提示
  const errors = liveItems.value.filter(i => i.error)
  await loadSession()
  liveItems.value = errors.map(e => ({ ...e, text: "", toolCalls: [], thinking: false }))
}

function onKeydown(e: Event | KeyboardEvent) {
  if (e instanceof KeyboardEvent && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault()
    submit()
  }
}

// ---------- Reset ----------
const resetVisible = ref(false)
const clearContext = ref(false)

async function doReset() {
  await resetChat(clearContext.value)
  resetVisible.value = false
  ElMessage.success(clearContext.value ? "已清空对话和 CONTEXT.md" : "已清空对话，CONTEXT.md 保留")
  clearContext.value = false
  await loadSession()
  await contextTab.value?.load()
}

// ---------- 侧边栏 skills ----------
const skills = ref<SkillNode[]>([])
const highlightIds = ref<number[]>([])
const drawerSkill = ref<SkillNode | null>(null)
const drawerVisible = ref(false)

async function loadSkills() {
  skills.value = await listSkillTree()
  if (drawerSkill.value)
    drawerSkill.value = skills.value.find(s => s.id === drawerSkill.value!.id) ?? null
}

function openSkill(node: SkillNode) {
  if (node.kind !== "skill")
    return
  drawerSkill.value = node
  drawerVisible.value = true
  highlightIds.value = highlightIds.value.filter(id => id !== node.id)
}

async function onPublished() {
  drawerVisible.value = false
  await loadSkills()
  // 发布后 prompt 变了，刷新估算
  estimatedTokens.value = (await getChatSession()).estimated_tokens
}

onBeforeRouteLeave(async () => {
  if (streaming.value)
    await ElMessageBox.confirm("正在生成，离开页面会停止生成。继续？", "提示", { type: "warning" })
  if (contextTab.value?.dirty)
    await ElMessageBox.confirm("CONTEXT.md 有未保存的修改，离开后会丢失。继续？", "未保存", { type: "warning" })
})

loadProviders()
loadSession()
loadSkills()
</script>

<template>
  <div class="h-full flex flex-col">
    <el-tabs v-model="activeTab" class="chat-tabs min-h-0 flex flex-1 flex-col">
      <el-tab-pane label="Chat" name="chat" class="h-full">
        <div class="h-full flex gap-3">
          <el-card class="w-72 shrink-0" body-class="h-full box-border flex flex-col gap-2" shadow="never">
            <div class="flex items-center justify-between text-sm font-bold">
              Skills
              <el-button link size="small" @click="router.push('/skills')">
                去管理
              </el-button>
            </div>
            <div class="text-xs text-[var(--el-text-color-secondary)]">
              点击查看草稿与已发布版本的差异并发布；黄色为本次 Chat 刚改过的
            </div>
            <div class="min-h-0 flex-1">
              <SkillTree :nodes="skills" :highlight-ids="highlightIds" :selected-id="drawerSkill?.id" @select="openSkill" />
            </div>
          </el-card>

          <el-card class="min-w-0 flex flex-1 flex-col" body-class="min-h-0 flex-1 flex flex-col gap-3" shadow="never">
            <div class="flex flex-wrap items-center gap-3">
              <el-select v-model="providerId" placeholder="选择模型配置" class="w-60" :disabled="streaming">
                <el-option v-for="p in providers" :key="p.id" :value="p.id" :label="`${p.name}（${p.model}）`" />
              </el-select>
              <el-select v-model="effort" class="w-44" :disabled="streaming" clearable :value-on-clear="null" :placeholder="`思考强度：跟随配置（${provider?.reasoning_effort ?? '不设置'}）`">
                <el-option v-for="e in EFFORTS" :key="e" :value="e" :label="`思考强度：${e}`" />
              </el-select>
              <el-tooltip content="下一轮发给模型的 system prompt（含已发布 skills、CONTEXT.md）与历史消息的估算大小" placement="bottom">
                <span class="text-xs" :class="tokenRatio > 0.8 ? 'text-red-500 font-bold' : 'text-[var(--el-text-color-secondary)]'">
                  上下文约 {{ estimatedTokens.toLocaleString() }}{{ provider ? ` / ${provider.context_window.toLocaleString()}` : "" }} token
                </span>
              </el-tooltip>
              <el-button class="ml-auto" type="danger" plain :disabled="streaming" @click="resetVisible = true">
                Reset
              </el-button>
            </div>

            <div ref="listBox" class="min-h-0 flex-1 overflow-auto pr-2">
              <el-empty v-if="items.length === 0" description="提问，例如：长鑫 4F² 架构对三星、海力士有冲击吗？在哪里抄底、止损？" />
              <MessageList v-else :items="items" />
            </div>

            <div class="flex items-end gap-2">
              <el-input
                v-model="input" type="textarea" :autosize="{ minRows: 2, maxRows: 8 }" resize="none"
                placeholder="输入问题，Ctrl+Enter 发送" @keydown="onKeydown"
              />
              <el-button v-if="streaming" type="warning" @click="stop">
                停止
              </el-button>
              <el-button v-else type="primary" :disabled="!input.trim()" @click="submit">
                发送
              </el-button>
            </div>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="CONTEXT.md" name="context" class="h-full">
        <ContextTab ref="contextTab" />
      </el-tab-pane>
    </el-tabs>

    <SkillVersionDrawer v-model="drawerVisible" :skill="drawerSkill" @published="onPublished" />

    <el-dialog v-model="resetVisible" title="Reset 清除上下文" width="440px">
      <p class="mt-0">
        清空当前对话，开始新的会话（旧对话保留在数据库中，不再展示）。skills 不受影响。
      </p>
      <el-checkbox v-model="clearContext">
        同时清空 CONTEXT.md（会保存为一个空版本，可在历史版本中找回）
      </el-checkbox>
      <template #footer>
        <el-button @click="resetVisible = false">
          取消
        </el-button>
        <el-button type="danger" @click="doReset">
          Reset
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
}
</style>
