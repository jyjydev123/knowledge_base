<script setup lang="ts">
/** 文档状态的统一显示。failed 时把 error_msg 挂到 tooltip 上，省得用户去猜为什么失败 */
import { NTag, NTooltip } from 'naive-ui'
import { computed } from 'vue'

import { statusMeta } from '@/utils/format'

const props = defineProps<{ status: string; errorMsg?: string | null }>()
const meta = computed(() => statusMeta(props.status))
</script>

<template>
  <NTooltip v-if="errorMsg" trigger="hover">
    <template #trigger>
      <NTag :type="meta.type" size="small" round>{{ meta.label }}</NTag>
    </template>
    {{ errorMsg }}
  </NTooltip>
  <NTag v-else :type="meta.type" size="small" round>{{ meta.label }}</NTag>
</template>
