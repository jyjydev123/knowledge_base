<!--
  重置密码（TODO §6.1 的前端那一半）。验证码版：两步 —— 申请验证码 → 填码 + 新密码。

  四个和后端约定有关的点，动这个文件前先读：
    1. 两个接口的返回文案**逐字显示**，不要在这里加"账号不存在"之类的分支判断 ——
       后端刻意让「没这个账号 / 码错 / 码过期」逐字相同（防枚举），前端猜一次就把它拆了；
    2. 验证码必须和 account 一起提交：6 位数字不自带身份（长令牌才自带），
       服务端要靠 account 找到那个人那条待用的码；
    3. 新密码这里限 8 位，而后端 schema 是 6-72：刻意和「我的-修改密码」一致，
       免得同一个账号两种强度口径；真要放宽就两个页面一起改；
    4. 改密成功后**不自动登录**：后端已经把这个账号所有端下线了，此时手里那张旧 token
       是废的，直接跳登录页比"假装还活着"更少一次诡异 401。
-->
<script setup lang="ts">
import { NAlert, NButton, NForm, NFormItem, NInput, NTag, useMessage } from 'naive-ui'
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { authApi } from '@/api/endpoints'

const message = useMessage()
const route = useRoute()
const router = useRouter()

const CODE_LENGTH = 6
// 支持 ?account= 直接落到第二步（管理员把账号告诉用户时省事）；没有就正常从第一步走
const initialAccount = typeof route.query.account === 'string' ? route.query.account.trim() : ''
const step = ref<'apply' | 'verify'>(initialAccount ? 'verify' : 'apply')

const account = ref(initialAccount)
const code = ref('')
const password = ref('')
const password2 = ref('')
const busy = ref(false)
const hint = ref('')

// 重发冷却：后端默认 60s（RESET_REQUEST_COOLDOWN_SECONDS），这里跟着倒数，
// 免得用户狂点然后吃 429 —— 倒计时只是体验，真正的闸门在服务端
const secondsLeft = ref(0)
let timer: ReturnType<typeof setInterval> | null = null

const titled = computed(() => (step.value === 'apply' ? '申请验证码' : '输入验证码并设置新密码'))

function startCooldown(seconds: number): void {
  secondsLeft.value = seconds
  if (timer) clearInterval(timer)
  timer = setInterval(() => {
    secondsLeft.value -= 1
    if (secondsLeft.value <= 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  }, 1000)
}

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

function onCodeInput(value: string): void {
  // 只留数字：手机输入法容易带空格/全角，后端 schema 见到非数字直接 422
  code.value = value.replace(/\D/g, '').slice(0, CODE_LENGTH)
}

async function apply(): Promise<void> {
  if (busy.value) return
  const value = account.value.trim()
  if (!value) {
    message.warning('请输入用户名或邮箱')
    return
  }
  busy.value = true
  try {
    hint.value = await authApi.forgotPassword(value)
    step.value = 'verify'
    startCooldown(60)
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '申请失败，请稍后重试')
  } finally {
    busy.value = false
  }
}

async function verify(): Promise<void> {
  if (busy.value) return
  if (code.value.length !== CODE_LENGTH) {
    message.warning(`请输入 ${CODE_LENGTH} 位数字验证码`)
    return
  }
  if (password.value.length < 8) {
    message.warning('新密码至少 8 位')
    return
  }
  if (password.value !== password2.value) {
    message.warning('两次输入的新密码不一致')
    return
  }
  busy.value = true
  try {
    message.success(await authApi.verifyResetCode(account.value.trim(), code.value, password.value))
    await router.replace({ name: 'login' })
  } catch (error) {
    message.error(error instanceof ApiError ? error.message : '重置失败，请稍后重试')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="kb-page reset">
    <div class="reset__head">
      <h1>重置密码</h1>
      <p class="kb-muted">{{ titled }}</p>
    </div>

    <div class="kb-card">
      <div class="reset__steps">
        <NTag :type="step === 'apply' ? 'primary' : 'default'" size="small" round>1 申请验证码</NTag>
        <NTag :type="step === 'verify' ? 'primary' : 'default'" size="small" round>2 设新密码</NTag>
      </div>

      <NForm v-if="step === 'apply'" @submit.prevent="apply">
        <NFormItem label="用户名或邮箱">
          <NInput v-model:value="account" placeholder="zhangsan 或 you@example.com" @keyup.enter="apply" />
        </NFormItem>
        <NButton type="primary" block :loading="busy" @click="apply">发送验证码</NButton>
        <NAlert v-if="hint" type="info" :bordered="false" class="reset__hint">{{ hint }}</NAlert>
        <p class="kb-muted reset__tip">
          验证码走后端配置的送达通道（<code>.env</code> 里的 <code>RESET_DELIVERY</code>）：
          <code>smtp</code> 发邮件；本机自测默认是 <code>console</code>，码会打在**服务端终端/日志**里
          （<code>grep 验证码 logs/app.log</code>）；<code>file</code> 则落 <code>var/reset/</code>。
        </p>
      </NForm>

      <NForm v-else @submit.prevent="verify">
        <NFormItem label="账号">
          <NInput v-model:value="account" placeholder="申请验证码时用的那个账号" />
        </NFormItem>
        <NFormItem label="验证码">
          <NInput
            :value="code"
            placeholder="6 位数字"
            inputmode="numeric"
            autocomplete="one-time-code"
            class="reset__code"
            @update:value="onCodeInput"
            @keyup.enter="verify"
          />
        </NFormItem>
        <NFormItem label="新密码">
          <NInput v-model:value="password" type="password" show-password-on="click" placeholder="至少 8 位" @keyup.enter="verify" />
        </NFormItem>
        <NFormItem label="确认新密码">
          <NInput v-model:value="password2" type="password" show-password-on="click" placeholder="再输一次" @keyup.enter="verify" />
        </NFormItem>
        <NButton type="primary" block :loading="busy" @click="verify">设置新密码</NButton>
        <div class="reset__resend">
          <NButton text size="small" :disabled="secondsLeft > 0 || busy" @click="apply">
            {{ secondsLeft > 0 ? `重新发送（${secondsLeft}s）` : '重新发送验证码' }}
          </NButton>
          <NButton text size="small" @click="step = 'apply'">换个账号</NButton>
        </div>
        <p class="kb-muted reset__tip">
          验证码 {{ CODE_LENGTH }} 位、10 分钟内有效、最多试 5 次（次数用完这条就作废，重新申请）。
          成功后这个账号在**所有设备**上的登录都会失效，需要用新密码重新登录。
        </p>
      </NForm>

      <div class="reset__back">
        <NButton text size="small" @click="router.replace({ name: 'login' })">返回登录</NButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.reset {
  max-width: 460px;
  margin: 0 auto;
  padding-top: 6vh;
}

.reset__head {
  margin-bottom: 14px;
}

.reset__head h1 {
  margin: 0 0 4px;
  font-size: 20px;
}

.reset__steps {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
}

.reset__hint {
  margin-top: 12px;
}

.reset__code :deep(input) {
  letter-spacing: 6px;
  font-size: 18px;
  text-align: center;
}

.reset__resend {
  display: flex;
  justify-content: space-between;
  margin-top: 10px;
}

.reset__tip {
  margin: 12px 0 0;
  line-height: 1.6;
}

.reset__back {
  margin-top: 14px;
  text-align: center;
}
</style>
