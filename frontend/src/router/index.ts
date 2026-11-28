/**
 * 路由。
 *
 * history 模式的选择是有原因的：
 *   - 浏览器里用 web history（路径好看）；
 *   - **装进 Capacitor 壳之后必须换 hash 模式**：壳里的页面是 capacitor://localhost 或
 *     原生 file://，没有服务器能把 /documents 这种路径回退到 index.html，
 *     刷新或深链就 404。判断依据是 window.Capacitor 是否存在（壳注入的全局对象）。
 */

import { createRouter, createWebHashHistory, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { getToken } from '@/utils/storage'

const inNativeShell = typeof window !== 'undefined' && 'Capacitor' in window

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: { name: 'kb' } },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/reset-password',
    name: 'reset-password',
    component: () => import('@/views/ResetPasswordView.vue'),
    // public：用户正因为登不进去才打开这条链接；chrome:false 和登录页一样不显示底部 tab。
    // 已登录也**不**弹回知识库：他可能是从另一台设备的邮件里点的链接，那台确实登录着。
    meta: { public: true, chrome: false, title: '重置密码' },
  },
  { path: '/kb', name: 'kb', component: () => import('@/views/KbListView.vue'), meta: { title: '知识库' } },
  {
    path: '/kb/:kbId/documents',
    name: 'documents',
    component: () => import('@/views/DocumentsView.vue'),
    meta: { title: '文档' },
  },
  {
    path: '/kb/:kbId/shares',
    name: 'shares',
    component: () => import('@/views/SharesView.vue'),
    meta: { title: '共享授权' },
  },
  { path: '/chat', name: 'chat', component: () => import('@/views/ChatView.vue'), meta: { title: '对话' } },
  {
    path: '/chat/:conversationId',
    name: 'chat-detail',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '对话' },
  },
  { path: '/search', name: 'search', component: () => import('@/views/SearchView.vue'), meta: { title: '搜索' } },
  { path: '/me', name: 'me', component: () => import('@/views/ProfileView.vue'), meta: { title: '我的' } },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFoundView.vue'), meta: { public: true } },
]

export const router = createRouter({
  history: inNativeShell ? createWebHashHistory() : createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: (_to, _from, saved) => saved ?? { top: 0 },
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已经登录就别在登录页打转
    if (to.name === 'login' && auth.isAuthed) return { name: 'kb' }
    return true
  }
  if (!auth.isAuthed) return { name: 'login', query: { redirect: to.fullPath } }

  // 带 token 但还没拿到用户资料：补一次，失败就当没登录
  if (!auth.user) {
    const ok = await auth.refresh()
    if (!ok) {
      auth.logout()
      return { name: 'login', query: { redirect: to.fullPath } }
    }
  }
  return true
})

export function bootToken(): boolean {
  return Boolean(getToken())
}
