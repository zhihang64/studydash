<script setup lang="ts">
/**
 * 一门课程。
 *
 * 右上角那个大数字有三种状态，必须区分开：
 *  - 有数据 → 百分比；带「落后 X 个百分点」的提示（由 flag 与 ratio 推出）
 *  - 没数据 → 「无数据」而不是 0%
 * flag 决定左边那条色带的颜色，和筛选栏里的紧急度排序同源。
 */
import { computed, ref } from 'vue'

import type { CourseView, TaskView } from '@/api/types'
import { useTasksStore } from '@/stores/tasks'
import { countdownText, formatAgo, formatDue, percentText } from '@/utils/format'

import AssignmentItem from './AssignmentItem.vue'
import PlatformChip from './PlatformChip.vue'
import ProgressBar from './ProgressBar.vue'

const props = defineProps<{ course: CourseView; busy?: boolean }>()

defineEmits<{ hide: [course: CourseView] }>()

const tasks = useTasksStore()

const expanded = ref(false)
const chaptersOpen = ref(false)

/**
 * 课程卡上的「上报任务点」按钮。
 *
 * 只有超星有这条写路径（其它平台的适配器不带 writeback），所以按钮也只在那出现。
 * 点了先变「确认上报」再点一次才真的发——写平台数据不该是一次误触就发生的事，
 * 但也不值得为它开个弹窗（全仓没有 modal 组件，弹窗反而是更大的改动）。
 */
const confirmOpen = ref(false)
const canReport = computed(() => props.course.platform === 'chaoxing')

/**
 * 这一次上报处理哪几类任务点。
 *
 * 学习通的任务点分六类，协议完全不同：音视频要 md5 签名上报播放位置，
 * 阅读/文档/图书是一次性 GET 任务点接口，直播要 streamName+vdoid。
 * 用户大概率只想补其中一类，所以给档位而不是六个复选框——课程卡右边太窄了。
 * `all` 传空数组，后端 `_as_kinds` 会把它当成「全都要」。
 */
const KIND_SCOPES = {
  all: [],
  media: ['video', 'audio'],
  text: ['read', 'document'],
  book: ['book'],
  live: ['live'],
} as const

type KindScope = keyof typeof KIND_SCOPES

const kindScope = ref<KindScope>('all')
const kindList = computed(() => [...KIND_SCOPES[kindScope.value]])

/**
 * 这条课程卡一次点下去用几个线程。
 *
 * 上报本身是「一个任务点一次 GET」，彼此不依赖，所以并发是纯粹的加速手段：
 * 一门课几十个任务点，串行要几十次往返，4 并发能压到四分之一。
 * 默认还是 1 —— 写平台数据这件事，默认该是最保守的那个。
 */
const workers = ref(1)

/**
 * 递进上报：把播放位置按 30% / 70% / 100% 分三步发，每步间隔 1.5 秒。
 *
 * 实测一次性跳满，5 个任务点平台只认 1 个，还把另外 4 个标上人脸抓拍变成死态；
 * 分三步递进 4 个全过。多两次请求换通过率，默认开。
 */
const ramp = ref(true)

/** 这一次上报对应的任务 id。进度和结果都从这里读，不再靠一个本地 bool 猜状态。 */
const reportTaskId = ref('')

/**
 * 上报任务的实时快照。
 *
 * 存在 `tasks` store 里，SSE 断了会降级成轮询（store 内部已经处理），
 * 所以这里直接读就行，不用自己再开定时器。
 */
const reportTask = computed<TaskView | null>(() => {
  if (!reportTaskId.value) return null
  return tasks.find(reportTaskId.value)?.task ?? null
})

const reporting = computed(() => {
  const task = reportTask.value
  return !!task && !tasks.isFinished(task)
})

/** 进度条只在真跑起来时出现；派发前的几十毫秒没必要闪一下。 */
const progressRatio = computed<number | null>(() => {
  const task = reportTask.value
  if (!task || !reporting.value) return null
  return task.progress
})

interface MissionLine {
  done: number
  total: number
}

