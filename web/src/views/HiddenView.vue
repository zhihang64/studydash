<script setup lang="ts">
/**
 * 已隐藏课程：结课了、或者这学期不想再看到它占位置的课程。
 *
 * 隐藏是后端过滤——这些课的作业不会出现在待办、总览和平台统计里，
 * 但它们的数据仍然留在 progress.json 里，所以「恢复」不需要重新抓取。
 */
import { onMounted } from 'vue'
import { RouterLink } from 'vue-router'

import type { HiddenCourseView } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PlatformChip from '@/components/PlatformChip.vue'
import { toast } from '@/composables/useToast'
import { useDashboardStore } from '@/stores/dashboard'
import { percentText } from '@/utils/format'

const dash = useDashboardStore()

function progress(course: HiddenCourseView): string {
  if (course.has_data === false) return '无数据'
  return percentText(course.progress, 1, '无数据')
}

async function restore(course: HiddenCourseView): Promise<void> {
  try {
    await dash.unhideCourse(course.key)
    toast.ok(`已恢复「${course.name}」`, '它和它的作业重新出现在课程、待办与总览里。')
  } catch (err) {
    toast.error('恢复失败', err instanceof Error ? err.message : String(err))
  }
}

onMounted(() => {
  void dash.load()
})
</script>

<template>
  <div class="stack">
    <header class="head">
      <div>
        <h1>已隐藏课程</h1>
        <p class="muted">
          共 {{ dash.hiddenCount }} 门。隐藏的课程不计入总览与待办，数据仍然保留。
        </p>
      </div>
      <RouterLink class="btn btn--ghost btn--sm" to="/courses">返回课程</RouterLink>
    </header>

    <EmptyState
      v-if="!dash.hiddenCount"
      icon="◌"
      title="还没有隐藏任何课程"
      body="想给结课的、或者不想再看到的课程腾位置，就在「课程」页点课程卡上的「隐藏」。"
    >
      <RouterLink class="btn btn--primary" to="/courses">去课程页</RouterLink>
    </EmptyState>

    <div v-else class="grid">
      <article v-for="course in dash.hiddenCourses" :key="course.key" class="hidden card">
        <div class="hidden__head">
          <div class="hidden__id">
            <PlatformChip :platform="course.platform" :name="course.platform_name" />
            <h3 class="hidden__name" :title="course.name">{{ course.name }}</h3>
            <p class="hidden__meta faint">
              <span v-if="course.teacher">{{ course.teacher }}</span>
              <span v-if="course.term">{{ course.term }}</span>
            </p>
          </div>
          <p class="hidden__pct" :class="{ 'hidden__pct--missing': course.has_data === false }">
            {{ progress(course) }}
          </p>
        </div>

        <p class="hidden__stats faint">
          <span v-if="course.overdue" class="pill pill--danger">逾期 {{ course.overdue }}</span>
          <span v-else-if="course.pending" class="pill pill--warn">待办 {{ course.pending }}</span>
          <span v-else class="pill pill--neutral">没有待办</span>
          <span>已完成 {{ course.done }} 项</span>
          <span v-if="course.next_due_title">下一项：{{ course.next_due_title }}</span>
        </p>

        <div class="hidden__actions">
          <a
            v-if="course.url"
            class="btn btn--ghost btn--sm"
            :href="course.url"
            target="_blank"
            rel="noreferrer noopener"
          >
            打开原页面 ↗
          </a>
          <button
            type="button"
            class="btn btn--primary btn--sm"
            :disabled="dash.hiding === course.key"
            @click="restore(course)"
          >
            {{ dash.hiding === course.key ? '恢复中…' : '恢复显示' }}
          </button>
        </div>
      </article>
    </div>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  gap: var(--s-4);
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
}

.grid {
  display: grid;
  gap: var(--s-4);
  grid-template-columns: repeat(auto-fit, minmax(21rem, 1fr));
}

.hidden {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  padding: var(--s-4);
}

.hidden__head {
  display: flex;
  gap: var(--s-3);
  align-items: flex-start;
  justify-content: space-between;
}

.hidden__id {
  min-width: 0;
}

.hidden__name {
  margin-top: var(--s-2);
  font-size: var(--fs-md);
}

.hidden__meta {
  display: flex;
  gap: var(--s-2);
  flex-wrap: wrap;
  margin-top: var(--s-1);
  font-size: var(--fs-sm);
}

.hidden__pct {
  flex: none;
  font-size: var(--fs-lg);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.hidden__pct--missing {
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--text-faint);
}

.hidden__stats {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  flex-wrap: wrap;
  font-size: var(--fs-sm);
}

.hidden__actions {
  display: flex;
  gap: var(--s-2);
  justify-content: flex-end;
  margin-top: auto;
}
</style>
