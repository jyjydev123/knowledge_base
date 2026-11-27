/**
 * 后端 33 条端点的薄封装：一个函数 = 一条接口，参数与返回类型都对齐 schemas/*.py。
 *
 * 为什么不用组件里直接写 fetch：接口路径/查询参数的写法一旦散到十几个 .vue 里，
 * 后端改一个 query 名就要全仓库搜索。集中在这里，改一处就够。
 *
 * 约定：
 *   - 路径不带 `/api/v1`（由 client.ts 的 API_BASE 统一给）；
 *   - 后端是 REST 与 query 混风格（`GET /documents/detail?document_id=`、
 *     `DELETE /documents/delete/{id}`），这里照抄，不做"更漂亮的重新设计"——
 *     前端一层糖衣只会让排错时两边对不上（DEVELOPMENT §3 也写了这条）。
 */

import { ORIGIN_BASE, http, toQuery } from './client'
import type {
  AgentOut,
  ChatAnswerOut,
  ChatHistoryOut,
  ChatOut,
  ChatStopOut,
  DocumentDetail,
  DocumentResponse,
  DocumentStatusOut,
  KbDeleteOut,
  KbOut,
  KbShareOut,
  PageData,
  SearchOut,
  TokenResponse,
  UserResponse,
} from './types'

export interface KbListScope {
  scope?: 'owned' | 'shared' | 'all'
  page?: number
  size?: number
}

export const authApi = {
  register: (payload: { username: string; email: string; password: string }) =>
    http.post<UserResponse>('/user/register', payload),

  login: (account: string, password: string) =>
    http.post<TokenResponse>('/user/login', { account, password }),

  info: () => http.get<UserResponse>('/user/info'),

  logout: () => http.raw<null>('/user/logout', { method: 'POST' }),

  update: (payload: { username?: string; email?: string }) => http.post<UserResponse>('/user/update', payload),

  resetPassword: (old_password: string, new_password: string) =>
    http.raw<null>('/user/reset_password', { method: 'POST', body: { old_password, new_password } }),

  remove: (password: string) => http.raw<null>('/user/delete', { method: 'POST', body: { password } }),

  list: (page = 1, size = 20) => http.get<PageData<UserResponse>>(`/user/list${toQuery({ page, size })}`),
}

export const kbApi = {
  create: (payload: {
    name: string
    description?: string
    visibility?: 'private' | 'public'
    chunk_size?: number
    chunk_overlap?: number
  }) => http.post<KbOut>('/kb', payload),

  list: (params: KbListScope = {}) =>
    http.get<PageData<KbOut>>(`/kb/list${toQuery({ scope: 'all', page: 1, size: 50, ...params })}`),

  detail: (kbId: string) => http.get<KbOut>(`/kb/detail${toQuery({ kb_id: kbId })}`),

  update: (kbId: string, payload: Partial<Pick<KbOut, 'name' | 'description' | 'visibility' | 'chunk_size' | 'chunk_overlap'>>) =>
    http.post<KbOut>(`/kb/update${toQuery({ kb_id: kbId })}`, payload),

  remove: (kbId: string) => http.del<KbDeleteOut>(`/kb/${kbId}`),

  share: (kbId: string, email: string, permission: 'read' | 'write') =>
    http.post<KbShareOut>(`/kb/share${toQuery({ kb_id: kbId })}`, { email, permission }),

  unshare: (kbId: string, email: string) => http.raw<null>(`/kb/share${toQuery({ kb_id: kbId, email })}`, { method: 'DELETE' }),

  shareList: (kbId: string) => http.get<KbShareOut[]>(`/kb/share/list${toQuery({ kb_id: kbId })}`),
}

export const docApi = {
  list: (params: { kb_id?: string; page?: number; size?: number } = {}) =>
    http.get<PageData<DocumentResponse>>(`/documents/list${toQuery({ page: 1, size: 20, ...params })}`),

  /** 文档车道：pdf/docx/pptx/md/txt，上限 UPLOAD_MAX_SIZE_MB，超限 413、后缀骗人 400 */
  upload: (kbId: string, file: File, documentName?: string) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<DocumentResponse>(`/documents/upload${toQuery({ kb_id: kbId, document_name: documentName })}`, form)
  },

  /** 图片车道：jpg/png/webp/gif/bmp，上限 IMAGE_MAX_SIZE_MB，落库 status=stored（不参与解析） */
  uploadImage: (kbId: string, file: File, imageName?: string) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<DocumentResponse>(`/documents/image/upload${toQuery({ kb_id: kbId, image_name: imageName })}`, form)
  },

  detail: (documentId: string) => http.get<DocumentDetail>(`/documents/detail${toQuery({ document_id: documentId })}`),

  status: (kbId: string) => http.get<DocumentStatusOut>(`/documents/status${toQuery({ kb_id: kbId })}`),

  reindex: (documentId: string) => http.post<DocumentStatusOut>(`/documents/reindex/${documentId}`),

  remove: (documentId: string) => http.del<null>(`/documents/delete/${documentId}`),

  download: (url: string, filename: string) => http.download(url, filename),
}

export const chatApi = {
  create: (payload: { title?: string; kb_id?: string | null } = {}) => http.post<ChatOut>('/aichat/conversations', payload),

  list: (params: { page?: number; size?: number } = {}) =>
    http.get<PageData<ChatOut>>(`/aichat/conversations${toQuery({ page: 1, size: 50, ...params })}`),

  rename: (id: string, title: string) => http.put<ChatOut>(`/aichat/conversations/${id}`, { title }),

  remove: (id: string) => http.del<null>(`/aichat/conversations/${id}`),

  history: (id: string, params: { limit?: number; before_seq?: number } = {}) =>
    http.get<ChatHistoryOut>(`/aichat/conversations/${id}/messages${toQuery(params)}`),

  /** 一次性（非流式）问答：调试和"就要整段 JSON"的场景用，对话页走 sse.ts */
  askOnce: (id: string, question: string) =>
    http.post<ChatAnswerOut>(`/aichat/conversations/${id}/chat`, { question, stream: false }),

  stop: (id: string) => http.post<ChatStopOut>(`/aichat/conversations/${id}/stop`),

  agents: () => http.get<AgentOut[]>('/aichat/agents'),
}

export const searchApi = {
  search: (payload: { q: string; kb_ids?: string[]; top_k?: number }) => http.post<SearchOut>('/search', payload),
}

export const healthApi = {
  /** /health 不在 /api/v1 下面，走同源根路径 */
  check: async (): Promise<{ ok: boolean; status: number; msg: string }> => {
    const response = await fetch(`${ORIGIN_BASE}/health`)
    const body = await response.json().catch(() => ({}) as Record<string, unknown>)
    return { ok: response.ok, status: response.status, msg: String((body as { msg?: string }).msg ?? '') }
  },
}
