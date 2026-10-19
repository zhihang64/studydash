/**
 * 入口。
 *
 * 顺序有讲究：先定主题再挂载——反过来的话，深色偏好的用户会先看到一帧亮色。
 * 令牌失效这类「整页没法用」的错误由 `client.ts` 的钩子上报给 `useFatal`，
 * 外壳据此换成整屏提示，而不是让每个页面各自弹 toast。
 */
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import { ApiError, onApiFailure } from '@/api/client'
import { reportFatal } from '@/composables/useFatal'
import { initTheme } from '@/composables/useTheme'
import { router } from '@/router'

import App from './App.vue'
import './styles/tokens.css'
import './styles/base.css'

initTheme()

onApiFailure((error: ApiError) => {
  if (error.code === 'unauthorized') {
    reportFatal({
      title: '访问令牌已经失效',
      body: 'studydash 每次启动都会重新生成一个访问令牌，所以重启之后旧页面就用不了了。这不是出错，刷新一下就好。',
      action: '刷新页面',
    })
    return
  }
  reportFatal({
    title: error.message || '请求没成功',
    body: error.hint || '后端返回了一个说不上来的结果。看命令行窗口里的日志可能更快。',
    action: error.hint ? '' : '刷新页面',
  })
})

const app = createApp(App)

app.config.errorHandler = (err) => {
  // 渲染期间的意外不该只剩白屏。这里不做恢复，只保证屏幕上有一句话。
  const message = err instanceof Error ? err.message : String(err)
  reportFatal({
    title: '页面出了意外',
    body: message,
    action: '刷新页面；如果一直这样，把命令行窗口里的最后几行发出来。',
  })
}

app.use(createPinia())
app.use(router)
app.mount('#app')
