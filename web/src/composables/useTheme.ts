/**
 * 深浅色主题。
 *
 * 三态：``auto`` 跟随系统，``light`` / ``dark`` 由用户钦定。**真正的偏好存在
 * 后端**（``config.json`` 的 ``ui.theme``），localStorage 只是为了让首屏别闪——
 * 页面加载时还没法发请求，但必须已经知道该画哪一套颜色。
 *
 * 所以写入顺序是：localStorage 立刻写 → 界面立刻变 → 后端异步存。
 */

import { computed, ref } from 'vue'

export type ThemeChoice = 'auto' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'studydash.theme'

const choice = ref<ThemeChoice>('auto')
const systemDark = ref(false)

const media = typeof window !== 'undefined' ? window.matchMedia('(prefers-color-scheme: dark)') : null

function readStored(): ThemeChoice {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === 'light' || raw === 'dark' || raw === 'auto') return raw
  } catch {
    // 隐私模式 / 被策略禁掉时 localStorage 会抛。主题不该因此挂掉。
  }
  return 'auto'
}

/** 把当前选择落到 <html> 上。CSS 只认 data-theme，不认 media query。 */
export function applyTheme(): ResolvedTheme {
  const resolved: ResolvedTheme = choice.value === 'auto' ? (systemDark.value ? 'dark' : 'light') : choice.value
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = resolved
  }
  return resolved
}

export function initTheme(): void {
  if (media) {
    systemDark.value = media.matches
    media.addEventListener('change', (event) => {
      systemDark.value = event.matches
      // 只有 auto 才跟随系统；用户钦定的主题不该被系统切换抢走。
      if (choice.value === 'auto') applyTheme()
    })
  }
  choice.value = readStored()
  applyTheme()
}

/**
 * 由设置接口回填。后端是权威，所以会覆盖 localStorage。
 * 返回是否真的变了，调用方据此决定要不要重画。
 */
export function setThemeFromServer(value: string | undefined): boolean {
  if (value !== 'light' && value !== 'dark' && value !== 'auto') return false
  if (value === choice.value) return false
  choice.value = value
  storeTheme(value)
  applyTheme()
  return true
}

function storeTheme(value: ThemeChoice): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, value)
  } catch {
    // 存不下就算了，本次会话仍然生效
  }
}

/** 用户点按钮：立刻生效并记下来，返回新值给调用方同步到后端。 */
export function setTheme(value: ThemeChoice): ThemeChoice {
  choice.value = value
  storeTheme(value)
  applyTheme()
  return value
}

/** auto → light → dark → auto 循环。按钮点一下就走一格，不用下拉框。 */
export function cycleTheme(): ThemeChoice {
  const next: ThemeChoice = choice.value === 'auto' ? 'light' : choice.value === 'light' ? 'dark' : 'auto'
  return setTheme(next)
}

export const themeChoice = computed(() => choice.value)
export const resolvedTheme = computed<ResolvedTheme>(() =>
  choice.value === 'auto' ? (systemDark.value ? 'dark' : 'light') : choice.value,
)

export const THEME_LABEL: Record<ThemeChoice, string> = {
  auto: '跟随系统',
  light: '浅色',
  dark: '深色',
}

export const THEME_ICON: Record<ThemeChoice, string> = {
  auto: '◐',
  light: '☀',
  dark: '☾',
}
