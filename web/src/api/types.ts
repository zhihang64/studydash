/**
 * 接口与数据的类型定义。
 *
 * 这些类型手写而不是从 OpenAPI 生成：后端把 openapi.json 关掉了（它会把整张
 * 接口表暴露在页面上），而且生成出来的类型没法表达「字段在什么情况下是 null」
 * 这类真正要紧的约束——那正是看板最容易踩的坑（进度条拿到 null 就崩）。
 */

/* ========================================================================== *
 * 错误信封
 * ========================================================================== */

/** 契约里的错误码，与后端 `web/errors.py` 的 ERROR_STATUS 一一对应。 */
export type ErrorCode =
  | 'unauthorized'
  | 'forbidden'
  | 'invalid_request'
  | 'not_found'
  | 'auth_error'
  | 'captcha_required'
  | 'two_factor_required'
  | 'provider_error'
  | 'login_timeout'
  | 'busy'
  | 'conflict'
  | 'internal'

export interface ApiErrorBody {
  error: {
    code: ErrorCode
    message: string
    hint?: string
    detail?: Record<string, unknown>
  }
}

/* ========================================================================== *
 * 任务
 * ========================================================================== */

export type TaskState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

export type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'ok' | 'step'

export interface TaskLogLine {
  ts: string
  level: LogLevel | string
  text: string
}

export interface TaskView {
  id: string
  kind: string
  state: TaskState | string
  progress: number
  message: string
  created_at: string
  started_at: string | null
  ended_at: string | null
  cancel_requested: boolean
  result: Record<string, unknown> | null
  error: { code?: string; message?: string; hint?: string } | null
  /** 只在 full 快照 / SSE 的 task 事件里有 */
  logs?: TaskLogLine[]
  /** 扫码任务的二维码等：key → 值（二维码是 data: URL） */
  artifacts?: Record<string, string>
}

export interface TaskKind {
  kind: string
  label: string
}

/* ========================================================================== *
 * 平台与账号
 * ========================================================================== */

export type ProviderStatus = 'stable' | 'beta' | 'unverified' | 'broken'

export interface PlatformMeta {
  id: string
  name: string
  short: string
  status: ProviderStatus | string
  status_label?: string
  color: string
  auth: string
  /** 后端是否真的实现了这个平台的登录流程；false 表示只能手工导入 Cookie。 */
  login?: boolean
  homepage?: string
  verified?: string
  note?: string
}

export interface AccountView {
  platform: string
  label: string
  enabled: boolean
  /** 永远不含 Cookie 值，只有「<已设置 N 字符>」这样的提示 */
  cookie_hint: string
  options: Record<string, unknown>
  ok: boolean
  message: string
  fetched_at: string | null
  course_count: number
}

/* ========================================================================== *
 * 设置与自检
 * ========================================================================== */

export interface HttpSettings {
  timeout: number
  delay: number
  user_agent: string
  retries: number
}

export interface SettingsView {
  config_path: string
  home: string
  http: HttpSettings
  ui: { theme: 'auto' | 'light' | 'dark'; alert_days: number }
  keep_raw: boolean
}

export interface SettingsPatch {
  http?: Partial<HttpSettings>
  ui?: { theme?: 'auto' | 'light' | 'dark'; alert_days?: number }
  keep_raw?: boolean
}

export type CheckLevel = 'ok' | 'warn' | 'fail'

export interface CheckItem {
  name: string
  ok: boolean
  level: CheckLevel
  detail: string
  fix: string
}

export interface VersionInfo {
  version: string
  schema: number
  python: string
  platform: string
  frozen: boolean
}

export interface HealthInfo {
  ok: boolean
  version: string
  schema: number
  uptime_s: number
}

/* ========================================================================== *
 * 看板数据（后端 aggregate.build_payload 的产物）
 * ========================================================================== */

export type AssignmentKind =
  | 'video'
  | 'read'
  | 'quiz'
  | 'homework'
  | 'exam'
  | 'discussion'
  | 'live'
  | 'other'

export type AssignmentStatus = 'pending' | 'submitted' | 'graded' | 'overdue' | 'unknown'

/** 紧急度。后端按 alert_days 算好，前端不再自己判断「几天算急」。 */
export type AssignmentState = 'overdue' | 'urgent' | 'soon' | 'later' | 'none' | 'done'

export interface AssignmentView {
  uid: string
  id: string
  title: string
  course: string
  course_id: string
  platform: string
  platform_name: string
  kind: AssignmentKind | string
  kind_label: string
  status: AssignmentStatus | string
  due: string | null
  days_left: number | null
  state: AssignmentState | string
  state_label: string
  score: number | null
  max_score: number | null
  url: string
  detail: string
}

export interface ProgressPart {
  label: string
  done: number
  total: number
  ratio: number | null
}

