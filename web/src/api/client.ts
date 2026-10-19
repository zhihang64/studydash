/**
 * 唯一的取数入口。组件不许直接 fetch——所有请求都要经过这里，才有统一的
 * 令牌、超时、错误翻译和「未授权」处理。
 *
 * 三件事特别容易被写漏，这里一次性做掉：
 *  1. 令牌放在自定义头里（不是 Cookie）。用 Cookie 就意味着任何网页都能借你的
 *     登录态打这个本地服务，那是 CSRF。
 *  2. 后端永远回同一个错误信封。4xx/5xx 都要读出 message 当人话显示，
 *     而不是把状态码糊到界面上。
 *  3. 401 是「服务重启了、令牌换了」，唯一正确的应对是刷新页面，不是重试。
 */

import type {
  AccountView,
  ApiErrorBody,
  CheckItem,
  DemoSelfSummary,
  ErrorCode,
  HealthInfo,
  PayloadMetaView,
  PayloadView,
  PlatformMeta,
  SettingsPatch,
  SettingsView,
  TaskKind,
  TaskLogLine,
  TaskView,
  VersionInfo,
} from './types'

/** 把后端的错误信封变成异常，方便调用方 `catch` 后直接读字段。 */
export class ApiError extends Error {
  readonly code: ErrorCode
  readonly hint: string
  readonly detail: Record<string, unknown>
  readonly status: number

  constructor(
    code: ErrorCode,
    message: string,
    options: { hint?: string; detail?: Record<string, unknown>; status?: number } = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.hint = options.hint ?? ''
    this.detail = options.detail ?? {}
    this.status = options.status ?? 0
  }

  /** 缺依赖、没数据这类是「还没准备好」，界面该显示引导而不是报错红条。 */
  get needsSetup(): boolean {
    return this.code === 'not_found' || this.code === 'invalid_request'
  }

  get isAuth(): boolean {
    return this.code === 'auth_error' || this.code === 'unauthorized'
  }
}

const TOKEN_HEADER = 'x-studydash-token'
const BASE = '/api'
const DEFAULT_TIMEOUT_MS = 30_000

function boot() {
  return typeof window === 'undefined' ? null : window.__STUDYDASH__
}

/** 前端拿不到启动参数时（被直接打开、或挂在没有注入的页面上）就抛这个。 */
export class NotLaunchedError extends Error {
  constructor() {
    super('这个页面不是由 studydash 启动的，拿不到访问令牌。')
    this.name = 'NotLaunchedError'
  }
}

/** 保证有一个令牌。没有就说明页面不是 studydash 起的，早点把话说清楚。 */
export function requireToken(): string {
  const info = boot()
  if (!info || !info.token) throw new NotLaunchedError()
  return info.token
}

export function bootInfo() {
  return boot()
}

interface RequestOptions {
  method?: string
  body?: unknown
  /** 毫秒。0 表示不设超时（SSE 这类长连接要）。 */
  timeoutMs?: number
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = options
  const token = requireToken()

  const controller = new AbortController()
  const timers: number[] = []
  if (timeoutMs > 0) {
    timers.push(window.setTimeout(() => controller.abort(new DOMException('超时', 'TimeoutError')), timeoutMs))
  }
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason)
    else signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true })
  }

  const headers: Record<string, string> = { [TOKEN_HEADER]: token, Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      credentials: 'omit',
      cache: 'no-store',
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError' && timeoutMs > 0 && !signal?.aborted) {
      throw new ApiError('internal', '请求超时了。后端可能正在做很慢的事（比如抓取），稍等一下再试。')
    }
    throw new ApiError('internal', '连不上本机的后端服务。是不是把 studydash 关掉了？')
  } finally {
    timers.forEach((id) => window.clearTimeout(id))
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      // 后端保证回 JSON。真走到这儿说明是反代或网关插了一脚。
      throw new ApiError('internal', '后端返回的不是 JSON，可能有别的程序占着这个端口。', {
        status: response.status,
      })
    }
  }

  if (!response.ok) {
    const envelope = (parsed ?? {}) as Partial<ApiErrorBody>
    const error = envelope.error
    const apiError = new ApiError(
      error?.code ?? 'internal',
      error?.message ?? `请求失败（HTTP ${response.status}）`,
      {
        hint: error?.hint,
        detail: error?.detail,
        status: response.status,
      },
    )
    // 令牌失效时，页面上任何一个请求都会 401。让它们各自弹一次 toast 只会刷屏，
    // 所以在这里向上报一次，由外壳统一换成「刷新页面」的整屏提示。
    if (apiError.isAuth) notifyFailure(apiError)
    throw apiError
  }

  return parsed as T
}

