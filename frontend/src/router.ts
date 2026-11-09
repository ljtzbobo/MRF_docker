import { createRouter, createWebHistory } from 'vue-router'

const routeContent = { render: () => null }

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/tasks', name: 'tasks', component: routeContent },
    { path: '/resources', name: 'resources', component: routeContent },
    { path: '/:pathMatch(.*)*', redirect: '/tasks' },
  ],
})
