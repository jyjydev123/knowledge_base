<script setup lang="ts">
import { NButton, NEmpty } from 'naive-ui'
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div class="kb-page">
    <NEmpty description="页面不存在" />
    <div class="kb-row" style="justify-content: center; margin-top: 12px">
      <NButton @click="router.replace({ name: 'kb' })">回知识库列表</NButton>
    </div>
  </div>
</template>
