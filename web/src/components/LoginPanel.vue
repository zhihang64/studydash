<script setup lang="ts">
/**
 * 登录面板。这是整个应用的入口——先有会话，才有数据。
 *
 * 四条路，各自对应后端一个任务类型：
 *  - 扫码 ``login_qr``：终端二维码那套搬到了网页上。二维码是任务产物（data: URL），
 *    过期会自动换一张，所以这里只负责画出来和显示还剩多久。
 *  - 密码 ``login_password``：密码只在这次请求的内存里活一次，不落盘、不进日志。
 *  - Cookie ``login_cookie``：从别处复制过来的会话，可以要求先验一道。
 *  - 浏览器 ``login_browser`` / ``login_live``：读本机浏览器的配置档，或开一个新窗口登。
 *
 * 密码**不进 URL、不进 localStorage、不进 console**。提交完就从这个组件里清掉。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { TaskView } from '@/api/types'
import { toast } from '@/composables/useToast'
import { useAccountsStore } from '@/stores/accounts'
import { useCatalogStore } from '@/stores/catalog'
import { useTasksStore } from '@/stores/tasks'
import { formatDateTime } from '@/utils/format'

import NoteBanner from './NoteBanner.vue'
import ProgressBar from './ProgressBar.vue'

type Method = 'qr' | 'password' | 'cookie' | 'browser'

const catalog = useCatalogStore()
const accounts = useAccountsStore()
const tasks = useTasksStore()

const method = ref<Method>('qr')
const platform = ref('chaoxing')
const label = ref('')

const username = ref('')
const password = ref('')
const cookieText = ref('')
const verifyCookie = ref(true)

const browser = ref('edge')
const profile = ref('')
const liveMode = ref(false)

const now = ref(Date.now())
let ticker: number | undefined

const METHODS: { id: Method; title: string; hint: string }[] = [
  { id: 'qr', title: '扫码', hint: '用手机 App 扫，最快也最安全' },
  { id: 'password', title: '账号密码', hint: '密码只在这一刻用一次' },
  { id: 'cookie', title: '粘贴 Cookie', hint: '从别处复制来的会话' },
  { id: 'browser', title: '本机浏览器', hint: '读你已经登录好的会话' },
]

const loginablePlatforms = computed(() => catalog.loginable())

/** 正在跑的登录任务。同一时刻只允许一个，界面上也只呈现一个。 */
const activeTask = computed<TaskView | null>(() => {
  const record = tasks.records.find((r) => r.task.kind.startsWith('login_') && !tasks.isFinished(r.task))
  return record?.task ?? null
})

/** 最近一次结束的登录任务，用来显示结果或错误。 */
const lastTask = computed<TaskView | null>(() => {
  const record = tasks.records.find((r) => r.task.kind.startsWith('login_'))
  return record?.task ?? null
})

const busy = computed(() => activeTask.value !== null)

const qrDataUrl = computed(() => activeTask.value?.artifacts?.qr_data_url ?? '')

const qrExpiresAt = computed(() => activeTask.value?.artifacts?.qr_expires_at ?? '')

const qrSecondsLeft = computed(() => {
  if (!qrExpiresAt.value) return 0
  const target = new Date(qrExpiresAt.value).getTime()
  if (Number.isNaN(target)) return 0
  return Math.max(0, Math.round((target - now.value) / 1000))
})

const qrUrgent = computed(() => qrSecondsLeft.value > 0 && qrSecondsLeft.value <= 20)

/** 登录失败但值得给一条专门建议的两种情况。 */
const specialError = computed(() => {
  const task = lastTask.value
  if (!task || task.state !== 'failed') return null
  const code = task.error?.code ?? ''
  if (code === 'captcha_required') {
    return {
      tone: 'warn' as const,
      title: '这个账号这次要求图形验证码',
      body: '网页端不猜验证码。改用扫码登录：同样能绑上账号，而且不用把密码交给任何程序。',
    }
  }
  if (code === 'two_factor_required') {
    return {
      tone: 'warn' as const,
      title: '这个账号开了二次验证',
      body: '二次验证的链接由平台发给你，程序接不住。请用扫码登录，或改用「打开独立浏览器登录」。',
    }
  }
  return null
})

const succeeded = computed(() => {
  const task = lastTask.value
  if (!task || task.state !== 'done' || !task.kind.startsWith('login_')) return null
  const account = task.result?.account as { label?: string; uid?: string } | undefined
  if (!account) return null
  return account
})

