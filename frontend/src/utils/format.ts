/** 展示层的小工具：口径统一在这里，别在各视图里重复实现一遍 */

import type { DocStatus, SourceItem } from '@/api/types'

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let index = 0
  let size = value
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size >= 10 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`
}

export function formatTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const diff = Date.now() - date.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  const today = new Date()
  const sameYear = date.getFullYear() === today.getFullYear()
  const pad = (input: number) => String(input).padStart(2, '0')
  const head = sameYear ? `${date.getMonth() + 1}/${date.getDate()}` : `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`
  return `${head} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 状态 → 文案 + naive-ui 的 type。stored 单独一类：它不是"没处理完"，是"本来就不处理" */
export const STATUS_META: Record<DocStatus, { label: string; type: 'default' | 'info' | 'success' | 'warning' | 'error' }> = {
  stored: { label: '已保存（不解析）', type: 'default' },
  pending: { label: '排队中', type: 'info' },
  parsing: { label: '解析中', type: 'warning' },
  chunking: { label: '切块中', type: 'warning' },
  indexed: { label: '已入库', type: 'success' },
  failed: { label: '失败', type: 'error' },
}

export function statusMeta(status: string): { label: string; type: 'default' | 'info' | 'success' | 'warning' | 'error' } {
  return STATUS_META[status as DocStatus] ?? { label: status, type: 'default' }
}

export function isBusy(status: string): boolean {
  return status === 'pending' || status === 'parsing' || status === 'chunking'
}

export function permissionLabel(permission: string, isOwner: boolean): string {
  if (isOwner) return '我的'
  return permission === 'write' ? '可读写' : '只读'
}

/** 引用角标的标题：后端已经带了 title，但老数据可能空，兜到文件名 */
export function sourceTitle(source: SourceItem): string {
  return source.title || source.heading || '文档'
}

export function sourceLabel(source: SourceItem): string {
  const parts = [`[${source.index}] ${sourceTitle(source)}`]
  if (source.page) parts.push(`第 ${source.page} 页`)
  if (source.heading) parts.push(source.heading)
  return parts.join(' · ')
}
