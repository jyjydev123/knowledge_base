<script setup lang="ts">
import { NButton, NForm, NFormItem, NInput, NTabPane, NTabs, useMessage } from 'naive-ui'
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { getLastUsername } from '@/utils/storage'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const message = useMessage()

const mode = ref<'login' | 'register'>('login')
const account = ref(getLastUsername())
const email = ref('')
const password = ref('')
const busy = ref(false)

async function submit(): Promise<void> {
  if (busy.value) return
  if (!password.value) {
    message.warning('请输入密码')
    return
  }
  busy.value = true
  try {
    if (mode.value === 'login') {
      if (!account.value.trim()) {
        message.warning('请输入用户名或邮箱')
        return
      }
      await auth.login(account.value.trim(), password.value)
    } else {
      if (!email.value.trim()) {
        message.warning('注册需要邮箱')
        return
      }
      await auth.register(account.value.trim(), email.value.trim(), password.value)
    }
    message.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/kb'
    await router.replace(redirect)
  } catch (error) {
    // 401 的文案后端已经给好了（用户名或密码错误），原样显示，不做"猜测式"改写
    message.error(error instanceof ApiError ? error.message : '登录失败，请稍后重试')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="kb-page login">
    <div class="login__brand">
      <h1>AI 知识库</h1>
      <p class="kb-muted">把自己的资料变成可以追问的答案</p>
    </div>

    <div class="kb-card login__card">
      <NTabs v-model:value="mode" type="segment" size="large">
        <NTabPane name="login" tab="登录">
          <NForm @submit.prevent="submit">
            <NFormItem label="用户名或邮箱">
              <NInput v-model:value="account" placeholder="zhangsan 或 you@example.com" @keyup.enter="submit" />
            </NFormItem>
            <NFormItem label="密码">
              <NInput v-model:value="password" type="password" show-password-on="click" @keyup.enter="submit" />
            </NFormItem>
            <NButton type="primary" block :loading="busy" @click="submit">登录</NButton>
          </NForm>
        </NTabPane>
        <NTabPane name="register" tab="注册">
          <NForm @submit.prevent="submit">
            <NFormItem label="用户名">
              <NInput v-model:value="account" placeholder="3-50 个字符，登录后用它" />
            </NFormItem>
            <NFormItem label="邮箱">
              <NInput v-model:value="email" placeholder="you@example.com" />
            </NFormItem>
            <NFormItem label="密码">
              <NInput v-model:value="password" type="password" show-password-on="click" />
            </NFormItem>
            <NButton type="primary" block :loading="busy" @click="submit">注册并登录</NButton>
          </NForm>
        </NTabPane>
      </NTabs>
    </div>
  </div>
</template>

<style scoped>
.login {
  max-width: 420px;
  margin: 0 auto;
  padding-top: 8vh;
}

.login__brand {
  text-align: center;
  margin-bottom: 18px;
}

.login__brand h1 {
  margin: 0 0 6px;
  font-size: 22px;
}
</style>
