<script setup>
import { ref, computed, watch, onMounted } from 'vue'

const KINDS = [
  { id: 'movie', label: 'Films' },
  { id: 'episode', label: 'Séries TV' },
  { id: 'anime', label: 'Animes' },
]

const GROUP_LABELS = {
  identite: 'Identité',
  episode: 'Épisode',
  technique: 'Technique',
  identifiants: 'Identifiants',
}

const tokens = ref([])
const presets = ref({})
const kind = ref('movie')
const template = ref('')
const preview = ref({ valid: true, error: null, results: [] })
const loading = ref(false)
const editor = ref(null)
const dragOver = ref(false)

const grouped = computed(() => {
  const out = {}
  for (const t of tokens.value) (out[t.group] ??= []).push(t)
  return out
})

// Les jetons pertinents dependent du type : proposer {season} sur un film
// n'aide personne et produirait un segment vide en silence.
const relevantGroups = computed(() => {
  const entries = Object.entries(grouped.value)
  if (kind.value === 'movie') return entries.filter(([g]) => g !== 'episode')
  return entries
})

async function loadCatalog() {
  const res = await fetch('/api/tokens')
  const data = await res.json()
  tokens.value = data.tokens
  presets.value = data.presets
  applyPreset('jellyfin')
}

function applyPreset(family) {
  template.value = presets.value[family]?.[kind.value] ?? ''
}

async function refreshPreview() {
  if (!template.value.trim()) {
    preview.value = { valid: true, error: null, results: [] }
    return
  }
  loading.value = true
  try {
    const res = await fetch('/api/templates/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template: template.value, kind: kind.value }),
    })
    preview.value = await res.json()
  } finally {
    loading.value = false
  }
}

// Debounce : on tape vite, l'API n'a pas besoin de suivre chaque frappe.
let timer
watch([template, kind], () => {
  clearTimeout(timer)
  timer = setTimeout(refreshPreview, 220)
})

watch(kind, () => applyPreset('jellyfin'))

function insert(text) {
  const el = editor.value
  const pos = el && el.selectionStart != null ? el.selectionStart : template.value.length
  template.value = template.value.slice(0, pos) + text + template.value.slice(pos)
  requestAnimationFrame(() => {
    el?.focus()
    el?.setSelectionRange(pos + text.length, pos + text.length)
  })
}

function onDragStart(e, token) {
  e.dataTransfer.setData('text/plain', `{${token.name}}`)
  e.dataTransfer.effectAllowed = 'copy'
}

function onDrop(e) {
  dragOver.value = false
  const text = e.dataTransfer.getData('text/plain')
  if (text) insert(text)
}

onMounted(loadCatalog)
</script>

<template>
  <div class="builder">
    <div class="toolbar">
      <div class="tabs">
        <button
          v-for="k in KINDS"
          :key="k.id"
          :class="{ active: kind === k.id }"
          @click="kind = k.id"
        >{{ k.label }}</button>
      </div>
      <div class="presets">
        <span class="hint inline">Préréglages</span>
        <button v-for="(_, family) in presets" :key="family" @click="applyPreset(family)">
          {{ family }}
        </button>
      </div>
    </div>

    <div class="grid">
      <aside class="palette">
        <h3>Jetons</h3>
        <p class="hint">Glisse-les dans le gabarit, ou clique pour insérer.</p>
        <div v-for="[group, list] in relevantGroups" :key="group" class="group">
          <div class="group-label">{{ GROUP_LABELS[group] ?? group }}</div>
          <div class="chips">
            <button
              v-for="t in list"
              :key="t.name"
              class="chip"
              draggable="true"
              :title="`Exemple : ${t.example}`"
              @dragstart="onDragStart($event, t)"
              @click="insert(`{${t.name}}`)"
            >{{ t.label }}</button>
          </div>
        </div>
      </aside>

      <section class="editor-pane">
        <h3>Gabarit</h3>
        <textarea
          ref="editor"
          v-model="template"
          rows="4"
          spellcheck="false"
          :class="{ invalid: !preview.valid, 'drag-over': dragOver }"
          @dragover.prevent="dragOver = true"
          @dragleave="dragOver = false"
          @drop.prevent="onDrop"
        ></textarea>

        <p v-if="!preview.valid" class="error">{{ preview.error }}</p>
        <p v-else class="hint">
          <code>{jeton}</code> substitue ·
          <code>{season:02}</code> zéro-padde ·
          <code>{? year: ($)}</code> n'écrit que si renseigné, <code>$</code> = la valeur
        </p>

        <h3 class="preview-title">
          Aperçu
          <span v-if="loading" class="spinner"></span>
        </h3>
        <ul class="preview">
          <li v-for="(r, i) in preview.results" :key="i">
            <div class="src">
              {{ r.values.title }}<span v-if="r.values.year"> · {{ r.values.year }}</span>
            </div>
            <div class="path">{{ r.path }}</div>
          </li>
          <li v-if="!preview.results.length && preview.valid" class="empty">
            Écris un gabarit pour voir le rendu.
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<style scoped>
.builder { display: flex; flex-direction: column; gap: 20px; }

.toolbar {
  display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 12px;
}
.tabs { display: flex; gap: 4px; }
.tabs button.active {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  border-color: var(--accent-dim);
  color: var(--accent);
}
.presets { display: flex; align-items: center; gap: 6px; }
.presets button { text-transform: capitalize; font-size: 13px; padding: 5px 10px; }

.grid { display: grid; grid-template-columns: 260px 1fr; gap: 20px; align-items: start; }
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }

h3 {
  margin: 0 0 4px; font-size: 12px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}

.hint { margin: 0 0 12px; font-size: 12px; color: var(--text-faint); }
.hint.inline { margin: 0; }
.hint code {
  font-family: var(--mono); font-size: 11px;
  background: var(--surface-2); padding: 1px 5px; border-radius: 3px; color: var(--text-dim);
}

.palette {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px;
}
.group { margin-bottom: 14px; }
.group-label {
  font-size: 11px; color: var(--text-faint); margin-bottom: 6px;
  text-transform: uppercase; letter-spacing: .05em;
}
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip {
  font-size: 12px; padding: 4px 9px; border-radius: 5px;
  background: var(--surface-2); cursor: grab;
}
.chip:active { cursor: grabbing; }

.editor-pane {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px;
}
textarea {
  width: 100%; font-family: var(--mono); font-size: 13px;
  line-height: 1.7; resize: vertical;
}
textarea.invalid { border-color: var(--err); }
textarea.drag-over {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg));
}

.error { margin: 8px 0 0; font-size: 12px; color: var(--err); }

.preview-title { margin-top: 20px; display: flex; align-items: center; gap: 8px; }
.preview { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.preview li {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 7px; padding: 9px 12px;
}
.preview .src { font-size: 11px; color: var(--text-faint); margin-bottom: 3px; }
.preview .path {
  font-family: var(--mono); font-size: 12.5px; color: var(--ok);
  word-break: break-all;
}
.preview .empty { color: var(--text-faint); font-size: 13px; border-style: dashed; }

.spinner {
  width: 9px; height: 9px; border-radius: 50%;
  border: 1.5px solid var(--border); border-top-color: var(--accent);
  animation: spin .6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