watch(qrDataUrl, (value) => {
  if (value && ticker === undefined) {
    ticker = window.setInterval(() => {
      now.value = Date.now()
    }, 1000)
  } else if (!value && ticker !== undefined) {
    window.clearInterval(ticker)
    ticker = undefined
  }
})

onBeforeUnmount(() => {
  if (ticker !== undefined) window.clearInterval(ticker)
})

async function submit(): Promise<void> {
  if (busy.value) return
  const target = platform.value

  if (method.value === 'password') {
    if (!username.value.trim() || !password.value) {
      toast.warn('还差点信息', '账号和密码都要填。')
      return
    }
  }
  if (method.value === 'cookie' && !cookieText.value.trim()) {
    toast.warn('还差点信息', '先把 Cookie 粘进来。')
    return
  }

  let kind = 'login_qr'
  const options: Record<string, unknown> = { platform: target, label: label.value.trim() }

  if (method.value === 'password') {
    kind = 'login_password'
    options.username = username.value.trim()
    options.password = password.value
  } else if (method.value === 'cookie') {
    kind = 'login_cookie'
    options.cookie = cookieText.value
    options.verify = verifyCookie.value
  } else if (method.value === 'browser') {
    kind = liveMode.value ? 'login_live' : 'login_browser'
    options.browser = browser.value
    if (!liveMode.value) options.profile = profile.value.trim()
  } else {
    kind = 'login_qr'
  }

  const task = await tasks.start(kind, options)

  // 提交完立刻把敏感输入从组件状态里抹掉：密码不该在页面上多停留一秒，
  // Cookie 同理（它等价于账号密码）。
  password.value = ''
  if (method.value === 'cookie') cookieText.value = ''

  if (task && method.value === 'qr') {
    toast.info('二维码已生成', '用手机上的学习通 App 扫下面的码。')
  }
}

function reset(): void {
  username.value = ''
  password.value = ''
  cookieText.value = ''
}

defineExpose({ submit })
</script>

