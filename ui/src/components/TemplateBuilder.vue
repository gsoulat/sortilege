<script setup>
import { ref, computed, watch, onMounted } from 'vue'

const KINDS = [
  { id: 'movie', label: 'Films' },
  { id: 'episode', label: 'Séries TV' },
  { id: 'anime', label: 'Animes' },
  { id: 'book', label: 'Livres' },
]

const GROUP_LABELS = {
  identite: 'Identité',
  livre: 'Livre',
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
  // Un livre n'a ni saison, ni résolution, ni codec : lui proposer ces jetons
  // ferait construire des chemins avec des segments toujours vides.
  if (kind.value === 'book') {
    return entries.filter(([g]) => g === 'identite' || g === 'livre')
  }
  // Et réciproquement : {auteur} ou {isbn} n'ont rien à faire dans le gabarit
  // d'un film.
  const sansLivre = entries.filter(([g]) => g !== 'livre')
  if (kind.value === 'movie') return sansLivre.filter(([g]) => g !== 'episode')
  return sansLivre
})

async function loadCatalog() {
  const res = await fetch('/api/tokens')
  const data = await res.json()
  tokens.value = data.tokens
  presets.value = data.presets
  await chargerEnregistres()
}

/**
 * Le gabarit RÉELLEMENT en vigueur, pas un préréglage.
 *
 * L'écran partait systématiquement du préréglage Jellyfin : on croyait
 * modifier son gabarit alors qu'on en composait un autre par-dessus, et le
 * sien n'était visible nulle part.
 */
const enregistres = ref({})

async function chargerEnregistres() {
  try {
    const prefs = await (await fetch('/api/settings/preferences')).json()
    enregistres.value = prefs.templates ?? {}
  } catch {
    enregistres.value = {}
  }
  template.value = enregistres.value[kind.value] || presets.value.jellyfin?.[kind.value] || ''
}

const modifie = computed(() => template.value !== (enregistres.value[kind.value] ?? ''))

/**
 * Enregistre le gabarit du type courant.
 *
 * Il manquait purement et simplement : on composait, l'aperçu marchait, on
 * changeait d'onglet, tout était perdu. Le serveur savait pourtant le
 * conserver — la préférence était envoyée vide à chaque sauvegarde.
 */
async function enregistrer() {
  if (!preview.value?.valid) return
  sauvegarde.value = true
  retour.value = null
  try {
    const res = await fetch('/api/settings/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ templates: { [kind.value]: template.value } }),
    })
    const corps = await res.json()
    if (res.ok) {
      enregistres.value = { ...enregistres.value, [kind.value]: template.value }
      retour.value = { ok: true, texte: 'Gabarit enregistré. Il servira au prochain rangement.' }
    } else {
      retour.value = { ok: false, texte: corps.detail ?? 'Enregistrement refusé.' }
    }
  } catch {
    retour.value = { ok: false, texte: 'Serveur injoignable.' }
  } finally {
    sauvegarde.value = false
  }
}

const sauvegarde = ref(false)
const retour = ref(null)

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

// Changer de type recharge le gabarit ENREGISTRÉ pour ce type, pas le
// préréglage : sinon on écrase sans le voir ce qu'on avait posé.
watch(kind, () => {
  retour.value = null
  template.value = enregistres.value[kind.value] || presets.value.jellyfin?.[kind.value] || ''
})

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
        <span class="spacer"></span>
        <button
          class="primary"
          :disabled="sauvegarde || !modifie || !preview?.valid"
          @click="enregistrer"
        >
          {{ sauvegarde ? 'Enregistrement…' : 'Enregistrer ce gabarit' }}
        </button>
      </div>
      <!-- Un bouton désactivé dit pourquoi : la règle vaut ici comme ailleurs. -->
      <p v-if="retour" :class="['retour', retour.ok ? 'ok' : 'ko']">{{ retour.texte }}</p>
      <p v-else-if="!modifie" class="hint">
        Ce gabarit est celui qui sert aujourd'hui à ranger les {{ KINDS.find((k) => k.id === kind)?.label.toLowerCase() }}.
      </p>
      <p v-else-if="!preview?.valid" class="hint">
        Corrige le gabarit avant d'enregistrer : l'aperçu ci-dessous dit ce qui cloche.
      </p>
      <p v-else class="hint">Modifié — enregistre pour que ça serve au prochain rangement.</p>
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
.presets .spacer { flex: 1; }
.retour { margin: 8px 0 0; font-size: 12.5px; }
.retour.ok { color: var(--ok, var(--accent)); }
.retour.ko { color: var(--warn); }
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
