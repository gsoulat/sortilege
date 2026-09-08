<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'

const KIND_LABELS = { movie: 'Film', episode: 'Série', anime: 'Anime' }

const data = ref(null)
const view = ref('all')
const building = ref(false)
const error = ref(null)
const message = ref(null)
const opened = ref(new Set())
const trashing = ref(false)
let poller = null

const works = computed(() => {
  const all = data.value?.works ?? []
  if (view.value === 'missing') return all.filter((w) => w.missing_count > 0)
  if (view.value === 'duplicates') return all.filter((w) => w.duplicates.length)
  return all
})

const counts = computed(() => {
  const all = data.value?.works ?? []
  return {
    all: all.length,
    missing: all.filter((w) => w.missing_count > 0).length,
    duplicates: all.filter((w) => w.duplicates.length).length,
    wasted: all.reduce((n, w) => n + w.wasted_bytes, 0),
  }
})

async function load() {
  try {
    data.value = await (await fetch('/api/collection')).json()
  } catch {
    error.value = 'Serveur injoignable.'
  }
}

async function build() {
  building.value = true
  error.value = null
  message.value = null

  poller = setInterval(async () => {
    try {
      const s = await (await fetch('/api/collection/status')).json()
      if (data.value) Object.assign(data.value, s)
      else data.value = { ...s, works: [] }
    } catch {
      clearInterval(poller)
    }
  }, 700)

  try {
    await fetch('/api/collection/build', { method: 'POST' })
    await load()
  } finally {
    clearInterval(poller)
    poller = null
    building.value = false
  }
}

function toggle(key) {
  const next = new Set(opened.value)
  next.has(key) ? next.delete(key) : next.add(key)
  opened.value = next
}

/** Met en corbeille les fichiers redondants d'une œuvre — jamais une suppression. */
async function trashRedundant(work) {
  const paths = work.duplicates.flatMap((g) => g.files.filter((f) => !f.keep).map((f) => f.relative_path))
  if (!paths.length) return

  trashing.value = true
  try {
    const res = await fetch('/api/collection/duplicates/trash', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    })
    const body = await res.json()
    message.value =
      `${body.trashed} fichier(s) mis en corbeille` +
      (body.failed ? `, ${body.failed} en échec.` : '. Annulable depuis la file de revue.')
    await build()
  } finally {
    trashing.value = false
  }
}

const gb = (bytes) => `${(bytes / 1024 ** 3).toFixed(1)} Go`
const percent = computed(() => {
  const d = data.value
  if (!d?.total) return 0
  return Math.min(100, Math.round((d.processed / d.total) * 100))
})

onMounted(load)
onUnmounted(() => clearInterval(poller))
</script>

