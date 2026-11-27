import './styles/main.css'

import { VueQueryPlugin } from '@tanstack/vue-query'
import { createApp } from 'vue'
import { createPinia } from 'pinia'

import { onUnauthorized } from './api/client'
import App from './App.vue'
import { router } from './router'

const app = createApp(App)

// pinia 必须在 router 之前装：路由守卫里要用 useAuthStore()
app.use(createPinia())
app.use(router)
app.use(VueQueryPlugin, {
  queryClientConfig: {
    defaultOptions: {
      queries: {
        // retry:1 而不是默认 3：手机端弱网下"转圈半分钟才报错"体验很差，
        // 而且后端的 5xx 大多是需要人看一眼的，不是重试能解决的
        retry: 1,
        refetchOnWindowFocus: false,
        staleTime: 15_000,
      },
    },
  },
})

// 401 只在这里跳登录：业务代码只管抛错，不各写各的"跳登录页"
onUnauthorized(() => {
  if (router.currentRoute.value.name !== 'login') {
    router.replace({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

void router.isReady().then(() => app.mount('#app'))
