/**
 * 后端返回体的 TS 形状。
 *
 * 为什么先手写一层而不是直接上 openapi-typescript 生成的 schema.d.ts：那条路要求
 * 「先跑 scripts/export_openapi.py 再 build」，新克隆的仓库少跑一步就整个编译不过去。
 * 所以这里只覆盖前端真正读到的字段，接口定稿后换成生成类型即可（导出脚本已留好口子）。
 *
 * 字段名与后端 Pydantic 一一对应、保持 snake_case，刻意**不做驼峰转换**：
 * 转换层看着漂亮，但排查时"浏览器 Network 里的名字和代码里的不一样"这种落差很费时间。
 */

export interface Envelope<T = unknown> {
  status: number
  msg: string
  data: T
}

export interface PageData<T> {
  items: T[]
  total: number
  page: number
  size: number
}

export interface UserResponse {
  id: string
  username: string
  email: string
  role: 'user' | 'admin'
  is_active: boolean
  created_at?: string | null
  last_login_at?: string | null
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
  user?: UserResponse | null
}

/** 知识库一行。permission / is_owner 是按钮可用性的唯一依据，别自己去比 owner_id */
export interface KbOut {
  kb_id: string
  name: string
  description: string
  visibility: 'private' | 'public'
  owner_id: string
  is_owner: boolean
  permission: 'read' | 'write'
  document_count: number
  chunk_count: number
  version: number
  last_indexed_at?: string | null
  chunk_size: number
  chunk_overlap: number
  created_at?: string | null
  updated_at?: string | null
}

export interface KbDeleteOut {
  kb_id: string
  document_count: number
  files_removed: number
  files_missing: number
  vectors_removed: number
}

export interface KbShareOut {
  share_id: string
  kb_id: string
  grantee_id: string
  grantee_username: string
  grantee_email: string
  permission: 'read' | 'write'
  created_at?: string | null
}

export type DocStatus = 'stored' | 'pending' | 'parsing' | 'chunking' | 'indexed' | 'failed'

export interface DocumentResponse {
  id: string
  kb_id: string
  title: string
  filename: string
  file_size: number
  mime_type?: string | null
  status: DocStatus
  chunk_count?: number
  token_count?: number
  error_msg?: string | null
  download_url?: string
  created_at?: string | null
  updated_at?: string | null
}

export interface DocumentChunk {
  id: string
  seq: number
  content: string
  page?: number | null
  heading?: string | null
  token_count?: number
}

export interface DocumentDetail extends DocumentResponse {
  kb_name?: string
  kb_version?: number
  chunk_total?: number
  chunks: DocumentChunk[]
}

export interface DocumentStatusOut {
  kb_id: string
  kb_version: number
  last_indexed_at?: string | null
  document_total: number
  status_counts: Record<string, number>
  disk_file_count: number
  missing_files: string[]
  orphan_files: string[]
  healthy: boolean
}

/** 一条引用来源。车道不同 score 量纲也不同（词频分 vs 0~1 相似度），别拿它做绝对阈值 */
export interface SourceItem {
  index: number
  document_id: string
  chunk_id: string
  kb_id: string
  chunk_seq: number
  page?: number | null
  heading?: string | null
  title: string
  score: number
  lane?: 'keyword' | 'vector' | 'both' | ''
  snippet: string
}

/** 一次检索的自述：mode 是"真正给出资料的那条车道"，lane 是"配置要求走的那条" */
export interface RetrievalInfo {
  mode: string
  lane?: string
  terms?: string[]
  candidates?: number
  hits?: number
  chars?: number
  fallback?: string
}

export interface ChatOut {
  id: string
  kb_id?: string | null
  title: string
  message_count: number
  token_count: number
  has_summary: boolean
  summary_chars: number
  last_message_at?: string | null
  created_at?: string | null
}

export interface MessageOut {
  id: string
  session_id: string
  seq: number
  role: 'user' | 'assistant'
  content: string
  token_count: number
  sources: SourceItem[]
  created_at?: string | null
}

export interface ChatHistoryOut {
  session: ChatOut
  items: MessageOut[]
  total: number
  limit: number
  before_seq?: number | null
  has_more: boolean
}

export interface ChatAnswerOut {
  session_id: string
  answer: string
  sources: SourceItem[]
  retrieval_mode: string
  stopped: boolean
  turn: Record<string, unknown>
}

export interface ChatStopOut {
  session_id: string
  stopped: boolean
  generation_id?: string | null
  partial_chars: number
}

export interface AgentOut {
  key: string
  name: string
  description: string
  needs_kb: boolean
  supports_stream: boolean
  available: boolean
  unavailable_reason?: string | null
}

export interface SearchOut {
  query: string
  mode: string
  lane: string
  fallback: string
  candidates: number
  scoped_kb_ids: string[]
  hits: SourceItem[]
}

/** SSE meta 帧载荷（对应 app/rag/answer.py 里 TurnPlan.meta() 那份 dict） */
export interface StreamMeta {
  session_id?: string
  kb_id?: string | null
  model?: string
  retrieval?: RetrievalInfo
  context_tokens?: number
  generation_id?: string
}

export interface StreamErrorPayload {
  trace_id?: string
  retryable?: boolean
  partial_text?: string
}
