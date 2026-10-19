<script setup lang="ts">
/**
 * 提示容器。
 *
 * 无障碍上有两处讲究：
 *  - 整块是 ``aria-live="polite"`` 的日志区，屏幕阅读器会把新提示念出来；
 *  - 错误的语气更急（assertive），成功/信息不打断用户当前正在读的内容。
 * 用 ``aria-live`` 而不是 ``role="alert"``：后者会把整块内容重念一遍。
 */
import { dismiss, toasts } from '@/composables/useToast'

const TONE: Record<string, { pill: string; label: string }> = {
  ok: { pill: 'pill--ok', label: '完成' },
  info: { pill: 'pill--info', label: '提示' },
  warn: { pill: 'pill--warn', label: '注意' },
  error: { pill: 'pill--danger', label: '出错了' },
}
</script>

<template>
  <div class="toasts" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <div
        v-for="item in toasts"
        :key="item.id"
        class="toast"
        :class="`toast--${item.kind}`"
        :role="item.kind === 'error' ? 'alert' : 'status'"
      >
        <div class="toast__head">
          <span class="pill" :class="TONE[item.kind].pill">{{ TONE[item.kind].label }}</span>
          <span class="toast__title">{{ item.title }}</span>
          <button
            type="button"
            class="btn btn--ghost btn--sm toast__close"
            aria-label="关闭"
            @click="dismiss(item.id)"
          >
            ✕
          </button>
        </div>
        <p v-if="item.detail" class="toast__detail">{{ item.detail }}</p>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  right: var(--s-4);
  bottom: var(--s-4);
  z-index: var(--z-toast);
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  width: min(24rem, calc(100vw - 2rem));
  pointer-events: none;
}

.toast {
  padding: var(--s-3) var(--s-3) var(--s-3) var(--s-4);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--info);
  border-radius: var(--r-md);
  background: var(--surface);
  box-shadow: var(--shadow-3);
  pointer-events: auto;
}

.toast--ok {
  border-left-color: var(--ok);
}

.toast--warn {
  border-left-color: var(--warn);
}

.toast--error {
  border-left-color: var(--danger);
}

.toast__head {
  display: flex;
  gap: var(--s-2);
  align-items: center;
}

.toast__title {
  flex: 1 1 auto;
  font-weight: 600;
}

.toast__close {
  flex: none;
}

.toast__detail {
  margin-top: var(--s-2);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.toast-enter-active,
.toast-leave-active {
  transition:
    opacity var(--dur-base) var(--ease),
    transform var(--dur-base) var(--ease);
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(0.6rem);
}

@media (max-width: 640px) {
  .toasts {
    right: var(--s-2);
    left: var(--s-2);
    bottom: var(--s-2);
    width: auto;
  }
}
</style>
