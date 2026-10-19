<script setup lang="ts">
/**
 * 设置与环境自检。
 *
 * 表单是「本地草稿 + 显式保存」而不是改一下就发请求：抓取参数一次要改好几个，
 * 每个击键都发一次 PATCH 既没必要也会让「到底存了没有」变得含糊。
 * 主题是例外——它得立刻生效，所以那个单独走即点即存。
 */
import { computed, onMounted, ref, watch } from 'vue'

import NoteBanner from '@/components/NoteBanner.vue'
import { toast } from '@/composables/useToast'
import { THEME_LABEL, setTheme, themeChoice, type ThemeChoice } from '@/composables/useTheme'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'
import { formatDuration } from '@/utils/format'

const settings = useSettingsStore()
const session = useSessionStore()

const timeout = ref(8)
const delay = ref(0.4)
const retries = ref(2)
const userAgent = ref('')
const alertDays = ref(3)
const keepRaw = ref(true)

const themes: ThemeChoice[] = ['auto', 'light', 'dark']

const dirty = computed(() => {
  const http = settings.http
  return (
    timeout.value !== http.timeout ||
    delay.value !== http.delay ||
    retries.value !== http.retries ||
    userAgent.value !== http.user_agent ||
    alertDays.value !== settings.alertDays ||
    keepRaw.value !== (settings.view?.keep_raw ?? true)
  )
})

function syncFromStore(): void {
  const http = settings.http
  timeout.value = http.timeout
  delay.value = http.delay
  retries.value = http.retries
  userAgent.value = http.user_agent
  alertDays.value = settings.alertDays
  keepRaw.value = settings.view?.keep_raw ?? true
}

watch(() => settings.view, syncFromStore, { immediate: true })

async function pickTheme(value: ThemeChoice): Promise<void> {
  setTheme(value)
  const ok = await settings.save({ ui: { theme: value } })
  if (!ok) toast.warn('主题没能存到配置文件', '这次会话仍然生效，但重启后会回到原来的设置。')
}

async function saveAll(): Promise<void> {
  const ok = await settings.save({
    http: {
      timeout: Number(timeout.value),
      delay: Number(delay.value),
      retries: Number(retries.value),
      user_agent: userAgent.value.trim(),
    },
    ui: { theme: themeChoice.value, alert_days: Number(alertDays.value) },
    keep_raw: keepRaw.value,
  })
  if (ok) toast.ok('设置已保存', '下次抓取就会用新的参数。')
  else toast.error('保存失败', settings.error)
}

function revert(): void {
  syncFromStore()
}

async function runDoctor(): Promise<void> {
  await settings.runDoctor()
  const failed = settings.failedChecks.length
  if (failed) toast.warn(`有 ${failed} 项没通过`, '看下面的清单，每一条都给了怎么修。')
  else toast.ok('自检通过', '环境看起来没问题。')
}

onMounted(() => {
  void settings.load()
  void session.load()
  void settings.runDoctor()
})
</script>

