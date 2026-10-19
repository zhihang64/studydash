<script setup lang="ts">
/**
 * 一条提示条。warnings（平台没验证、抓取失败）与「还没抓过数据」都用它，
 * 保证全站的提示长得一样、语气一样。
 */
withDefaults(
  defineProps<{
    tone?: 'info' | 'warn' | 'danger' | 'ok'
    title: string
    body?: string
    /** 右侧的补救建议，通常是「跑 xxx」这类操作指引 */
    fix?: string
    dismissible?: boolean
  }>(),
  { tone: 'info', body: '', fix: '', dismissible: false },
)

defineEmits<{ dismiss: [] }>()
</script>

<template>
  <div class="note" :class="`note--${tone}`" role="status">
    <span class="note__mark" aria-hidden="true">
      {{ tone === 'ok' ? '✓' : tone === 'warn' ? '!' : tone === 'danger' ? '×' : 'i' }}
    </span>
    <div class="note__body">
      <p class="note__title">{{ title }}</p>
      <p v-if="body" class="note__text">{{ body }}</p>
      <p v-if="fix" class="note__fix">{{ fix }}</p>
      <slot />
    </div>
    <button
      v-if="dismissible"
      type="button"
      class="btn btn--ghost btn--sm note__close"
      aria-label="关闭这条提示"
      @click="$emit('dismiss')"
    >
      ✕
    </button>
  </div>
</template>

<style scoped>
.note {
  display: flex;
  gap: var(--s-3);
  align-items: flex-start;
  padding: var(--s-3) var(--s-4);
  border: 1px solid var(--info-border);
  border-radius: var(--r-md);
  background: var(--info-soft);
  color: var(--text);
}

.note--warn {
  border-color: var(--warn-border);
  background: var(--warn-soft);
}

.note--danger {
  border-color: var(--danger-border);
  background: var(--danger-soft);
}

.note--ok {
  border-color: var(--ok-border);
  background: var(--ok-soft);
}

.note__mark {
  display: grid;
  place-items: center;
  flex: none;
  width: 1.15rem;
  height: 1.15rem;
  margin-top: 0.18rem;
  border-radius: var(--r-full);
  background: var(--info);
  color: #fff;
  font-size: 0.7rem;
  font-weight: 700;
}

.note--warn .note__mark {
  background: var(--warn);
}

.note--danger .note__mark {
  background: var(--danger);
}

.note--ok .note__mark {
  background: var(--ok);
}

.note__body {
  flex: 1 1 auto;
  min-width: 0;
}

.note__title {
  font-weight: 600;
}

.note__text {
  margin-top: var(--s-1);
  color: var(--text-muted);
  font-size: var(--fs-md);
  line-height: 1.7;
}

.note__fix {
  margin-top: var(--s-2);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}

.note__close {
  flex: none;
  margin-top: 0.05rem;
}
</style>