/** 从任务结果里取「已完成任务点」前后对比；取不到就返回 null（老任务没有这个字段）。 */
function missionGain(raw: unknown): { from: number; to: number; total: number } | null {
  if (!raw || typeof raw !== 'object') return null
  const bag = raw as { before?: Record<string, MissionLine>; after?: Record<string, MissionLine> }
  const before = bag.before ?? {}
  const after = bag.after ?? {}
  const key = Object.keys(before)[0]
  if (!key) return null
  const start = before[key]
  const end = after[key]
  if (!start || !end) return null
  return { from: start.done, to: end.done, total: end.total }
}

/** 跑完之后的结局，一句话。成功/失败/取消要能一眼区分。 */
const outcome = computed<{ tone: 'ok' | 'danger' | 'muted'; text: string } | null>(() => {
  const task = reportTask.value
  if (!task || !tasks.isFinished(task)) return null
  if (task.state === 'failed') {
    return { tone: 'danger', text: task.error?.message || task.message || '上报失败' }
  }
  if (task.state === 'cancelled') return { tone: 'muted', text: '已取消' }
  const counts = (task.result?.counts ?? {}) as {
    done?: number
    failed?: number
    total?: number
    stuck?: number
    unsupported?: number
  }
  const total = counts.total ?? 0
  if (!total) return { tone: 'muted', text: '没有需要上报的任务点' }
  const stuck = counts.stuck ?? 0
  const failed = (counts.failed ?? 0) - stuck
  /** 课程页上的「已完成任务点 X/Y」，这是平台自己算的，比 isPassed 可信。 */
  const gain = missionGain(task.result?.mission)
  const suffix = gain === null ? '' : `，已完成任务点 ${gain.from} → ${gain.to} / ${gain.total}`
  /** 章节测验、讨论这些本工具不做，但平台把它们算进任务点总数。 */
  const skip = counts.unsupported ?? 0
  const skipTail = skip ? `；另有 ${skip} 个任务点（章节测验等）本工具不做` : ''
  if (stuck && !failed) {
    return {
      tone: 'danger',
      text: `平台确认 ${counts.done ?? 0}/${total}；${stuck} 个播放已满却没判通过（平台死态，只能手动重看）${skipTail}${suffix}`,
    }
  }
  if (failed || stuck) {
    const tail = stuck ? `，另有 ${stuck} 个播放已满判不过` : ''
    return {
      tone: 'danger',
      text: `平台确认 ${counts.done ?? 0}/${total}，${failed} 个未确认${tail}${skipTail}${suffix}`,
    }
  }
  return {
    tone: 'ok',
    text: `平台确认 ${counts.done ?? 0}/${total} 个任务点${skipTail}${suffix}`,
  }
})

/** 点了第一次之后 4 秒不动就退回原样，免得「确认上报？」一直挂着。 */
let confirmTimer = 0

const reportLabel = computed(() => {
  if (reporting.value) return '上报中…'
  if (confirmOpen.value) return '确认上报？'
  return outcome.value ? '再上报一次' : '上报任务点'
})

async function reportVideo(): Promise<void> {
  if (reporting.value) return
  window.clearTimeout(confirmTimer)
  if (!confirmOpen.value) {
    confirmOpen.value = true
    confirmTimer = window.setTimeout(() => {
      confirmOpen.value = false
    }, 4000)
    return
  }
  confirmOpen.value = false
  const task = await tasks.start('writeback', {
    platform: props.course.platform,
    course_id: props.course.id,
    apply: true,
    kinds: kindList.value,
    workers: Math.max(1, Math.min(8, Number(workers.value) || 1)),
    ramp: ramp.value,
  })
  reportTaskId.value = task?.id ?? ''
}

/**
 * 章节测验答题 / 刷学习次数：和上报同一套「点两次才真干」的规矩。
 *
 * 测验会真的提交答案（低覆盖率时后端只存不交），刷次数会真的占平台计数窗口，
 * 所以两个按钮都不接受一次误触。
 */
const quizOpen = ref(false)
const learnOpen = ref(false)
const quizTaskId = ref('')
const learnTaskId = ref('')

