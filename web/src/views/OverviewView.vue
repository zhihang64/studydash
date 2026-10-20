<script setup lang="ts">
/**
 * 总览：一眼看清进度与风险。
 *
 * 这一页的排序是按「该不该现在动手」来的，不是按数据字段顺序：
 * 先给状态（有没有数据 / 抓到哪门），再给风险（逾期、即将到期），最后给分布。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import type { AssignmentView } from '@/api/types'
import AssignmentItem from '@/components/AssignmentItem.vue'
import EmptyState from '@/components/EmptyState.vue'
import KpiCard from '@/components/KpiCard.vue'
import NoteBanner from '@/components/NoteBanner.vue'
import PlatformChip from '@/components/PlatformChip.vue'
import ProgressBar from '@/components/ProgressBar.vue'
import { toast } from '@/composables/useToast'
import { useAccountsStore } from '@/stores/accounts'
import { useCatalogStore } from '@/stores/catalog'
import { useDashboardStore } from '@/stores/dashboard'
import { useTasksStore } from '@/stores/tasks'
import { cssColor, formatAgo, formatDateTime, percentText } from '@/utils/format'

const dash = useDashboardStore()
const tasks = useTasksStore()
const accounts = useAccountsStore()
const catalog = useCatalogStore()

const dismissedWarnings = ref<string[]>([])

const warnings = computed(() =>
  dash.warnings.filter((w) => !dismissedWarnings.value.includes(w)),
)

const overview = computed(() => dash.overview)

const avgText = computed(() => {
  const value = overview.value?.avg_progress
  if (dash.measuredCourses.length === 0 && (overview.value?.has_progress ?? true) === false) {
    return { text: '—', hint: '数据源没有给出任何进度信息' }
  }
  if (!value) return { text: '—', hint: '还没有可以计算进度的课程' }
  return { text: percentText(value, 1, '—'), hint: '' }
})

/** 「最要紧的几件」：后端已经排好序，这里只截前几条。 */
const urgent = computed<AssignmentView[]>(() =>
  dash.timeline.filter((a) => a.state === 'overdue' || a.state === 'urgent').slice(0, 6),
)

/**
 * 「进度告急」：有数据、且还没学完的课，按进度从低到高取前 4 门。
 *
 * 两个都不能省：
 *  - **必须真排序**。之前直接 `filter(has_data).slice(0,4)`，标题写着「进度最低的
 *    几门」，实际给的是原始顺序，0% 后面跟着一串 100%；
 *  - **必须排除已学完的**。100% 的课挂在「告急」里只会让人以为自己漏看了什么。
 */
const behind = computed(() =>
  dash.visibleCourses
    .filter((c) => c.has_data && (c.ratio ?? 0) < 1)
    .sort((a, b) => (a.ratio ?? 0) - (b.ratio ?? 0))
    .slice(0, 4),
)

const fetching = computed(() =>
  tasks.active.some((t) => t.kind === 'fetch'),
)

const canFetch = computed(() => accounts.enabled.length > 0)

const modeLabel = computed(() => {
  if (dash.mode === 'demo') return '演示数据'
  if (dash.mode === 'import') return '导入数据'
  if (dash.mode === 'live') return '实时抓取'
  return dash.mode || ''
})

const generatedText = computed(() => {
  if (!dash.generatedAt) return ''
  return `${formatDateTime(dash.generatedAt)}（${formatAgo(dash.generatedAt)}）`
})

async function fetchNow(): Promise<void> {
  if (!canFetch.value) {
    toast.warn('还没有可用的账号', '先去「账号」页扫码登录一个，再回来抓取。')
    return
  }
  await tasks.start('fetch', {})
}

onMounted(() => {
  void dash.load()
  void accounts.load()
  void catalog.load()
})

defineExpose({ fetchNow })
</script>

