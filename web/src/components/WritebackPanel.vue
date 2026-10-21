<script setup lang="ts">
/**
 * 任务点上报面板。
 *
 * 这是整个网页端**唯一会写平台数据**的地方，所以它跟别的面板长得不一样：
 *  - 默认是演练（``apply = false``），一个字节都不发出去，只把要发的东西算一遍。
 *  - 勾上「真的上报」还要再点一次确认，不是一次点击就写信。
 *  - 「逐分钟」会真的等，一分钟一次，所以必须能取消。
 *
 * 覆盖六类任务点：音视频（签名上报播放位置）、阅读/文档/图书（一次性 GET
 * 任务点接口）、直播（saveTimePc）。「范围」下拉只决定跑哪几类。
 *
 * 判定标准、签名算法、地址白名单全部在后端 ``writeback.py`` 里，
 * 这里只负责收集参数和显示结果，不做任何自己的判断。
 */
import { computed, ref, watch } from 'vue'

import { toast } from '@/composables/useToast'
import { useAccountsStore } from '@/stores/accounts'
import { useTasksStore } from '@/stores/tasks'

import NoteBanner from './NoteBanner.vue'
import ProgressBar from './ProgressBar.vue'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const tasks = useTasksStore()
const accounts = useAccountsStore()

/**
 * 只补哪几类任务点。空数组 = 全部。
 *
 * 后端 `_as_kinds` 把空值当成「全都要」，所以这里不用特意判断。
 */
const KIND_SCOPES: Record<string, string[]> = {
  all: [],
  media: ['video', 'audio'],
  text: ['read', 'document'],
  book: ['book'],
  live: ['live'],
}
const kindScope = ref<keyof typeof KIND_SCOPES>('all')

/** 真的上报。默认关——演练才是默认动作。 */
const apply = ref(false)
/** 逐分钟上报。关掉就是一口气补齐。 */
const paced = ref(false)
/** 递进上报：30% → 70% → 100% 分三步发。实测通过率远高于一次跳满，默认开。 */
const ramp = ref(true)
/** 0 = 不限。 */
const limit = ref(0)
/** 逐分钟模式下的间隔秒数，后端默认 59.8。 */
const interval = ref(59.8)
/** 课程名模糊过滤，空 = 全部。 */
const course = ref('')
/** 并发线程数。1 = 一条一条来（老行为）。 */
const workers = ref(1)

/** 点了「真的上报」之后要先过这一关。 */
const confirming = ref(false)

const taskId = ref('')

const canRun = computed(() => accounts.has('chaoxing'))

const tracked = computed(() => (taskId.value ? tasks.find(taskId.value) : undefined))
const task = computed(() => tracked.value?.task ?? null)
const logs = computed(() => tracked.value?.logs ?? [])

const running = computed(() => {
  const t = task.value
  if (!t) return false
  return t.state === 'queued' || t.state === 'running'
})

const finished = computed(() => {
  const t = task.value
  return !!t && (t.state === 'done' || t.state === 'failed' || t.state === 'cancelled')
})

interface Counts {
  total?: number
  done?: number
  failed?: number
  skipped?: number
  requests?: number
}

const counts = computed<Counts>(() => {
  const result = task.value?.result
  if (!result) return {}
  const raw = result.counts
  return raw && typeof raw === 'object' ? (raw as Counts) : {}
})

const applied = computed(() => task.value?.result?.applied === true)

interface TargetRow {
  label?: string
  ok?: boolean | null
  reason?: string
  requests?: number
}

const targets = computed<TargetRow[]>(() => {
  const result = task.value?.result
  const raw = result?.targets
  return Array.isArray(raw) ? (raw as TargetRow[]).slice(0, 40) : []
})

/** 默认值跟着「逐分钟」走：不逐分钟时把间隔藏起来，免得让人以为它有用。 */
const showInterval = computed(() => paced.value)

function start(): void {
  if (!canRun.value) {
    toast.warn('还没有学习通账号', '先去「账号」页绑一个，再回来上报。')
    return
  }
  if (apply.value && !confirming.value) {
    // 第一次点只是把确认按钮亮出来，不立刻发请求。
    confirming.value = true
    return
  }
  confirming.value = false
  void submit()
}

async function submit(): Promise<void> {
  const created = await tasks.start('writeback', {
    platform: 'chaoxing',
    apply: apply.value,
    paced: paced.value,
    ramp: paced.value ? false : ramp.value,
    limit: limit.value > 0 ? limit.value : 0,
    interval: interval.value,
    course: course.value.trim(),
    kinds: [...(KIND_SCOPES[kindScope.value] ?? [])],
    workers: paced.value ? 1 : Math.max(1, Math.min(8, Number(workers.value) || 1)),
  })
  if (created) taskId.value = created.id
}

