<script setup lang="ts">
/**
 * 平台小徽标：一个圆点 + 短名（超星 / 慕课 / 手动…）。
 *
 * 颜色和短名优先用调用方给的（payload 里已经带了 platform_name），
 * 没有才回查平台名录——这样时间线上的每一条都不用先去加载一遍名录。
 */
import { computed } from 'vue'

import { useCatalogStore } from '@/stores/catalog'
import { cssColor } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    platform: string
    name?: string
    color?: string
    /** 显示中文全名而不是短名 */
    full?: boolean
    size?: 'sm' | 'md'
  }>(),
  { name: '', color: '', full: false, size: 'md' },
)

const catalog = useCatalogStore()

const label = computed(() => {
  if (props.name) return props.name
  return props.full ? catalog.fullName(props.platform) : catalog.label(props.platform)
})

const tint = computed(() => cssColor(props.color || catalog.color(props.platform)))

const title = computed(() => catalog.fullName(props.platform) || props.platform)
</script>

<template>
  <span class="chip" :class="`chip--${size}`" :title="title">
    <i class="dot" :style="{ '--dot': tint }" aria-hidden="true" />
    <span class="chip__text">{{ label }}</span>
  </span>
</template>

<style scoped>
.chip {
  display: inline-flex;
  gap: 0.32rem;
  align-items: center;
  flex: none;
  max-width: 11rem;
  padding: 0.05rem var(--s-2) 0.05rem 0.36rem;
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  background: var(--surface-2);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 550;
  line-height: 1.7;
  white-space: nowrap;
}

.chip--sm {
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.chip__text {
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
