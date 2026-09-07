<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import LoginView from './components/LoginView.vue'
import TemplateBuilder from './components/TemplateBuilder.vue'
import LibraryView from './components/LibraryView.vue'
import ReviewView from './components/ReviewView.vue'
import SettingsView from './components/SettingsView.vue'

const authenticated = ref(null) // null = on ne sait pas encore
const health = ref(null)
const view = ref('library')

const VIEWS = [
  { id: 'library', label: 'Bibliothèque' },
  { id: 'review', label: 'File de revue' },
  { id: 'templates', label: 'Gabarits' },
  { id: 'settings', label: 'Réglages' },
]

async function checkAuth() {
  try {
    const res = await fetch('/api/auth/me')
    authenticated.value = (await res.json()).authenticated
  } catch {
    authenticated.value = false
  }
}

async function loadHealth() {
  try {
    health.value = await (await fetch('/api/health')).json()
  } catch {
    health.value = { status: 'unreachable' }
  }
}

async function onAuthenticated() {
  authenticated.value = true
  await loadHealth()
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' })
  authenticated.value = false
}

// Une session qui expire en cours d'usage ramène à l'écran de connexion
// plutôt que de laisser chaque vue afficher son erreur.
function onUnauthenticated() {
  authenticated.value = false
}

onMounted(async () => {
  window.addEventListener('sortilege:unauthenticated', onUnauthenticated)
  await checkAuth()
  if (authenticated.value) await loadHealth()
})

onUnmounted(() => {
  window.removeEventListener('sortilege:unauthenticated', onUnauthenticated)
})
</script>

<template>
  <!-- Rien tant qu'on ignore l'état : afficher la connexion puis l'application
       ferait clignoter l'écran à chaque chargement. -->
  <div v-if="authenticated === null" />

  <LoginView v-else-if="!authenticated" @authenticated="onAuthenticated" />

  <div v-else class="shell">
    <header>
      <div class="brand">
        <span class="mark">✦</span>
        <h1>Sortilège</h1>
      </div>

      <nav>
        <button
          v-for="v in VIEWS"
          :key="v.id"
          :class="{ active: view === v.id }"
          @click="view = v.id"
        >{{ v.label }}</button>
      </nav>

      <div class="status">
        <template v-if="health && health.status !== 'unreachable'">
          <span v-if="health.dry_run" class="badge warn" title="Aucun fichier ne sera déplacé">
            simulation
          </span>
          <span v-else class="badge live">actif</span>
          <span v-if="health.ffprobe === false" class="badge warn" title="Durée et tags non lisibles">
            sans ffprobe
          </span>
          <span class="badge" :class="health.ai_enabled ? 'on' : 'off'">
            IA {{ health.ai_enabled ? 'activée' : 'désactivée' }}
          </span>
        </template>
        <span v-else-if="health" class="badge err">hors ligne</span>
        <button class="logout" title="Se déconnecter" @click="logout">Quitter</button>
      </div>
    </header>

    <main>
      <LibraryView v-if="view === 'library'" />
      <ReviewView v-else-if="view === 'review'" />
      <TemplateBuilder v-else-if="view === 'templates'" />
      <SettingsView v-else-if="view === 'settings'" />
    </main>
  </div>
</template>

<style scoped>
.shell { max-width: 1180px; margin: 0 auto; padding: 0 24px 64px; }

header {
  display: flex; align-items: center; gap: 28px;
  padding: 20px 0 22px; margin-bottom: 28px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}

.brand { display: flex; align-items: baseline; gap: 9px; }
.mark { color: var(--accent); font-size: 17px; }
h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -.01em; }

nav { display: flex; gap: 2px; margin-right: auto; }
nav button {
  border: none; background: none; color: var(--text-dim);
  padding: 6px 11px; border-radius: 6px; font-size: 13px;
}
nav button:hover { color: var(--text); background: var(--surface); }
nav button.active { color: var(--text); background: var(--surface-2); }

.status { display: flex; align-items: center; gap: 6px; }
.badge {
  font-size: 11px; padding: 3px 9px; border-radius: 20px;
  border: 1px solid var(--border); color: var(--text-dim); letter-spacing: .02em;
}
.badge.warn {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}
.badge.live {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok) 35%, transparent);
  background: color-mix(in srgb, var(--ok) 10%, transparent);
}
.badge.err { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }
.badge.on { color: var(--accent); border-color: var(--accent-dim); }

.logout {
  font-size: 11px; padding: 3px 10px; border-radius: 20px;
  color: var(--text-faint); background: none;
}
.logout:hover { color: var(--text); }
</style>
