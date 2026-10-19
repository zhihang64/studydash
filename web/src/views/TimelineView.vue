<script setup lang="ts">
/**
 * 待办时间线。
 *
 * 分组用的是后端算好的 ``state``（already 按 alert_days 判过），所以这里的组名
 * 与总览的「N 项在 3 天内到期」永远对得上。分组可折叠，默认只展开有内容的组。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import AssignmentItem from '@/components/AssignmentItem.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useCatalogStore } from '@/stores/catalog'
import { useDashboardStore } from '@/stores/dashboard'

const dash = useDashboardStore()
const catalog = useCatalogStore()

const collapsed = ref<Record<string, boolean>>({})

const groups = computed(() => dash.timelineGroups)

const total = computed(() => groups.value.reduce((sum, g) => sum + g.items.length, 0))

const platforms = computed(() => dash.availablePlatforms)

function toggle(state: string): void {
  collapsed.value = { ...collapsed.value, [state]: !collapsed.value[state] }
}

function isOpen(state: string): boolean {
  return !collapsed.value[state]
}

/** 只有一组时默认全展开，组多了把「已完成」和「无截止」收起来更清爽。 */
watch(groups, (list) => {
  list.forEach((group) => {
    if (collapsed.value[group.state] === undefined) {
      collapsed.value[group.state] = false
    }
  })
})

onMounted(() => {
  void dash.load()
  void catalog.load()
})
</script>

<template>
  <div class="stack">
    <header class="head">
      <div>
        <h1>待办</h1>
        <p class="muted">
          按截止时间排好，逾期在最前。
          <template v-if="total">共 {{ total }} 项。</template>
        </p>
      </div>
      <RouterLink class="btn btn--ghost btn--sm" to="/">回到总览</RouterLink>
    </header>

    <div v-if="platforms.length > 1" class="chips" role="group" aria-label="按平台筛选">
      <button
        type="button"
        class="chipbtn"
        :aria-pressed="!dash.platformFilter"
        @click="dash.platformFilter = ''"
      >
        全部平台
        <span class="chipbtn__num">{{ dash.timeline.length }}</span>
      </button>
      <button
        v-for="item in platforms"
        :key="item.id"
        type="button"
        class="chipbtn"
        :aria-pressed="dash.platformFilter === item.id"
        @click="dash.platformFilter = dash.platformFilter === item.id ? '' : item.id"
      >
        <i class="dot" :style="{ '--dot': catalog.color(item.id) }" aria-hidden="true" />
        {{ item.name }}
        <span class="chipbtn__num">{{ item.count }}</span>
      </button>
    </div>

    <EmptyState
      v-if="!total"
      icon="◷"
      title="这个范围里没有待办"
      body="可能还没抓过数据，或者这个平台确实没有未完成的任务点与作业。"
    >
      <RouterLink class="btn btn--primary" to="/">回到总览抓取一次</RouterLink>
    </EmptyState>

    <section
      v-for="group in groups"
      v-else
      :key="group.state"
      class="card card--flush"
      :class="`group group--${group.tone}`"
      :aria-labelledby="`group-${group.state}`"
    >
      <header class="group__head">
        <button
          type="button"
          class="group__toggle"
          :aria-expanded="isOpen(group.state)"
          :aria-controls="`group-body-${group.state}`"
          @click="toggle(group.state)"
        >
          <span class="group__caret" aria-hidden="true">{{ isOpen(group.state) ? '▾' : '▸' }}</span>
          <span :id="`group-${group.state}`" class="group__title">{{ group.label }}</span>
          <span class="pill" :class="`pill--${group.tone}`">{{ group.items.length }}</span>
        </button>
      </header>

      <ul v-show="isOpen(group.state)" :id="`group-body-${group.state}`" class="group__list">
        <AssignmentItem v-for="item in group.items" :key="item.uid" :item="item" />
      </ul>
    </section>
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

.chips {
  display: flex;
  gap: var(--s-2);
  flex-wrap: wrap;
  padding: var(--s-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface);
}

.chipbtn {
  display: inline-flex;
  gap: var(--s-2);
  align-items: center;
  padding: 0.2rem var(--s-3);
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  background: var(--surface-2);
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease);
}

.chipbtn:hover {
  border-color: var(--border-strong);
  color: var(--text);
}

.chipbtn[aria-pressed='true'] {
  border-color: var(--accent-border);
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}

.chipbtn__num {
  padding: 0 0.32rem;
  border-radius: var(--r-full);
  background: var(--surface-3);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.chipbtn[aria-pressed='true'] .chipbtn__num {
  background: var(--surface);
  color: var(--accent);
}

.group {
  overflow: hidden;
}

.group__head {
  border-bottom: 1px solid var(--border-subtle);
}

.group--danger .group__head {
  border-bottom-color: var(--danger-border);
  background: var(--danger-soft);
}

.group--warn .group__head {
  border-bottom-color: var(--warn-border);
  background: var(--warn-soft);
}

.group__toggle {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  width: 100%;
  padding: var(--s-3) var(--s-5);
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.group__toggle:hover {
  background: var(--surface-2);
}

.group__caret {
  width: 0.75rem;
  color: var(--text-faint);
}

.group__title {
  font-size: var(--fs-lg);
  font-weight: 650;
}

.group__list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.group__list > :deep(li + li) {
  border-top: 1px solid var(--border-subtle);
}
</style>
