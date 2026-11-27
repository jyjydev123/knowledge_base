<script setup lang="ts">
/**
 * 模型/文档正文的渲染出口：marked → **DOMPurify** → v-html。
 *
 * 这两步一个都不能少，而且顺序不能反：
 *   - 大模型的回答里可能原样带出用户文档中的 HTML（提示注入的一条现成通路），
 *     直接 v-html 等于把一个 XSS 口子开在登录态上；
 *   - 先 sanitize 再插入，marked 产生的 <script>/onerror 全部被清掉。
 * 所以全站只有这一个组件用 v-html，别在业务页面里自己拼。
 */
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = defineProps<{ content: string; inline?: boolean }>()

const html = computed(() => {
  const source = props.content ?? ''
  if (!source.trim()) return ''
  const rendered = props.inline
    ? (marked.parseInline(source, { async: false }) as string)
    : (marked.parse(source, { async: false, gfm: true, breaks: true }) as string)
  return DOMPurify.sanitize(rendered, { USE_PROFILES: { html: true } })
})
</script>

<template>
  <div class="kb-markdown" v-html="html" />
</template>
