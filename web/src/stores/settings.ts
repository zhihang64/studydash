/**
 * 设置与环境自检。主题这类「要立刻生效」的项先改本地再发请求，
 * 免得点一下开关还要等一下才有反应。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { CheckItem, HttpSettings, SettingsPatch, SettingsView } from '@/api/types'
import { setThemeFromServer } from '@/composables/useTheme'

const DEFAULT_HTTP: HttpSettings = {
  timeout: 8,
  delay: 0.4,
  retries: 2,
  user_agent: '',
}

export const useSettingsStore = defineStore('settings', () => {
  const view = ref<SettingsView | null>(null)
  const checks = ref<CheckItem[]>([])
  const loading = ref(false)
  const saving = ref(false)
  const checking = ref(false)
  const error = ref('')

  const http = computed<HttpSettings>(() => view.value?.http ?? DEFAULT_HTTP)
  const configPath = computed(() => view.value?.config_path ?? '')
  const home = computed(() => view.value?.home ?? '')
  const alertDays = computed(() => view.value?.ui?.alert_days ?? 3)

  const failedChecks = computed(() => checks.value.filter((c) => c.level === 'fail'))
  const warnedChecks = computed(() => checks.value.filter((c) => c.level === 'warn'))
  /** 自检整体过没过：只有 fail 算不过，warn 是「能跑但得知道」。 */
  const healthy = computed(() => checks.value.length > 0 && failedChecks.value.length === 0)

  async function load(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      view.value = await api.settings()
      // 后端才是主题偏好的权威：localStorage 只是为了首屏不闪。
      setThemeFromServer(view.value.ui?.theme)
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  /**
   * 启动时只把主题认回来。
   *
   * 后端是主题偏好的权威，但 `load()` 只在打开「设置」页时才被调用 —— 于是换个
   * 浏览器（或清了 localStorage）进来，服务端存着的深色就白存了，直到用户去点
   * 一次设置才生效。这里专门做一件小事：把 ui.theme 拉回来并应用。
   * 失败就当作没这回事（按 localStorage / 系统走），主题拉不到不该拖垮启动。
   */
  async function loadTheme(): Promise<void> {
    try {
      const view = await api.settings()
      setThemeFromServer(view.ui?.theme)
    } catch {
      // 静默：调用方是首屏，弹错更糟
    }
  }

  async function save(patch: SettingsPatch): Promise<boolean> {
    saving.value = true
    try {
      view.value = await api.patchSettings(patch)
      setThemeFromServer(view.value.ui?.theme)
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      return false
    } finally {
      saving.value = false
    }
  }

  async function runDoctor(): Promise<void> {
    checking.value = true
    try {
      const data = await api.doctor()
      checks.value = data.checks ?? []
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      checking.value = false
    }
  }

  return {
    view,
    checks,
    loading,
    saving,
    checking,
    error,
    http,
    configPath,
    home,
    alertDays,
    failedChecks,
    warnedChecks,
    healthy,
    load,
    loadTheme,
    save,
    runDoctor,
  }
})
