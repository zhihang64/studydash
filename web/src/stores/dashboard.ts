/**
 * 看板数据。只读，不在这里发起抓取——抓取是任务（见 stores/tasks），
 * 这样「数据长什么样」和「怎么拿到数据」互不牵连。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, api } from '@/api/client'
import type { AssignmentState, CourseView, PayloadView } from '@/api/types'

/** 任务紧急度在时间线上的分组顺序，与后端 model.STATES 一致。 */
export const STATE_ORDER: AssignmentState[] = ['overdue', 'urgent', 'soon', 'later', 'none', 'done']

export const STATE_LABEL: Record<AssignmentState, string> = {
  overdue: '已逾期',
  urgent: '3 天内',
  soon: '7 天内',
  later: '以后',
  none: '无截止',
  done: '已完成',
}

export const STATE_TONE: Record<AssignmentState, 'danger' | 'warn' | 'info' | 'neutral' | 'ok'> = {
  overdue: 'danger',
  urgent: 'warn',
  soon: 'info',
  later: 'neutral',
  none: 'neutral',
  done: 'ok',
}

export type SortKey = 'urgency' | 'due' | 'progress' | 'name' | 'platform'

export const SORT_LABEL: Record<SortKey, string> = {
  urgency: '按紧急程度',
  due: '按截止时间',
  progress: '按进度低的排前',
  name: '按课程名',
  platform: '按平台',
}