<template>
  <div class="stack">
    <!-- 页头 -->
    <header class="hero">
      <div class="hero__text">
        <h1>总览</h1>
        <p class="muted">
          <template v-if="generatedText">数据刷新于 {{ generatedText }}</template>
          <template v-else>还没有数据。绑定账号后抓取一次。</template>
        </p>
      </div>

      <div class="hero__actions">
        <span v-if="modeLabel" class="pill" :class="dash.isDemo ? 'pill--warn' : 'pill--neutral'">
          {{ modeLabel }}
        </span>
        <RouterLink v-if="!canFetch" class="btn" to="/accounts">先去绑定账号</RouterLink>
        <button
          type="button"
          class="btn btn--primary"
          :disabled="fetching || !canFetch"
          @click="fetchNow"
        >
          {{ fetching ? '正在抓取…' : '抓取数据' }}
        </button>
      </div>
    </header>

    <!-- 警告 -->
    <NoteBanner
      v-for="warning in warnings"
      :key="warning"
      tone="warn"
      :title="warning"
      dismissible
      @dismiss="dismissedWarnings.push(warning)"
    />

    <NoteBanner
      v-if="accounts.broken.length"
      tone="danger"
      :title="`${accounts.broken.length} 个账号这次没取到数据`"
      :body="accounts.broken.map((a) => `${catalog.fullName(a.platform)}：${a.message || '没有说明'}`).join('；')"
      fix="会话可能过期了，去账号页重新登录。"
    />

    <!-- 空状态 -->
    <EmptyState
      v-if="!dash.hasData && !dash.loading"
      icon="◎"
      title="还没有采集到任何课程"
      body="先绑定你的学习账号，再点右上角的「抓取数据」。"
    >
      <RouterLink class="btn btn--primary" to="/accounts">绑定账号</RouterLink>
    </EmptyState>

    <template v-else>
      <!-- KPI -->
      <section class="kpis" aria-label="总体指标">
        <KpiCard label="课程数" :value="overview?.courses ?? 0" unit="门" tone="accent"
          :hint="`覆盖 ${overview?.platforms ?? 0} 个平台`" />
        <KpiCard label="涉及平台" :value="overview?.platforms ?? 0" unit="个" tone="info"
          :hint="`${dash.platformCards.length} 个数据源有内容`" />
        <KpiCard label="平均进度" :value="avgText.text" tone="ok" :hint="avgText.hint"
          :badge="overview?.done ? `已完成 ${overview.done} 项` : ''" />
        <KpiCard label="待办" :value="overview?.pending ?? 0" unit="项" tone="neutral"
          :hint="`共 ${dash.timeline.length} 项任务点与作业`" />
        <KpiCard label="已逾期" :value="overview?.overdue ?? 0" unit="项"
          :tone="(overview?.overdue ?? 0) > 0 ? 'danger' : 'neutral'"
          :hint="(overview?.overdue ?? 0) > 0 ? '请尽快到课程页面确认能否补交' : '没有逾期项'"
          :badge="overview?.due_soon ? `${overview.due_soon} 项在 ${overview.alert_days ?? 3} 天内到期` : ''" />
        <KpiCard label="已完成" :value="overview?.done ?? 0" unit="项" tone="ok"
          :hint="`已提交或已评分`" />
      </section>

      <!-- 平台分布 -->
      <section class="card" aria-labelledby="platforms-title">
        <header class="card__head">
          <h2 id="platforms-title" class="card__title">
            平台分布
            <span class="card__hint">点一个看它的待办</span>
          </h2>
          <RouterLink class="btn btn--ghost btn--sm" to="/timeline">看待办时间线</RouterLink>
        </header>

        <div v-if="!dash.platformCards.length" class="faint">没有平台数据。</div>

        <div v-else class="plats">
          <button
            v-for="card in dash.platformCards"
            :key="card.id"
            type="button"
            class="plat"
            :style="{ '--plat': cssColor(card.color) }"
            :aria-pressed="dash.platformFilter === card.id"
            @click="dash.platformFilter = dash.platformFilter === card.id ? '' : card.id"
          >
            <span class="plat__top">
              <span class="dot" aria-hidden="true" />
              <span class="plat__name truncate">{{ card.name }}</span>
              <span v-if="card.status === 'beta'" class="pill pill--warn">beta</span>
              <span v-else-if="card.status === 'unverified'" class="pill pill--warn">未验证</span>
            </span>

            <span class="plat__stats">
              <span><strong>{{ card.courses }}</strong> 门课</span>
              <span aria-hidden="true">·</span>
              <span>
                <strong>{{ card.has_data === false ? '无数据' : percentText(card.avg_progress) }}</strong> 平均
              </span>
            </span>

            <span class="plat__stats plat__stats--sub">
              <span v-if="card.overdue" class="plat__bad">{{ card.overdue }} 逾期</span>
              <span v-else-if="card.pending">{{ card.pending }} 待办</span>
              <span v-else-if="card.ok" class="plat__good">没有待办</span>
              <span v-else class="plat__bad">取数失败</span>
            </span>

            <span v-if="card.message" class="plat__msg" :title="card.message">{{ card.message }}</span>
          </button>
        </div>
      </section>

      <!-- 最要紧的几件 -->
      <section class="card" aria-labelledby="urgent-title">
        <header class="card__head">
          <h2 id="urgent-title" class="card__title">
            最要紧的几件
            <span class="card__hint">逾期与 {{ overview?.alert_days ?? 3 }} 天内到期</span>
          </h2>
          <RouterLink class="btn btn--ghost btn--sm" to="/timeline">全部待办</RouterLink>
        </header>

        <EmptyState
          v-if="!urgent.length"
          compact
          icon="✓"
          title="没有逾期，也没有快到期的东西"
          body="这段时间是安全的。有新作业的话，抓取一次就会出现在这里。"
        />

        <ul v-else class="urgent">
          <AssignmentItem v-for="item in urgent" :key="item.uid" :item="item" />
        </ul>
      </section>

      <!-- 进度最低的几门 -->
      <section v-if="behind.length" class="card" aria-labelledby="behind-title">
        <header class="card__head">
          <h2 id="behind-title" class="card__title">
            进度告急
            <span class="card__hint">有数据、进度最低的几门</span>
          </h2>
          <RouterLink class="btn btn--ghost btn--sm" to="/courses">全部课程</RouterLink>
        </header>

        <div class="behind">
          <article v-for="course in behind" :key="course.platform + course.id" class="behind__item">
            <div class="behind__head">
              <PlatformChip :platform="course.platform" :name="course.platform_name" size="sm" />
              <span class="behind__name truncate" :title="course.name">{{ course.name }}</span>
              <span class="behind__pct mono">{{ percentText(course.ratio) }}</span>
            </div>
            <ProgressBar :ratio="course.ratio" size="sm" />
            <p class="behind__meta faint">
              <span v-if="course.overdue">{{ course.overdue }} 项逾期</span>
              <span v-else-if="course.pending">{{ course.pending }} 项待办</span>
              <span v-else>没有待办</span>
            </p>
          </article>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.hero {
  display: flex;
  gap: var(--s-4);
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
}

