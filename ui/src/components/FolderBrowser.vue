<script setup>
import { ref, onMounted } from 'vue'

const props = defineProps({
  // Chemin de départ. Null = liste les zones montées.
  start: { type: String, default: null },
  // Libellé du bouton de sélection du dossier courant.
  pickLabel: { type: String, default: 'Choisir ce dossier' },
})
const emit = defineEmits(['pick', 'close'])

const node = ref(null)
const error = ref(null)
const loading = ref(false)

async function go(path) {
  loading.value = true
  error.value = null
  try {
    const url = path
      ? `/api/settings/browse?path=${encodeURIComponent(path)}`
      : '/api/settings/browse'
    const res = await fetch(url)
    const body = await res.json()
    if (!res.ok) {
      error.value = body.detail ?? 'Lecture impossible.'
      return
    }
    node.value = body
  } catch {
    error.value = 'Serveur injoignable.'
  } finally {
    loading.value = false
  }
}

onMounted(() => go(props.start))
</script>

<template>
  <div class="browser">
    <div class="head">
      <button class="nav-up" :disabled="!node?.parent && !node?.path" @click="go(node?.parent ?? null)">
        ↑ Remonter
      </button>
      <code class="current">{{ node?.path ?? 'Zones montées' }}</code>
      <button class="close" @click="emit('close')">Fermer</button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <ul v-if="node" class="entries">
      <li v-for="e in node.entries" :key="e.path">
        <button class="nav" @click="go(e.path)">
          <span class="chev">▸</span> {{ e.name }}
          <span v-if="e.is_library" class="tag">bibliothèque</span>
        </button>
        <button class="pick" @click="emit('pick', e.path)">Choisir</button>
      </li>
      <li v-if="!node.entries.length" class="none">Aucun sous-dossier.</li>
    </ul>

    <div v-if="node?.path" class="foot">
      <button class="pick wide" @click="emit('pick', node.path)">{{ pickLabel }}</button>
    </div>

    <span v-if="loading" class="loading">…</span>
  </div>
</template>

<style scoped>
.browser {
  border: 1px solid var(--accent-dim); border-radius: 8px;
  background: var(--bg); padding: 11px 13px;
}
.head { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; }
.current { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-up, .close { font-size: 11.5px; padding: 3px 9px; }

code {
  font-family: var(--mono); font-size: 11.5px;
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.entries { list-style: none; margin: 0; padding: 0; max-height: 230px; overflow-y: auto; }
.entries li { display: flex; align-items: center; gap: 8px; }
.entries li.none { color: var(--text-faint); font-size: 12px; padding: 6px 2px; }

.nav {
  flex: 1; text-align: left; border: none; background: none;
  padding: 4px 6px; font-size: 12.5px; border-radius: 5px;
  display: flex; align-items: center; gap: 7px;
}
.nav:hover { background: var(--surface-2); }
.chev { color: var(--accent); font-size: 10px; }
.tag {
  font-size: 9.5px; padding: 1px 5px; border-radius: 3px;
  color: var(--text-faint); border: 1px solid var(--border);
}

.pick { font-size: 11px; padding: 2px 9px; color: var(--accent); border-color: var(--accent-dim); }
.pick.wide { font-size: 12px; padding: 4px 11px; }

.foot { margin-top: 9px; padding-top: 9px; border-top: 1px solid var(--border); }
.err { margin: 0 0 8px; font-size: 12px; color: var(--err); }
.loading { font-size: 12px; color: var(--text-faint); }
</style>
