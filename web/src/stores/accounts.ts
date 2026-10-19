/**
 * 账号条目。**这里只管「配置里有哪些账号」，不负责登录**——登录是任务，
 * 见 stores/tasks。分开的好处是账号列表能在登录跑的几十秒里保持可交互。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { AccountView } from '@/api/types'

export const useAccountsStore = defineStore('accounts', () => {
  const items = ref<AccountView[]>([])
  const loading = ref(false)
  const error = ref('')
  const loaded = ref(false)

  const byPlatform = computed(() => {
    const map = new Map<string, AccountView[]>()
    items.value.forEach((a) => {
      const list = map.get(a.platform)
      if (list) list.push(a)
      else map.set(a.platform, [a])
    })
    return map
  })

  const enabled = computed(() => items.value.filter((a) => a.enabled))

  /** 抓不到数据的账号。账号页和总览都拿它来提醒。 */
  const broken = computed(() => items.value.filter((a) => !a.ok))

  /** 哪些平台已经绑过账号了。登录表单据此决定要不要提示「会新建一个账号」。 */
  function has(platform: string): boolean {
    return items.value.some((a) => a.platform === platform)
  }

  function countOf(platform: string): number {
    return items.value.filter((a) => a.platform === platform).length
  }

  function find(platform: string, label: string): AccountView | undefined {
    return items.value.find((a) => a.platform === platform && (a.label || '默认') === label)
  }

  async function load(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const data = await api.accounts()
      items.value = data.accounts ?? []
      loaded.value = true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  async function add(platform: string, label = '', enabledFlag = true): Promise<boolean> {
    try {
      const data = await api.createAccount({ platform, label, enabled: enabledFlag })
      items.value = data.accounts ?? items.value
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      throw err
    }
  }

  async function patch(
    platform: string,
    label: string,
    payload: { enabled?: boolean; label?: string },
  ): Promise<boolean> {
    try {
      const data = await api.patchAccount(platform, label, payload)
      items.value = data.accounts ?? items.value
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      throw err
    }
  }

  async function remove(platform: string, label: string): Promise<boolean> {
    try {
      await api.deleteAccount(platform, label)
      // 204 不带新列表，本地先摘掉，再回读一次跟后端对齐
      items.value = items.value.filter(
        (a) => !(a.platform === platform && (a.label || '默认') === label),
      )
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      throw err
    }
  }

  return {
    items,
    loading,
    error,
    loaded,
    byPlatform,
    enabled,
    broken,
    has,
    countOf,
    find,
    load,
    add,
    patch,
    remove,
  }
})
