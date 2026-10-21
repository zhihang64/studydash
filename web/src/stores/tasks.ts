/**
 * 任务：唯一会「跑得很久」的东西。
 *
 * 进度有两条路，都实现：SSE 流（快、省请求）与轮询（公司网络里 SSE 常被代理掐掉）。
 * 先试流，流断了**自动降级**到轮询——用户不该因为网络环境不同就得换个用法。
 *
 * 任务快照里没有 ``options``（后端承诺密码不进快照），所以这个 store 也从不
 * 保存提交过的表单内容。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, streamTask } from '@/api/client'
import type { TaskLogLine, TaskView } from '@/api/types'
import { useAccountsStore } from './accounts'
import { useDashboardStore } from './dashboard'
import { toast } from '@/composables/useToast'

export const TERMINAL_STATES = ['done', 'failed', 'cancelled'] as const

export const TASK_STATE_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const POLL_MS = 1200
const MAX_LOG_LINES = 400

/**
 * 一个被跟踪的任务。名字不能叫 `Record`——那会遮蔽掉 TypeScript 的
 * `Record<K, V>` 工具类型，本文件里所有 `Record<string, string>` 都会静默变成
 * 这个接口，报出来的错还是「Record 不是泛型」，跟真正的毛病差得很远。
 */
interface TrackedTask {
  task: TaskView
  logs: TaskLogLine[]
  /** SSE 断了以后改成轮询；界面据此显示「实时连接已断开，改用轮询」 */
  degraded: boolean
}

