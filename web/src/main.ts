import { createApp, h } from 'vue'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import { routes } from './routes'

const router = createRouter({ history: createWebHistory(), routes })

createApp({ render: () => h(RouterView) }).use(router).mount('#app')