const quizLabel = computed(() => {
  if (quizRunning.value) return '答题中…'
  return quizOpen.value ? '确认答题？' : '测验答题'
})

const learnLabel = computed(() => {
  if (learnRunning.value) return '刷次数中…'
  return learnOpen.value ? '确认刷次数？' : '刷学习次数'
})

function runningOf(taskId: string): boolean {
  if (!taskId) return false
  const task = tasks.find(taskId)?.task
  return !!task && !tasks.isFinished(task)
}

const quizRunning = computed(() => runningOf(quizTaskId.value))
const learnRunning = computed(() => runningOf(learnTaskId.value))

async function startQuiz(): Promise<void> {
  if (quizRunning.value) return
  window.clearTimeout(quizTimer)
  if (!quizOpen.value) {
    quizOpen.value = true
    quizTimer = window.setTimeout(() => {
      quizOpen.value = false
    }, 4000)
    return
  }
  quizOpen.value = false
  const task = await tasks.start('quiz', {
    platform: props.course.platform,
    course_id: props.course.id,
    apply: true,
  })
  quizTaskId.value = task?.id ?? ''
}

async function startLearn(): Promise<void> {
  if (learnRunning.value) return
  window.clearTimeout(learnTimer)
  if (!learnOpen.value) {
    learnOpen.value = true
    learnTimer = window.setTimeout(() => {
      learnOpen.value = false
    }, 4000)
    return
  }
  learnOpen.value = false
  const task = await tasks.start('learning', {
    platform: props.course.platform,
    course_id: props.course.id,
    apply: true,
    times: 1,
  })
  learnTaskId.value = task?.id ?? ''
}

let quizTimer = 0
let learnTimer = 0

const hasData = computed(() => props.course.has_data !== false)
const percent = computed(() => percentText(props.course.ratio, 1, ''))

const flagTone = computed(() => {
  if (!hasData.value) return 'none'
  if (props.course.overdue > 0) return 'overdue'
  if (props.course.flag === 'done') return 'done'
  if (props.course.pending > 0) return 'pending'
  return 'normal'
})

const next = computed(() =>
  countdownText(props.course.next_due, null),
)

const pendingAssignments = computed(() =>
  props.course.assignments.filter((a) => a.state !== 'done'),
)

const hasParts = computed(() => props.course.progress_parts.length > 0)

// 章节明细只列「还差任务点」的节点（后端已经筛过），所以数量和号数直接加总。
const chapters = computed(() => props.course.chapters ?? [])
const chapterPending = computed(() =>
  chapters.value.reduce((sum, chapter) => sum + (Number(chapter.pending) || 0), 0),
)

// 「已全部完成」必须看比例，不能看「有没有数据」。
// 回归：早先写成 `v-else-if="hasData"`，于是任务点 0/77 的课也顶着
// 「已全部完成」的绿标、右边大数字却是 0%——两个结论直接打架。
const complete = computed(
  () => hasData.value && (props.course.ratio ?? 0) >= 1,
)

// 只有一个进度项时，总进度条和它下面的那一行说的是同一件事
// （「总进度 100%」+「任务点 100/100 100%」），去掉总进度条，留下带明细的那行。
const showSummaryBar = computed(
  () => hasData.value && props.course.progress_parts.length !== 1,
)

// 有些课（超星旧版课程页）只公布「还剩几个任务点」，不公布总任务点数。
// 这种课算不出百分比，但「还剩 14 个」本身就有用，别让它退化成一句「无数据」。
const pendingPoints = computed(() => Number(props.course.pending_points) || 0)

const emptyPartHint = computed(() => {
  if (hasData.value) return '这门课没有细分进度数据，只有总进度。'
  if (pendingPoints.value > 0) {
    return `这门课只公布了「还有 ${pendingPoints.value} 个任务点待完成」，没给总数，所以算不出百分比。`
  }
  return '适配器没有取到这门课的进度或作业数据。'
})
</script>

