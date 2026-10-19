/**
 * 会话：这次网页版运行的基本信息。
 *
 * 单独一个 store 是为了让「令牌不对 / 页面不是 studydash 起的」这两种情况有一个
 * 明确的落点——界面靠 `launched` 与 `authBroken` 决定是显示应用还是显示引导页，
 * 而不是让每个组件各自去猜为什么请求都失败了。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, api, bootInfo, requireToken } from '@/api/client'
import type { HealthInfo, VersionInfo } from '@/api/types'

export const useSessionStore = defineStore('session', () => {
  const version = ref('')
  const python = ref('')
  const platform = ref('')
  const frozen = ref(false)
  const schema = ref(0)
  const uptimeS = ref(0)

  const checking = ref(false)
  /** 页面不是 studydash 启动的：拿不到令牌，什么都干不了。 */
  const notLaunched = ref(false)
  /** 令牌不对（服务重启过）。只需要刷新页面。 */
  const authBroken = ref(false)
  /** 后端整个连不上。 */
  const offline = ref(false)
  const lastError = ref('')

  const launched = computed(() => !notLaunched.value)
  const ready = computed(() => launched.value && !authBroken.value && !offline.value)

  function describe(err: unknown): string {
    if (err instanceof ApiError) return err.message
    if (err instanceof Error) return err.message
    return String(err)
  }

  /** 统一的错误归类。返回 true 表示这是个「全局性」故障，调用方不用再弹提示。 */
  function absorb(err: unknown): boolean {
    lastError.value = describe(err)
    if (err instanceof Error && err.name === 'NotLaunchedError') {
      notLaunched.value = true
      return true
    }
    if (err instanceof ApiError) {
      if (err.code === 'unauthorized') {
        authBroken.value = true
        return true
      }
      if (err.status === 0) {
        offline.value = true
        return true
      }
    }
    return false
  }

  async function load(): Promise<void> {
    try {
      requireToken()
    } catch (err) {
      absorb(err)
      return
    }

    checking.value = true
    try {
      const info: VersionInfo & Partial<HealthInfo> = await api.version()
      version.value = info.version
      python.value = info.python
      platform.value = info.platform
      frozen.value = info.frozen
      schema.value = info.schema
      notLaunched.value = false
      authBroken.value = false
      offline.value = false
    } catch (err) {
      absorb(err)
    } finally {
      checking.value = false
    }
  }

  /** 「已运行」每 30 秒刷新一次就够，没必要为了一个数字狂发请求。 */
  async function refreshHealth(): Promise<void> {
    if (!ready.value) return
    try {
      const info = await api.health()
      uptimeS.value = info.uptime_s
    } catch {
      // 心跳失败不影响任何功能，静默
    }
  }

  const boot = bootInfo()
  const bootVersion = boot?.version ?? ''

  return {
    version,
    python,
    platform,
    frozen,
    schema,
    uptimeS,
    checking,
    notLaunched,
    authBroken,
    offline,
    lastError,
    bootVersion,
    launched,
    ready,
    absorb,
    describe,
    load,
    refreshHealth,
  }
})
