<script lang="ts" setup>
import type { ChatMessage, ChatStreamHandler } from "@@/apis/chat/type"
import type { Effort, Provider } from "@@/apis/settings/type"
import type { SkillNode } from "@@/apis/skills/type"
import type { DisplayItem } from "./type"
import { getChatSessionApi, resetChatApi } from "@@/apis/chat"
import { listProvidersApi } from "@@/apis/settings"
import { listSkillTreeApi } from "@@/apis/skills"
import SkillTree from "@@/components/SkillTree/index.vue"
import SkillVersionDrawer from "@@/components/SkillVersionDrawer/index.vue"
import { useSessionScope } from "@@/composables/useSessionScope"
import { EFFORTS } from "@@/constants/ai"
import { usePersisted } from "@@/utils/storage"
import { RefreshLeft } from "@element-plus/icons-vue"
import { ElNotification } from "element-plus"
import { logRequestError } from "@/http/axios"
import { streamCanceled } from "@/http/stream"
import { useUserStore } from "@/pinia/stores/user"
import ContextTab from "./components/ContextTab.vue"
import MessageList from "./components/MessageList.vue"
import { useChatStream } from "./composables/useChatStream"
import { toItems } from "./message-items"

const sessionScope = useSessionScope()

const router = useRouter()
const skillsVisible = ref(false)
const resetting = ref(false)
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

const items = computed(() => [...toItems(messages.value), ...liveItems.value])
const tokenRatio = computed(() => provider.value ? estimatedTokens.value / provider.value.context_window : 0)

async function scrollToBottom() {
  await nextTick()
  listBox.value?.scrollTo({ top: listBox.value.scrollHeight })
}

const RELOAD_RETRY_MS = 300
const RELOAD_RETRIES = 10

async function loadSession() {
  let session = await getChatSessionApi()
  // 停止生成后服务端还要片刻才落库部分回复：等它结束再以服务端为准
  for (let i = 0; session.generating && i < RELOAD_RETRIES; i++) {
    await new Promise(resolve => setTimeout(resolve, RELOAD_RETRY_MS))
    sessionScope.assertCurrent()
    session = await getChatSessionApi()
  }
  messages.value = session.messages
  estimatedTokens.value = session.estimated_tokens
  liveItems.value = []
  scrollToBottom()
}