function cancel(): void {
  if (taskId.value) void tasks.cancel(taskId.value)
}

function close(): void {
  confirming.value = false
  emit('close')
}

/** 面板一关就忘掉上一轮结果，下次打开是干净的。 */
watch(
  () => confirming.value,
  (value) => {
    if (!value) return
    // 三秒内没确认就自己收回去，避免确认按钮一直亮着被误点。
    window.setTimeout(() => {
      confirming.value = false
    }, 8000)
  },
)
</script>

<template>
  <div v-if="open" class="wb" role="dialog" aria-modal="true" aria-labelledby="wb-title">
    <div class="wb__backdrop" @click="close" />

    <section class="wb__panel card">
      <header class="card__head">
        <div>
          <h2 id="wb-title" class="card__title">上报任务点</h2>
          <p class="card__hint">
            只有这一项会写平台数据。剩下的都还是只读。
          </p>
        </div>
        <button type="button" class="btn btn--ghost btn--sm" @click="close">关掉</button>
      </header>

      <NoteBanner
        v-if="!canRun"
        tone="warn"
        title="还没有学习通账号"
        body="先去「账号」页扫码登录一个，再回来。"
        fix="这一步没法跳过：没有会话就没法签出上报地址。"
      />

      <div v-else class="wb__body">
        <!-- ---------- 参数 ---------- -->
        <fieldset class="wb__group" :disabled="running">
          <legend class="label">怎么跑</legend>

          <label class="wb__check">
            <input v-model="paced" type="checkbox" />
            <span>
              逐分钟上报
              <em class="wb__em">一分钟推一次进度，像真的在放。慢，但更像正常观看。</em>
            </span>
          </label>

          <label class="wb__check">
            <input v-model="ramp" type="checkbox" :disabled="paced" />
            <span>
              递进上报
              <em class="wb__em">
                播放位置按 30% → 70% → 100% 分三步发，每步等 1.5 秒。一次性跳满实测大多不被平台认，分步才记通过。
              </em>
            </span>
          </label>

          <label v-if="showInterval" class="field">
            <span class="label">间隔秒数</span>
            <input v-model.number="interval" class="input" type="number" min="1" max="600" step="0.1" />
            <span class="help">留在 59.8 就好，动它只是为了迁就慢网络。</span>
          </label>

          <div class="wb__grid">
            <label class="field">
              <span class="label">范围</span>
              <select v-model="kindScope" class="input">
                <option value="all">全部任务点</option>
                <option value="media">只要音视频</option>
                <option value="text">只要阅读/文档</option>
                <option value="book">只要图书</option>
                <option value="live">只要直播</option>
              </select>
              <span class="help">
                音视频要按秒签名上报；阅读/文档/图书/直播是一次性提交任务点接口。
              </span>
            </label>

            <label class="field">
              <span class="label">最多处理几个任务点</span>
              <input v-model.number="limit" class="input" type="number" min="0" step="1" />
              <span class="help">0 = 不限。先填 1 试一轮最稳。</span>
            </label>

            <label class="field">
              <span class="label">只要课程名包含</span>
              <input v-model="course" class="input" type="text" placeholder="留空 = 全部课程" />
              <span class="help">按课程名模糊匹配，用来只补某一门。</span>
            </label>

            <label class="field">
              <span class="label">并发数</span>
              <input
                v-model.number="workers"
                class="input"
                type="number"
                min="1"
                max="8"
                step="1"
                :disabled="paced"
              />
              <span class="help">
                1 = 一条一条发。调到 4 能快几倍，但请求会同时打出去。
                <template v-if="paced">逐分钟模式下只能 1。</template>
              </span>
            </label>
          </div>
        </fieldset>

        <!-- ---------- 写不写 ---------- -->
        <div class="wb__write">
          <label class="wb__check wb__check--danger">
            <input v-model="apply" type="checkbox" :disabled="running" />
            <span>
              真的上报（关掉就是演练）
              <em class="wb__em">
                勾上之后会向学习通发出真实的观看记录请求。演练只把要发的地址算出来，不发。
              </em>
            </span>
          </label>
        </div>

        <p v-if="apply" class="wb__warn mono">
          目标：multimedia/log/a…（音视频）· ananas/job…（阅读/文档/图书）· zhibo saveTimePc（直播）
        </p>

        <!-- ---------- 动作 ---------- -->
        <div class="row wb__actions">
          <button
            type="button"
            class="btn"
            :class="apply && confirming ? 'btn--danger' : 'btn--primary'"
            :disabled="running"
            @click="start"
          >
            <template v-if="running">正在跑…</template>
            <template v-else-if="confirming">确认，真的发出去</template>
            <template v-else-if="apply">开始上报</template>
            <template v-else>演练一遍</template>
          </button>

          <button v-if="confirming" type="button" class="btn btn--ghost" @click="confirming = false">
            等等，我再看看
          </button>

          <button v-if="running" type="button" class="btn btn--danger" @click="cancel">取消</button>
        </div>

        <!-- ---------- 跑起来了 ---------- -->
        <div v-if="task" class="wb__run">
          <div class="wb__runhead">
            <span class="pill" :class="running ? 'pill--accent' : finished ? 'pill--ok' : 'pill--neutral'">
              {{ apply ? '上报' : '演练' }}{{ paced ? ' · 逐分钟' : '' }}{{ !paced && ramp ? ' · 递进' : '' }}{{ workers > 1 && !paced ? ` · ${workers} 并发` : '' }}
            </span>
            <span class="wb__msg">{{ task.message || '正在准备…' }}</span>
          </div>
          <ProgressBar v-if="running" :ratio="task.progress" size="sm" />

          <div v-if="finished && task.state === 'done'" class="wb__result">
            <p class="wb__sum">
              {{ applied ? '已上报' : '演练通过' }}
              <strong>{{ counts.done ?? 0 }}</strong> 个任务点
              <template v-if="counts.total">／共 {{ counts.total }} 个</template>
              <template v-if="counts.failed">· 失败 {{ counts.failed }} 个</template>
              <template v-if="counts.skipped">· 跳过 {{ counts.skipped }} 个</template>
            </p>
            <p class="muted">发出 {{ counts.requests ?? 0 }} 次请求。</p>

            <ul v-if="targets.length" class="wb__list">
              <li v-for="(row, index) in targets" :key="index" class="wb__item">
                <span
                  class="pill"
                  :class="row.ok === true ? 'pill--ok' : row.ok === false ? 'pill--danger' : 'pill--neutral'"
                >
                  {{ row.ok === true ? '成功' : row.ok === false ? '失败' : '跳过' }}
                </span>
                <span class="wb__itemlabel">{{ row.label || '（没有名字）' }}</span>
                <span v-if="row.reason" class="wb__itemreason muted">{{ row.reason }}</span>
              </li>
            </ul>
          </div>

          <NoteBanner
            v-else-if="finished && task.state === 'failed'"
            tone="danger"
            title="上报失败"
            :body="task.error?.message || task.message || '没有更多信息'"
            :fix="task.error?.hint || ''"
          />

          <details v-if="logs.length" class="wb__logs">
            <summary class="muted">看日志（{{ logs.length }} 行）</summary>
            <pre class="mono wb__pre">{{ logs.map((line) => line.text).join('\n') }}</pre>
          </details>
        </div>

        <p class="wb__foot muted">
          演练与上报共用同一套签名和白名单，演练能算出来，上报才发得出去。
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.wb {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--s-5);
}

