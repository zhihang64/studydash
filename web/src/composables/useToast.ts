/**
 * 轻提示。模块级单例：谁都能 `toast.ok('...')`，`<ToastStack>` 负责渲染。
 *
 * 成功提示会自动消失；**错误提示不自动消失**——用户很可能正好没看屏幕，
 * 把一条「登录失败」在 3 秒后吞掉是最招人烦的设计之一。
 */

import { readonly, ref } from 'vue'

export type ToastKind = 'ok' | 'warn' | 'error' | 'info'

export interface Toast {
  id: number
  kind: ToastKind
  title: string
  detail: string
  /** 毫秒。0 表示不自动关闭。 */
  timeout: number
  createdAt: number
}

const DEFAULT_TIMEOUT: Record<ToastKind, number> = {
  ok: 3200,
  info: 4200,
  warn: 7000,
  error: 0,
}

const MAX_VISIBLE = 4

const items = ref<Toast[]>([])
const timers = new Map<number, number>()
let seq = 0

function push(kind: ToastKind, title: string, detail = '', timeout?: number): number {
  const id = ++seq
  const item: Toast = {
    id,
    kind,
    title,
    detail,
    timeout: timeout ?? DEFAULT_TIMEOUT[kind],
    createdAt: Date.now(),
  }
  items.value = [...items.value, item].slice(-MAX_VISIBLE)

  if (item.timeout > 0) {
    timers.set(
      id,
      window.setTimeout(() => dismiss(id), item.timeout),
    )
  }
  return id
}

export function dismiss(id: number): void {
  const timer = timers.get(id)
  if (timer !== undefined) {
    window.clearTimeout(timer)
    timers.delete(id)
  }
  items.value = items.value.filter((t) => t.id !== id)
}

export function dismissAll(): void {
  timers.forEach((timer) => window.clearTimeout(timer))
  timers.clear()
  items.value = []
}

export const toasts = readonly(items)

export const toast = {
  ok: (title: string, detail = '') => push('ok', title, detail),
  info: (title: string, detail = '') => push('info', title, detail),
  warn: (title: string, detail = '') => push('warn', title, detail),
  /** 错误默认常驻，需要用户自己点掉或重试成功后被替换。 */
  error: (title: string, detail = '') => push('error', title, detail),
  push,
  dismiss,
  dismissAll,
}
