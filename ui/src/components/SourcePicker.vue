<script setup>
import { ref } from 'vue'

const props = defineProps({
  prefs: { type: Object, required: true },
})
const emit = defineEmits(['change'])

const browsing = ref(false)
const node = ref(null)
const browseError = ref(null)
const loading = ref(false)

/**
 * Une liste `enabled_sources` vide signifie « toutes ». Pour pouvoir en
 * décocher une, il faut d'abord matérialiser ce « toutes » en liste explicite,
 * sinon décocher la première ne produirait aucun changement visible.
 */
function materialised() {
  const list = props.prefs.enabled_sources
  if (list.length) return [...list]
  return props.prefs.available_sources.map((s) => s.path)
}

function isEnabled(path) {
  const list = props.prefs.enabled_sources
  return list.length === 0 || list.includes(path)
}

function toggle(path) {
  const next = new Set(materialised())
  next.has(path) ? next.delete(path) : next.add(path)
  emit('change', { enabled_sources: [...next] })
}

function remove(path) {
  emit('change', {
    custom_sources: props.prefs.custom_sources.filter((p) => p !== path),
    enabled_sources: props.prefs.enabled_sources.filter((p) => p !== path),
  })
}

function add(path) {
  if (props.prefs.available_sources.some((s) => s.path === path)) return
  emit('change', { custom_sources: [...props.prefs.custom_sources, path] })
  browsing.value = false
}

async function go(path) {
  loading.value = true
  browseError.value = null
  try {
    const url = path ? `/api/settings/browse?path=${encodeURIComponent(path)}` : '/api/settings/browse'
    const res = await fetch(url)
    const body = await res.json()
    if (!res.ok) {
      browseError.value = body.detail ?? 'Lecture impossible.'
      return
    }
    node.value = body
  } finally {
    loading.value = false
  }
}

function openBrowser() {
  browsing.value = true
  go(null)
}
</script>

<template>
  <div class="picker">
    <ul class="sources">
      <li v-for="src in prefs.available_sources" :key="src.path">
        <label>
          <input type="checkbox" :checked="isEnabled(src.path)" @change="toggle(src.path)" />
          <code>{{ src.path }}</code>
        </label>
        <span v-if="src.mounted" class="tag" title="Vient du docker-compose, non supprimable">
          montée
        </span>
        <span v-if="!src.exists" class="warn">introuvable</span>
        <button v-if="!src.mounted" class="remove" title="Retirer cette source" @click="remove(src.path)">
          ✕
        </button>
      </li>
    </ul>

    <button v-if="!browsing" class="add" @click="openBrowser">+ Ajouter une source</button>

    <div v-else class="browser">
      <div class="browser-head">
        <button
          class="up"
          :disabled="!node?.path"
          @click="go(node?.parent ?? null)"
        >↑ Remonter</button>
        <code class="current">{{ node?.path ?? 'Racines montées' }}</code>
        <button class="close" @click="browsing = false">Fermer</button>
      </div>

      <p v-if="browseError" class="err-msg">{{ browseError }}</p>

      <ul v-if="node" class="entries">
        <li v-for="e in node.entries" :key="e.path">
          <button class="nav" @click="go(e.path)">
            <span class="folder">▸</span> {{ e.name }}
          </button>
          <button class="pick" @click="add(e.path)">Ajouter</button>
        </li>
        <li v-if="!node.entries.length" class="none">Aucun sous-dossier.</li>
      </ul>

      <div v-if="node?.path" class="browser-foot">
        <button class="pick current-pick" @click="add(node.path)">
          Ajouter ce dossier
        </button>
      </div>

      <span v-if="loading" class="loading">…</span>
    </div>
  </div>
</template>

<style scoped>
.picker { display: flex; flex-direction: column; gap: 11px; }

.sources { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.sources li { display: flex; align-items: center; gap: 9px; }
.sources label { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.sources input { width: auto; }

code {
  font-family: var(--mono); font-size: 11.5px;
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.tag {
  font-size: 9.5px; padding: 1px 6px; border-radius: 3px; letter-spacing: .04em;
  color: var(--text-faint); border: 1px solid var(--border);
}
.warn { color: var(--warn); font-size: 11.5px; }

.remove {
  margin-left: auto; padding: 1px 7px; font-size: 11px;
  color: var(--text-faint); border-color: transparent; background: none;
}
.remove:hover { color: var(--err); border-color: color-mix(in srgb, var(--err) 30%, transparent); }

.add { align-self: flex-start; font-size: 12.5px; }

/* --- Explorateur --- */
.browser {
  border: 1px solid var(--accent-dim); border-radius: 8px;
  background: var(--bg); padding: 11px 13px;
}
.browser-head { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; }
.browser-head .current { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.up, .close { font-size: 11.5px; padding: 3px 9px; }

.entries { list-style: none; margin: 0; padding: 0; max-height: 230px; overflow-y: auto; }
.entries li { display: flex; align-items: center; gap: 8px; }
.entries li.none { color: var(--text-faint); font-size: 12px; padding: 6px 2px; }

.nav {
  flex: 1; text-align: left; border: none; background: none;
  padding: 4px 6px; font-size: 12.5px; border-radius: 5px;
}
.nav:hover { background: var(--surface-2); }
.folder { color: var(--accent); font-size: 10px; }

.pick {
  font-size: 11px; padding: 2px 9px;
  color: var(--accent); border-color: var(--accent-dim);
}

.browser-foot { margin-top: 9px; padding-top: 9px; border-top: 1px solid var(--border); }
.current-pick { font-size: 12px; padding: 4px 11px; }

.err-msg { margin: 0 0 8px; font-size: 12px; color: var(--err); }
.loading { font-size: 12px; color: var(--text-faint); }
</style>
