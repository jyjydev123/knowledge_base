<script setup lang="ts">
/**
 * 对话页（M7 的最后一屏）。
 *
 * 四件与"桌面 demo"不一样的事，这里都按移动端标准处理：
 *   1. **流式**用 fetch + ReadableStream（EventSource 不能带 Authorization 头），
 *      以 done 帧的整段文本落定，累加值只用于打字机效果；
 *   2. **键盘顶起**：Android WebView 里软键盘会把输入框推到看不见的地方，
 *      所以用 visualViewport 的高度实时调 --kb-viewport-bottom，别靠 100vh；
 *   3. **切后台/断网**：流断掉不算错，答案已经落库，回来读 /messages 就能看到 ——
 *      所以这里的重连策略是"提示用户刷新"，而不是假装能续播（后端 Last-Event-ID 还没做，TODO §3）；
 *   4. **停止**要保留半截答案：/stop 之后后端会正常收尾落库，前端别再丢弃已生成的字。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  NButton,
  NDrawer,
  NDrawerContent,
  NEmpty,
  NInput,
  NList,
  NListItem,
  NPopconfirm,
  NSelect,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { chatApi, kbApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import { askStream, describeRetrieval } from '@/api/sse'
import type { ChatOut, RetrievalInfo, SourceItem } from '@/api/types'
import MarkdownBlock from '@/components/MarkdownBlock.vue'
import SourceList from '@/components/SourceList.vue'
import { formatTime } from '@/utils/format'

interface UiMessage {
  key: string
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
  retrieval?: RetrievalInfo
  error?: string
  pending?: boolean
}

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const conversations = useQuery({ queryKey: ['conversations'], queryFn: () => chatApi.list({ size: 50 }) })
const kbs = useQuery({ queryKey: ['kbs', 'all'], queryFn: () => kbApi.list({ scope: 'all', size: 50 }) })

const activeId = ref<string>(String(route.params.conversationId ?? ''))
const messages = ref<UiMessage[]>([])
const loadingHistory = ref(false)
const input = ref('')
const streaming = ref(false)
const drawer = ref(false)
const newKbId = ref<string | null>(null)
const scroller = ref<HTMLElement | null>(null)
const inputBox = ref<HTMLElement | null>(null)
let controller: AbortController | null = null

const kbOptions = computed(() => [
  { label: '全部可读的知识库', value: null as unknown as string },
  ...(kbs.data.value?.items ?? []).map((item) => ({ label: item.name, value: item.kb_id })),
])

const activeTitle = computed(
  () => conversations.data.value?.items.find((item) => item.id === activeId.value)?.title ?? '新对话',
)

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const node = scroller.value
  if (node) node.scrollTop = node.scrollHeight
}

async function loadHistory(id: string): Promise<void> {
  if (!id) {
    messages.value = []
    return
  }
  loadingHistory.value = true
  try {
    const data = await chatApi.history(id, { limit: 100 })
    messages.value = data.items.map((item) => ({
      key: item.id,
      role: item.role,
      content: item.content,
      sources: item.sources ?? [],
    }))
    await scrollToBottom()
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '读取历史失败')
  } finally {
    loadingHistory.value = false
  }
}

// send() 里自己改 activeId 时**不要**去拉历史：新会话历史必然是空的，
// 拉回来的赋值会把刚乐观插入的「问题 + 待回答气泡」整屏抹掉 ——
// 现象就是"发了没反应，刷新才看到自己问过什么"（问题后端其实已经落库了）。
let suppressHistoryFor = ''

watch(activeId, (id) => {
  if (suppressHistoryFor && id === suppressHistoryFor) {
    suppressHistoryFor = ''
    return
  }
  void loadHistory(id)
})

watch(
  () => route.params.conversationId,
  (value) => {
    const next = String(value ?? '')
    if (next !== activeId.value) activeId.value = next
  },
)

async function select(id: string): Promise<void> {
  drawer.value = false
  activeId.value = id
  await router.replace({ name: 'chat-detail', params: { conversationId: id } })
}

async function startNew(): Promise<void> {
  drawer.value = false
  activeId.value = ''
  messages.value = []
  await router.replace({ name: 'chat' })
}

async function removeConversation(item: ChatOut): Promise<void> {
  try {
    await chatApi.remove(item.id)
    message.success('会话已删除')
    if (item.id === activeId.value) await startNew()
    await conversations.refetch()
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '删除失败')
  }
}

async function rename(item: ChatOut): Promise<void> {
  const title = window.prompt('新的会话标题', item.title)
  if (!title || title === item.title) return
  try {
    await chatApi.rename(item.id, title.trim())
    await conversations.refetch()
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '重命名失败')
  }
}

async function send(): Promise<void> {
  const question = input.value.trim()
  if (!question || streaming.value) return

  input.value = ''
  let id = activeId.value
  if (!id) {
    try {
      const created = await chatApi.create(newKbId.value ? { kb_id: newKbId.value } : {})
      id = created.id
      suppressHistoryFor = id          // 这一路的列表由下面的乐观插入负责，别让它去拉空历史
      activeId.value = id
      await router.replace({ name: 'chat-detail', params: { conversationId: id } })
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : '创建会话失败')
      input.value = question
      return
    }
  }

  messages.value.push({ key: `u-${Date.now()}`, role: 'user', content: question, sources: [] })
  messages.value.push({ key: `a-${Date.now()}`, role: 'assistant', content: '', sources: [], pending: true })

  // 必须从数组里**读回**这一个：push 进去的是原始对象，而 ref 渲染读的是它包出来的 proxy。
  // 攥着原始对象写 content 不会触发重渲染，现象是"打字机不动、结束时整段突然贴出来"。
  const holder = messages.value[messages.value.length - 1] as UiMessage
  await scrollToBottom()

  streaming.value = true
  controller = new AbortController()

  await askStream({
    conversationId: id,
    question,
    signal: controller.signal,
    handlers: {
      onMeta: (meta) => {
        holder.retrieval = meta.retrieval
      },
      onToken: (text) => {
        holder.content += text
        void scrollToBottom()
      },
      onReferences: (items) => {
        holder.sources = items
      },
      onDone: (text) => {
        // 以 done 帧的整段文本为准：分块边界/心跳/重连都可能让累加值差一点
        if (text) holder.content = text
        holder.pending = false
      },
      onError: (error) => {
        holder.pending = false
        holder.error = `${error.msg}${error.payload.trace_id ? `（trace ${error.payload.trace_id}）` : ''}`
        if (error.payload.partial_text && !holder.content) holder.content = error.payload.partial_text
      },
    },
  })
    .catch(async (error: unknown) => {
      if ((error as Error).name === 'AbortError') return
      if (error instanceof ApiError) {
        // 推流之前的错误还是正常 HTTP：409=该会话已有回答在跑，503=模型没配
        holder.error = error.message
        message.error(error.message)
      } else {
        holder.error = '连接中断。这一轮如果已经答完，刷新会话就能看到（续播还没实现，TODO §3）'
      }
    })
    .finally(async () => {
      holder.pending = false
      streaming.value = false
      controller = null
      await conversations.refetch()
      void queryClient.invalidateQueries({ queryKey: ['messages'] })
    })
}

async function stop(): Promise<void> {
  if (!activeId.value) return
  try {
    const data = await chatApi.stop(activeId.value)
    message.info(data.stopped ? `已停止（保留 ${data.partial_chars} 字）` : '当前没有在生成的回答')
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '停止失败')
  }
}

/** 键盘顶起是聊天类 WebApp 最高频的真机 bug：不用 100vh，用可视视口的高度 */
function onViewportResize(): void {
  const viewport = window.visualViewport
  if (!viewport) return
  document.documentElement.style.setProperty('--kb-viewport-height', `${viewport.height}px`)
  const offset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)
  document.documentElement.style.setProperty('--kb-keyboard-offset', `${offset}px`)
  void scrollToBottom()
}

