import { createApp, h } from 'vue'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import AdminApp from './AdminApp.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/admin/overview' },
    { path: '/admin', redirect: '/admin/overview' },
    { path: '/admin/:pathMatch(.*)*', component: AdminApp },
    { path: '/:pathMatch(.*)*', redirect: '/admin/overview' },
  ],
})

createApp({ render: () => h(RouterView) }).use(router).mount('#app')
