<script setup lang="ts">
/**
 * 进度条。
 *
 * 两种「没有进度」必须区分开：
 *  - ``missing``（没数据）：不画条，显示占位文案。适配器没取到 ≠ 学了 0%。
 *  - ``ratio = 0``：老老实实画一条空条。
 */
import { computed } from 'vue'

import { clamp01, percentText } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    /** 0~1 */
    ratio?: number | null
    label?: string
    /** 覆盖填充色，一般是平台色 */
    color?: string
    size?: 'sm' | 'md'
    missing?: boolean
    missingText?: string
    /** 右侧是否再显示一遍百分比（卡片右上角已经显示时就不必了） */
    showValue?: boolean
  }>(),
  {
    ratio: null,
    label: '',
    color: '',
    size: 'md',
    missing: false,
    missingText: '暂无数据',
    showValue: false,
  },
)

const available = computed(() => !props.missing && props.ratio !== null && props.ratio !== undefined)

const clamped = computed(() => (available.value ? clamp01(Number(props.ratio)) : 0))

const text = computed(() => percentText(props.ratio ?? null))

/**
 * 填充宽度必须写在这儿：scoped 样式里只有 ``transition: width``，没有宽度值，
 * 少了这一条进度条永远只是一条空轨道。
 */
const barStyle = computed(() => {
  const style: Record<string, string> = { width: `${clamped.value * 100}%` }
  if (props.color) style.background = props.color
  return style
})
</script>

<template>
  <div class="bar" :class="[`bar--${size}`, { 'bar--empty': !available }]">
    <div v-if="label" class="bar__label">
      <span class="truncate">{{ label }}</span>
      <span v-if="showValue" class="bar__value mono">{{ available ? text : missingText }}</span>
    </div>

    <div
      v-if="available"
      class="bar__track"
      role="progressbar"
      :aria-label="label || '进度'"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="Math.round(clamped * 1000) / 10"
      :aria-valuetext="text"
    >
      <i class="bar__fill" :style="barStyle" />
    </div>

    <p v-else class="bar__missing faint">{{ missingText }}</p>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  flex-direction: column;
  gap: var(--s-1);
  min-width: 0;
}

.bar__label {
  display: flex;
  gap: var(--s-3);
  align-items: baseline;
  justify-content: space-between;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}

.bar__value {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--text);
}

.bar__track {
  position: relative;
  height: 0.4rem;
  border-radius: var(--r-full);
  background: var(--surface-3);
  overflow: hidden;
}

.bar--sm .bar__track {
  height: 0.3rem;
}

.bar__fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width var(--dur-slow) var(--ease);
}

.bar__missing {
  font-size: var(--fs-sm);
  font-style: normal;
}
</style>
