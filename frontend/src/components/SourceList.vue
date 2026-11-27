<script setup lang="ts">
/**
 * 引用来源列表：答案下面的「来源 N」。
 *
 * 展开而不是弹新页：手机上多一次跳转就多一次回不来。
 * lane 标出来是有用的 —— 用户问"为什么这条没引用我想要的文档"时，
 * 能看到是关键词命中的还是语义命中的，比让我们翻日志快得多。
 */
import { NCollapse, NCollapseItem, NTag } from 'naive-ui'

import type { SourceItem } from '@/api/types'
import { sourceLabel } from '@/utils/format'

const props = defineProps<{ sources: SourceItem[] }>()

const laneType: Record<string, 'default' | 'info' | 'success'> = {
  keyword: 'default',
  vector: 'info',
  both: 'success',
}
</script>

<template>
  <div v-if="props.sources.length" class="sources">
    <NCollapse>
      <NCollapseItem :title="`参考资料 ${props.sources.length} 条`" name="s">
        <div v-for="source in props.sources" :key="source.chunk_id" class="sources__item">
          <div class="sources__head">
            <span class="sources__title">{{ sourceLabel(source) }}</span>
            <NTag size="tiny" :bordered="false" :type="laneType[source.lane ?? ''] ?? 'default'">
              {{ source.lane === 'vector' ? '语义' : source.lane === 'both' ? '双路' : '关键词' }}
            </NTag>
          </div>
          <div class="kb-muted sources__snippet">{{ source.snippet }}</div>
        </div>
      </NCollapseItem>
    </NCollapse>
  </div>
</template>

<style scoped>
.sources {
  margin-top: 8px;
}

.sources__item + .sources__item {
  margin-top: 8px;
}

.sources__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
}

.sources__snippet {
  margin-top: 2px;
  line-height: 1.6;
}
</style>
