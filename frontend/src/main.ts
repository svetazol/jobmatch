import { createApp } from 'vue'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import 'primeicons/primeicons.css'
import preset from './theme'
import router from './router'
import App from './App.vue'
import './styles.css'

createApp(App)
  .use(router)
  .use(PrimeVue, {
    theme: {
      preset,
      options: {
        // paired with the .dark class App.vue toggles on <html>
        darkModeSelector: '.dark',
        cssLayer: false,
      },
    },
  })
  .use(ToastService)
  .mount('#app')