onMounted(() => {
  window.visualViewport?.addEventListener('resize', onViewportResize)
  onViewportResize()
  if (activeId.value) void loadHistory(activeId.value)
})

onBeforeUnmount(() => {
  window.visualViewport?.removeEventListener('resize', onViewportResize)
  controller?.abort()
})
</script>

<template>
  <div class="chat">
    <div class="chat__bar">
      <NButton size="small" quaternary @click="drawer = true">☰</NButton>
      <div class="chat__title">{{ activeTitle }}</div>
      <NButton size="small" quaternary @click="startNew">新建</NButton>
    </div>

    <div ref="scroller" class="chat__body kb-page--with-tabs">
      <NSpin :show="loadingHistory">
        <NEmpty v-if="!messages.length && !loadingHistory" description="问点什么吧，答案会带上参考资料" style="margin-top: 40px" />
        <div v-for="row in messages" :key="row.key" class="msg" :class="row.role === 'user' ? 'msg--user' : 'msg--ai'">
          <div class="msg__who">
            <span>{{ row.role === 'user' ? '我' : '助手' }}</span>
            <span v-if="row.retrieval" class="kb-muted">{{ describeRetrieval(row.retrieval) }}</span>
          </div>
          <div class="msg__bubble">
            <MarkdownBlock :content="row.content" />
            <span v-if="row.pending" class="cursor">▍</span>
          </div>
          <SourceList v-if="row.sources.length" :sources="row.sources" />
          <div v-if="row.error" class="msg__error">{{ row.error }}</div>
        </div>
      </NSpin>
    </div>

    <div ref="inputBox" class="chat__input">
      <div v-if="!activeId" class="chat__kb">
        <NSelect v-model:value="newKbId" :options="kbOptions" size="tiny" placeholder="限定知识库（可选）" />
      </div>
      <div class="chat__row">
        <NInput
          v-model:value="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 5 }"
          placeholder="提问，Enter 发送 / Shift+Enter 换行"
          @keydown.enter.exact.prevent="send"
        />
        <NButton v-if="streaming" type="warning" size="small" @click="stop">停止</NButton>
        <NButton v-else type="primary" size="small" :disabled="!input.trim()" @click="send">发送</NButton>
      </div>
    </div>

    <NDrawer v-model:show="drawer" :width="300" placement="left">
      <NDrawerContent title="会话" closable>
        <NButton block style="margin-bottom: 10px" @click="startNew">新建会话</NButton>
        <NEmpty v-if="!conversations.data.value?.items.length" description="还没有会话" />
        <NList v-else bordered>
          <NListItem v-for="item in conversations.data.value.items" :key="item.id">
            <div class="side" @click="select(item.id)">
              <div>
                <div :class="item.id === activeId ? 'side__on' : ''">{{ item.title }}</div>
                <div class="kb-muted">{{ formatTime(item.last_message_at ?? item.created_at) }} · {{ item.message_count }} 条</div>
              </div>
              <div class="kb-row">
                <NButton size="tiny" text @click.stop="rename(item)">改名</NButton>
                <NPopconfirm @positive-click="removeConversation(item)">
                  <template #trigger><NButton size="tiny" text type="error" @click.stop>删</NButton></template>
                  删除会话及其全部消息？
                </NPopconfirm>
              </div>
            </div>
          </NListItem>
        </NList>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
