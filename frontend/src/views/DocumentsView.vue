<script setup lang="ts">
/**
 * 文档管理页：上传 / 状态轮询 / 下载 / 详情 / 重新解析 / 删除。
 *
 * 为什么需要轮询而不是等一次响应：上传接口是**立刻返回 status=pending** 的，
 * 解析和切块在后台任务里跑。手机用户传完就切走了，回来得能看到"它现在到哪一步"。
 * 轮询节奏：有文档在跑时 2.5s，全安静下来退到 15s（别让一个没人看的页面一直敲后端）。
 *
 * accept 属性和前端的大小预检查都只是**体验优化**：客户端的一切限制都能被绕过，
 * 真正的闸门在 app/services/file_types.py + UPLOAD_MAX_SIZE_MB / IMAGE_MAX_SIZE_MB。
 * 所以这里的错误提示原样显示后端的 msg（413/400 都写清了原因）。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NCollapse, NCollapseItem, NEmpty, NModal, NPagination, NSpin, NTag, useDialog, useMessage } from 'naive-ui'
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import { docApi, kbApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { DocumentDetail, DocumentResponse } from '@/api/types'
import StatusTag from '@/components/StatusTag.vue'
import { formatBytes, formatTime, isBusy } from '@/utils/format'

/** 只是用来"提前拦一下"的展示值，服务端才是唯一有效的限制 */
const DOC_LIMIT_MB = 20
const IMAGE_LIMIT_MB = 8
const DOC_ACCEPT = '.pdf,.docx,.pptx,.md,.txt'
const IMAGE_ACCEPT = '.jpg,.jpeg,.png,.webp,.gif,.bmp'

const route = useRoute()
const kbId = computed(() => String(route.params.kbId ?? ''))
const message = useMessage()
const dialog = useDialog()
const queryClient = useQueryClient()

const page = ref(1)
const size = 20
const docInput = ref<HTMLInputElement | null>(null)
const imageInput = ref<HTMLInputElement | null>(null)
const detail = ref<DocumentDetail | null>(null)
const showDetail = ref(false)

const kb = useQuery({ queryKey: computed(() => ['kb', kbId.value]), queryFn: () => kbApi.detail(kbId.value) })

const docs = useQuery({
  queryKey: computed(() => ['docs', kbId.value, page.value]),
  queryFn: () => docApi.list({ kb_id: kbId.value, page: page.value, size }),
  enabled: computed(() => Boolean(kbId.value)),
})

const items = computed<DocumentResponse[]>(() => docs.data.value?.items ?? [])
const total = computed(() => docs.data.value?.total ?? 0)

const status = useQuery({
  queryKey: computed(() => ['docStatus', kbId.value]),
  queryFn: () => docApi.status(kbId.value),
  enabled: computed(() => Boolean(kbId.value)),
  // 有任何文档在解析中就跳得勤，否则退到 15 秒
  refetchInterval: computed(() => (items.value.some((item) => isBusy(item.status)) ? 2500 : 15_000)),
})

function refetchAll(): void {
  void docs.refetch()
  void status.refetch()
  void queryClient.invalidateQueries({ queryKey: ['kbs'] })
}

const uploadDoc = useMutation({
  mutationFn: (file: File) => docApi.upload(kbId.value, file),
  onSuccess: (data) => {
    message.success(`已提交解析：${data.title}`)
    refetchAll()
  },
  onError: (error) => message.error(error instanceof ApiError ? error.message : '上传失败'),
})

const uploadImage = useMutation({
  mutationFn: (file: File) => docApi.uploadImage(kbId.value, file),
  onSuccess: (data) => {
    message.success(`图片已保存：${data.title}`)
    refetchAll()
  },
  onError: (error) => message.error(error instanceof ApiError ? error.message : '图片上传失败'),
})

function pick(kind: 'doc' | 'image'): void {
  ;(kind === 'doc' ? docInput : imageInput).value?.click()
}

function onPicked(kind: 'doc' | 'image', event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const limit = kind === 'doc' ? DOC_LIMIT_MB : IMAGE_LIMIT_MB
  if (file.size > limit * 1024 * 1024) {
    message.warning(`这个文件 ${formatBytes(file.size)}，超过 ${limit}MB 上限，服务端也会拒收`)
    input.value = ''
    return
  }
  if (file.size === 0) {
    message.warning('文件是空的（0 字节），后端会拒收')
    input.value = ''
    return
  }
  ;(kind === 'doc' ? uploadDoc : uploadImage).mutate(file)
  input.value = '' // 同一个文件再选一次也要能触发 change
}

const reindex = useMutation({
  mutationFn: (id: string) => docApi.reindex(id),
  onSuccess: (data) => {
    message.success(data ? '已提交重新解析' : '已提交')
    refetchAll()
  },
  onError: (error) => message.error(error instanceof ApiError ? error.message : '重跑失败'),
})

async function openDetail(row: DocumentResponse): Promise<void> {
  try {
    detail.value = await docApi.detail(row.id)
    showDetail.value = true
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '读取详情失败')
  }
}

async function download(row: DocumentResponse): Promise<void> {
  try {
    // 不能用 <a href> 直接指过去：那条请求不会带 Authorization 头，必然 401
    await docApi.download(row.download_url ?? `/documents/download/${row.id}`, row.filename)
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '下载失败')
  }
}

function confirmDelete(row: DocumentResponse): void {
  dialog.warning({
    title: '删除文档',
    content: `「${row.title}」的记录、切块、向量与磁盘文件都会删除。确定？`,
    positiveText: '确定删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await docApi.remove(row.id)
        message.success('已删除')
        refetchAll()
      } catch (error) {
        message.error(error instanceof ApiError ? error.message : '删除失败')
      }
    },
  })
}

