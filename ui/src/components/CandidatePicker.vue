<script setup>
import { ref, computed } from 'vue'
import ImageZoom from './ImageZoom.vue'

const props = defineProps({
  candidates: { type: Array, required: true },
  busy: { type: Boolean, default: false },
  // Nécessaire pour chercher : le serveur retrouve le type d'œuvre et le
  // fournisseur à interroger à partir du plan, jamais à partir du client.
  planId: { type: String, default: '' },
})
defineEmits(['choose', 'close'])

/**
 * Les propositions viennent du titre LU dans le nom de fichier. Quand ce nom
 * est trop abîmé — un titre traduit, une abréviation, une faute du groupe de
 * release — aucune ne peut être bonne, et on voyait que c'était faux sans
 * pouvoir le corriger. D'où la recherche à la main.
 */
const requete = ref('')
const trouves = ref(null)
const cherche = ref(false)
const erreur = ref(null)

const liste = computed(() => trouves.value ?? props.candidates)

async function chercher() {
  const q = requete.value.trim()
  if (q.length < 2 || !props.planId) return
  cherche.value = true
  erreur.value = null
  try {
    const res = await fetch(`/api/review/${props.planId}/search?q=${encodeURIComponent(q)}`)
    const body = await res.json()
    if (res.ok) {
      trouves.value = body.results
      if (!body.results.length) erreur.value = `Aucun résultat pour « ${q} ».`
    } else {
      erreur.value = body.detail ?? 'Recherche impossible.'
    }
  } catch {
    erreur.value = 'Serveur injoignable.'
  } finally {
    cherche.value = false
  }
}

// Agrandissement SEPARE du choix. Ici, cliquer une carte retient l'oeuvre :
// zoomer au meme endroit rendrait le geste imprevisible, et on validerait une
// identification en croyant regarder l'affiche.
const zoom = ref(null)

function revenir() {
  trouves.value = null
  requete.value = ''
  erreur.value = null
}
</script>

<template>
  <div class="picker">
    <div class="head">
      <span class="hint">
        Choisis la bonne œuvre. Aucun signal automatique ne distingue deux titres
        identiques — l'affiche, si.
      </span>
      <button class="close" @click="$emit('close')">Fermer</button>
    </div>

    <div v-if="planId" class="recherche">
      <input
        type="search"
        placeholder="Aucune ne convient ? Cherche par titre…"
        v-model="requete"
        @keyup.enter="chercher"
      />
      <button :disabled="cherche || requete.trim().length < 2" @click="chercher">
        {{ cherche ? 'Recherche…' : 'Chercher' }}
      </button>
      <button v-if="trouves" class="retour" @click="revenir">Propositions d'origine</button>
    </div>
    <p v-if="erreur" class="err">{{ erreur }}</p>
    <p v-if="trouves && trouves.length" class="source">
      {{ trouves.length }} résultat(s) pour « {{ requete.trim() }} »
    </p>

    <ul class="grid">
      <li v-for="c in liste" :key="`${c.provider}-${c.external_id}`">
        <button class="card" :disabled="busy" @click="$emit('choose', c)">
          <div class="poster">
            <img v-if="c.poster_url" :src="c.poster_url" :alt="c.title" loading="lazy" />
            <span v-else class="noposter">sans affiche</span>
            <!-- Bouton distinct : cliquer la carte CHOISIT, la loupe agrandit. -->
            <span
              v-if="c.poster_url"
              class="loupe"
              role="button"
              title="Agrandir l'affiche"
              @click.stop.prevent="zoom = {
                src: c.poster_url,
                legende: `${c.title}${c.year ? ` (${c.year})` : ''}`,
              }"
            >⌕</span>
          </div>
          <div class="label">
            <span class="title">{{ c.title }}</span>
            <span class="meta">
              <span v-if="c.year">{{ c.year }}</span>
              <span class="provider">{{ c.provider }}</span>
            </span>
          </div>
        </button>
      </li>
    </ul>

    <ImageZoom v-if="zoom" :src="zoom.src" :legende="zoom.legende" @close="zoom = null" />
  </div>
</template>

<style scoped>
.picker {
  border: 1px solid var(--accent-dim); border-radius: 8px;
  background: var(--bg); padding: 12px 14px; margin: 8px 0 4px;
}

.head { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 11px; }
.hint { font-size: 12px; color: var(--text-faint); line-height: 1.5; flex: 1; }
.close { font-size: 11.5px; padding: 3px 9px; flex: none; }

.recherche { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.recherche input {
  flex: 1; min-width: 180px; font-size: 12.5px; padding: 5px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text);
}
.recherche button { font-size: 11.5px; padding: 3px 11px; flex: none; }
.recherche .retour { color: var(--text-faint); }
.err { margin: 0 0 9px; font-size: 12px; color: var(--err); }
.source { margin: 0 0 9px; font-size: 11.5px; color: var(--text-faint); }

.grid {
  list-style: none; margin: 0; padding: 0;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 10px;
}

.card {
  width: 100%; padding: 0; border: 1px solid var(--border);
  border-radius: 7px; overflow: hidden; background: var(--surface);
  display: flex; flex-direction: column; text-align: left;
}
.card:hover:not(:disabled) { border-color: var(--accent); }
.card:disabled { opacity: .5; }

.poster {
  position: relative;
  aspect-ratio: 2 / 3; background: var(--surface-2);
  display: grid; place-items: center; overflow: hidden;
}
.loupe {
  position: absolute; top: 4px; right: 4px;
  width: 22px; height: 22px; display: grid; place-items: center;
  font-size: 15px; line-height: 1; cursor: zoom-in;
  background: color-mix(in srgb, #000 62%, transparent); color: #fff;
  border-radius: 4px; opacity: 0; transition: opacity .12s;
}
.card:hover .loupe, .loupe:hover { opacity: 1; }
.poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
.noposter { font-size: 10px; color: var(--text-faint); }

.label { padding: 6px 8px 8px; display: flex; flex-direction: column; gap: 3px; }
.title {
  font-size: 12px; line-height: 1.3; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.meta { display: flex; gap: 6px; font-size: 10px; color: var(--text-faint); }
.provider { margin-left: auto; }
</style>
