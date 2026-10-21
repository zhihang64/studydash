<script setup lang="ts">
/**
 * 应用外壳：左侧导航 + 顶栏 + 内容区，外加两种「整页没法用」的兜底。
 *
 * 兜底比功能重要：普通人打开页面看到白屏，唯一的结论是「这东西坏了」，
 * 不会想到去看控制台。所以这里任何一条路都保证屏幕上有一句人话。
 */
import { computed, onMounted, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import ToastStack from '@/components/ToastStack.vue'
import { fatal } from '@/composables/useFatal'
import { THEME_ICON, THEME_LABEL, cycleTheme, themeChoice } from '@/composables/useTheme'
import { NAV } from '@/router'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'
import { useTasksStore } from '@/stores/tasks'

const route = useRoute()
const session = useSessionStore()
const tasks = useTasksStore()
const settings = useSettingsStore()

const blocked = computed(() => session.notLaunched || session.authBroken || session.offline || !!fatal.value)

const blockedTitle = computed(() => {
  if (session.notLaunched) return '这个页面要通过 studydash 启动'
  if (fatal.value) return fatal.value.title
  if (session.authBroken) return '访问令牌已经失效'
  return '连不上本机的后端服务'
})

const blockedBody = computed(() => {
  if (session.notLaunched) {
    return '前端要连本机的后端服务才能拿到数据，直接双击 HTML 是不行的。在命令行里跑 studydash web，浏览器会自己打开正确的地址。'
  }
  if (fatal.value) return fatal.value.body
  if (session.authBroken) {
    return 'studydash 每次启动都会重新生成一个访问令牌，所以重启之后旧页面就用不了了。这不是出错，刷新一下就好。'
  }
  return '可能是 studydash 被关掉了，或者它监听的不是这个端口。确认服务还在跑，然后刷新页面。'
})

const blockedAction = computed(() => {
  if (fatal.value) return fatal.value.action
  if (session.notLaunched) return '在终端里运行：studydash web'
  return '刷新页面'
})

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? '总览')

function reload(): void {
  window.location.reload()
}

/** 每个导航项带上「这一页有事」的角标，省得用户靠点进去才发现。 */
function badge(path: string): string {
  if (path === '/' || path === '/timeline') {
    const count = tasks.active.length
    return count ? String(count) : ''
  }
  return ''
}

onMounted(() => {
  void session.load()
  void tasks.hydrate()
  // 主题偏好存在后端，而设置页可能一直不会被打开 —— 进来就认一次，
  // 否则换个浏览器会发现服务端存的深色不见了。
  void settings.loadTheme()
})

// 「已运行」只在设置页露脸，没必要在别的页面也每 30 秒敲一次后端
watch(
  () => route.path,
  (path) => {
    if (path === '/settings') void session.refreshHealth()
  },
  { immediate: true },
)
</script>

<template>
  <!-- ------------------------- 整页不可用 ------------------------- -->
  <div v-if="blocked" class="blocked">
    <div class="blocked__card">
      <p class="blocked__mark" aria-hidden="true">◌</p>
      <h1 class="blocked__title">{{ blockedTitle }}</h1>
      <p class="blocked__body">{{ blockedBody }}</p>
      <p class="blocked__action">{{ blockedAction }}</p>
      <button v-if="!session.notLaunched" type="button" class="btn btn--primary" @click="reload">
        刷新页面
      </button>
    </div>
  </div>

  <!-- ------------------------- 正常外壳 ------------------------- -->
  <div v-else class="shell">
    <a class="skip" href="#main">跳到主要内容</a>

    <aside class="side">
      <RouterLink class="brand" to="/">
        <span class="brand__mark" aria-hidden="true">▦</span>
        <span class="brand__text">
          <strong>studydash</strong>
          <small>学习进度看板</small>
        </span>
      </RouterLink>

      <nav class="nav" aria-label="主导航">
        <RouterLink
          v-for="item in NAV"
          :key="item.path"
          class="nav__item"
          :to="item.path"
          :aria-current="route.path === item.path ? 'page' : undefined"
        >
          <span class="nav__icon" aria-hidden="true">{{ item.icon }}</span>
          <span class="nav__text">
            <span class="nav__title">{{ item.title }}</span>
            <span class="nav__hint">{{ item.hint }}</span>
          </span>
          <span v-if="badge(item.path)" class="nav__badge">{{ badge(item.path) }}</span>
        </RouterLink>
      </nav>

      <footer class="side__foot">
        <p class="side__note">
          默认只读：抓取、看板、提醒都不动平台数据。唯一例外是课程页每张超星课程卡右侧的「上报任务点」——点两下确认才会真的写。
        </p>
        <p class="side__meta mono">v{{ session.version || session.bootVersion || '—' }}</p>
      </footer>
    </aside>

    <div class="body">
      <header class="top">
        <h1 class="top__title">{{ pageTitle }}</h1>

        <div class="top__tools">
          <span v-if="tasks.active.length" class="pill pill--warn top__busy">
            {{ tasks.active.length }} 个任务在跑
          </span>
          <button
            type="button"
            class="btn btn--ghost btn--sm"
            :title="`主题：${THEME_LABEL[themeChoice]}（点击切换）`"
            :aria-label="`当前主题 ${THEME_LABEL[themeChoice]}，点击切换`"
            @click="cycleTheme()"
          >
            <span aria-hidden="true">{{ THEME_ICON[themeChoice] }}</span>
          </button>
          <RouterLink class="btn btn--ghost btn--sm" to="/settings">设置</RouterLink>
        </div>
      </header>

      <main id="main" class="main">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </main>
    </div>

    <ToastStack />
  </div>
