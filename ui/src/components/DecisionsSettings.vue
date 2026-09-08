<script setup>
import { ref, onMounted } from 'vue'

const KIND_LABELS = { movie: 'Film', episode: 'Série', anime: 'Anime' }

const decisions = ref([])
const error = ref(null)

async function load() {
  try {
    decisions.value = (await (await fetch('/api/settings/decisions')).json()).decisions
  } catch {
    error.value = 'Serveur injoignable.'
  }
}

async function forget(d) {
  const res = await fetch(
    `/api/settings/decisions/${encodeURIComponent(d.kind)}/${encodeURIComponent(d.title_key)}`,
    { method: 'DELETE' },
  )
  if (res.ok) decisions.value = (await res.json()).decisions
}

onMounted(load)
</script>

<template>
  <section>
    <h3>Identifications retenues</h3>
    <p class="note">
      Chaque fois que tu tranches entre deux œuvres, le choix est retenu et la question
      n'est plus reposée aux scans suivants. C'est ce qui fait converger la file de revue
      au lieu de la voir se remplir à l'identique.
    </p>

    <p v-if="error" class="err">{{ error }}</p>

    <p v-if="!decisions.length" class="empty">
      Aucune pour l'instant. Elles apparaîtront dès que tu utiliseras « Ce n'est pas ça ».
    </p>

    <ul v-else class="list">
      <li v-for="d in decisions" :key="`${d.kind}-${d.title_key}`">
        <img v-if="d.poster_url" class="thumb" :src="d.poster_url" :alt="d.title" loading="lazy" />
        <span v-else class="thumb empty"></span>
        <div class="body">
          <div class="title">
            {{ d.title }}<span v-if="d.year" class="year"> ({{ d.year }})</span>
          </div>
          <div class="meta">
            <span class="kind">{{ KIND_LABELS[d.kind] ?? d.kind }}</span>
            <code>{{ d.provider }}:{{ d.external_id }}</code>
            <span v-if="d.hits" class="hits">réutilisée {{ d.hits }} fois</span>
          </div>
        </div>
        <button class="forget" title="La question sera reposée au prochain scan" @click="forget(d)">
          Oublier
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px;
}
h3 {
  margin: 0 0 10px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}
.note { margin: 0 0 14px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.empty { margin: 0; font-size: 12.5px; color: var(--text-faint); font-style: italic; }
.err { margin: 0 0 10px; font-size: 12px; color: var(--err); }

.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 9px; }
.list li { display: flex; align-items: center; gap: 11px; }

.thumb { width: 32px; height: 48px; border-radius: 4px; object-fit: cover; background: var(--surface-2); flex: none; }
.thumb.empty { border: 1px dashed var(--border); }

.body { flex: 1; min-width: 0; }
.title { font-size: 13px; }
.year { color: var(--text-faint); }
.meta { display: flex; gap: 9px; align-items: baseline; margin-top: 2px; font-size: 11px; color: var(--text-faint); flex-wrap: wrap; }
.meta code { font-family: var(--mono); font-size: 10.5px; background: var(--surface-2); padding: 1px 5px; border-radius: 3px; }
.hits { color: var(--ok); }

.forget { font-size: 11.5px; padding: 3px 10px; color: var(--text-faint); flex: none; }
.forget:hover { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }
</style>
