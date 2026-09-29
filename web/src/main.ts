import { createApp, h } from 'vue'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import App from './App.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/chat', name: 'chat', component: App }, { path: '/:pathMatch(.*)*', redirect: '/chat' }],
})

createApp({ render: () => h(RouterView) }).use(router).mount('#app')