<template>
  <article class="course card" :class="`course--${flagTone}`">
    <header class="course__head">
      <div class="course__id">
        <div class="course__chips">
          <PlatformChip :platform="course.platform" :name="course.platform_name" color="" />
          <span v-if="course.overdue" class="pill pill--danger">逾期 {{ course.overdue }}</span>
          <span v-else-if="course.pending" class="pill pill--warn">待办 {{ course.pending }}</span>
          <span v-else-if="complete" class="pill pill--ok">已全部完成</span>
          <span v-else-if="hasData" class="pill pill--neutral">进行中</span>
          <span v-else-if="pendingPoints" class="pill pill--warn">剩 {{ pendingPoints }} 任务点</span>
          <span v-else class="pill pill--neutral">无进度数据</span>
        </div>

        <h3 class="course__name truncate" :title="course.name">{{ course.name }}</h3>

        <p class="course__meta">
          <span v-if="course.teacher">{{ course.teacher }}</span>
          <span v-if="course.term" class="faint">{{ course.term }}</span>
          <a v-if="course.url" class="course__link" :href="course.url" target="_blank" rel="noreferrer noopener">
            打开原页面 ↗
          </a>
          <button
            type="button"
            class="course__hide"
            :disabled="busy"
            :title="`隐藏「${course.name}」：作业不再出现在待办和总览里`"
            @click="$emit('hide', course)"
          >
            {{ busy ? '隐藏中…' : '隐藏' }}
          </button>
        </p>
      </div>

      <div class="course__side">
        <p class="course__pct" :class="{ 'course__pct--missing': !hasData }">
          <span v-if="hasData" class="course__num">{{ percent }}</span>
          <span v-else class="course__nodata">无数据</span>
        </p>
        <button
          v-if="canReport"
          type="button"
          class="btn btn--sm course__report"
          :class="confirmOpen ? 'btn--danger' : 'btn--ghost'"
          :disabled="reporting"
          :title="confirmOpen ? '再点一下就真的向学习通上报' : `把「${course.name}」里没做完的任务点补上`"
          @click="reportVideo"
        >
          {{ reportLabel }}
        </button>
        <button
          v-if="canReport && !reporting"
          type="button"
          class="btn btn--sm course__report"
          :class="quizOpen ? 'btn--danger' : 'btn--ghost'"
          :disabled="quizRunning"
          :title="
            quizOpen
              ? '再点一下就真的把答案交上去'
              : `把「${course.name}」里的章节测验做一遍（题库答不全时只保存不交卷）`
          "
          @click="startQuiz"
        >
          {{ quizLabel }}
        </button>
        <button
          v-if="canReport && !reporting"
          type="button"
          class="btn btn--sm course__report"
          :class="learnOpen ? 'btn--danger' : 'btn--ghost'"
          :disabled="learnRunning"
          :title="
            learnOpen
              ? '再点一下就真的去刷学习次数'
              : `给「${course.name}」刷一轮章节学习次数（每轮约 30 秒）`
          "
          @click="startLearn"
        >
          {{ learnLabel }}
        </button>
        <label
          v-if="canReport && !reporting"
          class="course__speed"
          :title="'只补某一类任务点：音视频要签名、阅读/文档/图书是一次性提交、直播要 streamName'"
        >
          <span class="faint">范围</span>
          <select v-model="kindScope" class="course__select">
            <option value="all">全部</option>
            <option value="media">音视频</option>
            <option value="text">阅读/文档</option>
            <option value="book">图书</option>
            <option value="live">直播</option>
          </select>
        </label>
        <label
          v-if="canReport && !reporting"
          class="course__speed"
          :title="'一条一条发最稳；调到 4 能快几倍，但请求会同时打出去'"
        >
          <span class="faint">并发</span>
          <select v-model.number="workers" class="course__select">
            <option :value="1">1</option>
            <option :value="2">2</option>
            <option :value="4">4</option>
            <option :value="8">8</option>
          </select>
        </label>
        <label
          v-if="canReport && !reporting"
          class="course__speed"
          :title="'递进上报：播放位置按 30% / 70% / 100% 分三步发。一次性跳满实测大多不被平台认，分步才记通过'"
        >
          <input v-model="ramp" type="checkbox" />
          <span class="faint">递进</span>
        </label>
        <div v-if="progressRatio !== null" class="course__reportbar">
          <ProgressBar :ratio="progressRatio" size="sm" show-value />
          <p class="course__reportmsg faint truncate" :title="reportTask?.message">
            {{ reportTask?.message || '正在上报' }}
          </p>
        </div>
        <p v-else-if="outcome" class="course__reportmsg" :class="`course__reportmsg--${outcome.tone}`">
          {{ outcome.text }}
        </p>
      </div>
    </header>

    <div v-if="showSummaryBar" class="course__bar">
      <ProgressBar :ratio="course.ratio" label="总进度" size="sm" show-value />
    </div>

    <div v-if="hasParts" class="course__parts">
      <ProgressBar
        v-for="part in course.progress_parts"
        :key="part.label"
        :ratio="part.ratio"
        :label="`${part.label} ${part.done}/${part.total}`"
        size="sm"
        show-value
      />
    </div>
    <p v-else class="course__noparts faint">{{ emptyPartHint }}</p>

    <div v-if="chapters.length" class="course__chapters">
      <button
        type="button"
        class="course__toggle"
        :aria-expanded="chaptersOpen"
        @click="chaptersOpen = !chaptersOpen"
      >
        <span class="course__caret" aria-hidden="true">{{ chaptersOpen ? '▾' : '▸' }}</span>
        还差任务点的章节
        <span class="pill pill--neutral">{{ chapters.length }} 节</span>
        <span class="faint">共 {{ chapterPending }} 个任务点</span>
      </button>

      <ul v-if="chaptersOpen" class="course__chapter-list">
        <li
          v-for="(chapter, index) in chapters"
          :key="`${chapter.sbar}-${chapter.name}-${index}`"
          class="course__chapter"
          :style="{ paddingLeft: `${0.6 + (Number(chapter.depth) || 0) * 0.9}rem` }"
        >
          <span v-if="chapter.sbar" class="course__chapter-sbar">{{ chapter.sbar }}</span>
          <span class="course__chapter-name">{{ chapter.name }}</span>
          <span v-if="chapter.unit" class="course__chapter-unit faint">{{ chapter.unit }}</span>
          <span class="pill pill--warn">还差 {{ chapter.pending }}</span>
        </li>
      </ul>
    </div>

    <p class="course__footer">
      <span>最近活跃：{{ formatAgo(course.last_active, new Date(), '暂无记录') }}</span>
      <span aria-hidden="true">·</span>
      <span v-if="course.next_due">
        下一项截止：{{ formatDue(course.next_due) }}<span v-if="course.next_due_title">（{{ course.next_due_title }}）</span>
        <span class="course__count" :class="`course__count--${next.tone}`">{{ next.text }}</span>
      </span>
      <span v-else>下一项截止：无</span>
      <span aria-hidden="true">·</span>
      <span>已完成 {{ course.done }} 项</span>
    </p>

    <div v-if="course.assignments.length" class="course__hw">
      <button
        type="button"
        class="course__toggle"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span class="course__caret" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
        作业明细
        <span class="pill pill--neutral">{{ course.assignments.length }} 项</span>
        <span v-if="pendingAssignments.length" class="faint">其中 {{ pendingAssignments.length }} 项未完成</span>
      </button>

      <ul v-if="expanded" class="course__list">
        <AssignmentItem v-for="item in course.assignments" :key="item.uid" :item="item" :show-course="false" />
      </ul>
    </div>
  </article>