/**
 * 一个「还差任务点」的章节（超星课程页的 `fanyaChapterWhite` 树）。
 *
 * `pending` 是**还剩几个**任务点，不是总数——平台只给这一列，别拿它算百分比。
 * `depth` 是层级（0 = 节，1 = 小节…），用来缩进；`unit` 是它所属的「第N章」。
 */
export interface ChapterProgress {
  name: string
  unit: string
  sbar: string
  depth: number
  pending: number
}

export interface CourseView {
  platform: string
  platform_name: string
  id: string
  name: string
  teacher: string
  term: string
  url: string
  /**
   * 后端字段名就是 `progress`，值是 **0~1** 的小数（0.667 表示 66.7%）。
   *
   * 注意别和后面的 `progress_parts` 搞混：这个是课程总进度（一个数），
   * 那个是「任务点 34/51、作业 4/6」这种分项明细（一组对象）。
   */
  progress: number
  /**
   * `progress` 的别名，由 `stores/dashboard.ts` 在载入时填好。
   *
   * 为什么要有别名：显示层一律用 0~1 表达比例，再交给 `percentText()` 乘 100。
   * 如果各处自己判断「这个字段是 0~1 还是 0~100」，迟早会写错一个——之前就写错过，
   * 结果平均进度显示成 0.6%。
   *
   * `has_data` 为 false 时这里是 **null**，不是 0：没有数据和进度为零不是一回事。
   */
  ratio: number | null
  progress_parts: ProgressPart[]
  pending: number
  overdue: number
  done: number
  next_due: string | null
  next_due_title: string
  last_active: string | null
  assignments: AssignmentView[]
  /** 聚合层加的排序/上色标记 */
  flag: 'overdue' | 'urgent' | 'done' | 'normal' | string
  /**
   * 有没有进度或作业数据。false 时**不要**把 ratio 当成 0% 显示——
   * 「适配器没取到」和「真的一分没学」是两回事。
   */
  has_data: boolean
  /**
   * 只在这种情况出现：平台只公布了「还有 N 个任务点」，没给总数。
   * 有它的时候 ratio 仍是 null，但它比一句「无数据」有用得多。
   */
  pending_points?: number
  /** 还差任务点的章节明细；只在这门课确实有章节数据时出现。 */
  chapters?: ChapterProgress[]
  /**
   * 「隐藏课程」用的稳定标识：`平台:课程id:学期id`。
   *
   * 后端生成，前端只负责原样回传。不要自己用 `platform + id` 拼——
   * 同一门课可以有多个学期（高数上下册），那样会把别的学期一起藏掉。
   */
  key: string
  /** 学期 id，和 key 的第三段一致；没有学期信息时是空串。 */
  term_id?: string
}

/**
 * 「已隐藏课程」页用的精简课程卡。和 CourseView 的区别是不带作业明细——
 * 隐藏页只需要知道「是哪门课、还欠多少」，没必把整份作业列表再传一遍。
 */
export interface HiddenCourseView {
  key: string
  platform: string
  platform_name: string
  id: string
  name: string
  teacher: string
  term: string
  url: string
  progress: number
  has_data?: boolean
  pending: number
  overdue: number
  done: number
  next_due: string | null
  next_due_title: string
  flag?: string
}

export interface PlatformCard {
  id: string
  name: string
  short: string
  status: ProviderStatus | string
  status_label?: string
  color: string
  homepage?: string
  note?: string
  courses: number
  avg_progress: number
  pending: number
  overdue: number
  ok: boolean
  message: string
  has_data?: boolean
}

export interface OverviewView {
  courses: number
  platforms: number
  avg_progress: number
  pending: number
  overdue: number
  due_soon: number
  done: number
  alert_days: number
  has_progress?: boolean
}

export interface PayloadMeta {
  version: number
  generated_at: string
  mode: 'demo' | 'live' | 'import' | string
  readonly: boolean
  warnings: string[]
  /** 被隐藏的课程数（隐藏课不计入 overview / timeline）。 */
  hidden_count?: number
}

export interface PayloadView {
  meta: PayloadMeta
  overview: OverviewView
  platforms: PlatformCard[]
  timeline: AssignmentView[]
  courses: CourseView[]
  /** 已隐藏的课程。后端过滤时一并返回，隐藏页直接用它渲染。 */
  hidden?: HiddenCourseView[]
  accounts: {
    platform: string
    label?: string
    ok: boolean
    message: string
    fetched_at?: string | null
    course_count?: number
  }[]
}

export interface PayloadMetaView {
  generated_at: string | null
  mode: string | null
  warnings: string[]
  overview: Partial<OverviewView>
  hidden_count?: number
}

/** 隐藏 / 取消隐藏都返回同一份最新列表。 */
export interface HiddenCoursesReply {
  hidden: HiddenCourseView[]
  count: number
}

export interface DemoSelfSummary {
  mode: string
  summary: {
    courses: number
    pending: number
    overdue: number
    avg_progress: number
  }
}