export const useDashboardStore = defineStore('dashboard', () => {
  const payload = ref<PayloadView | null>(null)
  const loading = ref(false)
  /** 没数据不是错误，是「还没抓过」。界面据此显示引导而不是红条。 */
  const empty = ref(false)
  const error = ref('')

  const loadingDemo = ref(false)

  /* ---------------------------------------------------------------- 筛选 */

  const query = ref('')
  const platformFilter = ref('')
  const coursePlatformFilter = ref('')
  const sortKey = ref<SortKey>('urgency')

  const courses = computed<CourseView[]>(() => payload.value?.courses ?? [])
  const overview = computed(() => payload.value?.overview ?? null)
  const timeline = computed(() => payload.value?.timeline ?? [])
  const platformCards = computed(() => payload.value?.platforms ?? [])
  const warnings = computed(() => payload.value?.meta?.warnings ?? [])
  const mode = computed(() => payload.value?.meta?.mode ?? '')
  const generatedAt = computed(() => payload.value?.meta?.generated_at ?? '')
  const isDemo = computed(() => mode.value === 'demo')

  /** 已隐藏的课程（后端过滤时一起给出的精简卡）。 */
  const hiddenCourses = computed(() => payload.value?.hidden ?? [])
  const hiddenCount = computed(() => hiddenCourses.value.length)
  /** 正在隐藏 / 取消隐藏的课程键，用来禁用按钮。 */
  const hiding = ref('')

  const hasData = computed(() => courses.value.length > 0)

  /** 有进度或作业数据的课程。平台平均值、整体进度都只能拿这些去算。 */
  const measuredCourses = computed(() => courses.value.filter((c) => c.has_data))

  /** 平台筛选下拉里能选的平台，只列数据里真出现的。 */
  const availablePlatforms = computed(() => {
    const map = new Map<string, { id: string; name: string; count: number }>()
    courses.value.forEach((course) => {
      const key = course.platform
      const entry = map.get(key)
      if (entry) entry.count += 1
      else map.set(key, { id: key, name: course.platform_name || key, count: 1 })
    })
    return [...map.values()].sort((a, b) => b.count - a.count)
  })

  function matches(course: CourseView, needle: string): boolean {
    if (!needle) return true
    const haystack = [
      course.name,
      course.teacher,
      course.term,
      course.platform_name,
      course.id,
      ...course.assignments.map((a) => a.title),
    ]
      .join('\n')
      .toLowerCase()
    return haystack.includes(needle)
  }

  const visibleCourses = computed<CourseView[]>(() => {
    const needle = query.value.trim().toLowerCase()
    let list = courses.value.filter((c) => matches(c, needle))
    if (coursePlatformFilter.value) {
      list = list.filter((c) => c.platform === coursePlatformFilter.value)
    }
    const rank: Record<string, number> = { overdue: 0, urgent: 1, normal: 2, done: 3 }
    const sorted = [...list]
    switch (sortKey.value) {
      case 'due':
        sorted.sort((a, b) => dueRank(a) - dueRank(b))
        break
      case 'progress':
        // 没数据的排最后：它不是 0%，没法和别人比
        sorted.sort((a, b) => progressRank(a) - progressRank(b))
        break
      case 'name':
        sorted.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'))
        break
      case 'platform':
        sorted.sort(
          (a, b) =>
            a.platform_name.localeCompare(b.platform_name, 'zh-Hans-CN') ||
            a.name.localeCompare(b.name, 'zh-Hans-CN'),
        )
        break
      default:
        sorted.sort(
          (a, b) =>
            (rank[a.flag] ?? 9) - (rank[b.flag] ?? 9) ||
            b.overdue - a.overdue ||
            b.pending - a.pending ||
            a.name.localeCompare(b.name, 'zh-Hans-CN'),
        )
    }
    return sorted
  })

  function dueRank(course: CourseView): number {
    if (!course.next_due) return Number.MAX_SAFE_INTEGER
    const time = new Date(course.next_due).getTime()
    return Number.isNaN(time) ? Number.MAX_SAFE_INTEGER : time
  }

  function progressRank(course: CourseView): number {
    // 「没有数据」和「进度很低」都不是一回事，但排序上都排到最后——
    // 用户点「按进度低的排前」是想找落后的课，不是为了看问号。
    if (!course.has_data || course.ratio === null) return Number.MAX_SAFE_INTEGER
    return Number.isFinite(course.ratio) ? course.ratio : Number.MAX_SAFE_INTEGER
  }

  /** 时间线按平台过滤后仍按紧急度分组，组内保持后端给的截止时间顺序。 */
  const timelineGroups = computed(() => {
    const filtered = platformFilter.value
      ? timeline.value.filter((a) => a.platform === platformFilter.value)
      : timeline.value
    const buckets = new Map<string, typeof filtered>()
    STATE_ORDER.forEach((state) => buckets.set(state, []))
    filtered.forEach((item) => {
      const key = buckets.has(item.state) ? item.state : 'none'
      buckets.get(key)!.push(item)
    })
    return STATE_ORDER.map((state) => ({
      state,
      label: STATE_LABEL[state],
      tone: STATE_TONE[state],
      items: buckets.get(state) ?? [],
    })).filter((group) => group.items.length > 0)
  })

  /** 详情抽屉用的单门课程。 */
  function courseById(platform: string, id: string): CourseView | undefined {
    return courses.value.find((c) => c.platform === platform && c.id === id)
  }

  /* ---------------------------------------------------------------- 动作 */

  function clearFilters(): void {
    query.value = ''
    coursePlatformFilter.value = ''
  }

  /**
   * 把后端的 payload 归一化成界面好用的形状。
   *
   * 只做一件事：课程的总进度在后端叫 `progress`（0~1），这里补一个同值的
   * `ratio`，没有数据时给 null。放在这一处做，显示层就不用各自猜量纲。
   */
  function normalize(raw: PayloadView): PayloadView {
    return {
      ...raw,
      courses: raw.courses.map((course) => ({
        ...course,
        // 后端会给 key；旧版 progress.json 没有时按同样的规则补一个，
        // 否则「隐藏」按钮会传来 undefined。
        key: course.key || `${course.platform}:${course.id}:${course.term_id ?? ''}`,
        ratio: course.has_data === false ? null : (course.progress ?? null),
      })),
    }
  }

  async function load(): Promise<void> {
    if (!payload.value) loading.value = true
    error.value = ''
    try {
      payload.value = normalize(await api.payload())
      empty.value = false
    } catch (err) {
      if (err instanceof ApiError && err.code === 'not_found') {
        empty.value = true
        payload.value = null
      } else {
        error.value = err instanceof Error ? err.message : String(err)
      }
    } finally {
      loading.value = false
    }
  }

  /** 演示数据：一条命令生成一份能看的看板，用户第一次打开就有东西可看。 */
  async function loadDemo(): Promise<boolean> {
    loadingDemo.value = true
    error.value = ''
    try {
      await api.demo()
      await load()
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      return false
    } finally {
      loadingDemo.value = false
    }
  }

  /* -------------------------------------------------------------- 隐藏课程 */

  /**
   * 隐藏一门课。隐藏是**后端**的事：时间线、总览数字、平台卡都会跟着重算，
   * 所以这里不做本地过滤，直接重新拉一份 payload —— 让「哪些数字算进去了」
   * 只有一个真相来源。
   */
  async function hideCourse(key: string): Promise<void> {
    if (!key || hiding.value) return
    hiding.value = key
    try {
      await api.hideCourse(key)
      await load()
    } finally {
      hiding.value = ''
    }
  }

  async function unhideCourse(key: string): Promise<void> {
    if (!key || hiding.value) return
    hiding.value = key
    try {
      await api.unhideCourse(key)
      await load()
    } finally {
      hiding.value = ''
    }
  }

  return {
    payload,
    loading,
    empty,
    error,
    loadingDemo,
    query,
    platformFilter,
    coursePlatformFilter,
    sortKey,
    courses,
    measuredCourses,
    overview,
    timeline,
    platformCards,
    warnings,
    mode,
    generatedAt,
    isDemo,
    hasData,
    hiddenCourses,
    hiddenCount,
    hiding,
    availablePlatforms,
    visibleCourses,
    timelineGroups,
    courseById,
    clearFilters,
    load,
    loadDemo,
    hideCourse,
    unhideCourse,
  }
})