<template>
  <section class="login card" aria-labelledby="login-title">
    <header class="card__head">
      <div>
        <h2 id="login-title" class="card__title">绑定账号</h2>
        <p class="card__hint">登录后自动抓取课程与作业，并直接带你回到看板。</p>
      </div>
    </header>

    <NoteBanner
      v-if="succeeded"
      tone="ok"
      :title="`已绑定账号「${succeeded.label}」`"
      :body="succeeded.uid ? `uid ${succeeded.uid}　会话已存进配置文件。` : '会话已存进配置文件。'"
    />

    <NoteBanner v-if="specialError" :tone="specialError.tone" :title="specialError.title" :body="specialError.body" />

    <!-- ---------------- 正在跑 ---------------- -->

    <div v-if="busy" class="running">
      <div class="running__head">
        <span class="pill pill--accent">{{ tasks.label(activeTask!.kind) }}</span>
        <span class="running__msg">{{ activeTask!.message || '正在准备…' }}</span>
      </div>
      <ProgressBar :ratio="activeTask!.progress" size="sm" />

      <div v-if="qrDataUrl" class="qr">
        <img class="qr__img" :src="qrDataUrl" alt="登录二维码：请用手机上的学习通 App 扫描" />
        <div class="qr__side">
          <p class="qr__tip">用手机上的「学习通」App 扫这个码。</p>
          <p class="qr__tip faint">打开 App → 右上角扫一扫 → 确认登录。</p>
          <p class="qr__timer" :class="{ 'qr__timer--urgent': qrUrgent }">
            <template v-if="qrSecondsLeft > 0">这张码还能用 {{ qrSecondsLeft }} 秒</template>
            <template v-else>正在换一张新码…</template>
          </p>
          <p class="faint qr__note">过期会自动换码，不用管它。</p>
        </div>
      </div>

      <p v-if="activeTask!.kind === 'login_live'" class="running__note">
        已经打开了一个独立的浏览器窗口，在里面正常登录就行。登录成功后这里会自动继续。
      </p>

      <div class="row">
        <button type="button" class="btn" @click="tasks.cancel(activeTask!.id)">取消</button>
        <RouterLink class="btn btn--ghost" to="/accounts">看详细进度</RouterLink>
      </div>
    </div>

    <!-- ---------------- 表单 ---------------- -->

    <form v-else class="form" @submit.prevent="submit">
      <div class="form__row">
        <div class="field">
          <label class="label" for="login-platform">平台</label>
          <select id="login-platform" v-model="platform" class="select">
            <option v-for="item in loginablePlatforms" :key="item.id" :value="item.id">
              {{ item.name }}{{ item.status === 'beta' ? '（beta）' : item.status === 'unverified' ? '（未验证）' : '' }}
            </option>
          </select>
        </div>

        <div class="field">
          <label class="label" for="login-label">账号备注<span class="faint">（可选）</span></label>
          <input
            id="login-label"
            v-model="label"
            class="input"
            type="text"
            maxlength="24"
            placeholder="例如：大号 / 小号"
            autocomplete="off"
          />
          <p class="help">
            {{ accounts.countOf(platform) ? `这个平台已经绑了 ${accounts.countOf(platform)} 个，留空会自动起名。` : '留空就用默认名字。' }}
          </p>
        </div>
      </div>

      <div class="tabs" role="tablist" aria-label="登录方式">
        <button
          v-for="item in METHODS"
          :key="item.id"
          type="button"
          role="tab"
          class="tab"
          :class="{ 'tab--on': method === item.id }"
          :aria-selected="method === item.id"
          @click="method = item.id"
        >
          <span class="tab__title">{{ item.title }}</span>
          <span class="tab__hint">{{ item.hint }}</span>
        </button>
      </div>

      <!-- 扫码 -->
      <div v-if="method === 'qr'" class="pane">
        <p class="pane__text">
          点下面的按钮，这里会直接出现一张二维码。用手机上的「学习通」App 扫码并在手机上确认，
          程序就能拿到登录会话。密码完全不经过这个程序。
        </p>
        <ul class="pane__list">
          <li>手机没装学习通？改用「账号密码」。</li>
          <li>扫码后如果手机上弹了二次验证，按手机上的提示走完就行。</li>
        </ul>
      </div>

      <!-- 密码 -->
      <div v-else-if="method === 'password'" class="pane">
        <div class="form__row">
          <div class="field">
            <label class="label" for="login-user">账号</label>
            <input
              id="login-user"
              v-model="username"
              class="input"
              type="text"
              autocomplete="username"
              placeholder="手机号 / 学号 / 邮箱"
              spellcheck="false"
            />
          </div>
          <div class="field">
            <label class="label" for="login-pass">密码</label>
            <input
              id="login-pass"
              v-model="password"
              class="input"
              type="password"
              autocomplete="current-password"
              placeholder="只用于这一次登录"
            />
          </div>
        </div>
        <p class="pane__warn">
          密码不回显、不写进配置文件、不进日志，只在这一次请求里用一下就丢掉。
          平台要求图形验证码时会直接告诉你——那种情况请改用扫码。
        </p>
      </div>

      <!-- Cookie -->
      <div v-else-if="method === 'cookie'" class="pane">
        <div class="field">
          <label class="label" for="login-cookie">Cookie</label>
          <textarea
            id="login-cookie"
            v-model="cookieText"
            class="textarea"
            spellcheck="false"
            placeholder="整段粘贴：浏览器 F12 → Network → 任意请求 → 请求头里的 Cookie 那一行"
          />
          <p class="help">带不带 <code>Cookie:</code> 前缀都行，一行 <code>a=1; b=2</code> 或导出的 JSON 都能认。</p>
        </div>
        <label class="check">
          <input v-model="verifyCookie" type="checkbox" />
          <span>先验证一下这段 Cookie 还能不能用（推荐）</span>
        </label>
      </div>

      <!-- 本机浏览器 -->
      <div v-else class="pane">
        <div class="form__row">
          <div class="field">
            <label class="label" for="login-browser">浏览器</label>
            <select id="login-browser" v-model="browser" class="select">
              <option value="edge">Edge</option>
              <option value="chrome">Chrome</option>
              <option value="brave">Brave</option>
              <option value="chromium">Chromium</option>
              <option value="firefox">Firefox</option>
            </select>
          </div>
          <div v-if="!liveMode" class="field">
            <label class="label" for="login-profile">配置档<span class="faint">（可选）</span></label>
            <input
              id="login-profile"
              v-model="profile"
              class="input"
              type="text"
              placeholder="默认 Default"
              autocomplete="off"
            />
          </div>
        </div>

        <label class="check">
          <input v-model="liveMode" type="checkbox" />
          <span>不读现有配置档，另开一个独立窗口让我登录</span>
        </label>

        <p v-if="!liveMode" class="pane__warn">
          浏览器运行时会独占 Cookie 库，读不出来。用这条路之前，请把
          {{ browser }} <strong>完全退出</strong>（含托盘图标里的后台进程）。
          不想关浏览器就用「扫码」，或勾上上面那个选项另开独立窗口。
        </p>
        <p v-else class="pane__text">
          会打开一个独立的浏览器配置档，你在里面正常登录，程序从调试端口把会话取走。
          和你日常用的浏览器互不干扰。
        </p>
      </div>

      <div class="form__actions">
        <button type="submit" class="btn btn--primary" :disabled="busy">
          {{ method === 'qr' ? '生成二维码' : '登录并抓取' }}
        </button>
        <button v-if="method === 'password' || method === 'cookie'" type="button" class="btn btn--ghost" @click="reset">
          清空输入
        </button>
        <span class="spacer" />
        <span class="faint form__privacy">只读工具：登录只为把数据读出来，不会替你交作业或刷视频。</span>
      </div>
    </form>

    <p v-if="lastTask && lastTask.state === 'failed' && !specialError" class="form__error">
      上次登录失败：{{ lastTask.error?.message || lastTask.message }}
    </p>

    <p v-if="tasks.lastFinishedId && succeeded" class="faint form__stamp">
      完成于 {{ formatDateTime(lastTask?.ended_at) }}
    </p>
  </section>
