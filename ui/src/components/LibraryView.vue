<script setup>
import { ref, computed, onMounted } from 'vue'

const KIND_LABELS = { movie: 'Film', episode: 'Série', anime: 'Anime', unknown: '?' }

const data = ref(null)
const loading = ref(false)
const error = ref(null)
const deep = ref(true)
const filter = ref('all')

const files = computed(() => {
  const all = data.value?.files ?? []
  if (filter.value === 'all') return all
  if (filter.value === 'weak') return all.filter((f) => f.parsed.quality < 0.6)
  return all.filter((f) => f.parsed.kind === filter.value)
})

const counts = computed(() => {
  const all = data.value?.files ?? []
  return {
    all: all.length,
    movie: all.filter((f) => f.parsed.kind === 'movie').length,
    episode: all.filter((f) => f.parsed.kind === 'episode').length,
    anime: all.filter((f) => f.parsed.kind === 'anime').length,
    weak: all.filter((f) => f.parsed.quality < 0.6).length,
  }
})

async function load() {
  const res = await fetch('/api/library')
  data.value = await res.json()
}

async function runScan() {
  loading.value = true
  error.value = null
  try {
    const res = await fetch(`/api/library/scan?deep=${deep.value}`, { method: 'POST' })
    if (!res.ok) {
      error.value = (await res.json()).detail ?? 'Le scan a échoué.'
      return
    }
    data.value = await res.json()
  } catch (e) {
    error.value = 'Impossible de joindre le serveur.'
  } finally {
    loading.value = false
  }
}

function duration(seconds) {
  if (!seconds) return null
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return h > 0 ? `${h} h ${String(m).padStart(2, '0')}` : `${m} min`
}

function size(bytes) {
  return `${(bytes / 1024 ** 3).toFixed(1)} Go`
}

function episodeLabel(p) {
  if (p.season != null && p.episode != null) {
    return `S${String(p.season).padStart(2, '0')}E${String(p.episode).padStart(2, '0')}`
  }
  if (p.absolute_episode != null) return `ép. ${p.absolute_episode}`
  return null
}

onMounted(load)
</script>

<template>
  <div class="library">
    <div class="toolbar">
      <div class="left">
        <button class="primary" :disabled="loading" @click="runScan">
          {{ loading ? 'Analyse en cours…' : 'Lancer un scan' }}
        </button>
        <label class="toggle">
          <input v-model="deep" type="checkbox" />
          Lire le contenu des fichiers
        </label>
        <span class="hint">durée, tags et .nfo — plus lent, bien plus fiable</span>
      </div>
      <div v-if="data?.total" class="summary">
        {{ data.total }} fichier{{ data.total > 1 ? 's' : '' }}
        <span v-if="data.skipped"> · {{ data.skipped }} ignoré{{ data.skipped > 1 ? 's' : '' }}</span>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <p v-for="(e, i) in data?.errors ?? []" :key="i" class="error">{{ e }}</p>

    <div v-if="data?.total" class="filters">
      <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
        Tout ({{ counts.all }})
      </button>
      <button v-if="counts.movie" :class="{ active: filter === 'movie' }" @click="filter = 'movie'">
        Films ({{ counts.movie }})
      </button>
      <button v-if="counts.episode" :class="{ active: filter === 'episode' }" @click="filter = 'episode'">
        Séries ({{ counts.episode }})
      </button>
      <button v-if="counts.anime" :class="{ active: filter === 'anime' }" @click="filter = 'anime'">
        Animes ({{ counts.anime }})
      </button>
      <button v-if="counts.weak" class="warn" :class="{ active: filter === 'weak' }" @click="filter = 'weak'">
        Lecture douteuse ({{ counts.weak }})
      </button>
    </div>

    <div v-if="!data?.total && !loading" class="empty">
      <h3>Aucun fichier analysé</h3>
      <p>
        Lance un scan pour voir ce que Sortilège comprend de tes fichiers.
        Aucune clé d'API n'est nécessaire à cette étape, et
        <strong>rien n'est déplacé</strong> : le scan lit, il n'écrit pas.
      </p>
    </div>

    <ul v-else class="files">
      <li v-for="f in files" :key="f.relative_path" :class="{ skipped: f.skipped_reason }">
        <div class="row-head">
          <span class="kind" :class="f.parsed.kind">{{ KIND_LABELS[f.parsed.kind] }}</span>
          <span class="title">{{ f.parsed.title || '(titre non lu)' }}</span>
          <span v-if="f.parsed.year" class="year">{{ f.parsed.year }}</span>
          <span v-if="episodeLabel(f.parsed)" class="ep">{{ episodeLabel(f.parsed) }}</span>
          <span v-if="f.probe.declared_id" class="badge id" title="Identifiant déclaré dans un .nfo ou les tags">
            {{ f.probe.declared_id }}
          </span>
          <span class="quality" :class="{ low: f.parsed.quality < 0.6 }">
            {{ (f.parsed.quality * 100).toFixed(0) }}%
          </span>
        </div>

        <div class="path">{{ f.relative_path }}</div>

        <div class="meta">
          <span v-if="f.parsed.resolution">{{ f.parsed.resolution }}</span>
          <span v-if="f.parsed.codec">{{ f.parsed.codec }}</span>
          <span v-if="f.probe.duration_seconds">{{ duration(f.probe.duration_seconds) }}</span>
          <span v-if="f.parsed.language">{{ f.parsed.language }}</span>
          <span v-if="f.parsed.fansub_group">[{{ f.parsed.fansub_group }}]</span>
          <span>{{ size(f.size_bytes) }}</span>
          <span v-if="!f.probe.probed" class="dim" title="ffprobe absent ou lecture désactivée">
            contenu non lu
          </span>
        </div>

        <div v-if="f.skipped_reason" class="skip-note">{{ f.skipped_reason }}</div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.library { display: flex; flex-direction: column; gap: 16px; }

