/**
 * 唯一的出网口：所有 HTTP 请求都从这里走。
 *
 * 三件事只在这里做一次，业务代码里不许重复：
 *   1. **解包 {status, msg, data}**。后端所有接口（含报错）都是这个信封，
 *      `status !== 200` 一律抛 ApiError —— 业务层只写 happy path，异常交给统一提示；
 *   2. **401 就地失效**：清 token + 通知路由跳登录。不在每个页面判"是不是登录过期了"；
 *   3. **Authorization 头**。所以 `download` 之类的接口不能用 <a href> 直接指过去
 *      （那样不会带 header），必须走 fetch 拿 blob。
 *
 * 路径都写成 `/xxx`，前缀 `/api/v1` 由 BASE 统一给（dev 走 vite 代理，壳里走绝对地址）。
 */

import { clearToken, getToken } from '@/utils/storage'
import type { Envelope } from './types'

export class ApiError extends Error {
  readonly status: number
  readonly data: unknown

  constructor(status: number, message: string, data?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

const VERSION_PREFIX = '/api/v1'

/**
 * 把 VITE_API_BASE 解析成请求前缀。**只给主机名也自动补上版本前缀**：
 * 壳里（Capacitor）没有 vite 的 dev proxy，指错一次就是整屏 404，
 * 而 404 在 WebView 里长得跟"后端坏了"一模一样，非常费时间。
 *
 *   留空                        → /api/v1                     同源，开发用 vite 代理
 *   http://192.168.1.5:8000     → http://192.168.1.5:8000/api/v1
 *   https://kb.example.com/api/v1 → 原样
 */
export function resolveBase(raw: string): string {
  const trimmed = (raw ?? '').trim().replace(/\/+$/, '')
  if (!trimmed) return VERSION_PREFIX
  return trimmed.endsWith(VERSION_PREFIX) ? trimmed : trimmed + VERSION_PREFIX
}

/** 站点根（给 /health 这种**不在版本前缀下**的接口用）。同源时返回空串 */
export function resolveOrigin(raw: string): string {
  const trimmed = (raw ?? '').trim()
  if (!trimmed) return ''
  try {
    return new URL(trimmed).origin
  } catch {
    // 写成了相对路径（比如 /backend）：同源部署，根就是当前 origin
    return ''
  }
}

const RAW_BASE = import.meta.env.VITE_API_BASE ?? ''
export const API_BASE = resolveBase(RAW_BASE)
export const ORIGIN_BASE = resolveOrigin(RAW_BASE)

type UnauthorizedHandler = () => void
let onUnauthorizedHook: UnauthorizedHandler | null = null

/** 由 main.ts 注册（路由实例在那里，模块之间不互相 import，免得绕圈） */
export function onUnauthorized(handler: UnauthorizedHandler): void {
  onUnauthorizedHook = handler
}

export type QueryValue = string | number | boolean | null | undefined

export function toQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  form?: FormData
  signal?: AbortSignal
  /** DELETE/204 之类没有响应体时用 'text'，只要状态码用 'none' */
  parse?: 'json' | 'blob' | 'none'
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<Envelope<T>> {
  const { method = 'GET', body, form, signal, parse = 'json' } = options
  const token = getToken()

  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  // multipart 绝不手写 Content-Type：boundary 要交给浏览器生成，写了后端反而解析不出文件
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(API_BASE + path, {
      method,
      headers,
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
      signal,
    })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    // 走到这里是没有 HTTP 响应：断网、DNS、CORS、壳里地址写错。
    // 文案必须说"连不上服务"而不是"操作失败"，否则用户会在错误的方向上重试
    throw new ApiError(0, '连不上服务器，请检查网络或后端是否在运行', String((error as Error).message))
  }

  if (response.status === 401) {
    clearToken()
    onUnauthorizedHook?.()
  }

  if (parse === 'none') {
    if (!response.ok) throw new ApiError(response.status, `请求失败（HTTP ${response.status}）`)
    return { status: 200, msg: '', data: undefined as unknown as T }
  }

  if (parse === 'blob') {
    if (!response.ok) throw new ApiError(response.status, await readErrorText(response))
    return {
      status: 200,
      msg: '',
      data: (await response.blob()) as unknown as T,
    }
  }

  const payload = await readEnvelope<T>(response)

  if (!response.ok || payload.status !== 200) {
    throw new ApiError(payload.status || response.status, payload.msg || `请求失败（HTTP ${response.status}）`, payload.data)
  }
  return payload
}

async function readEnvelope<T>(response: Response): Promise<Envelope<T>> {
  const text = await response.text()
  if (!text) return { status: response.status, msg: response.statusText, data: undefined as unknown as T }
  try {
    const parsed = JSON.parse(text) as Partial<Envelope<T>>
    return {
      status: typeof parsed.status === 'number' ? parsed.status : response.status,
      msg: parsed.msg ?? '',
      data: parsed.data as T,
    }
  } catch {
    // 非 JSON：多半是被反代/网关拦了（502 的 HTML 错误页就是这么进来的）
    return { status: response.status, msg: truncate(text, 160), data: undefined as unknown as T }
  }
}

async function readErrorText(response: Response): Promise<string> {
  const text = await response.text().catch(() => '')
  return text ? truncate(text, 160) : `请求失败（HTTP ${response.status}）`
}

function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  return clean.length > max ? `${clean.slice(0, max)}…` : clean
}

export const http = {
  /** 拿到信封本身（需要读 msg 的地方用，比如"没有需要更新的内容"这类提示） */
  raw: request,

  /** 只要 data，msg 丢掉。绝大多数查询走这里 */
  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const envelope = await request<T>(path, { signal })
    return envelope.data
  },

  async post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const envelope = await request<T>(path, { method: 'POST', body, signal })
    return envelope.data
  },

  async put<T>(path: string, body?: unknown): Promise<T> {
    const envelope = await request<T>(path, { method: 'PUT', body })
    return envelope.data
  },

  async del<T>(path: string): Promise<T> {
    const envelope = await request<T>(path, { method: 'DELETE', body: undefined })
    return envelope.data
  },

  /** 上传（multipart）。进度回调留给以后的 XHR 版本，现在先把边界处理做对 */
  async upload<T>(path: string, form: FormData, signal?: AbortSignal): Promise<T> {
    const envelope = await request<T>(path, { method: 'POST', form, signal })
    return envelope.data
  },

  /** 带鉴权头地取一个二进制资源，返回可直接给 <a download> 的 objectURL。
   *  用完必须 revokeObjectURL，不然手机上内存只涨不降 */
  async download(path: string, filename: string): Promise<string> {
    const envelope = await request<Blob>(path, { parse: 'blob' })
    const url = URL.createObjectURL(envelope.data)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename || 'download'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    // 留给浏览器一点时间真正开始下载再释放
    window.setTimeout(() => URL.revokeObjectURL(url), 4000)
    return url
  },

  /** 只关心成功/失败的写操作 */
  async ping(path: string, body?: unknown): Promise<string> {
    const envelope = await request<unknown>(path, { method: 'POST', body })
    return envelope.msg
  },
}