</template>

<style scoped>
.login {
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
}

/* ---------- 运行中 ---------- */

.running {
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
}

.running__head {
  display: flex;
  gap: var(--s-3);
  align-items: center;
}

.running__msg {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.running__note {
  padding: var(--s-3);
  border-radius: var(--r-sm);
  background: var(--info-soft);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.7;
}

.qr {
  display: flex;
  gap: var(--s-5);
  align-items: center;
  flex-wrap: wrap;
  padding: var(--s-4);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}

.qr__img {
  width: 14rem;
  height: 14rem;
  flex: none;
  border-radius: var(--r-sm);
  /* 二维码必须画在白底上：透明背景在深色主题里会让扫码器认不出 */
  background: #fff;
  image-rendering: pixelated;
}

.qr__side {
  min-width: 12rem;
  flex: 1 1 auto;
}

.qr__tip {
  font-size: var(--fs-md);
}

.qr__timer {
  margin-top: var(--s-3);
  font-weight: 650;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}

.qr__timer--urgent {
  color: var(--warn);
}

.qr__note {
  margin-top: var(--s-1);
  font-size: var(--fs-xs);
}

/* ---------- 表单 ---------- */

.form {
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
}

.form__row {
  display: grid;
  gap: var(--s-4);
}

@media (min-width: 640px) {
  .form__row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.tabs {
  display: grid;
  gap: var(--s-2);
  padding: var(--s-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}

@media (min-width: 700px) {
  .tabs {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

.tab {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  padding: var(--s-2) var(--s-3);
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}

.tab:hover {
  background: var(--surface);
}

.tab--on {
  border-color: var(--accent-border);
  background: var(--surface);
  color: var(--text);
  box-shadow: var(--shadow-1);
}

.tab__title {
  font-weight: 600;
  font-size: var(--fs-md);
}

.tab__hint {
  font-size: var(--fs-xs);
  line-height: 1.5;
}

.pane {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  padding: var(--s-4);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}

.pane__text {
  font-size: var(--fs-md);
  line-height: 1.8;
}

.pane__list {
  margin: 0;
  padding-left: 1.1rem;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.9;
}

.pane__warn {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.8;
}

.check {
  display: flex;
  gap: var(--s-2);
  align-items: flex-start;
  font-size: var(--fs-sm);
  color: var(--text-muted);
  cursor: pointer;
}

.check input {
  margin-top: 0.3rem;
  flex: none;
}

.form__actions {
  display: flex;
  gap: var(--s-3);
  align-items: center;
  flex-wrap: wrap;
  padding-top: var(--s-1);
}

.form__privacy {
  font-size: var(--fs-xs);
  text-align: right;
}

.form__error {
  color: var(--danger);
  font-size: var(--fs-sm);
}

.form__stamp {
  font-size: var(--fs-xs);
}
</style>