/* ========================================================================== *
 * 致命错误上报（只有外壳在用）
 * ========================================================================== */

let failureHandler: ((error: ApiError) => void) | null = null

/**
 * 注册一个全局的失败回调。`client.ts` 不认识 Vue——它只留一个钩子，
 * 由 `main.ts` 决定怎么显示。
 */
export function onApiFailure(handler: (error: ApiError) => void): void {
  failureHandler = handler
}

function notifyFailure(error: ApiError): void {
  try {
    failureHandler?.(error)
  } catch {
    // 上报失败绝不能盖掉真正的错误
  }
}

/* ========================================================================== *
 * 只读接口
 * ========================================================================== */

export const api = {
  health: () => request<HealthInfo>('/health'),
  version: () => request<VersionInfo>('/version'),
  doctor: () => request<{ checks: CheckItem[]; failed: number }>('/doctor'),

  platforms: () =>
    request<{ platforms: PlatformMeta[]; sources: string[]; task_kinds: TaskKind[] }>('/platforms'),

  settings: () => request<SettingsView>('/settings'),
  patchSettings: (patch: SettingsPatch) =>
    request<SettingsView>('/settings', { method: 'PATCH', body: patch }),

  accounts: () => request<{ accounts: AccountView[] }>('/accounts'),
  createAccount: (payload: { platform: string; label?: string; enabled?: boolean }) =>
    request<{ accounts: AccountView[] }>('/accounts', { method: 'POST', body: payload }),
  patchAccount: (platform: string, label: string, payload: { enabled?: boolean; label?: string }) =>
    request<{ accounts: AccountView[] }>(
      `/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(label)}`,
      { method: 'PATCH', body: payload },
    ),
  deleteAccount: (platform: string, label: string) =>
    request<void>(`/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(label)}`, {
      method: 'DELETE',
    }),

  payload: () => request<PayloadView>('/payload'),
  payloadMeta: () => request<PayloadMetaView>('/payload/meta'),
  demo: () => request<DemoSelfSummary>('/payload/demo', { method: 'POST' }),

  tasks: () => request<{ tasks: TaskView[] }>('/tasks'),
  task: (id: string) => request<TaskView>(`/tasks/${encodeURIComponent(id)}`),
  createTask: (kind: string, options: Record<string, unknown> = {}) =>
    request<{ task: TaskView }>('/tasks', { method: 'POST', body: { kind, options } }),
  cancelTask: (id: string) =>
    request<TaskView>(`/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
}

/**
 * 订阅任务进度。返回一个取消订阅函数。
 *
 * 有个反直觉的地方：EventSource **不支持自定义请求头**，而令牌只能放头里。
 * 所以 SSE 用不了原生 EventSource，得用 fetch + ReadableStream 自己解析 SSE 帧。
 * 好在这不难，而且顺带能设超时和 AbortSignal。
 */
export function streamTask(
  taskId: string,
  handlers: {
    onTask?: (task: TaskView) => void
    onLog?: (line: TaskLogLine) => void
    onDone?: (task: TaskView) => void
    onError?: (err: Error) => void
  },
): () => void {
  const controller = new AbortController()
  let closed = false

  const close = () => {
    if (closed) return
    closed = true
    controller.abort()
  }

  void (async () => {
    let token: string
    try {
      token = requireToken()
    } catch (err) {
      handlers.onError?.(err as Error)
      return
    }

    let response: Response
    try {
      response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/events`, {
        headers: { [TOKEN_HEADER]: token, Accept: 'text/event-stream' },
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'omit',
      })
    } catch (err) {
      if (!closed) handlers.onError?.(err as Error)
      return
    }

    if (!response.ok || !response.body) {
      if (!closed) handlers.onError?.(new Error(`任务进度流连不上（HTTP ${response.status}）`))
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE 帧以空行分隔。最后一段可能不完整，留在 buffer 里等下一轮。
        let split = buffer.indexOf('\n\n')
        while (split !== -1) {
          const frame = buffer.slice(0, split)
          buffer = buffer.slice(split + 2)
          dispatchFrame(frame, handlers)
          split = buffer.indexOf('\n\n')
        }
      }
    } catch (err) {
      if (!closed && !(err instanceof DOMException)) handlers.onError?.(err as Error)
    }
  })()

  return close
}

function dispatchFrame(
  frame: string,
  handlers: Parameters<typeof streamTask>[1],
): void {
  let event = 'message'
  const dataLines: string[] = []

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue // 心跳注释
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }

  if (!dataLines.length) return

  let payload: unknown
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }

  if (event === 'log') handlers.onLog?.(payload as TaskLogLine)
  else if (event === 'task') handlers.onTask?.(payload as TaskView)
  else if (event === 'done') handlers.onDone?.(payload as TaskView)
}
