/**
 * 平台名录。改动很少，所以加载一次就缓存住，全站共用。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { PlatformMeta, ProviderStatus, TaskKind } from '@/api/types'

/** 没查到就退回中性灰，不要凭空猜一个颜色。 */
const FALLBACK_COLOR = '#64748b'

export const useCatalogStore = defineStore('catalog', () => {
  const platforms = ref<PlatformMeta[]>([])
  const taskKinds = ref<TaskKind[]>([])
  const sources = ref<string[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref('')

  const byId = computed(() => {
    const map = new Map<string, PlatformMeta>()
    platforms.value.forEach((p) => map.set(p.id, p))
    return map
  })

  /** 只有登录类任务需要点平台；fetch/probe 也一样，但顺序按契约排。 */
  const loginKinds = computed(() =>
    taskKinds.value.filter((k) => k.kind.startsWith('login_')),
  )

  function meta(platformId: string): PlatformMeta | undefined {
    return byId.value.get(platformId)
  }

  function label(platformId: string): string {
    return meta(platformId)?.short || platformId || '未知'
  }

  function fullName(platformId: string): string {
    return meta(platformId)?.name || platformId || '未知平台'
  }

  function color(platformId: string): string {
    return meta(platformId)?.color || FALLBACK_COLOR
  }

  /** 只在有把握时给徽标，别把 stable 也标出来——那会让界面到处是标签。 */
  function statusLabel(platformId: string): string {
    const status = meta(platformId)?.status as ProviderStatus | undefined
    if (status === 'beta') return 'beta'
    if (status === 'unverified') return '未验证'
    if (status === 'broken') return '不可用'
    return ''
  }

  function statusTone(platformId: string): 'neutral' | 'warn' | 'danger' {
    const status = meta(platformId)?.status as ProviderStatus | undefined
    if (status === 'broken') return 'danger'
    if (status === 'beta' || status === 'unverified') return 'warn'
    return 'neutral'
  }

  /** 能真登录的平台：后端必须显式声明 ``login``；demo 是纯样例数据。 */
  function loginable(): PlatformMeta[] {
    return platforms.value.filter((p) => p.login === true)
  }

  async function load(force = false): Promise<void> {
    if (loaded.value && !force) return
    loading.value = true
    error.value = ''
    try {
      const data = await api.platforms()
      platforms.value = data.platforms ?? []
      taskKinds.value = data.task_kinds ?? []
      sources.value = data.sources ?? []
      loaded.value = true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  return {
    platforms,
    taskKinds,
    sources,
    loaded,
    loading,
    error,
    byId,
    loginKinds,
    meta,
    label,
    fullName,
    color,
    statusLabel,
    statusTone,
    loginable,
    load,
  }
})
