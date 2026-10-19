<script setup lang="ts">
/**
 * 课程列表。搜索、平台筛选、排序三件事都放在这一页，因为它们互相影响——
 * 分开放在不同页会让用户来回跳。
 *
 * 路由 ``/courses/:platform/:id`` 复用这个组件，进去只显示那一门课。
 */
import { computed, onMounted, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import CourseCard from '@/components/CourseCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useCatalogStore } from '@/stores/catalog'
import { SORT_LABEL, useDashboardStore, type SortKey } from '@/stores/dashboard'

const dash = useDashboardStore()
const catalog = useCatalogStore()
const route = useRoute()
const router = useRouter()

const SORTS = Object.keys(SORT_LABEL) as SortKey[]

const detailPlatform = computed(() => (route.params.platform as string) || '')
const detailId = computed(() => (route.params.id as string) || '')
const inDetail = computed(() => Boolean(detailPlatform.value && detailId.value))

const detailCourse = computed(() =>
  inDetail.value ? dash.courseById(detailPlatform.value, detailId.value) : undefined,
)

const list = computed(() => {
  if (inDetail.value) {
    const course = detailCourse.value
    return course ? [course] : []
  }
  return dash.visibleCourses
})

const filtering = computed(() => Boolean(dash.query.trim()) || Boolean(dash.coursePlatformFilter))

const emptyTitle = computed(() => {
  if (inDetail.value) return '找不到这门课'
  if (filtering.value) return '没有符合条件的课程'
  return '还没有课程数据'
})

const emptyBody = computed(() => {
  if (inDetail.value) return '它可能已经被移除，或者这次抓取没取到。'
  if (filtering.value) return '换个关键词，或者清掉筛选条件。'
  return '先绑定账号并抓取一次，或者看看演示数据。'
})

function closeDetail(): void {
  void router.push('/courses')
}

watch([detailPlatform, detailId], () => {
  if (inDetail.value && !detailCourse.value) void dash.load()
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
        <h1>{{ inDetail ? '课程详情' : '课程' }}</h1>
        <p class="muted">
          <template v-if="inDetail">
            <RouterLink to="/courses">全部课程</RouterLink> / {{ detailCourse?.name || '未知课程' }}
          </template>
          <template v-else>
            共 {{ dash.courses.length }} 门；当前显示 {{ list.length }} 门。
          </template>
        </p>
      </div>
      <button v-if="inDetail" type="button" class="btn btn--ghost btn--sm" @click="closeDetail">
        返回列表
      </button>
    </header>

    <div v-if="!inDetail" class="toolbar">
      <div class="search">
        <span class="search__icon" aria-hidden="true">⌕</span>
        <input
          v-model="dash.query"
          class="input search__input"
          type="search"
          placeholder="搜索课程 / 教师 / 作业标题…"
          aria-label="搜索课程"
        />
      </div>

      <label class="field toolbar__sort">
        <span class="sr-only">排序方式</span>
        <select v-model="dash.sortKey" class="select" aria-label="排序方式">
          <option v-for="key in SORTS" :key="key" :value="key">{{ SORT_LABEL[key] }}</option>
        </select>
      </label>

      <button v-if="filtering" type="button" class="btn btn--ghost btn--sm" @click="dash.clearFilters()">
        清除筛选
      </button>

      <span class="spacer" />

      <div class="chips" role="group" aria-label="按平台筛选">
        <button
          type="button"
          class="chipbtn"
          :aria-pressed="!dash.coursePlatformFilter"
          @click="dash.coursePlatformFilter = ''"
        >
          全部平台
          <span class="chipbtn__num">{{ dash.courses.length }}</span>
        </button>
        <button
          v-for="item in dash.availablePlatforms"
          :key="item.id"
          type="button"
          class="chipbtn"
          :aria-pressed="dash.coursePlatformFilter === item.id"
          @click="dash.coursePlatformFilter = dash.coursePlatformFilter === item.id ? '' : item.id"
        >
          <i class="dot" :style="{ '--dot': catalog.color(item.id) }" aria-hidden="true" />
          {{ item.name }}
          <span class="chipbtn__num">{{ item.count }}</span>
        </button>
      </div>
    </div>

    <EmptyState v-if="!list.length" icon="▤" :title="emptyTitle" :body="emptyBody">
      <RouterLink v-if="!filtering && !inDetail" class="btn btn--primary" to="/accounts">
        绑定账号
      </RouterLink>
      <button v-else-if="filtering" type="button" class="btn" @click="dash.clearFilters()">
        清除筛选
      </button>
      <button v-else type="button" class="btn" @click="closeDetail">返回列表</button>
    </EmptyState>

    <div v-else class="grid">
      <CourseCard v-for="course in list" :key="course.platform + ':' + course.id" :course="course" />
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

.toolbar {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  flex-wrap: wrap;
  padding: var(--s-3);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface);
}

.search {
  position: relative;
  flex: 1 1 16rem;
  min-width: 12rem;
}

.search__icon {
  position: absolute;
  left: var(--s-3);
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-faint);
  pointer-events: none;
}

.search__input {
  padding-left: 2.1rem;
}

.toolbar__sort {
  flex: none;
  min-width: 11rem;
}

.chips {
  display: flex;
  gap: var(--s-2);
  flex-wrap: wrap;
}

.chipbtn {
  display: inline-flex;
  gap: var(--s-2);
  align-items: center;
  padding: 0.15rem var(--s-3);
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  background: var(--surface-2);
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
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
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.grid {
  display: grid;
  gap: var(--s-4);
  grid-template-columns: repeat(auto-fit, minmax(23rem, 1fr));
  align-items: start;
}

@media (max-width: 640px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
