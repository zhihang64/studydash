/**
 * 路由。history 模式，深链接由后端的 SPA 回退接住（见 web/app.py）。
 * 视图全部懒加载：首屏只需要总览，没必要把账号页和设置页也一起下下来。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

export interface NavItem {
  path: string
  title: string
  hint: string
  icon: string
}

export const NAV: NavItem[] = [
  { path: '/', title: '总览', hint: '一眼看清进度与风险', icon: '◎' },
  { path: '/timeline', title: '待办', hint: '按截止时间排好', icon: '◷' },
  { path: '/courses', title: '课程', hint: '逐门课程与作业明细', icon: '▤' },
  { path: '/accounts', title: '账号', hint: '登录、绑定、管理', icon: '⚿' },
  { path: '/settings', title: '设置', hint: '抓取参数与环境自检', icon: '⚙' },
]

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'overview',
    component: () => import('@/views/OverviewView.vue'),
    meta: { title: '总览' },
  },
  {
    path: '/timeline',
    name: 'timeline',
    component: () => import('@/views/TimelineView.vue'),
    meta: { title: '待办' },
  },
  {
    path: '/courses',
    name: 'courses',
    component: () => import('@/views/CoursesView.vue'),
    meta: { title: '课程' },
  },
  {
    path: '/courses/:platform/:id',
    name: 'course-detail',
    component: () => import('@/views/CoursesView.vue'),
    meta: { title: '课程' },
  },
  {
    // 二级页面：从「课程」页进，不占主导航的位置
    path: '/hidden',
    name: 'hidden',
    component: () => import('@/views/HiddenView.vue'),
    meta: { title: '已隐藏课程' },
  },
  {
    path: '/accounts',
    name: 'accounts',
    component: () => import('@/views/AccountsView.vue'),
    meta: { title: '账号' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '找不到页面' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(to, _from, saved) {
    if (saved) return saved
    if (to.hash) return { el: to.hash, behavior: 'smooth' }
    return { top: 0 }
  },
})

/** 标题带上页面名，多开几个标签页时好认。 */
router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · studydash` : 'studydash'
})

export default router