</template>

<style scoped>
.course {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  padding: var(--s-5);
  overflow: hidden;
}

.course::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--border-strong);
}

.course--overdue::before {
  background: var(--danger);
}
.course--pending::before {
  background: var(--warn);
}
.course--done::before {
  background: var(--ok);
}

.course__head {
  display: flex;
  gap: var(--s-4);
  align-items: flex-start;
  justify-content: space-between;
}

.course__id {
  min-width: 0;
  flex: 1 1 auto;
}

.course__chips {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: var(--s-2);
}

.course__name {
  font-size: var(--fs-lg);
}

.course__meta {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  flex-wrap: wrap;
  margin-top: 0.2rem;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.course__link {
  font-size: var(--fs-sm);
}

/* 隐藏是个「低干扰」动作：平时像一截淡淡的文字链，悬停才显出来。 */
.course__hide {
  padding: 0;
  border: 0;
  background: none;
  color: var(--text-faint);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}

.course__hide:hover:not(:disabled) {
  color: var(--warn);
}

.course__hide:disabled {
  cursor: progress;
  opacity: 0.6;
}

.course__side {
  display: flex;
  flex: none;
  flex-direction: column;
  gap: var(--s-1);
  align-items: flex-end;
}

.course__report {
  white-space: nowrap;
}

/* 上报进度就贴在按钮下面，跟按钮同宽，不另起一行占版面。 */
.course__reportbar {
  display: flex;
  flex-direction: column;
  gap: var(--s-1);
  width: 8.5rem;
  margin-top: var(--s-1);
}

.course__reportmsg {
  margin: 0;
  max-width: 12rem;
  font-size: var(--fs-xs);
  line-height: 1.35;
  text-align: right;
}

.course__reportmsg--ok {
  color: var(--ok);
}

.course__reportmsg--danger {
  color: var(--danger);
}

.course__reportmsg--muted {
  color: var(--text-faint);
}

/* 并发档位：跟按钮同一列，小一号，看起来像按钮的附注而不是另一个控件。 */
.course__speed {
  display: flex;
  align-items: center;
  gap: var(--s-1);
  margin-top: var(--s-1);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.course__select {
  padding: 0 var(--s-1);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--bg);
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-xs);
  cursor: pointer;
}

