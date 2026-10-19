<script setup lang="ts">
/**
 * 一条作业 / 任务点 / 测验。
 *
 * 倒计时文案与紧急度都取后端算好的值（`state` / `days_left` / `state_label`）：
 * 看板的分组、预警、排序全都基于它，前端自己再算一套一定会和分组对不上。
 * 只有一种情况前端要动手——后端没给 `days_left`（没有截止时间），那就显示「无截止」。
 */
import { computed } from 'vue'

import type { AssignmentView } from '@/api/types'
import { STATE_TONE } from '@/stores/dashboard'
import { formatDue } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    item: AssignmentView
    /** 课程卡片里已经在课程上下文内，不必重复显示课程名 */
    showCourse?: boolean
  }>(),
  { showCourse: true },
)

const tone = computed(() => STATE_TONE[props.item.state as keyof typeof STATE_TONE] ?? 'neutral')

const dueText = computed(() => formatDue(props.item.due, '无截止时间'))

const scoreText = computed(() => {
  const { score, max_score: max } = props.item
  if (score === null || score === undefined) return ''
  return max ? `${score}/${max}` : String(score)
})

const kindLabel = computed(() => props.item.kind_label || props.item.kind)

const linkUrl = computed(() => props.item.url || '')
</script>

<template>
  <li class="hw" :class="`hw--${tone}`">
    <div class="hw__main">
      <div class="hw__top">
        <span class="pill pill--neutral hw__kind">{{ kindLabel }}</span>
        <span v-if="showCourse && item.course" class="hw__course truncate" :title="item.course">
          {{ item.course }}
        </span>
      </div>

      <p class="hw__title">
        <a v-if="linkUrl" :href="linkUrl" target="_blank" rel="noreferrer noopener">
          {{ item.title }}
        </a>
        <template v-else>{{ item.title }}</template>
      </p>

      <div class="hw__meta">
        <span class="hw__due">{{ dueText }}</span>
        <span v-if="item.state_label" class="pill" :class="`pill--${tone}`">
          {{ item.state_label }}
        </span>
        <span v-if="scoreText" class="hw__score mono">{{ scoreText }} 分</span>
        <span v-if="item.detail" class="hw__detail faint truncate" :title="item.detail">
          {{ item.detail }}
        </span>
      </div>
    </div>
  </li>
</template>

<style scoped>
.hw {
  display: flex;
  gap: var(--s-3);
  padding: var(--s-3) var(--s-4);
  border-left: 2px solid transparent;
}

.hw--danger {
  border-left-color: var(--danger);
}

.hw--warn {
  border-left-color: var(--warn);
}

.hw--info {
  border-left-color: var(--info);
}

.hw--ok {
  border-left-color: var(--ok);
}

.hw__main {
  min-width: 0;
  flex: 1 1 auto;
}

.hw__top {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  min-width: 0;
}

.hw__kind {
  flex: none;
}

.hw__course {
  min-width: 0;
  color: var(--text-faint);
  font-size: var(--fs-sm);
}

.hw__title {
  margin-top: 0.15rem;
  font-weight: 550;
  line-height: 1.5;
}

.hw__title a {
  color: inherit;
}

.hw__title a:hover {
  color: var(--accent);
}

.hw__meta {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  flex-wrap: wrap;
  margin-top: 0.2rem;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.hw__due {
  font-variant-numeric: tabular-nums;
}

.hw--danger .hw__due {
  color: var(--danger);
  font-weight: 600;
}

.hw__score {
  color: var(--text);
  font-weight: 600;
}

.hw__detail {
  min-width: 0;
  max-width: 18rem;
}
</style>
