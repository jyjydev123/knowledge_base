<script setup lang="ts">
/**
 * 纯搜索页：调 POST /search，不叫模型作答。
 *
 * 范围提示必须显示（scoped_kb_ids）—— 用户以为搜的是全部资料、实际只搜了两个库，
 * 是这类界面最容易产生的误解。后端把"这次到底搜了哪些库"透出来就是给这里用的。
 */
import { NButton, NEmpty, NInput, NTag, useMessage } from 'naive-ui'
import { ref } from 'vue'

import { searchApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { SearchOut } from '@/api/types'
import SourceList from '@/components/SourceList.vue'
import { describeRetrieval } from '@/api/sse'

const query = ref('')
const result = ref<SearchOut | null>(null)
const busy = ref(false)
const message = useMessage()

async function run(): Promise<void> {
  if (!query.value.trim()) return
  busy.value = true
  try {
    result.value = await searchApi.search({ q: query.value.trim(), top_k: 8 })
    if (!result.value.hits.length) message.info(result.value.mode === 'no_scope' ? '没有可读的知识库' : '没有匹配的资料')
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '搜索失败')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="kb-page kb-stack">
    <div class="kb-card kb-row">
      <NInput
        v-model:value="query"
        placeholder="搜索资料里的原话，回车即搜"
        clearable
        style="flex: 1; min-width: 180px"
        @keyup.enter="run"
      />
      <NButton type="primary" :loading="busy" @click="run">搜索</NButton>
    </div>

    <template v-if="result">
      <div class="kb-row">
        <NTag size="small" :bordered="false">{{ result.hits.length }} 条结果</NTag>
        <span class="kb-muted">{{ describeRetrieval({ mode: result.mode, lane: result.lane, candidates: result.candidates }) }}</span>
        <span v-if="result.fallback" class="kb-muted">（降级原因：{{ result.fallback }}）</span>
      </div>
      <div class="kb-muted">检索范围：{{ result.scoped_kb_ids.length }} 个知识库</div>

      <NEmpty v-if="!result.hits.length" description="没有匹配的资料" />
      <SourceList v-else :sources="result.hits" />
    </template>

    <div v-else class="kb-muted" style="text-align: center; margin-top: 24px">
      搜索与问答用的是同一条检索链路，所以这里搜得到的东西，问答一定也能引用到。
    </div>
  </div>
</template>