.hero__actions {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  flex-wrap: wrap;
}

.kpis {
  display: grid;
  gap: var(--s-3);
  grid-template-columns: repeat(auto-fit, minmax(10.5rem, 1fr));
}

.plats {
  display: grid;
  gap: var(--s-3);
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
}

.plat {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  padding: var(--s-3) var(--s-4);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease);
}

.plat:hover {
  border-color: var(--border-strong);
  background: var(--surface);
}

.plat[aria-pressed='true'] {
  border-color: var(--plat);
  background: var(--surface);
  box-shadow: inset 3px 0 0 var(--plat);
}

.plat__top {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  min-width: 0;
}

.plat__top .dot {
  background: var(--plat);
}

.plat__name {
  min-width: 0;
  font-weight: 600;
}

.plat__stats {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.plat__stats strong {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.plat__stats--sub {
  font-size: var(--fs-xs);
}

.plat__bad {
  color: var(--danger);
  font-weight: 600;
}

.plat__good {
  color: var(--ok);
  font-weight: 600;
}

.plat__msg {
  color: var(--text-faint);
  font-size: var(--fs-xs);
  line-height: 1.55;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}

.urgent {
  margin: 0;
  padding: 0;
  list-style: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  overflow: hidden;
}

.urgent > :deep(li + li) {
  border-top: 1px solid var(--border-subtle);
}

.behind {
  display: grid;
  gap: var(--s-3);
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
}

.behind__item {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  padding: var(--s-3) var(--s-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-2);
}

.behind__head {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  min-width: 0;
}

.behind__name {
  min-width: 0;
  flex: 1 1 auto;
  font-weight: 550;
  font-size: var(--fs-sm);
}

.behind__pct {
  flex: none;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.behind__meta {
  font-size: var(--fs-xs);
}
</style>
