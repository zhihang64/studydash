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

import type { CourseView } from '@/api/types'
import { countdownText, formatAgo, formatDue, percentText } from '@/utils/format'

import AssignmentItem from './AssignmentItem.vue'
import PlatformChip from './PlatformChip.vue'
import ProgressBar from './ProgressBar.vue'

const props = defineProps<{ course: CourseView; busy?: boolean }>()

defineEmits<{ hide: [course: CourseView] }>()

const expanded = ref(false)

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

      <p class="course__pct" :class="{ 'course__pct--missing': !hasData }">
        <span v-if="hasData" class="course__num">{{ percent }}</span>
        <span v-else class="course__nodata">无数据</span>
      </p>
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
