/**
 * SSE 客户端：fetch + ReadableStream，不用 EventSource。
 *
 * 为什么不用 EventSource（浏览器内置那个）：
 *   1. 它**不能带 Authorization 头**，而本项目的身份全在 header 里；
 *   2. 它只有 GET，而问答是 POST（问题在请求体里）；
 *   3. 它对"连接被代理掐了"的处理是自动重连，会让我们看到重复的流。
 *
 * 帧协议（后端 app/rag/output/sse.py 是唯一真源）：
 *   event: meta | token | references | done | error
 *   id: <帧序号>          ← 断线续传靠它，续传本身后端还没做（TODO §3）
 *   data: {"status":200,"msg":"...","data":{...}}
 *   另有一种 `: ping` 注释帧（心跳），**不触发任何事件**，解析时必须跳过。
 *
 * 两条容易写错的规矩：
 *   - `done` 的 data.text 是**整段答案**，落定气泡以它为准，不要用累加出来的那份
 *     （分块边界、心跳、重连都可能让累加值差一点）；累加只用于打字机效果。
 *   - HTTP 状态码在流开始后必然是 200，所以"出错"只有一种形式：`error` 帧。
 *     异常原文不进流，只给 trace_id，界面把它显示出来方便报障。
 */

import { API_BASE, ApiError } from './client'
import { getToken } from '@/utils/storage'
import type { ChatAnswerOut, Envelope, RetrievalInfo, SourceItem, StreamErrorPayload, StreamMeta } from './types'

export interface StreamHandlers {
  onMeta?: (meta: StreamMeta) => void
  onToken?: (text: string) => void
  onReferences?: (items: SourceItem[]) => void
  onDone?: (text: string) => void
  onError?: (error: { status: number; msg: string; payload: StreamErrorPayload }) => void
}

export interface AskStreamOptions {
  conversationId: string
  question: string
  handlers: StreamHandlers
  signal?: AbortSignal
  topK?: number
  writeMemory?: boolean
}

/** 把 SSE 文本块按空行切成帧，逐字段解析。独立出来才能单测（前端没有测试框架时至少能手动喂样本） */
export function parseSseChunk(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = []
  const parts = buffer.split('\n\n')
  const rest = parts.pop() ?? ''
  for (const raw of parts) {
    const frame = parseFrame(raw)
    if (frame) frames.push(frame)
  }
  return { frames, rest }
}

export interface SseFrame {
  event: string
  id?: string
  data?: string
}

function parseFrame(block: string): SseFrame | null {
  const frame: SseFrame = { event: 'message' }
  let touched = false
  for (const line of block.split('\n')) {
    if (!line) continue
    if (line.startsWith(':')) continue // 心跳/注释帧
    if (line.startsWith('event:')) {
      frame.event = line.slice(6).trim()
      touched = true
    } else if (line.startsWith('id:')) {
      frame.id = line.slice(3).trim()
      touched = true
    } else if (line.startsWith('data:')) {
      // 多行 data 要按 SSE 规范用 \n 拼接（本项目只有单行，但按规矩写才不出意外）
      frame.data = frame.data ? `${frame.data}\n${line.slice(5).trim()}` : line.slice(5).trim()
      touched = true
    }
  }
  return touched ? frame : null
}

export async function askStream(options: AskStreamOptions): Promise<void> {
  const { conversationId, question, handlers, signal, topK, writeMemory } = options
  const token = getToken()

  const response = await fetch(`${API_BASE}/aichat/conversations/${conversationId}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      question,
      stream: true,
      ...(topK ? { top_k: topK } : {}),
      ...(writeMemory === undefined ? {} : { write_memory: writeMemory }),
    }),
    signal,
  })

  // 推流**之前**的错误还是正常 HTTP 状态码（会话不存在、没配模型、该会话已有回答在跑），
  // 这段必须处理掉，否则用户只会看到一个永远空白的回答气泡
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const msg = (body as { msg?: string } | null)?.msg ?? `请求失败（HTTP ${response.status}）`
    throw new ApiError(response.status, msg, (body as { data?: unknown } | null)?.data)
  }
  if (!response.body) throw new ApiError(0, '浏览器没有给到可读的响应流（可能是代理把 SSE 缓冲了）')

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // Windows 换行的代理也会用 \r\n\r\n 切帧，先归一化，否则一帧都解不出来
    const { frames, rest } = parseSseChunk(buffer.replace(/\r\n/g, '\n'))
    buffer = rest
    for (const frame of frames) dispatch(frame, handlers)
  }
  // 收尾：最后一帧后面没有空行时，把残留再解一次（不然 done 帧可能永远不触发）
  const tail = parseSseChunk(`${buffer.replace(/\r\n/g, '\n')}\n\n`)
  for (const frame of tail.frames) dispatch(frame, handlers)
}

function dispatch(frame: SseFrame, handlers: StreamHandlers): void {
  if (!frame.data) return
  let envelope: Envelope<unknown>
  try {
    envelope = JSON.parse(frame.data)
  } catch {
    return // 解不出 JSON 的帧直接丢，别把整条流带崩
  }

  switch (frame.event) {
    case 'meta':
      handlers.onMeta?.(envelope.data as StreamMeta)
      break
    case 'token':
      handlers.onToken?.(((envelope.data as { text?: string }) ?? {}).text ?? '')
      break
    case 'references':
      handlers.onReferences?.(((envelope.data as { items?: SourceItem[] }) ?? {}).items ?? [])
      break
    case 'done':
      handlers.onDone?.(((envelope.data as { text?: string }) ?? {}).text ?? '')
      break
    case 'error':
      handlers.onError?.({
        status: envelope.status,
        msg: envelope.msg,
        payload: (envelope.data as StreamErrorPayload) ?? {},
      })
      break
    default:
      break
  }
}

/** meta.retrieval.mode 的人话说明，界面直接摆出来：向量车道挂了会退回关键词，这个必须让用户看得见 */
export function describeRetrieval(retrieval?: RetrievalInfo): string {
  if (!retrieval) return ''
  const modeText: Record<string, string> = {
    keyword: '关键词检索',
    vector: '语义检索',
    hybrid: '语义 + 关键词',
    none: '没找到相关资料',
    no_scope: '这个会话没限定可读的知识库',
    no_terms: '问题里提不出检索词',
    disabled: '检索已关闭',
  }
  const base = modeText[retrieval.mode] ?? retrieval.mode
  const hits = retrieval.hits ?? 0
  if (retrieval.fallback) return `${base}（向量车道不可用，已退回：${retrieval.fallback}）`
  if (hits) return `${base} · ${hits} 条资料 / ${retrieval.candidates ?? 0} 个候选`
  return base
}

export type { ChatAnswerOut }