<template>
  <div class="collection">
    <div class="toolbar">
      <button class="primary" :disabled="building" @click="build">
        {{ building ? 'Analyse en cours…' : data?.count ? 'Actualiser' : 'Analyser la bibliothèque' }}
      </button>
      <span v-if="data?.built_at" class="hint">
        {{ data.count }} œuvre{{ data.count > 1 ? 's' : '' }}
      </span>
      <span v-if="counts.wasted" class="wasted">{{ gb(counts.wasted) }} en double</span>
    </div>

    <div v-if="building && data?.total" class="progress">
      <div class="bar"><div class="fill" :style="{ width: percent + '%' }"></div></div>
      <div class="stats">
        <span>{{ data.processed }} / {{ data.total }}</span>
        <span class="current">{{ data.current }}</span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-if="data?.error" class="err">{{ data.error }}</p>
    <p v-if="message" class="ok">{{ message }}</p>

    <div v-if="data?.count" class="filters">
      <button :class="{ active: view === 'all' }" @click="view = 'all'">
        Tout ({{ counts.all }})
      </button>
      <button v-if="counts.missing" class="warn" :class="{ active: view === 'missing' }" @click="view = 'missing'">
        Épisodes manquants ({{ counts.missing }})
      </button>
      <button v-if="counts.duplicates" class="warn" :class="{ active: view === 'duplicates' }" @click="view = 'duplicates'">
        Doublons ({{ counts.duplicates }})
      </button>
    </div>

    <div v-if="!data?.count && !building" class="empty">
      <h3>Bibliothèque non analysée</h3>
      <p>
        L'analyse parcourt ta bibliothèque, identifie chaque œuvre et compare avec ce que
        TheMovieDB connaît. Rien n'est déplacé.
      </p>
    </div>

    <!-- Grille -->
    <ul v-else-if="view === 'all'" class="grid">
      <li v-for="w in works" :key="w.key">
        <div class="poster">
          <img v-if="w.poster_url" :src="w.poster_url" :alt="w.title" loading="lazy" />
          <span v-else class="noposter">{{ KIND_LABELS[w.kind] }}</span>
          <span v-if="w.missing_count" class="flag warn">{{ w.missing_count }} manquant(s)</span>
          <span v-if="w.duplicates.length" class="flag dup">doublon</span>
        </div>
        <div class="label">
          <span class="title">{{ w.title }}</span>
          <span class="meta">
            <span v-if="w.year">{{ w.year }}</span>
            <span>{{ w.file_count }} fichier{{ w.file_count > 1 ? 's' : '' }}</span>
          </span>
        </div>
      </li>
    </ul>

    <!-- Manquants -->
    <ul v-else-if="view === 'missing'" class="list">
      <li v-for="w in works" :key="w.key">
        <div class="head">
          <img v-if="w.poster_url" class="thumb" :src="w.poster_url" :alt="w.title" loading="lazy" />
          <span v-else class="thumb empty"></span>
          <div>
            <div class="title">{{ w.title }}<span v-if="w.year" class="year"> ({{ w.year }})</span></div>
            <div class="sub">{{ w.owned_count }} épisode(s) · {{ w.missing_count }} manquant(s)</div>
          </div>
        </div>
        <div v-for="s in w.seasons.filter((s) => s.missing.length)" :key="s.number" class="season">
          <span class="season-label">Saison {{ String(s.number).padStart(2, '0') }}</span>
          <ul class="episodes">
            <li v-for="n in s.missing" :key="n">
              <code>E{{ String(n).padStart(2, '0') }}</code>
              <span class="ep-title">{{ s.missing_titles[String(n)] }}</span>
            </li>
          </ul>
        </div>
      </li>
    </ul>

    <!-- Doublons -->
    <ul v-else class="list">
      <li v-for="w in works" :key="w.key">
        <div class="head">
          <img v-if="w.poster_url" class="thumb" :src="w.poster_url" :alt="w.title" loading="lazy" />
          <span v-else class="thumb empty"></span>
          <div>
            <div class="title">{{ w.title }}<span v-if="w.year" class="year"> ({{ w.year }})</span></div>
            <div class="sub">{{ gb(w.wasted_bytes) }} récupérables</div>
          </div>
          <button class="trash" :disabled="trashing" @click="trashRedundant(w)">
            Mettre les doublons en corbeille
          </button>
        </div>

        <div v-for="g in w.duplicates" :key="g.label" class="dup-group">
          <span class="season-label">{{ g.label }}</span>
          <ul class="files">
            <li v-for="f in g.files" :key="f.relative_path" :class="{ keep: f.keep }">
              <span class="badge">{{ f.keep ? 'gardé' : 'redondant' }}</span>
              <code class="path">{{ f.relative_path }}</code>
              <span class="specs">
                <span v-if="f.resolution">{{ f.resolution }}</span>
                <span v-if="f.codec">{{ f.codec }}</span>
                <span>{{ gb(f.size_bytes) }}</span>
              </span>
            </li>
          </ul>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.collection { display: flex; flex-direction: column; gap: 14px; }

.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
button.primary {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  border-color: var(--accent-dim); color: var(--accent);
}
.hint { font-size: 12.5px; color: var(--text-dim); }
.wasted { margin-left: auto; font-size: 12px; color: var(--warn); }