<template>
  <div class="stack">
    <header class="head">
      <div>
        <h1>设置</h1>
        <p class="muted">这些改动写进配置文件，命令行版和网页版共用同一份。</p>
      </div>
    </header>

    <!-- ------------------------------ 外观 ------------------------------ -->

    <section class="card" aria-labelledby="theme-title">
      <header class="card__head">
        <h2 id="theme-title" class="card__title">
          外观
          <span class="card__hint">这一项点一下立刻生效</span>
        </h2>
      </header>

      <div class="themes" role="radiogroup" aria-labelledby="theme-title">
        <button
          v-for="item in themes"
          :key="item"
          type="button"
          role="radio"
          class="theme"
          :class="{ 'theme--on': themeChoice === item }"
          :aria-checked="themeChoice === item"
          @click="pickTheme(item)"
        >
          <span class="theme__mark" aria-hidden="true">{{ item === 'auto' ? '◐' : item === 'light' ? '☀' : '☾' }}</span>
          <span class="theme__name">{{ THEME_LABEL[item] }}</span>
          <span class="theme__hint faint">
            {{ item === 'auto' ? '跟着系统走' : item === 'light' ? '始终浅色' : '始终深色' }}
          </span>
        </button>
      </div>
    </section>

    <!-- ------------------------------ 抓取参数 ------------------------------ -->

    <section class="card" aria-labelledby="http-title">
      <header class="card__head">
        <h2 id="http-title" class="card__title">
          抓取参数
          <span class="card__hint">温和一点不容易被平台当成异常流量</span>
        </h2>
      </header>

      <div class="form">
        <div class="field">
          <label class="label" for="set-timeout">请求超时（秒）</label>
          <input id="set-timeout" v-model.number="timeout" class="input" type="number" min="1" max="120" step="1" />
          <p class="help">单个请求最多等多久。网络慢就调大，默认 8 秒。</p>
        </div>

        <div class="field">
          <label class="label" for="set-delay">请求间隔（秒）</label>
          <input id="set-delay" v-model.number="delay" class="input" type="number" min="0" max="10" step="0.1" />
          <p class="help">每次请求之间歇一下。0 表示不限速；抓一个账号建议留 0.4 秒以上。</p>
        </div>

        <div class="field">
          <label class="label" for="set-retries">失败重试次数</label>
          <input id="set-retries" v-model.number="retries" class="input" type="number" min="0" max="6" step="1" />
          <p class="help">只对超时和服务端错误重试；4xx 一次就放弃，免得白敲。</p>
        </div>

        <div class="field">
          <label class="label" for="set-alert">提前预警天数</label>
          <input id="set-alert" v-model.number="alertDays" class="input" type="number" min="0" max="30" step="1" />
          <p class="help">距截止还有几天时开始标红。影响时间线的分组和总览的「即将到期」。</p>
        </div>

        <div class="field field--wide">
          <label class="label" for="set-ua">User-Agent</label>
          <input id="set-ua" v-model="userAgent" class="input mono" type="text" spellcheck="false" />
          <p class="help">
            留空就用内置的 Chrome UA。改成别的通常只会让平台多一层怀疑，不建议动。
          </p>
        </div>
      </div>

      <div class="row actions">
        <button type="button" class="btn btn--primary" :disabled="!dirty || settings.saving" @click="saveAll">
          {{ settings.saving ? '保存中…' : '保存设置' }}
        </button>
        <button type="button" class="btn" :disabled="!dirty" @click="revert">放弃改动</button>
        <span v-if="dirty" class="pill pill--warn">有未保存的改动</span>
      </div>
    </section>

    <!-- ------------------------------ 数据 ------------------------------ -->

    <section class="card" aria-labelledby="data-title">
      <header class="card__head">
        <h2 id="data-title" class="card__title">数据</h2>
      </header>

      <label class="check">
        <input v-model="keepRaw" type="checkbox" />
        <span>
          保留探测到的原始响应
          <span class="faint">（写在 data/raw/，方便平台改版时对齐字段；里面可能含课程名，分享前留意）</span>
        </span>
      </label>

      <dl class="facts">
        <div>
          <dt>工作目录</dt>
          <dd class="mono">{{ settings.home || '—' }}</dd>
        </div>
        <div>
          <dt>配置文件</dt>
          <dd class="mono">{{ settings.configPath || '—' }}</dd>
        </div>
      </dl>

      <p v-if="dirty" class="help">「保留原始响应」要连上面的设置一起保存。</p>
    </section>

    <!-- ------------------------------ 自检 ------------------------------ -->

    <section class="card" aria-labelledby="doctor-title">
      <header class="card__head">
        <h2 id="doctor-title" class="card__title">
          环境自检
          <span class="card__hint">只有「×」才算真出问题，「!」是提醒</span>
        </h2>
        <button type="button" class="btn btn--sm" :disabled="settings.checking" @click="runDoctor">
          {{ settings.checking ? '检查中…' : '重新检查' }}
        </button>
      </header>

      <NoteBanner
        v-if="settings.checks.length && !settings.healthy"
        tone="danger"
        :title="`有 ${settings.failedChecks.length} 项没通过`"
        :body="settings.failedChecks.map((c) => c.name).join('、')"
      />

      <ul class="checks">
        <li v-for="item in settings.checks" :key="item.name" class="check-row" :class="`check-row--${item.level}`">
          <span class="check-row__mark" aria-hidden="true">
            {{ item.level === 'ok' ? '✓' : item.level === 'warn' ? '!' : '×' }}
          </span>
          <div class="check-row__body">
            <p class="check-row__name">{{ item.name }}</p>
            <p class="check-row__detail">{{ item.detail }}</p>
            <p v-if="item.fix && item.level !== 'ok'" class="check-row__fix">{{ item.fix }}</p>
          </div>
        </li>
      </ul>

      <p v-if="!settings.checks.length" class="faint">还没跑过自检。</p>
    </section>

    <!-- ------------------------------ 关于 ------------------------------ -->

    <section class="card" aria-labelledby="about-title">
      <header class="card__head">
        <h2 id="about-title" class="card__title">关于</h2>
      </header>

      <dl class="facts">
        <div>
          <dt>版本</dt>
          <dd class="mono">studydash {{ session.version || session.bootVersion || '—' }}</dd>
        </div>
        <div>
          <dt>数据格式版本</dt>
          <dd class="mono">payload schema v{{ session.schema || '—' }}</dd>
        </div>
        <div>
          <dt>运行环境</dt>
          <dd class="mono">{{ session.python || '—' }} · {{ session.platform || '—' }}</dd>
        </div>
        <div>
          <dt>分发方式</dt>
          <dd>{{ session.frozen ? '打包版（exe）' : '源码 / pip 安装' }}</dd>
        </div>
        <div>
          <dt>本次已运行</dt>
          <dd>{{ formatDuration(session.uptimeS) }}</dd>
        </div>
      </dl>

      <NoteBanner
        tone="info"
        title="这是个只读工具"
        body="全项目只有一个网络出口，GET 随便读、POST 只能打到适配器显式声明的查询端点。它不会替你交作业、刷视频或点任何按钮。"
      />
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

