/**
 * 登录态：token 是唯一凭据，用户资料只是它的附属缓存。
 *
 * 三条规矩：
 *   1. token 只从 utils/storage 进出（壳里换成原生 Preferences 时只改那一个文件）；
 *   2. **刷新页面后要重新拉一次 /user/info**：不能因为 localStorage 里有 token 就假定
 *      账号还有效（被禁用/注销是服务端状态，本地猜不出来）；
 *   3. 401 一律走 logout(本地清理) + 跳登录，由 api/client.ts 的 onUnauthorized 触发。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { authApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { UserResponse } from '@/api/types'
import { clearToken, getLastUsername, getToken, setLastUsername, setToken } from '@/utils/storage'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(getToken())
  const user = ref<UserResponse | null>(null)
  // 身份还没验过一次之前不要渲染业务页面，否则每个请求都会先 401 再跳登录，很难看
  const resolving = ref(false)

  const isAuthed = computed(() => Boolean(token.value))
  const isAdmin = computed(() => user.value?.role === 'admin')
  const username = computed(() => user.value?.username ?? getLastUsername())

  function adopt(nextToken: string, nextUser: UserResponse | null): void {
    token.value = nextToken
    user.value = nextUser
    setToken(nextToken)
    if (nextUser) setLastUsername(nextUser.username)
  }

  async function login(account: string, password: string): Promise<void> {
    const data = await authApi.login(account, password)
    adopt(data.access_token, data.user ?? null)
  }

  async function register(usernameValue: string, email: string, password: string): Promise<void> {
    await authApi.register({ username: usernameValue, email, password })
    // 注册完直接登录：手机端"注册完还要再输一次账号密码"是最劝退的流程
    await login(usernameValue, password)
  }

  async function refresh(): Promise<boolean> {
    if (!token.value) return false
    resolving.value = true
    try {
      user.value = await authApi.info()
      return true
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return false
      // 网络类错误不清 token：地铁里断网刷新不该把人踢下线
      return Boolean(user.value)
    } finally {
      resolving.value = false
    }
  }

  function logout(keepUsername = true): void {
    token.value = ''
    user.value = null
    clearToken()
    if (!keepUsername) setLastUsername('')
  }

  return { token, user, resolving, isAuthed, isAdmin, username, login, register, refresh, logout, adopt }
})
