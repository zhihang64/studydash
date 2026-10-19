<script setup lang="ts">
/**
 * 任务抽屉：登录、抓取、探测都在这里跑给你看。
 *
 * 两处刻意的设计：
 *  - 日志用「深底控制台」而不是普通列表。它本来就是终端输出的延续，等宽字体 +
 *    深底能让用户一眼认出「这是系统在说话」，不会和界面的提示混在一起。
 *  - 日志自动滚到底，但**用户往上滚了就不再抢**——正在翻旧日志时被拽回底部
 *    是很烦的。
 */
import { computed, nextTick, ref, watch } from 'vue'

import type { TaskView } from '@/api/types'
import { TASK_STATE_LABEL, useTasksStore } from '@/stores/tasks'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const tasks = useTasksStore()
const logBoxes = ref<Record<string, HTMLElement | null>>({})
const pinned = ref<Record<string, boolean>>({})

const activeCount = computed(() => tasks.active.length)

function stateTone(task: TaskView): string {
  if (task.state === 'done') return 'ok'
  if (task.state === 'failed') return 'danger'
  if (task.state === 'cancelled') return 'neutral'
  return 'accent'
}

function stateText(task: TaskView): string {
  return TASK_STATE_LABEL[task.state] ?? task.state
}

function setLogBox(id: string, el: unknown): void {
  logBoxes.value[id] = (el as HTMLElement | null) ?? null
}

function onScroll(id: string, event: Event): void {
  const el = event.target as HTMLElement
  // 距底部 32px 内算「贴着底」，否则认为用户在往回看
  pinned.value[id] = el.scrollHeight - el.scrollTop - el.clientHeight < 32
}

watch(
  () => tasks.records.map((r) => `${r.task.id}:${r.logs.length}`).join('|'),
  () => {
    void nextTick(() => {
      tasks.records.forEach((record) => {
        const el = logBoxes.value[record.task.id]
        if (!el) return
        if (pinned.value[record.task.id] === false) return
        el.scrollTop = el.scrollHeight
      })
    })
  },
)

function levelClass(level: string): string {
  return `log__line--${level}`
}
</script>

<template>
  <Transition name="drawer">
    <aside
      v-if="open"
      class="drawer"
      role="complementary"
      aria-label="任务进度"
      @keydown.esc="emit('close')"
    >
      <header class="drawer__head">
        <h2 class="drawer__title">
          任务
          <span v-if="activeCount" class="pill pill--accent">{{ activeCount }} 个在跑</span>
        </h2>
        <div class="row">
          <button
            v-if="tasks.records.some((r) => tasks.isFinished(r.task))"
            type="button"
            class="btn btn--ghost btn--sm"
            @click="tasks.clearFinished()"
          >
            清空已结束
          </button>
          <button type="button" class="btn btn--ghost btn--icon" aria-label="关闭" @click="emit('close')">
            ✕
          </button>
        </div>
      </header>

      <div class="drawer__body">
        <p v-if="!tasks.records.length" class="faint drawer__empty">
          还没有任务。去「账号」页登录，或抓取一次数据。
        </p>

        <article v-for="record in tasks.records" :key="record.task.id" class="task card">
          <div class="task__head">
            <span class="task__name">{{ tasks.label(record.task.kind) }}</span>
            <span class="pill" :class="`pill--${stateTone(record.task)}`">{{ stateText(record.task) }}</span>
            <button
              v-if="!tasks.isFinished(record.task)"
              type="button"
              class="btn btn--ghost btn--sm"
              @click="tasks.cancel(record.task.id)"
            >
              取消
            </button>
            <button
              v-else
              type="button"
              class="btn btn--ghost btn--sm"
              aria-label="从列表里移除"
              @click="tasks.forget(record.task.id)"
            >
              ✕
            </button>
          </div>

          <p v-if="record.task.message" class="task__message">{{ record.task.message }}</p>

          <div
            v-if="!tasks.isFinished(record.task)"
            class="task__track"
            role="progressbar"
            :aria-label="`${tasks.label(record.task.kind)} 进度`"
            aria-valuemin="0"
            aria-valuemax="100"
            :aria-valuenow="Math.round(record.task.progress * 100)"
          >
            <i class="task__fill" :style="{ width: `${Math.round(record.task.progress * 100)}%` }" />
          </div>

          <p v-if="record.task.error" class="task__error">
            {{ record.task.error.message }}
            <span v-if="record.task.error.hint" class="task__hint">{{ record.task.error.hint }}</span>
          </p>

          <p v-if="record.degraded && !tasks.isFinished(record.task)" class="task__degraded">
            实时进度连接断开了，已改用轮询。任务本身还在跑。
          </p>

          <details v-if="record.logs.length" class="task__logs" :open="!tasks.isFinished(record.task)">
            <summary>过程日志（{{ record.logs.length }} 行）</summary>
            <div
              :ref="(el) => setLogBox(record.task.id, el)"
              class="log"
              role="log"
              aria-label="任务日志"
              @scroll="onScroll(record.task.id, $event)"
            >
              <p v-for="(line, index) in record.logs" :key="index" class="log__line" :class="levelClass(line.level)">
                <span class="log__mark" aria-hidden="true">
                  {{ line.level === 'ok' ? '✓' : line.level === 'error' ? '×' : line.level === 'warn' ? '!' : line.level === 'step' ? '▸' : '·' }}
                </span>
                <span class="log__text">{{ line.text }}</span>
              </p>
            </div>
          </details>
        </article>
      </div>
    </aside>
  </Transition>
