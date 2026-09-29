import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: { name: 'list' } },
    { path: '/vacancies', name: 'list', component: () => import('@/views/VacancyListView.vue') },
    { path: '/vacancies/:id', name: 'vacancy', component: () => import('@/views/VacancyDetailView.vue') },
    { path: '/stats', name: 'stats', component: () => import('@/views/StatsView.vue') },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
