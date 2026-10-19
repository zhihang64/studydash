/**
 * 致命错误：整个页面已经没法用了（令牌失效、后端被关掉），
 * 这时不该再让用户去点某个按钮，而是把话说清楚。
 *
 * 与 toast 的分工：toast 说「这一次操作没成」，这里说「这个页面完了」。
 */
import { readonly, ref } from 'vue'

export interface FatalNotice {
  title: string
  body: string
  /** 建议用户做的动作，写成一句人话 */
  action: string
}

const notice = ref<FatalNotice | null>(null)

export const fatal = readonly(notice)

export function reportFatal(value: FatalNotice): void {
  // 同一个原因会从好几个并发请求上报过来，覆盖等价的提示即可
  if (notice.value && notice.value.title === value.title) return
  notice.value = value
}

export function clearFatal(): void {
  notice.value = null
}