.themes {
  display: grid;
  gap: var(--s-3);
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
}

.theme {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  align-items: flex-start;
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

.theme:hover {
  border-color: var(--border-strong);
  background: var(--surface);
}

.theme--on {
  border-color: var(--accent-border);
  background: var(--accent-soft);
  box-shadow: inset 0 0 0 1px var(--accent-border);
}

.theme__mark {
  font-size: 1.15rem;
}

.theme--on .theme__mark {
  color: var(--accent);
}

.theme__name {
  font-weight: 600;
}

.theme__hint {
  font-size: var(--fs-xs);
}

.form {
  display: grid;
  gap: var(--s-4);
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
}

.field--wide {
  grid-column: 1 / -1;
}

.actions {
  margin-top: var(--s-4);
  padding-top: var(--s-4);
  border-top: 1px solid var(--border-subtle);
}

.check {
  display: flex;
  gap: var(--s-2);
  align-items: flex-start;
  font-size: var(--fs-md);
  cursor: pointer;
}

.check input {
  margin-top: 0.3rem;
  flex: none;
}

.facts {
  display: grid;
  gap: var(--s-3);
  margin: var(--s-4) 0 0;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
}

.facts > div {
  padding: var(--s-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-2);
}

.facts dt {
  color: var(--text-faint);
  font-size: var(--fs-xs);
  font-weight: 600;
}

.facts dd {
  margin: var(--s-1) 0 0;
  overflow-wrap: anywhere;
  font-size: var(--fs-sm);
}

.checks {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.check-row {
  display: flex;
  gap: var(--s-3);
  padding: var(--s-3);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--ok);
  border-radius: var(--r-sm);
  background: var(--surface-2);
}

.check-row--warn {
  border-left-color: var(--warn);
}

.check-row--fail {
  border-left-color: var(--danger);
}

.check-row__mark {
  flex: none;
  width: 1rem;
  color: var(--ok);
  font-weight: 700;
}

.check-row--warn .check-row__mark {
  color: var(--warn);
}

.check-row--fail .check-row__mark {
  color: var(--danger);
}

.check-row__body {
  min-width: 0;
}

.check-row__name {
  font-weight: 600;
}

.check-row__detail {
  margin-top: var(--s-1);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

.check-row__fix {
  margin-top: var(--s-1);
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
</style>
