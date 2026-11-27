<script setup lang="ts">
/**
 * 知识库列表 —— 移动端的"首页"。
 *
 * 三个必须守住的点：
 *   1. **permission 由后端给**，按钮可用性一律看 `kb.permission` / `kb.is_owner`。
 *      在前端拿 owner_id 和 user_id 比是错的：权限矩阵只有一个真源（services/kb_access.py），
 *      复制一份到浏览器里，后端一改就悄悄不一致；
 *   2. 空列表不是错误，要给"建第一个库"的引导 —— 这个产品对新用户的第一个问题就是这个；
 *   3. 删除用 dialog 二次确认，并把"会连带删掉 N 篇文档"说出来（后端返回 document_count）。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NEmpty, NSpin, NTabPane, NTabs, NTag, useDialog, useMessage } from 'naive-ui'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { kbApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { KbOut } from '@/api/types'
import KbFormModal from '@/components/KbFormModal.vue'
import { formatTime, permissionLabel } from '@/utils/format'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const queryClient = useQueryClient()

const scope = ref<'all' | 'owned' | 'shared'>('all')
const showForm = ref(false)
const editing = ref<KbOut | null>(null)

const kbs = useQuery({
  queryKey: computed(() => ['kbs', scope.value]),
  queryFn: () => kbApi.list({ scope: scope.value, size: 50 }),
})

const items = computed<KbOut[]>(() => kbs.data.value?.items ?? [])

function openCreate(): void {
  editing.value = null
  showForm.value = true
}

function openEdit(kb: KbOut): void {
  editing.value = kb
  showForm.value = true
}

function enter(kb: KbOut): void {
  void router.push({ name: 'documents', params: { kbId: kb.kb_id } })
}

const remove = useMutation({
  mutationFn: (kb: KbOut) => kbApi.remove(kb.kb_id),
  onSuccess: (data) => {
    const parts = [`删除成功：文档 ${data.document_count} 篇`]
    if (data.files_removed) parts.push(`清理文件 ${data.files_removed} 个`)
    // 向量清理失败不该吓用户：库里记录已经没了，残留向量会因为回表找不到而自然失效
    if (data.vectors_removed) parts.push(`向量 ${data.vectors_removed} 条`)
    message.success(parts.join('，'))
    void queryClient.invalidateQueries({ queryKey: ['kbs'] })
  },
  onError: (error) => message.error(error instanceof ApiError ? error.message : '删除失败'),
})

function confirmDelete(kb: KbOut): void {
  dialog.warning({
    title: '删除知识库',
    content: `「${kb.name}」里的 ${kb.document_count} 篇文档、切块与授权会一起删除，磁盘文件也会清理。此操作不可恢复。`,
    positiveText: '确定删除',
    negativeText: '取消',
    onPositiveClick: () => remove.mutate(kb),
  })
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '加载失败，请检查后端是否在运行'
}
</script>

<template>
  <div class="kb-page">
    <NTabs v-model:value="scope" type="line" justify-content="space-between">
      <NTabPane name="all" tab="全部" />
      <NTabPane name="owned" tab="我建的" />
      <NTabPane name="shared" tab="别人给我的" />
    </NTabs>

    <NSpin :show="kbs.isFetching.value">
      <div v-if="kbs.isError.value" class="kb-card" style="margin-top: 10px">
        <div>{{ errorMessage(kbs.error.value) }}</div>
        <NButton size="small" style="margin-top: 8px" @click="kbs.refetch()">重试</NButton>
      </div>

      <template v-else-if="!items.length && !kbs.isLoading.value">
        <NEmpty :description="scope === 'shared' ? '还没有人共享知识库给你' : '还没有知识库'" style="margin: 36px 0" />
        <div v-if="scope !== 'shared'" class="kb-row" style="justify-content: center">
          <NButton type="primary" @click="openCreate">新建知识库</NButton>
        </div>
      </template>

      <div v-else class="kb-stack" style="margin-top: 10px">
        <div v-for="kb in items" :key="kb.kb_id" class="kb-card kb-item">
          <div class="kb-item__head" @click="enter(kb)">
            <div class="kb-item__title">
              {{ kb.name }}
              <NTag size="tiny" :bordered="false" :type="kb.is_owner ? 'success' : 'info'">
                {{ permissionLabel(kb.permission, kb.is_owner) }}
              </NTag>
              <NTag v-if="kb.visibility === 'public'" size="tiny" :bordered="false" type="warning">公开</NTag>
            </div>
            <div class="kb-muted">
              {{ kb.document_count }} 篇 · {{ kb.chunk_count }} 块 · v{{ kb.version }} · {{ formatTime(kb.updated_at) }}
            </div>
          </div>
          <div v-if="kb.description" class="kb-item__desc">{{ kb.description }}</div>
          <div class="kb-item__actions">
            <NButton size="tiny" @click="enter(kb)">文档</NButton>
            <template v-if="kb.is_owner">
              <NButton size="tiny" @click="router.push({ name: 'shares', params: { kbId: kb.kb_id } })">授权</NButton>
              <NButton size="tiny" @click="openEdit(kb)">编辑</NButton>
              <NButton size="tiny" text type="error" :loading="remove.isPending.value" @click="confirmDelete(kb)">
                删除
              </NButton>
            </template>
          </div>
        </div>
      </div>
    </NSpin>

    <button v-if="scope !== 'shared'" class="kb-fab" @click="openCreate">＋</button>

    <KbFormModal v-model:show="showForm" :kb="editing" @saved="kbs.refetch()" />
  </div>
</template>

<style scoped>
.kb-item__head {
  cursor: pointer;
}

.kb-item__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}

.kb-item__desc {
  margin-top: 6px;
  font-size: 13px;
  color: var(--kb-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.kb-item__actions {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.kb-fab {
  position: fixed;
  right: 18px;
  bottom: calc(var(--kb-tab-height) + 18px + env(safe-area-inset-bottom));
  width: 48px;
  height: 48px;
  border-radius: 50%;
  border: none;
  background: #18a058;
  color: #fff;
  font-size: 26px;
  box-shadow: 0 4px 14px rgba(24, 160, 88, 0.35);
}
</style>