</template>

<style scoped>
.drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: var(--z-drawer);
  display: flex;
  flex-direction: column;
  width: min(30rem, 100vw);
  border-left: 1px solid var(--border);
  background: var(--bg);
  box-shadow: var(--shadow-4);
}

.drawer__head {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  justify-content: space-between;
  padding: var(--s-4) var(--s-5);
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.drawer__title {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  font-size: var(--fs-lg);
}

.drawer__body {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  padding: var(--s-4) var(--s-5) var(--s-8);
  overflow-y: auto;
}

.drawer__empty {
  padding: var(--s-8) 0;
  text-align: center;
}

.task {
  padding: var(--s-4);
}

.task__head {
  display: flex;
  gap: var(--s-2);
  align-items: center;
}

.task__name {
  flex: 1 1 auto;
  font-weight: 600;
}

.task__message {
  margin-top: var(--s-2);
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.task__track {
  height: 0.3rem;
  margin-top: var(--s-3);
  border-radius: var(--r-full);
  background: var(--surface-3);
  overflow: hidden;
}

.task__fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width var(--dur-base) linear;
}

.task__error {
  margin-top: var(--s-3);
  padding: var(--s-2) var(--s-3);
  border-radius: var(--r-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: var(--fs-sm);
}

.task__hint {
  display: block;
  margin-top: var(--s-1);
  color: var(--text-muted);
}

.task__degraded {
  margin-top: var(--s-2);
  color: var(--warn);
  font-size: var(--fs-xs);
}

.task__logs {
  margin-top: var(--s-3);
}

.task__logs summary {
  cursor: pointer;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.log {
  max-height: 14rem;
  margin-top: var(--s-2);
  padding: var(--s-3);
  border-radius: var(--r-sm);
  background: #0d1220;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.log__line {
  display: flex;
  gap: var(--s-2);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  line-height: 1.65;
  color: #cbd5e1;
  overflow-wrap: anywhere;
}

.log__mark {
  flex: none;
  width: 0.7rem;
  color: #64748b;
}

.log__line--ok .log__mark {
  color: #4ade80;
}
.log__line--warn .log__mark {
  color: #fbbf24;
}
.log__line--error .log__mark {
  color: #f87171;
}
.log__line--step .log__mark {
  color: #818cf8;
}
.log__line--debug {
  color: #7c8aa3;
}
.log__line--ok {
  color: #86efac;
}
.log__line--warn {
  color: #fcd34d;
}
.log__line--error {
  color: #fca5a5;
}

.drawer-enter-active,
.drawer-leave-active {
  transition: transform var(--dur-slow) var(--ease);
}

.drawer-enter-from,
.drawer-leave-to {
  transform: translateX(100%);
}
</style>
