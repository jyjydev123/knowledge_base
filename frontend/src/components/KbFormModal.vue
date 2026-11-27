<script setup lang="ts">
/**
 * 建库 / 改库的表单弹窗（同一个组件两用）。
 *
 * 两个细节是后端契约要求的，不是随手加的：
 *   - 编辑时**只提交改过的字段**：后端的 POST /kb/update 是"未提交=不动"的语义，
 *     全量回填会把别人的并发改动覆盖掉；
 *   - chunk_overlap 必须小于 chunk_size（后端 schema 会 422），这里先挡一层，
 *     省得用户提交后才看到红色错误。
 */
import { NButton, NForm, NFormItem, NInput, NInputNumber, NModal, NSwitch, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { kbApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { KbOut } from '@/api/types'

const message = useMessage()

const props = defineProps<{ show: boolean; kb: KbOut | null }>()
const emit = defineEmits<{ (e: 'update:show', value: boolean): void; (e: 'saved'): void }>()

const name = ref('')
const description = ref('')
const publicVisible = ref(false)
const chunkSize = ref(800)
const chunkOverlap = ref(120)
const busy = ref(false)
const original = ref<KbOut | null>(null)

watch(
  () => props.show,
  (open) => {
    if (!open) return
    original.value = props.kb
    name.value = props.kb?.name ?? ''
    description.value = props.kb?.description ?? ''
    publicVisible.value = props.kb?.visibility === 'public'
    chunkSize.value = props.kb?.chunk_size ?? 800
    chunkOverlap.value = props.kb?.chunk_overlap ?? 120
  },
)

const title = computed(() => (original.value ? '编辑知识库' : '新建知识库'))
async function submit(): Promise<void> {
  if (!name.value.trim()) {
    message.warning('名称不能为空')
    return
  }
  if (chunkOverlap.value >= chunkSize.value) {
    message.warning('重叠长度必须小于切块长度')
    return
  }
  busy.value = true
  try {
    const visibility = publicVisible.value ? 'public' : 'private'
    if (original.value) {
      const payload: Record<string, unknown> = {}
      if (name.value.trim() !== original.value.name) payload.name = name.value.trim()
      if (description.value !== original.value.description) payload.description = description.value
      if (visibility !== original.value.visibility) payload.visibility = visibility
      if (chunkSize.value !== original.value.chunk_size) payload.chunk_size = chunkSize.value
      if (chunkOverlap.value !== original.value.chunk_overlap) payload.chunk_overlap = chunkOverlap.value
      if (!Object.keys(payload).length) {
        message.info('没有需要修改的内容')
        emit('update:show', false)
        return
      }
      await kbApi.update(original.value.kb_id, payload)
    } else {
      await kbApi.create({
        name: name.value.trim(),
        description: description.value,
        visibility,
        chunk_size: chunkSize.value,
        chunk_overlap: chunkOverlap.value,
      })
    }
    message.success(original.value ? '已保存' : '知识库已创建')
    emit('saved')
    emit('update:show', false)
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '保存失败')
  } finally {
    busy.value = false
  }
}

</script>

<template>
  <NModal
    :show="props.show"
    preset="card"
    :title="title"
    style="width: min(520px, 94vw)"
    @update:show="emit('update:show', $event)"
  >
    <NForm layout="vertical">
      <NFormItem label="名称" required>
        <NInput v-model:value="name" maxlength="100" placeholder="例如：差旅与报销制度" />
      </NFormItem>
      <NFormItem label="具体说明">
        <NInput
          v-model:value="description"
          type="textarea"
          :autosize="{ minRows: 3, maxRows: 6 }"
          maxlength="2000"
          placeholder="这个库装什么内容、给谁用、口径怎么定。回答时会拼进 prompt，写清楚能明显提升命中率。"
        />
      </NFormItem>
      <NFormItem label="可见范围">
        <NSwitch v-model:value="publicVisible">
          <template #checked>公开：全站登录用户只读</template>
          <template #unchecked>私有：仅授权者可见</template>
        </NSwitch>
      </NFormItem>
      <div class="kb-row">
        <NFormItem label="切块长度" style="flex: 1">
          <NInputNumber v-model:value="chunkSize" :min="50" :max="8000" :step="100" style="width: 100%" />
        </NFormItem>
        <NFormItem label="重叠长度" style="flex: 1">
          <NInputNumber v-model:value="chunkOverlap" :min="0" :max="2000" :step="20" style="width: 100%" />
        </NFormItem>
      </div>
      <p class="kb-muted">
        改切块参数只对之后的解析生效，不会自动重切已有文档；要重切请在文档页逐篇「重新解析」。
      </p>
    </NForm>
    <template #footer>
      <div class="kb-row" style="justify-content: flex-end">
        <NButton @click="emit('update:show', false)">取消</NButton>
        <NButton type="primary" :loading="busy" @click="submit">{{ original ? '保存' : '创建' }}</NButton>
      </div>
    </template>
  </NModal>
</template>
