import type { RouteRecordRaw } from 'vue-router'
import App from './App.vue'

export const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', name: 'chat', component: App },
  { path: '/chat/:conversation_id', name: 'conversation', component: App, props: true },
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]