export const useTasksStore = defineStore('tasks', () => {
  const records = ref<TrackedTask[]>([])
  const streams = new Map<string, () => void>()
  const timers = new Map<string, number>()
  /** 最近一次完成的任务，登录面板据此显示成功结果。 */
  const lastFinishedId = ref('')

  const all = computed(() => records.value.map((r) => r.task))
  /** 还没跑完的任务。外面只关心任务本身，不关心我们怎么跟踪它。 */
  const active = computed(() =>
    records.value.filter((r) => !isFinished(r.task)).map((r) => r.task),
  )
  const busy = computed(() => active.value.length > 0)

  function isFinished(task: TaskView): boolean {
    return (TERMINAL_STATES as readonly string[]).includes(task.state)
  }

  function find(id: string): TrackedTask | undefined {
    return records.value.find((r) => r.task.id === id)
  }

  function upsert(task: TaskView, logs?: TaskLogLine[]): void {
    const existing = find(task.id)
    if (existing) {
      existing.task = task
      if (logs) existing.logs = logs.slice(-MAX_LOG_LINES)
      return
    }
    records.value = [{ task, logs: logs ?? [], degraded: false }, ...records.value].slice(0, 20)
  }

  function appendLog(id: string, line: TaskLogLine): void {
    const record = find(id)
    if (!record) return
    record.logs = [...record.logs, line].slice(-MAX_LOG_LINES)
  }

  /* ------------------------------------------------------------------ 订阅 */

  function stopStream(id: string): void {
    const close = streams.get(id)
    if (close) {
      close()
      streams.delete(id)
    }
    const timer = timers.get(id)
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timers.delete(id)
    }
  }

  function subscribe(id: string): void {
    stopStream(id)
    let sawAnything = false

    const close = streamTask(id, {
      onTask: (task) => {
        sawAnything = true
        upsert(task)
        if (isFinished(task)) stopStream(id)
      },
      onLog: (line) => {
        sawAnything = true
        appendLog(id, line)
      },
      onDone: (task) => {
        sawAnything = true
        upsert(task)
        stopStream(id)
        onFinished(task)
      },
      onError: () => {
        // 流连不上或被掐了。降级到轮询，而不是把任务显示成失败——
        // 任务在后端可能还好好的，只是这条进度通道不通。
        const record = find(id)
        if (record && !isFinished(record.task)) {
          record.degraded = true
          if (!sawAnything) record.degraded = true
          poll(id)
        }
      },
    })
    streams.set(id, close)
  }

  function poll(id: string): void {
    if (timers.has(id)) return
    const tick = async () => {
      timers.delete(id)
      if (!find(id) || isFinished(find(id)!.task)) return
      try {
        const task = await api.task(id)
        upsert(task, task.logs)
        if (isFinished(task)) {
          stopStream(id)
          onFinished(task)
          return
        }
      } catch {
        // 单次失败继续，别把轮询停掉
      }
      timers.set(id, window.setTimeout(tick, POLL_MS))
    }
    timers.set(id, window.setTimeout(tick, POLL_MS))
  }

  /* ------------------------------------------------------------------ 生命周期 */

  function onFinished(task: TaskView): void {
    lastFinishedId.value = task.id
    if (task.state === 'done') {
      const summary = describeResult(task)
      toast.ok(`${label(task.kind)}完成`, summary)
    } else if (task.state === 'failed') {
      toast.error(`${label(task.kind)}失败`, task.error?.message || task.message || '没有更多信息')
    } else if (task.state === 'cancelled') {
      toast.info(`${label(task.kind)}已取消`, '')
    }
    void refreshAfter(task)
  }

  /** 登录成功了账号列表变了；抓取成功了看板变了。别让用户自己按刷新。 */
  async function refreshAfter(task: TaskView): Promise<void> {
    if (task.state !== 'done') return
    if (task.kind.startsWith('login_')) {
      await useAccountsStore().load()
      await useDashboardStore().load()
    } else if (task.kind === 'fetch') {
      await useDashboardStore().load()
      await useAccountsStore().load()
    } else if (task.kind === 'writeback') {
      const result = task.result ?? {}
      if (result.applied) await useDashboardStore().load()
    }
  }

  function describeResult(task: TaskView): string {
    const result = task.result ?? {}
    if (task.kind === 'fetch') {
      const summary = result.summary as
        | { courses?: number; pending?: number; overdue?: number }
        | undefined
      if (summary) {
        const parts = [`${summary.courses ?? 0} 门课`]
        if (summary.pending) parts.push(`${summary.pending} 项待办`)
        if (summary.overdue) parts.push(`逾期 ${summary.overdue} 项`)
        return parts.join(' · ')
      }
    }
    if (task.kind.startsWith('login_')) {
      const account = result.account as { label?: string; uid?: string } | undefined
      if (account?.label) {
        return account.uid ? `账号「${account.label}」（uid ${account.uid}）` : `账号「${account.label}」`
      }
    }
    if (task.kind === 'probe') {
      const traces = result.traces as unknown[] | undefined
      return traces ? `留档了 ${traces.length} 条请求` : ''
    }
    if (task.kind === 'writeback') {
      const counts = result.counts as
        | { done?: number; failed?: number; total?: number; requests?: number }
        | undefined
      if (counts) {
        const verb = result.applied ? '已上报' : '演练通过'
        const parts = [`${verb} ${counts.done ?? 0} 个`]
        if (counts.total) parts.push(`共 ${counts.total} 个任务点`)
        if (counts.failed) parts.push(`失败 ${counts.failed} 个`)
        // 后端把新的任务点计数直接写回了看板数据，这里顺手告诉用户「不用再抓取」。
        if (result.sync) parts.push('看板已同步')
        return parts.join(' · ')
      }
    }
    return ''
  }

  const KIND_LABEL: Record<string, string> = {
    login_qr: '扫码登录',
    login_password: '密码登录',
    login_cookie: 'Cookie 登录',
    login_browser: '读取浏览器会话',
    login_live: '浏览器登录',
    fetch: '抓取学业数据',
    probe: '接口探测',
    writeback: '任务点上报',
    quiz: '章节测验答题',
    learning: '刷章节学习次数',
  }

  function label(kind: string): string {
    return KIND_LABEL[kind] ?? kind
  }

  /* ------------------------------------------------------------------ 动作 */

  async function start(kind: string, options: Record<string, unknown> = {}): Promise<TaskView | null> {
    try {
      const { task } = await api.createTask(kind, options)
      upsert(task)
      subscribe(task.id)
      return task
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.error(`没法开始「${label(kind)}」`, message)
      return null
    }
  }

  async function cancel(id: string): Promise<void> {
    try {
      const task = await api.cancelTask(id)
      upsert(task)
    } catch (err) {
      toast.warn('取消失败', err instanceof Error ? err.message : String(err))
    }
  }

  function forget(id: string): void {
    stopStream(id)
    records.value = records.value.filter((r) => r.task.id !== id)
  }

  function clearFinished(): void {
    records.value
      .filter((r) => isFinished(r.task))
      .forEach((r) => stopStream(r.task.id))
    records.value = records.value.filter((r) => !isFinished(r.task))
  }

  /** 页面刷新后把后端的任务历史捞回来（比如换标签页再回来）。 */
  async function hydrate(): Promise<void> {
    try {
      const { tasks } = await api.tasks()
      tasks.forEach((task) => upsert(task))
      tasks.filter((t) => !isFinished(t)).forEach((t) => subscribe(t.id))
    } catch {
      // 拉不到任务历史不影响看板
    }
  }

  function teardown(): void {
    Array.from(streams.keys()).forEach(stopStream)
  }

  return {
    records,
    all,
    active,
    busy,
    lastFinishedId,
    isFinished,
    find,
    label,
    start,
    cancel,
    forget,
    clearFinished,
    hydrate,
    teardown,
  }
})
