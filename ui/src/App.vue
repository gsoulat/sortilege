<script setup>
import { ref, onMounted } from 'vue'
import TemplateBuilder from './components/TemplateBuilder.vue'

const health = ref(null)
const view = ref('templates')

const VIEWS = [
  { id: 'templates', label: 'Gabarits', ready: true },
  { id: 'library', label: 'Bibliothèque', ready: false },
  { id: 'review', label: 'File de revue', ready: false },
  { id: 'settings', label: 'Réglages', ready: false },
]

onMounted(async () => {
  try {
    health.value = await (await fetch('/api/health')).json()
  } catch {
    health.value = { status: 'unreachable' }
  }
})
</script>

<template>
  <div class="shell">
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
          :disabled="!v.ready"
          :title="v.ready ? '' : 'Pas encore implémenté'"
          @click="view = v.id"
        >{{ v.label }}</button>
      </nav>

      <div class="status" v-if="health">
        <span v-if="health.dry_run" class="badge warn" title="Aucun fichier ne sera déplacé">
          simulation
        </span>
        <span v-else class="badge live">actif</span>
        <span class="badge" :class="health.ai_enabled ? 'on' : 'off'">
          IA {{ health.ai_enabled ? 'activée' : 'désactivée' }}
        </span>
      </div>
    </header>

    <main>
      <TemplateBuilder v-if="view === 'templates'" />
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
h1 {
  margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -.01em;
}

nav { display: flex; gap: 2px; margin-right: auto; }
nav button {
  border: none; background: none; color: var(--text-dim);
  padding: 6px 11px; border-radius: 6px; font-size: 13px;
}
nav button:hover:not(:disabled) { color: var(--text); background: var(--surface); }
nav button.active { color: var(--text); background: var(--surface-2); }
nav button:disabled { color: var(--text-faint); }

.status { display: flex; gap: 6px; }
.badge {
  font-size: 11px; padding: 3px 9px; border-radius: 20px;
  border: 1px solid var(--border); color: var(--text-dim);
  letter-spacing: .02em;
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
.badge.on { color: var(--accent); border-color: var(--accent-dim); }
</style>