</template>

<style scoped>
/* ---------------------------------- 不可用 --------------------------------- */

.blocked {
  display: grid;
  place-items: center;
  min-height: 100vh;
  padding: var(--s-5);
  background: var(--canvas);
}

.blocked__card {
  max-width: 34rem;
  padding: var(--s-6);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  background: var(--surface);
  box-shadow: var(--shadow-md);
}

.blocked__mark {
  font-size: 2rem;
  color: var(--text-faint);
}

.blocked__title {
  margin-top: var(--s-2);
  font-size: var(--fs-lg);
}

.blocked__body {
  margin-top: var(--s-3);
  color: var(--text-muted);
  line-height: 1.85;
}

.blocked__action {
  margin: var(--s-4) 0;
  padding: var(--s-3) var(--s-4);
  border-radius: var(--r-sm);
  background: var(--surface-3);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

/* ----------------------------------- 外壳 ---------------------------------- */

.shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  min-height: 100vh;
}

.skip {
  position: absolute;
  left: -9999px;
}

.skip:focus {
  left: var(--s-4);
  top: var(--s-4);
  z-index: 50;
  padding: var(--s-2) var(--s-4);
  border-radius: var(--r-sm);
  background: var(--accent);
  color: #fff;
}

/* ---------------------------------- 侧栏 ---------------------------------- */

.side {
  display: flex;
  flex-direction: column;
  gap: var(--s-5);
  padding: var(--s-5) var(--s-4);
  border-right: 1px solid var(--border);
  background: var(--surface);
}

.brand {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  color: inherit;
  text-decoration: none;
}

.brand__mark {
  display: grid;
  place-items: center;
  width: 2.25rem;
  height: 2.25rem;
  border-radius: var(--r-sm);
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
  font-size: 1.1rem;
}

.brand__text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.brand__text strong {
  letter-spacing: -0.01em;
}

.brand__text small {
  color: var(--text-faint);
  font-size: var(--fs-xs);
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  flex: 1;
}

.nav__item {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  padding: var(--s-2) var(--s-3);
  border-radius: var(--r-sm);
  color: var(--text-muted);
  text-decoration: none;
  transition:
    background var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}

.nav__item:hover {
  background: var(--surface-3);
  color: var(--text);
}

.nav__item[aria-current='page'] {
  background: var(--accent-soft);
  color: var(--accent);
}

.nav__icon {
  flex: none;
  width: 1.1rem;
  font-size: 0.95rem;
  text-align: center;
}

.nav__text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.4;
}

.nav__title {
  font-weight: 600;
  font-size: var(--fs-sm);
}

.nav__hint {
  color: var(--text-faint);
  font-size: var(--fs-xs);
}

.nav__badge {
  margin-left: auto;
  padding: 0.05rem 0.4rem;
  border-radius: 999px;
  background: var(--warn);
  color: #fff;
  font-size: var(--fs-xs);
  font-weight: 700;
}

.side__foot {
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--s-4);
}

.side__note {
  color: var(--text-faint);
  font-size: var(--fs-xs);
  line-height: 1.7;
}

.side__meta {
  margin-top: var(--s-2);
  color: var(--text-faint);
  font-size: var(--fs-xs);
}

/* ---------------------------------- 主体 ---------------------------------- */

.body {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.top {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  gap: var(--s-4);
  align-items: center;
  justify-content: space-between;
  padding: var(--s-4) var(--s-6);
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--canvas) 88%, transparent);
  backdrop-filter: blur(8px);
}

.top__title {
  font-size: var(--fs-lg);
}

.top__tools {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  flex-wrap: wrap;
}

.top__busy {
  font-size: var(--fs-xs);
}

.main {
  padding: var(--s-6);
}

@media (max-width: 900px) {
  .shell {
    grid-template-columns: minmax(0, 1fr);
  }

  .side {
    border-right: none;
    border-bottom: 1px solid var(--border);
    gap: var(--s-4);
  }

  .nav {
    flex-direction: row;
    flex-wrap: wrap;
  }

  .nav__hint {
    display: none;
  }

  .side__foot {
    display: none;
  }

  .main {
    padding: var(--s-4);
  }

  .top {
    padding: var(--s-3) var(--s-4);
  }
}
</style>
