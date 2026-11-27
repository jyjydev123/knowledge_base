<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { NButton, NCard, NForm, NFormItem, NInput, useDialog, useMessage } from 'naive-ui'
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { authApi, healthApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const username = ref('')
const email = ref('')
const editing = ref(false)
const oldPassword = ref('')
const newPassword = ref('')
const changing = ref(false)

const health = useQuery({
  queryKey: ['health'],
  queryFn: healthApi.check,
  staleTime: 10_000,
})

async function saveProfile(): Promise<void> {
  try {
    const payload: { username?: string; email?: string } = {}
    if (username.value) payload.username = username.value
    if (email.value) payload.email = email.value
    if (!Object.keys(payload).length) {
      message.info('没有要更新的内容')
      return
    }
    auth.user = await authApi.update(payload)
    message.success('已保存')
    editing.value = false
    username.value = ''
    email.value = ''
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '保存失败')
  }
}

async function changePassword(): Promise<void> {
  if (newPassword.value.length < 8) {
    message.warning('新密码至少 8 位')
    return
  }
  changing.value = true
  try {
    await authApi.resetPassword(oldPassword.value, newPassword.value)
    message.success('密码已修改，需要重新登录')
    auth.logout()
    await router.replace({ name: 'login' })
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '修改失败')
  } finally {
    changing.value = false
  }
}

function deleteAccount(): void {
  // 注销是破坏性动作：先二次确认，密码由用户当场输入，前端不缓存
  dialog.error({
    title: '注销账号',
    content: '注销会删掉你的知识库、文档与对话记录，且不可恢复。确定继续？',
    positiveText: '确定注销',
    negativeText: '取消',
    onPositiveClick: async () => {
      const password = window.prompt('请输入当前密码以确认')
      if (!password) return
      try {
        await authApi.remove(password)
        message.success('账号已注销')
        auth.logout(false)
        await router.replace({ name: 'login' })
      } catch (error) {
        message.error(error instanceof ApiError ? error.message : '注销失败')
      }
    },
  })
}
</script>

<template>
  <div class="kb-page kb-stack">
    <NCard size="small" :bordered="false">
      <div class="kb-stack">
        <div>
          <strong>{{ auth.user?.username ?? '—' }}</strong>
          <span class="kb-muted">（{{ auth.user?.role === 'admin' ? '管理员' : '普通用户' }}）</span>
        </div>
        <div class="kb-muted">{{ auth.user?.email }}</div>
      </div>
    </NCard>

    <NCard title="账号资料" size="small" :bordered="false">
      <template #header-extra>
        <NButton size="small" text @click="editing = !editing">{{ editing ? '取消' : '编辑' }}</NButton>
      </template>
      <div v-if="!editing" class="kb-muted">
        上次登录：{{ auth.user?.last_login_at ? new Date(auth.user.last_login_at).toLocaleString() : '—' }}
      </div>
      <NForm v-else layout="vertical" @submit.prevent="saveProfile">
        <NFormItem label="用户名">
          <NInput v-model:value="username" :placeholder="auth.user?.username" />
        </NFormItem>
        <NFormItem label="邮箱">
          <NInput v-model:value="email" :placeholder="auth.user?.email" />
        </NFormItem>
        <NButton type="primary" @click="saveProfile">保存</NButton>
      </NForm>
    </NCard>

    <NCard title="修改密码" size="small" :bordered="false">
      <NForm layout="vertical" @submit.prevent="changePassword">
        <NFormItem label="原密码">
          <NInput v-model:value="oldPassword" type="password" show-password-on="click" />
        </NFormItem>
        <NFormItem label="新密码">
          <NInput v-model:value="newPassword" type="password" show-password-on="click" placeholder="至少 8 位" />
        </NFormItem>
        <NButton type="primary" :loading="changing" @click="changePassword">修改并退出所有设备</NButton>
      </NForm>
      <p class="kb-muted">改密后所有已登录端的 token 立即失效（后端会吊销全部 token）。</p>
    </NCard>

    <NCard title="后端状态" size="small" :bordered="false">
      <div class="kb-row">
        <NButton size="small" :loading="health.isFetching.value" @click="health.refetch()">重新探测</NButton>
        <span :class="health.data.value?.ok ? 'ok' : 'bad'">
          {{ health.data.value ? (health.data.value.ok ? '可连通' : '异常') : '未探测' }}
          <template v-if="health.data.value?.status">（HTTP {{ health.data.value.status }}）</template>
        </span>
      </div>
      <p class="kb-muted">
        连不上通常是后端没起或地址不对；手机上请把 VITE_API_BASE 指向电脑的局域网地址。
      </p>
    </NCard>

    <NCard title="危险操作" size="small" :bordered="false">
      <NButton quaternary type="error" @click="deleteAccount">注销账号</NButton>
      <p class="kb-muted">
        退出登录请用右上角「我的 → 退出登录」。注销与退出的区别：注销删数据，退出只吊销当前 token。
      </p>
    </NCard>
  </div>
</template>

<style scoped>
.ok {
  color: #18a058;
}

.bad {
  color: #d03050;
}
</style>
