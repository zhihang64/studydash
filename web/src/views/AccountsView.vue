<script setup lang="ts">
/**
 * 账号管理：登录 + 已绑定账号的日常操作。
 *
 * 删除走「点一下变成确认」的两步，而不是浏览器原生 confirm()：原生弹窗会阻塞
 * 整个页面、在深色主题下还是一块白，而且它问不出「删掉的是哪个账号」。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import EmptyState from '@/components/EmptyState.vue'
import LoginPanel from '@/components/LoginPanel.vue'
import NoteBanner from '@/components/NoteBanner.vue'
import PlatformChip from '@/components/PlatformChip.vue'
import { toast } from '@/composables/useToast'
import { useAccountsStore } from '@/stores/accounts'
import { useCatalogStore } from '@/stores/catalog'
import { useDashboardStore } from '@/stores/dashboard'
import { useTasksStore } from '@/stores/tasks'
import { formatAgo, formatDateTime } from '@/utils/format'

const accounts = useAccountsStore()
const catalog = useCatalogStore()
const tasks = useTasksStore()
const dash = useDashboardStore()

const pendingDelete = ref('')
const addingPlatform = ref('')
const addingLabel = ref('')

const loginPanel = ref<InstanceType<typeof LoginPanel> | null>(null)

const fetching = computed(() => tasks.active.some((t) => t.kind === 'fetch'))

const addablePlatforms = computed(() => catalog.platforms.filter((p) => p.id !== 'demo'))

const showAddForm = ref(false)

function rowKey(platform: string, label: string): string {
  return `${platform}\u0000${label}`
}

async function toggleEnabled(platform: string, label: string, enabled: boolean): Promise<void> {
  try {
    await accounts.patch(platform, label, { enabled })
    toast.ok(enabled ? '已启用' : '已停用', `${catalog.fullName(platform)} · ${label}`)
  } catch (err) {
    toast.error('改不动这个账号', err instanceof Error ? err.message : String(err))
  }
}

async function confirmDelete(platform: string, label: string): Promise<void> {
  try {
    await accounts.remove(platform, label)
    pendingDelete.value = ''
    toast.ok('已删除账号', `${catalog.fullName(platform)} · ${label}`)
  } catch (err) {
    toast.error('删除失败', err instanceof Error ? err.message : String(err))
  }
}

async function addManual(): Promise<void> {
  if (!addingPlatform.value) return
  try {
    await accounts.add(addingPlatform.value, addingLabel.value.trim())
    showAddForm.value = false
    addingLabel.value = ''
    toast.ok(
      '已添加一个空账号',
      '它还没有会话。用上面的登录面板绑上，或者之后手动填 Cookie。',
    )
  } catch (err) {
    toast.error('添加失败', err instanceof Error ? err.message : String(err))
  }
}

async function fetchAll(): Promise<void> {
  if (!accounts.enabled.length) {
    toast.warn('没有启用的账号', '先启用或新建一个账号。')
    return
  }
  await tasks.start('fetch', {})
}

async function fetchOne(platform: string): Promise<void> {
  await tasks.start('fetch', { only: [platform] })
}

async function probe(platform: string): Promise<void> {
  await tasks.start('probe', { platform })
}

async function refresh(): Promise<void> {
  await Promise.all([accounts.load(), tasks.hydrate()])
}

onMounted(() => {
  void refresh()
  void catalog.load()
})
</script>

<template>
  <div class="stack">
    <header class="head">
      <div>
        <h1>账号</h1>
        <p class="muted">
          登录只为把学习数据读出来。已绑定 {{ accounts.items.length }} 个，启用
          {{ accounts.enabled.length }} 个。
        </p>
      </div>
      <div class="row">
        <RouterLink class="btn btn--ghost btn--sm" to="/">回到总览</RouterLink>
        <button type="button" class="btn btn--primary" :disabled="fetching || !accounts.enabled.length" @click="fetchAll">
          {{ fetching ? '正在抓取…' : '抓取全部账号' }}
        </button>
      </div>
    </header>

    <NoteBanner
      v-if="dash.isDemo"
      tone="warn"
      title="当前看板显示的是演示数据"
      body="演示数据来自内置样例，和你的账号无关。绑好账号后点一次「抓取全部账号」就会换成真实数据。"
    />

    <LoginPanel ref="loginPanel" />

    <!-- ------------------------------ 已绑定 ------------------------------ -->

    <section class="card card--flush" aria-labelledby="bound-title">
      <header class="card__head bound__head">
        <h2 id="bound-title" class="card__title">
          已绑定
          <span class="card__hint">停用后抓取会跳过它，但配置留着</span>
        </h2>
        <button type="button" class="btn btn--ghost btn--sm" @click="showAddForm = !showAddForm">
          {{ showAddForm ? '收起' : '手动新建空账号' }}
        </button>
      </header>

      <div v-if="showAddForm" class="addform">
        <div class="field">
          <label class="label" for="add-platform">平台</label>
          <select id="add-platform" v-model="addingPlatform" class="select">
            <option value="">请选择…</option>
            <option v-for="item in addablePlatforms" :key="item.id" :value="item.id">
              {{ item.name }}
            </option>
          </select>
        </div>
        <div class="field">
          <label class="label" for="add-label">备注</label>
          <input id="add-label" v-model="addingLabel" class="input" type="text" maxlength="24" placeholder="可选" />
        </div>
        <button type="button" class="btn" :disabled="!addingPlatform" @click="addManual">添加</button>
      </div>

      <EmptyState
        v-if="!accounts.items.length && !accounts.loading"
        compact
        icon="⚿"
        title="还没有绑定任何账号"
        body="用上面的扫码登录，一分钟就能搞定。"
      />

      <div v-else class="tablewrap">
        <table class="table">
          <caption class="sr-only">已绑定的账号列表</caption>
          <thead>
            <tr>
              <th scope="col">平台</th>
              <th scope="col">备注</th>
              <th scope="col">会话</th>
              <th scope="col">状态</th>
              <th scope="col">启用</th>
              <th scope="col"><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="account in accounts.items" :key="rowKey(account.platform, account.label)">
              <td><PlatformChip :platform="account.platform" /></td>
              <td>
                <span class="label-name">{{ account.label || '默认' }}</span>
              </td>
              <td>
                <span class="mono cookie-hint">{{ account.cookie_hint || '—' }}</span>
              </td>
              <td>
                <span v-if="!account.ok" class="pill pill--danger">取数失败</span>
                <span v-else-if="account.fetched_at" class="pill pill--ok">正常</span>
                <span v-else class="pill pill--neutral">未抓取</span>
                <p v-if="account.message" class="status-msg faint">{{ account.message }}</p>
                <p v-if="account.fetched_at" class="status-msg faint">
                  上次 {{ formatAgo(account.fetched_at) }}
                  <span class="mono">·</span>
                  {{ account.course_count }} 门课
                </p>
              </td>
              <td>
                <label class="switch">
                  <input
                    type="checkbox"
                    :checked="account.enabled"
                    :aria-label="`${account.enabled ? '停用' : '启用'} ${account.label || '默认'}`"
                    @change="toggleEnabled(account.platform, account.label || '默认', ($event.target as HTMLInputElement).checked)"
                  />
                  <span class="switch__text">{{ account.enabled ? '已启用' : '已停用' }}</span>
                </label>
              </td>
              <td class="actions">
                <template v-if="pendingDelete === rowKey(account.platform, account.label)">
                  <span class="confirm">确定删除？</span>
                  <button
                    type="button"
                    class="btn btn--danger btn--sm"
                    @click="confirmDelete(account.platform, account.label || '默认')"
                  >
                    删除
                  </button>
                  <button type="button" class="btn btn--ghost btn--sm" @click="pendingDelete = ''">取消</button>
                </template>
                <template v-else>
                  <button type="button" class="btn btn--sm" :disabled="fetching" @click="fetchOne(account.platform)">
                    抓这个平台
                  </button>
                  <button type="button" class="btn btn--ghost btn--sm" @click="probe(account.platform)">
                    探测接口
                  </button>
                  <button
                    type="button"
                    class="btn btn--ghost btn--sm"
                    @click="pendingDelete = rowKey(account.platform, account.label)"
                  >
                    删除
                  </button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ------------------------------ 平台名录 ------------------------------ -->

    <section class="card" aria-labelledby="catalog-title">
      <header class="card__head">
        <h2 id="catalog-title" class="card__title">
          支持的平台
          <span class="card__hint">标了「未验证」的适配器还没用真实账号跑透过</span>
        </h2>
      </header>

      <div class="catalog">
        <article v-for="item in catalog.platforms" :key="item.id" class="src">
          <div class="src__head">
            <PlatformChip :platform="item.id" :name="item.name" :color="item.color" />
            <span v-if="item.status === 'beta'" class="pill pill--warn">beta</span>
            <span v-else-if="item.status === 'unverified'" class="pill pill--warn">未验证</span>
            <span v-else-if="item.status === 'stable'" class="pill pill--ok">已验证</span>
          </div>
          <p v-if="item.note" class="src__note muted">{{ item.note }}</p>
          <p class="src__meta faint">
            <span v-if="item.auth === 'none'">不需要登录</span>
            <span v-else>需要登录</span>
            <span v-if="item.homepage" class="mono src__home">{{ item.homepage }}</span>
          </p>
        </article>
      </div>

      <p class="faint catalog__stamp" v-if="dash.generatedAt">
        看板数据刷新于 {{ formatDateTime(dash.generatedAt) }}
      </p>
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

.bound__head {
  padding: var(--s-4) var(--s-5) 0;
  margin-bottom: var(--s-2);
}

.addform {
  display: flex;
  gap: var(--s-3);
  align-items: flex-end;
  flex-wrap: wrap;
  padding: 0 var(--s-5) var(--s-4);
}

.addform .field {
  min-width: 12rem;
  flex: 1 1 12rem;
}

.tablewrap {
  overflow-x: auto;
}

.label-name {
  font-weight: 600;
}

.cookie-hint {
  color: var(--text-muted);
}

.status-msg {
  margin-top: var(--s-1);
  font-size: var(--fs-xs);
  line-height: 1.55;
  max-width: 20rem;
}

.switch {
  display: inline-flex;
  gap: var(--s-2);
  align-items: center;
  cursor: pointer;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}

.switch__text {
  white-space: nowrap;
}

.actions {
  white-space: nowrap;
}

.actions > * + * {
  margin-left: var(--s-2);
}

.confirm {
  color: var(--danger);
  font-size: var(--fs-sm);
  font-weight: 600;
  margin-right: var(--s-2);
}

.catalog {
  display: grid;
  gap: var(--s-3);
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
}

.src {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  padding: var(--s-3) var(--s-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-2);
}

.src__head {
  display: flex;
  gap: var(--s-2);
  align-items: center;
  flex-wrap: wrap;
}

.src__note {
  font-size: var(--fs-sm);
  line-height: 1.7;
}

.src__meta {
  display: flex;
  gap: var(--s-3);
  flex-wrap: wrap;
  font-size: var(--fs-xs);
}

.src__home {
  overflow-wrap: anywhere;
}

.catalog__stamp {
  margin-top: var(--s-4);
  font-size: var(--fs-xs);
}
</style>