.course__select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.course__pct {
  flex: none;
  text-align: right;
}

.course__num {
  font-size: var(--fs-2xl);
  font-weight: 680;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}

.course__nodata {
  color: var(--text-faint);
  font-size: var(--fs-lg);
  font-weight: 600;
}

.course__parts {
  display: grid;
  gap: var(--s-3);
}

@media (min-width: 900px) {
  .course__parts {
    grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  }
}

.course__noparts {
  padding: var(--s-2) 0;
  font-size: var(--fs-sm);
}

.course__chapters {
  margin-top: calc(-1 * var(--s-1));
}

.course__chapter-list {
  margin: 0 0 var(--s-1);
  padding: 0;
  max-height: 16rem;
  overflow-y: auto;
  list-style: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-2);
}

.course__chapter {
  display: flex;
  gap: var(--s-2);
  align-items: baseline;
  padding: 0.35rem 0.6rem;
  font-size: var(--fs-sm);
}

.course__chapter + .course__chapter {
  border-top: 1px solid var(--border-subtle);
}

.course__chapter-sbar {
  flex: none;
  min-width: 3.2rem;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.course__chapter-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.course__chapter-unit {
  flex: none;
  font-size: var(--fs-xs);
}

.course__chapter .pill {
  flex: none;
  margin-left: auto;
}

.course__footer {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  flex-wrap: wrap;
  padding-top: var(--s-1);
  border-top: 1px solid var(--border-subtle);
  color: var(--text-faint);
  font-size: var(--fs-sm);
}

.course__count {
  margin-left: var(--s-2);
  font-weight: 600;
}

.course__count--overdue {
  color: var(--danger);
}
.course__count--urgent {
  color: var(--warn);
}

.course__hw {
  margin-top: var(--s-1);
}

.course__toggle {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  width: 100%;
  padding: var(--s-2) 0;
  border: 0;
  background: none;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
}

.course__toggle:hover {
  color: var(--text);
}

.course__caret {
  width: 0.7rem;
  color: var(--text-faint);
}

.course__list {
  margin: var(--s-1) 0 0;
  padding: 0;
  list-style: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  overflow: hidden;
  background: var(--surface-2);
}

.course__list > :deep(li + li) {
  border-top: 1px solid var(--border-subtle);
}
</style>
