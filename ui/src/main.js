import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

/**
 * Une session peut expirer en pleine utilisation. Sans ceci, chaque vue
 * afficherait sa propre erreur cryptique au lieu de ramener à la connexion.
 * On enveloppe fetch une fois pour toutes plutôt que de traiter le 401 dans
 * chacun des appels — un oubli ailleurs redeviendrait un message obscur.
 */
const nativeFetch = window.fetch.bind(window)
window.fetch = async (...args) => {
  const response = await nativeFetch(...args)
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent('sortilege:unauthenticated'))
  }
  return response
}

createApp(App).mount('#app')
