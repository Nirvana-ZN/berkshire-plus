import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import PositionsView from '../views/PositionsView.vue'
import ScreenView from '../views/ScreenView.vue'
import ResearchView from '../views/ResearchView.vue'
import DiscoveryView from '../views/DiscoveryView.vue'
import CheapView from '../views/CheapView.vue'
import SettingsView from '../views/SettingsView.vue'
import NotFoundView from '../views/NotFoundView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView,
    },
    {
      path: '/positions',
      name: 'positions',
      component: PositionsView,
    },
    {
      path: '/screen',
      name: 'screen',
      component: ScreenView,
    },
    {
      path: '/discover',
      name: 'discover',
      component: DiscoveryView,
    },
    {
      path: '/cheap',
      name: 'cheap',
      component: CheapView,
      meta: { legacy: true },
    },
    {
      path: '/research/:id',
      name: 'research',
      component: ResearchView,
    },
    {
      path: '/settings',
      name: 'settings',
      component: SettingsView,
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: NotFoundView,
    },
  ],
})

export default router
