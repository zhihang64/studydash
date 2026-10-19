/**
 * 展示层的格式转换。
 *
 * 一个原则：**所有「还剩几天」「逾期几天」都以字符串形式来自后端**，前端只在
 * 后端没给（比如自己算的 KPI）时才动手。看板一大半的可信度来自时间判断，
 * 前后端各算一套迟早会对不上。
 *
 * 另一个原则：**比例一律用 0~1 表达**，乘 100 只发生在 `toPercent()` 里。
 *
 * 这里曾经有个 `percentNumber()`，用于「后端已经给了 0~100 的百分数」的字段。
 * 实际上后端所有比例都是 0~1，那个函数唯一的作用就是把 0.606 渲染成「0.6%」，
 * 于是被删了。要显示百分比就用 `percentText()`，不要自己拼 `%`。
 */

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** `0.667` → `66.7`。null / undefined / NaN 一律给 null，调用方据此显示「—」。 */
export function toPercent(ratio: number | null | undefined, digits = 1): number | null {
  if (ratio === null || ratio === undefined) return null
  const num = Number(ratio)
  if (!Number.isFinite(num)) return null
  const scale = 10 ** digits
  return Math.round(num * 100 * scale) / scale
}

/** `0.667` → `"66.7%"`；拿不到数就给 `fallback`（默认「—」）。 */
export function percentText(ratio: number | null | undefined, digits = 1, fallback = '—'): string {
  const value = toPercent(ratio, digits)
  return value === null ? fallback : `${value}%`
}

export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

const pad = (n: number) => String(n).padStart(2, '0')

/** `2026-10-19T00:25:41+08:00` → `2026-10-19 00:25`。时间戳用等宽字体显示更整齐。 */
export function formatDateTime(value: string | null | undefined, fallback = '—'): string {
  const date = parseDate(value)
  if (!date) return fallback
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** `10月21日 23:59` —— 时间线上的截止时刻，月份写中文读起来快。 */
export function formatDue(value: string | null | undefined, fallback = '无截止时间'): string {
  const date = parseDate(value)
  if (!date) return fallback
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatDate(value: string | null | undefined, fallback = '—'): string {
  const date = parseDate(value)
  if (!date) return fallback
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 相对「现在」的一句话：`刚刚` / `16 小时前` / `4 天前`。 */
export function formatAgo(value: string | null | undefined, now: Date = new Date(), fallback = '—'): string {
  const date = parseDate(value)
  if (!date) return fallback
  const seconds = (now.getTime() - date.getTime()) / 1000
  if (seconds < 0) return '刚刚'
  if (seconds < MINUTE) return '刚刚'
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)} 分钟前`
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)} 小时前`
  const days = Math.floor(seconds / DAY)
  if (days < 30) return `${days} 天前`
  if (days < 365) return `${Math.floor(days / 30)} 个月前`
  return `${Math.floor(days / 365)} 年前`
}

/**
 * 倒计时文案。优先用后端算好的 `days_left`，它与看板分组、预警阈值同源；
 * 后端没给时才用 `due` 现算。
 */
export function countdownText(
  due: string | null | undefined,
  daysLeft: number | null | undefined,
  now: Date = new Date(),
): { text: string; tone: 'overdue' | 'urgent' | 'soon' | 'later' | 'none' } {
  let days = daysLeft ?? null
  if (days === null) {
    const date = parseDate(due)
    if (!date) return { text: '无截止时间', tone: 'none' }
    days = (date.getTime() - now.getTime()) / (DAY * 1000)
  }
  if (days < 0) {
    const over = Math.abs(days)
    const text = over < 1 ? `逾期 ${Math.round(over * 24)} 小时` : `逾期 ${formatDays(over)}`
    return { text, tone: 'overdue' }
  }
  if (days < 1) return { text: `剩 ${Math.max(1, Math.round(days * 24))} 小时`, tone: 'urgent' }
  if (days <= 3) return { text: `剩 ${formatDays(days)}`, tone: 'urgent' }
  if (days <= 7) return { text: `剩 ${formatDays(days)}`, tone: 'soon' }
  return { text: `剩 ${formatDays(days)}`, tone: 'later' }
}

/** `2` → `2 天`；`0.5` → `12 小时`；`45` → `1 个半月`。 */
export function formatDays(days: number): string {
  if (days < 1) return `${Math.max(1, Math.round(days * 24))} 小时`
  if (days < 45) return `${Math.round(days * 10) / 10} 天`
  if (days < 365) return `${Math.round(days / 30)} 个月`
  return `${Math.round((days / 365) * 10) / 10} 年`
}

/** 秒 → `3 分 12 秒` / `1 小时 4 分`。用在「已运行」和任务耗时上。 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  const total = Math.floor(seconds)
  if (total < 60) return `${total} 秒`
  if (total < HOUR) {
    const m = Math.floor(total / MINUTE)
    const s = total % MINUTE
    return s ? `${m} 分 ${s} 秒` : `${m} 分`
  }
  const h = Math.floor(total / HOUR)
  const m = Math.floor((total % HOUR) / MINUTE)
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`
}

/** 作业条数的量词：中文里「1 条 / 3 条」不分单复数，但避免「1 条作业」写成「1 条作业项」。 */
export function countText(count: number, unit: string): string {
  return `${count} ${unit}`
}

/** 课程 / 平台的标识符可能很长，列表里截断显示，title 里给全的。 */
export function shortId(id: string, keep = 18): string {
  if (!id) return ''
  return id.length <= keep ? id : `${id.slice(0, keep - 1)}…`
}

export function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/** 平台色可能是 ``#3b82f6`` 或 ``rgb(...)``，直接内联到 style 里，不参与拼接。 */
export function cssColor(value: string | undefined, fallback = 'var(--accent)'): string {
  if (!value) return fallback
  return /^#[0-9a-f]{3,8}$/i.test(value) ? value : fallback
}
