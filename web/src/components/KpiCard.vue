<script setup lang="ts">
/**
 * 总览里的一个数字卡。
 *
 * 数字用等宽数字（tabular-nums），否则每张卡的数字宽度不一，一排看过去是歪的。
 */
withDefaults(
  defineProps<{
    label: string
    value: string | number
    unit?: string
    hint?: string
    tone?: 'neutral' | 'accent' | 'ok' | 'warn' | 'danger' | 'info'
    /** 数值旁边的小徽标，比如「已完成 2 项」 */
    badge?: string
  }>(),
  { unit: '', hint: '', tone: 'neutral', badge: '' },
)
</script>

<template>
  <div class="kpi" :class="`kpi--${tone}`">
    <p class="kpi__label">
      <span class="dot" aria-hidden="true" />
      {{ label }}
    </p>
    <p class="kpi__value">
      <span class="kpi__num">{{ value }}</span>
      <span v-if="unit" class="kpi__unit">{{ unit }}</span>
    </p>
    <p v-if="badge" class="kpi__badge">{{ badge }}</p>
    <div v-if="$slots.default" class="kpi__extra">
      <slot />
    </div>
    <p v-if="hint" class="kpi__hint faint">{{ hint }}</p>
  </div>
</template>

<style scoped>
.kpi {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  padding: var(--s-4) var(--s-5);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface);
  box-shadow: var(--shadow-1);
}

.kpi__label {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.kpi--accent .dot {
  background: var(--accent);
}
.kpi--ok .dot {
  background: var(--ok);
}
.kpi--warn .dot {
  background: var(--warn);
}
.kpi--danger .dot {
  background: var(--danger);
}
.kpi--info .dot {
  background: var(--info);
}

.kpi__value {
  display: flex;
  gap: 0.25rem;
  align-items: baseline;
}

.kpi__num {
  font-size: var(--fs-3xl);
  font-weight: 680;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  line-height: 1.15;
}

.kpi--danger .kpi__num {
  color: var(--danger);
}

.kpi--ok .kpi__num {
  color: var(--ok);
}

.kpi__unit {
  color: var(--text-muted);
  font-size: var(--fs-md);
}

.kpi__badge {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.kpi__hint {
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.kpi__extra {
  margin-top: var(--s-2);
}
</style>
