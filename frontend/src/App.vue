<script setup lang="ts">
/**
 * 应用外壳：主题/语言 + 顶栏 + 移动端底部标签栏。
 *
 * 为什么用 hash/底部 tab 这种"移动优先"的布局而不是 PC 侧边栏：
 * 这个项目的终局是装进手机（DEVELOPMENT §13），桌面只是开发时的调试窗口。
 * 反过来做（先 PC 后改移动端）几乎一定会漏掉安全区、键盘顶起、返回键这三件事。
 */
import { computed, defineComponent, h } from 'vue'
import {
  NConfigProvider,
  NDialogProvider,
  NDropdown,
  NMessageProvider,
  NTabPane,
  NTabs,
  zhCN,
  dateZhCN,
  type DropdownOption,
} from 'naive-ui'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { authApi } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

/** 需要 useMessage/useDialog 的内容必须放在 Provider **里面**，所以拆一个 Inner 组件 */
const AppInner = defineComponent({
  setup() {
    const route = useRoute()
    const router = useRouter()
    const auth = useAuthStore()

    const tabs = [
      { name: 'kb', label: '知识库', to: { name: 'kb' } },
      { name: 'chat', label: '对话', to: { name: 'chat' } },
      { name: 'search', label: '搜索', to: { name: 'search' } },
      { name: 'me', label: '我的', to: { name: 'me' } },
    ]

    const pageTitle = computed(() => String(route.meta.title ?? 'AI 知识库'))
    // 顶栏/底部 tab 的开关交给路由 meta：以后再加匿名页（重置密码之类）不用回来改这里
    const showChrome = computed(() => route.name !== 'login' && route.meta.chrome !== false)

    const userOptions: DropdownOption[] = [
      { label: '账号设置', key: 'me' },
      { label: '退出登录', key: 'logout' },
    ]

    async function onUserSelect(key: string): Promise<void> {
      if (key === 'me') {
        void router.push({ name: 'me' })
        return
      }
      // 登出要把服务端的 token 也吊销掉，只在本地清 = 旧 token 到过期前一直有效
      try {
        await authApi.logout()
      } catch (error) {
        if (!(error instanceof ApiError)) throw error
      } finally {
        auth.logout()
        void router.replace({ name: 'login' })
      }
    }

    return () =>
      h('div', { class: 'kb-app' }, [
        showChrome.value
          ? h('header', { class: 'kb-header' }, [
              h('div', { class: 'kb-header__title' }, pageTitle.value),
              h(
                NDropdown,
                { trigger: 'click', options: userOptions, onSelect: onUserSelect },
                {
                  default: () =>
                    h('button', { class: 'kb-header__user' }, auth.user?.username ?? auth.username ?? '我的'),
                },
              ),
            ])
          : null,
        h('main', { class: showChrome.value ? 'kb-main kb-page--with-tabs' : 'kb-main' }, [h(RouterView)]),
        showChrome.value
          ? h(
              'nav',
              { class: 'kb-tabs' },
              tabs.map((tab) =>
                h(
                  RouterLink,
                  { to: tab.to, class: 'kb-tabs__item', activeClass: 'is-active' },
                  { default: () => tab.label },
                ),
              ),
            )
          : null,
      ])
  },
})
</script>

<template>
  <NConfigProvider :locale="zhCN" :date-locale="dateZhCN">
    <NMessageProvider :duration="4000">
      <NDialogProvider>
        <AppInner />
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>

<style>
/* 这段是全局的（不带 scoped），因为它属于外壳布局 */
.kb-app {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.kb-header {
  position: sticky;
  top: 0;
  z-index: 20;
  height: var(--kb-header-height);
  padding: 0 12px;
  padding-top: env(safe-area-inset-top);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--kb-card-bg);
  border-bottom: 1px solid var(--kb-border);
}

.kb-header__title {
  font-weight: 600;
}

.kb-header__user {
  border: 1px solid var(--kb-border);
  background: transparent;
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 13px;
  color: var(--kb-text);
}

.kb-main {
  flex: 1;
}

.kb-tabs {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 30;
  display: flex;
  height: calc(var(--kb-tab-height) + env(safe-area-inset-bottom));
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--kb-card-bg);
  border-top: 1px solid var(--kb-border);
}

.kb-tabs__item {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  color: var(--kb-muted);
  text-decoration: none;
}

.kb-tabs__item.is-active {
  color: #18a058;
  font-weight: 600;
}

/* 桌面宽屏时底部 tab 换成顶栏右侧菜单，手机/桌面同一份代码 */
@media (min-width: 820px) {
  .kb-tabs {
    top: 0;
    bottom: auto;
    height: var(--kb-header-height);
    padding-bottom: 0;
    justify-content: flex-end;
    gap: 4px;
    border-top: none;
    border-bottom: 1px solid var(--kb-border);
  }

  .kb-tabs__item {
    flex: none;
    padding: 0 14px;
  }

  .kb-header {
    border-bottom: none;
    padding-top: 0;
  }

  .kb-app {
    padding-top: var(--kb-header-height);
  }

  .kb-main {
    max-width: 1080px;
    width: 100%;
    margin: 0 auto;
  }
}
</style>