/* --kb-viewport-height / --kb-keyboard-offset 由 onViewportResize 写入，
   没有这两个变量时（桌面浏览器）就退回 100vh */
.chat {
  display: flex;
  flex-direction: column;
  height: var(--kb-viewport-height, 100vh);
  padding-bottom: var(--kb-keyboard-offset, 0);
}

.chat__bar {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  border-bottom: 1px solid var(--kb-border);
  background: var(--kb-card-bg);
}

.chat__title {
  flex: 1;
  text-align: center;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat__body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  padding-bottom: calc(var(--kb-tab-height) + 90px + env(safe-area-inset-bottom));
  -webkit-overflow-scrolling: touch;
}

.msg {
  margin-bottom: 14px;
}

.msg__who {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: var(--kb-muted);
  margin-bottom: 4px;
}

.msg__bubble {
  background: var(--kb-card-bg);
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-radius);
  padding: 10px 12px;
}

.msg--user .msg__bubble {
  background: #e8f5ee;
  border-color: #cfe9dc;
  white-space: pre-wrap;
}

.cursor {
  animation: blink 1s steps(1) infinite;
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

.msg__error {
  margin-top: 6px;
  color: #d03050;
  font-size: 13px;
}

.chat__input {
  position: sticky;
  bottom: calc(var(--kb-tab-height) + env(safe-area-inset-bottom));
  background: var(--kb-page-bg);
  padding: 8px 12px;
  border-top: 1px solid var(--kb-border);
}

.chat__kb {
  margin-bottom: 6px;
}

.chat__row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.side {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
}

.side__on {
  color: #18a058;
  font-weight: 600;
}
</style>
