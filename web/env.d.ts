/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

/** 后端注入到 HTML 里的启动参数。不是从 studydash 启动时它是 null。 */
interface StudydashBoot {
  token: string
  version: string
  basePath?: string
}

interface Window {
  __STUDYDASH__: StudydashBoot | null
}