.progress { background: var(--surface); border: 1px solid var(--accent-dim); border-radius: 9px; padding: 12px 14px; }
.bar { height: 4px; border-radius: 2px; overflow: hidden; background: var(--surface-2); margin-bottom: 8px; }
.fill { height: 100%; background: var(--accent); transition: width .4s ease; }
.stats { display: flex; gap: 12px; font-size: 12.5px; font-family: var(--mono); color: var(--text-dim); }
.stats .current { color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.err, .ok { margin: 0; font-size: 13px; border-radius: 7px; padding: 9px 12px; }
.err { color: var(--err); border: 1px solid color-mix(in srgb, var(--err) 30%, transparent); }
.ok { color: var(--ok); border: 1px solid color-mix(in srgb, var(--ok) 25%, transparent); }

.filters { display: flex; gap: 4px; flex-wrap: wrap; }
.filters button { font-size: 12px; padding: 4px 10px; }
.filters button.active { background: var(--surface-2); border-color: var(--accent-dim); color: var(--text); }
.filters button.warn { color: var(--warn); }

.empty { background: var(--surface); border: 1px dashed var(--border); border-radius: 10px; padding: 32px; text-align: center; }
.empty h3 { margin: 0 0 8px; font-size: 14px; color: var(--text); text-transform: none; letter-spacing: 0; }
.empty p { margin: 0 auto; max-width: 470px; font-size: 13px; color: var(--text-dim); line-height: 1.6; }

/* --- Grille --- */
.grid {
  list-style: none; margin: 0; padding: 0;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); gap: 14px;
}
.poster {
  position: relative; aspect-ratio: 2 / 3; border-radius: 7px; overflow: hidden;
  background: var(--surface-2); display: grid; place-items: center;
  border: 1px solid var(--border);
}
.poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
.noposter { font-size: 11px; color: var(--text-faint); }
.flag {
  position: absolute; left: 5px; font-size: 9.5px; padding: 2px 6px; border-radius: 3px;
  background: color-mix(in srgb, var(--bg) 82%, transparent); backdrop-filter: blur(3px);
}
.flag.warn { bottom: 5px; color: var(--warn); }
.flag.dup { top: 5px; color: #f9a8d4; }
.label { padding: 6px 2px 0; display: flex; flex-direction: column; gap: 2px; }
.label .title {
  font-size: 12.5px; line-height: 1.3;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.label .meta { display: flex; gap: 7px; font-size: 10.5px; color: var(--text-faint); }

/* --- Listes --- */
.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.list > li { background: var(--surface); border: 1px solid var(--border); border-radius: 9px; padding: 13px 15px; }

.head { display: flex; align-items: center; gap: 11px; margin-bottom: 10px; }
.thumb { width: 38px; height: 57px; border-radius: 4px; object-fit: cover; background: var(--surface-2); flex: none; }
.thumb.empty { border: 1px dashed var(--border); }
.head .title { font-size: 14px; font-weight: 500; }
.head .year { color: var(--text-faint); font-weight: 400; }
.head .sub { font-size: 11.5px; color: var(--text-faint); margin-top: 2px; }
.trash {
  margin-left: auto; font-size: 11.5px; padding: 4px 10px;
  color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent);
}

.season, .dup-group { margin-left: 49px; margin-top: 8px; }
.season-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--text-faint); }

.episodes { list-style: none; margin: 5px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 5px 14px; }
.episodes li { display: flex; align-items: baseline; gap: 6px; font-size: 12px; }
.episodes code { font-family: var(--mono); font-size: 11px; color: var(--warn); }
.ep-title { color: var(--text-faint); }

.files { list-style: none; margin: 5px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.files li { display: flex; align-items: center; gap: 9px; font-size: 11.5px; }
.badge {
  font-size: 9.5px; padding: 1px 6px; border-radius: 3px; flex: none;
  color: var(--text-faint); border: 1px solid var(--border);
}
.files li.keep .badge { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 30%, transparent); }
.path { font-family: var(--mono); color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.specs { margin-left: auto; display: flex; gap: 8px; color: var(--text-faint); flex: none; }
</style>