.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
button.primary {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  border-color: var(--accent-dim); color: var(--accent);
}
.toggle { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-dim); cursor: pointer; }
.toggle input { width: auto; }
.hint { font-size: 12px; color: var(--text-faint); }
.summary { font-size: 13px; color: var(--text-dim); }

.filters { display: flex; gap: 4px; flex-wrap: wrap; }
.filters button { font-size: 12px; padding: 4px 10px; }
.filters button.active { background: var(--surface-2); border-color: var(--accent-dim); color: var(--text); }
.filters button.warn { color: var(--warn); }

.error {
  margin: 0; font-size: 13px; color: var(--err);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 30%, transparent);
  border-radius: 7px; padding: 9px 12px;
}

.empty {
  background: var(--surface); border: 1px dashed var(--border);
  border-radius: 10px; padding: 32px; text-align: center;
}
.empty h3 { margin: 0 0 8px; font-size: 14px; color: var(--text); text-transform: none; letter-spacing: 0; }
.empty p { margin: 0 auto; max-width: 460px; font-size: 13px; color: var(--text-dim); line-height: 1.6; }

.files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.files li {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 13px;
}
.files li.skipped { opacity: .5; }

.row-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kind {
  font-size: 10px; padding: 2px 7px; border-radius: 4px; letter-spacing: .04em;
  background: var(--surface-2); color: var(--text-dim);
}
.kind.movie { color: #7dd3fc; }
.kind.episode { color: #a78bfa; }
.kind.anime { color: #f9a8d4; }
.title { font-weight: 500; }
.year, .ep { font-size: 12px; color: var(--text-dim); font-family: var(--mono); }
.badge.id {
  font-size: 10px; font-family: var(--mono); padding: 2px 6px; border-radius: 4px;
  color: var(--ok); border: 1px solid color-mix(in srgb, var(--ok) 30%, transparent);
}
.quality { margin-left: auto; font-size: 11px; font-family: var(--mono); color: var(--ok); }
.quality.low { color: var(--warn); }

.path { font-family: var(--mono); font-size: 11px; color: var(--text-faint); margin-top: 3px; word-break: break-all; }
.meta { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 5px; font-size: 11px; color: var(--text-faint); }
.meta .dim { font-style: italic; }
.skip-note { margin-top: 5px; font-size: 11px; color: var(--warn); }
</style>