.wb__backdrop {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.55);
}

.wb__panel {
  position: relative;
  width: min(38rem, 100%);
  max-height: min(82vh, 100%);
  overflow-y: auto;
  box-shadow: var(--shadow-4);
}

.wb__body {
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
}

.wb__group {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  border: 0;
  padding: 0;
  margin: 0;
}

.wb__grid {
  display: grid;
  gap: var(--s-3);
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
}

.wb__check {
  display: flex;
  align-items: flex-start;
  gap: var(--s-2);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.wb__check--danger {
  padding: var(--s-3);
  border: 1px solid var(--danger-border);
  border-radius: var(--r-md);
  background: var(--danger-soft);
}

.wb__em {
  display: block;
  margin-top: 2px;
  font-style: normal;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.wb__write {
  border-top: 1px solid var(--border);
  padding-top: var(--s-3);
}

.wb__warn {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--danger);
}

.wb__actions {
  flex-wrap: wrap;
  gap: var(--s-2);
}

.wb__run {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  border-top: 1px solid var(--border);
  padding-top: var(--s-3);
}

.wb__runhead {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  flex-wrap: wrap;
}

.wb__msg {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}

.wb__result {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
}

.wb__sum {
  margin: 0;
  font-size: var(--fs-sm);
}

.wb__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 15rem;
  overflow-y: auto;
}

.wb__item {
  display: flex;
  align-items: baseline;
  gap: var(--s-2);
  font-size: var(--fs-xs);
}

.wb__itemlabel {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wb__itemreason {
  flex: 0 1 auto;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wb__logs summary {
  cursor: pointer;
  font-size: var(--fs-xs);
}

.wb__pre {
  margin: var(--s-2) 0 0;
  max-height: 12rem;
  overflow: auto;
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  word-break: break-all;
}

.wb__foot {
  margin: 0;
  font-size: var(--fs-xs);
}
</style>
