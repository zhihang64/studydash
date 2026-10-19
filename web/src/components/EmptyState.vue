<script setup lang="ts">
/**
 * 空状态 / 引导面板。看板在「还没抓过数据」时显示的是引导而不是报错，
 * 所以需要一个像样的空状态组件，而不是一行灰字。
 */
withDefaults(
  defineProps<{
    icon?: string
    title: string
    body?: string
    tone?: 'neutral' | 'warn' | 'danger'
    compact?: boolean
  }>(),
  { icon: '◌', body: '', tone: 'neutral', compact: false },
)
</script>

<template>
  <div class="empty" :class="[`empty--${tone}`, { 'empty--compact': compact }]">
    <span v-if="icon" class="empty__icon" aria-hidden="true">{{ icon }}</span>
    <div class="empty__text">
      <p class="empty__title">{{ title }}</p>
      <p v-if="body" class="empty__body muted">{{ body }}</p>
    </div>
    <div v-if="$slots.default" class="empty__actions">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  align-items: center;
  padding: var(--s-10) var(--s-5);
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--surface);
  text-align: center;
}

.empty--compact {
  padding: var(--s-6) var(--s-4);
}

.empty--warn {
  border-color: var(--warn-border);
  background: var(--warn-soft);
}

.empty--danger {
  border-color: var(--danger-border);
  background: var(--danger-soft);
}

.empty__icon {
  font-size: 1.9rem;
  line-height: 1;
  color: var(--text-faint);
}

.empty__text {
  max-width: 34rem;
}

.empty__title {
  font-size: var(--fs-lg);
  font-weight: 600;
}

.empty__body {
  margin-top: var(--s-2);
  font-size: var(--fs-md);
  line-height: 1.75;
}

.empty__actions {
  display: flex;
  gap: var(--s-3);
  flex-wrap: wrap;
  justify-content: center;
  margin-top: var(--s-2);
}
</style>
