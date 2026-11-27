<script setup lang="ts">
/**
 * 共享授权面板（owner 专属）。
 *
 * 三条接口都是 manage 权限，非 owner 进来会拿到 403/404 —— 这里当成"只读提示页"处理，
 * 而不是让页面白屏：入口按钮虽然只对 owner 显示，但用户可能直接把 URL 发给别人。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NAlert, NButton, NEmpty, NInput, NRadio, NRadioGroup, NSpin, NTag, useDialog, useMessage } from 'naive-ui'
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import { kbApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { KbShareOut } from '@/api/types'
import { formatTime } from '@/utils/format'

const route = useRoute()
const kbId = computed(() => String(route.params.kbId ?? ''))
const message = useMessage()
const dialog = useDialog()
const queryClient = useQueryClient()

const email = ref('')
const permission = ref<'read' | 'write'>('read')

const shares = useQuery({
  queryKey: computed(() => ['shares', kbId.value]),
  queryFn: () => kbApi.shareList(kbId.value),
  retry: 0, // 403/404 重试没意义，直接走错误分支给提示
})

const items = computed<KbShareOut[]>(() => shares.data.value ?? [])

const grant = useMutation({
  mutationFn: () => kbApi.share(kbId.value, email.value.trim(), permission.value),
  onSuccess: (data) => {
    message.success(`已授权 ${data.grantee_username}（${data.permission === 'write' ? '可读写' : '只读'}）`)
    email.value = ''
    void queryClient.invalidateQueries({ queryKey: ['shares'] })
  },
  onError: (error) => message.error(error instanceof ApiError ? error.message : '授权失败'),
})

function revoke(row: KbShareOut): void {
  dialog.warning({
    title: '取消授权',
    content: `取消 ${row.grantee_username} 对这个知识库的访问？对方自己的库和上传的文件不会被删。`,
    positiveText: '取消授权',
    negativeText: '算了',
    onPositiveClick: async () => {
      try {
        const envelope = await kbApi.unshare(kbId.value, row.grantee_email)
        message.success((envelope as unknown as { msg?: string })?.msg || '已取消授权')
        void queryClient.invalidateQueries({ queryKey: ['shares'] })
      } catch (error) {
        message.error(error instanceof ApiError ? error.message : '操作失败')
      }
    },
  })
}
</script>

<template>
  <div class="kb-page kb-stack">
    <NAlert v-if="shares.isError.value" type="warning" :bordered="false">
      只有知识库创建者能管理授权。如果你是协作者，这个页面看不到名单是正常的。
    </NAlert>

    <div class="kb-card">
      <div class="kb-muted" style="margin-bottom: 8px">按邮箱找人（用户名可能重名，邮箱是唯一的）。重复授权=更新权限。</div>
      <NInput v-model:value="email" placeholder="对方邮箱，例如 lisi@example.com" />
      <NRadioGroup v-model:value="permission" style="margin-top: 10px">
        <NRadio value="read">只读：能看列表/详情/下载</NRadio>
        <NRadio value="write" style="margin-left: 12px">可读写：还能上传和删除文档</NRadio>
      </NRadioGroup>
      <NButton
        type="primary"
        block
        style="margin-top: 12px"
        :disabled="!email.includes('@')"
        :loading="grant.isPending.value"
        @click="grant.mutate()"
      >
        授权
      </NButton>
    </div>

    <NSpin :show="shares.isFetching.value">
      <NEmpty v-if="!items.length && !shares.isLoading.value" description="还没有共享给任何人" />
      <div v-else class="kb-stack">
        <div v-for="row in items" :key="row.share_id" class="kb-card share">
          <div>
            <strong>{{ row.grantee_username }}</strong>
            <div class="kb-muted">{{ row.grantee_email }} · {{ formatTime(row.created_at) }}</div>
          </div>
          <div class="kb-row">
            <NTag size="small" :bordered="false" :type="row.permission === 'write' ? 'success' : 'info'">
              {{ row.permission === 'write' ? '可读写' : '只读' }}
            </NTag>
            <NButton size="tiny" quaternary type="error" @click="revoke(row)">取消</NButton>
          </div>
        </div>
      </div>
    </NSpin>
  </div>
</template>

<style scoped>
.share {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
</style>