const summary = computed(() => {
  const counts = status.data.value?.status_counts ?? {}
  return Object.entries(counts)
    .filter(([, value]) => value > 0)
    .map(([key, value]) => ({ key, value }))
})
</script>

<template>
  <div class="kb-page">
    <div class="kb-card kb-head">
      <div>
        <strong>{{ kb.data.value?.name ?? '知识库' }}</strong>
        <div class="kb-muted">
          {{ total }} 篇文档 · v{{ kb.data.value?.version ?? '—' }}
          <template v-if="kb.data.value?.last_indexed_at"> · 最近索引 {{ formatTime(kb.data.value.last_indexed_at) }}</template>
        </div>
      </div>
      <div class="kb-row" style="margin-top: 8px">
        <NButton type="primary" size="small" :loading="uploadDoc.isPending.value" @click="pick('doc')">上传文档</NButton>
        <NButton size="small" :loading="uploadImage.isPending.value" @click="pick('image')">上传图片</NButton>
        <NButton size="small" quaternary @click="refetchAll">刷新</NButton>
      </div>
      <input ref="docInput" type="file" :accept="DOC_ACCEPT" hidden @change="onPicked('doc', $event)" />
      <input ref="imageInput" type="file" :accept="IMAGE_ACCEPT" hidden @change="onPicked('image', $event)" />
      <div class="kb-muted" style="margin-top: 6px">
        文档支持 pdf/docx/pptx/md/txt（≤{{ DOC_LIMIT_MB }}MB）；图片支持 jpg/png/webp/gif/bmp（≤{{ IMAGE_LIMIT_MB }}MB，不参与解析）
      </div>
    </div>

    <div v-if="status.data.value && !status.data.value.healthy" class="kb-card kb-warn">
      <div class="kb-row">
        <NTag type="warning" size="small" :bordered="false">磁盘与数据库不一致</NTag>
        <NButton size="tiny" quaternary @click="status.refetch()">重新核对</NButton>
      </div>
      <div v-if="status.data.value.missing_files.length" class="kb-muted">
        缺文件 {{ status.data.value.missing_files.length }} 个：需要重新上传
      </div>
      <div v-if="status.data.value.orphan_files.length" class="kb-muted">
        多余文件 {{ status.data.value.orphan_files.length }} 个：可用 scripts 清理
      </div>
    </div>

    <div v-if="summary.length" class="kb-row" style="margin: 10px 2px">
      <NTag v-for="row in summary" :key="row.key" size="small" :bordered="false">{{ row.key }} · {{ row.value }}</NTag>
    </div>

    <NSpin :show="docs.isFetching.value">
      <div v-if="!items.length && !docs.isLoading.value" class="kb-stack" style="margin-top: 12px">
        <NEmpty description="这个库还没有文档" />
        <div class="kb-muted" style="text-align: center">上传第一篇资料后，问答才会带上参考资料</div>
      </div>

      <div v-else class="kb-stack" style="margin-top: 10px">
        <div v-for="row in items" :key="row.id" class="kb-card doc">
          <div class="doc__main" @click="openDetail(row)">
            <div class="doc__title">{{ row.title }}</div>
            <div class="kb-muted">
              {{ formatBytes(row.file_size) }} · {{ formatTime(row.created_at) }}
              <template v-if="row.chunk_count"> · {{ row.chunk_count }} 块</template>
            </div>
          </div>
          <div class="doc__side">
            <StatusTag :status="row.status" :error-msg="row.error_msg" />
            <div class="doc__actions">
              <NButton size="tiny" quaternary @click="download(row)">下载</NButton>
              <NButton v-if="row.status === 'failed' || row.status === 'indexed'" size="tiny" quaternary @click="reindex.mutate(row.id)">
                重解析
              </NButton>
              <NButton size="tiny" quaternary type="error" @click="confirmDelete(row)">删除</NButton>
            </div>
          </div>
        </div>
      </div>
    </NSpin>

    <div v-if="total > size" class="kb-pager">
      <NPagination v-model:page="page" :page-count="Math.ceil(total / size)" :page-slot="5" />
    </div>

    <NModal v-model:show="showDetail" preset="card" :title="detail?.title ?? '文档详情'" style="width: min(720px, 94vw)">
      <div v-if="detail" class="kb-stack">
        <div class="kb-row">
          <StatusTag :status="detail.status" :error-msg="detail.error_msg" />
          <span class="kb-muted">
            {{ detail.filename }} · {{ formatBytes(detail.file_size) }} · 共 {{ detail.chunk_total ?? detail.chunks.length }} 块
          </span>
        </div>
        <NCollapse>
          <NCollapseItem v-for="chunk in detail.chunks" :key="chunk.id" :title="`第 ${chunk.seq + 1} 块${chunk.heading ? ' · ' + chunk.heading : ''}`" :name="chunk.id">
            <pre class="chunk">{{ chunk.content }}</pre>
          </NCollapseItem>
        </NCollapse>
      </div>
    </NModal>
  </div>
</template>

<style scoped>
.kb-head {
  display: flex;
  flex-direction: column;
}

.kb-warn {
  margin-top: 10px;
  border-color: #f0c78a;
  background: #fdf6ec;
}

.doc {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  justify-content: space-between;
}

.doc__main {
  flex: 1;
  min-width: 0;
  cursor: pointer;
}

.doc__title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc__side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

.doc__actions {
  display: flex;
  gap: 2px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.kb-pager {
  display: flex;
  justify-content: center;
  margin-top: 14px;
}

.chunk {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.6;
  font-family: inherit;
}
</style>