async function loadProviders() {
  providers.value = await listProvidersApi()
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

const onEvent: ChatStreamHandler = (event) => {
  const { name, data } = event
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
    void loadSkills().catch(logRequestError)
    ElNotification({ title: "Chat 生成了 skill 草稿", message: `${data.path} v${data.version}，需审阅并发布后才生效`, type: "warning" })
  } else if (name === "context_updated") {
    void contextTab.value?.refreshFromChat().catch(logRequestError)
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
  const version = useUserStore().sessionVersion
  try {
    await send({ text, provider_id: provider.value.id, reasoning_effort: effort.value }, onEvent)
  } catch (error) {
    // 流的失败已显示在聊天区；取消仍需读取服务端保存的部分回复。
    if (version !== useUserStore().sessionVersion) return
    if (!streamCanceled(error)) return
  }
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
  if (resetting.value) return
  resetting.value = true
  try {
    await resetChatApi(clearContext.value)
    resetVisible.value = false
    ElMessage.success(clearContext.value ? "已清空对话和 CONTEXT.md" : "已清空对话，CONTEXT.md 保留")
    clearContext.value = false
    await loadSession()
    await contextTab.value?.load()
  } finally {
    resetting.value = false
  }
}

// ---------- 侧边栏 skills ----------
const skills = ref<SkillNode[]>([])
const highlightIds = ref<number[]>([])
const drawerSkill = ref<SkillNode | null>(null)
const drawerVisible = ref(false)

async function loadSkills() {
  skills.value = await listSkillTreeApi()
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
  estimatedTokens.value = (await getChatSessionApi()).estimated_tokens
}

onBeforeRouteLeave(async () => {
  if (!sessionScope.isCurrent()) return true
  if (!useUserStore().token) return true
  if (streaming.value)
    await ElMessageBox.confirm("正在生成，离开页面会停止生成。继续？", "提示", { type: "warning" })
  sessionScope.assertCurrent()
  if (contextTab.value?.dirty)
    await ElMessageBox.confirm("CONTEXT.md 有未保存的修改，离开后会丢失。继续？", "未保存", { type: "warning" })
  sessionScope.assertCurrent()
})

onMounted(() => Promise.all([loadProviders(), loadSession(), loadSkills()]))
</script>

<template>
  <div class="h-full flex flex-col gap-3">
    <div class="flex items-center gap-3">
      <el-segmented
        v-model="activeTab" :options="[{ label: 'Chat', value: 'chat' }, { label: 'CONTEXT.md', value: 'context' }]"
        class="self-start"
      />
      <el-button v-if="activeTab === 'chat'" size="small" @click="skillsVisible = !skillsVisible">
        {{ skillsVisible ? '收起 Skills' : '查看 Skills' }}
      </el-button>
    </div>

    <div v-show="activeTab === 'chat'" class="chat-workspace min-h-0 flex flex-1 flex-col gap-3 overflow-auto lg:flex-row">
      <el-card v-if="skillsVisible" class="chat-skills max-h-60 w-full flex shrink-0 flex-col lg:max-h-none lg:w-64" body-class="min-h-0 flex-1 flex flex-col gap-2" shadow="never">
        <template #header>
          <div class="flex items-center justify-between">
            Skills
            <el-button link type="primary" size="small" @click="router.push('/skills')">
              去管理
            </el-button>
          </div>
        </template>
        <div class="text-xs text-slate-500">
          点击查看草稿与已发布版本的差异并发布；黄色为本次 Chat 刚改过的
        </div>
        <div class="min-h-0 flex-1">
          <SkillTree :nodes="skills" :highlight-ids="highlightIds" :selected-id="drawerSkill?.id" @select="openSkill" />
        </div>
      </el-card>

      <el-card class="min-w-0 flex flex-1 flex-col" body-class="min-h-0 flex-1 flex flex-col gap-3" shadow="never">
        <template #header>
          <div class="flex flex-wrap items-center gap-3 font-normal">
            <span class="text-xs text-slate-500">模型</span>
            <el-select v-model="providerId" placeholder="选择模型配置" size="small" class="w-56" :disabled="streaming">
              <el-option v-for="p in providers" :key="p.id" :value="p.id" :label="`${p.name}（${p.model}）`" />
            </el-select>
            <span class="text-xs text-slate-500">思考强度</span>
            <el-select v-model="effort" size="small" class="w-40" :disabled="streaming" clearable :value-on-clear="null" :placeholder="`跟随配置（${provider?.reasoning_effort ?? '不设置'}）`">
              <el-option v-for="e in EFFORTS" :key="e" :value="e" :label="e" />
            </el-select>
            <div class="flex-1" />
            <el-tooltip content="下一轮发给模型的 system prompt（含已发布 skills、CONTEXT.md）与历史消息的估算大小" placement="bottom">
              <div class="w-56 text-xs" :class="tokenRatio > 0.8 ? 'text-rose-600 font-bold' : 'text-slate-500'">
                <div class="num text-right">
                  上下文约 {{ estimatedTokens.toLocaleString() }}{{ provider ? ` / ${provider.context_window.toLocaleString()}` : "" }} token
                </div>
                <el-progress
                  :percentage="Math.min(100, tokenRatio * 100)" :show-text="false" :stroke-width="4"
                  :status="tokenRatio > 0.8 ? 'exception' : undefined" class="mt-1"
                />
              </div>
            </el-tooltip>
            <el-button type="danger" plain size="small" :icon="RefreshLeft" :disabled="streaming" @click="resetVisible = true">
              Reset
            </el-button>
          </div>
        </template>

        <div ref="listBox" class="min-h-0 flex-1 overflow-auto pr-2">
          <el-empty v-if="items.length === 0" description="提问，例如：长鑫 4F² 架构对三星、海力士有冲击吗？在哪里抄底、止损？" />
          <MessageList v-else :items="items" />
        </div>

        <div class="chat-input border border-slate-300 rounded-lg border-solid bg-white">
          <el-input
            v-model="input" type="textarea" :autosize="{ minRows: 2, maxRows: 8 }" resize="none"
            placeholder="输入问题，Ctrl+Enter 发送" @keydown="onKeydown"
          />
          <div class="flex items-center gap-2 border-t border-t-slate-100 border-t-solid px-3 py-2">
            <span class="text-xs text-slate-400">Ctrl+Enter 发送</span>
            <div class="flex-1" />
            <el-button v-if="streaming" type="warning" @click="stop">
              停止
            </el-button>
            <el-button v-else type="primary" :disabled="!input.trim()" @click="submit">
              发送
            </el-button>
          </div>
        </div>
      </el-card>
    </div>

    <div v-show="activeTab === 'context'" class="min-h-0 flex-1">
      <ContextTab ref="contextTab" />
    </div>

    <SkillVersionDrawer v-model="drawerVisible" :skill="drawerSkill" @published="onPublished" />

    <el-dialog v-model="resetVisible" title="Reset 清除上下文" width="min(440px, 94vw)" :close-on-click-modal="!resetting" :close-on-press-escape="!resetting" :show-close="!resetting">
      <p class="mt-0">
        清空当前对话，开始新的会话（旧对话保留在数据库中，不再展示）。skills 不受影响。
      </p>
      <el-checkbox v-model="clearContext">
        同时清空 CONTEXT.md（会保存为一个空版本，可在历史版本中找回）
      </el-checkbox>
      <template #footer>
        <el-button :disabled="resetting" @click="resetVisible = false">
          取消
        </el-button>
        <el-button type="danger" :loading="resetting" @click="doReset">
          Reset
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-input:focus-within {
  border-color: var(--el-color-primary);
}
.chat-input :deep(.el-textarea__inner) {
  padding: 10px 12px;
  box-shadow: none;
}
</style>
