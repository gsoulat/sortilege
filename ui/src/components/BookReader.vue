<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  path: { type: String, required: true },
})
const emit = defineEmits(['close'])

const info = ref(null)
const erreur = ref(null)
const chapitre = ref(0)

const url = computed(() => encodeURIComponent(props.path))

/**
 * Le contenu du livre est affiché dans un cadre BAC-À-SABLE, sans autorisation
 * d'exécution. Un EPUB peut contenir du JavaScript — le format l'autorise — et
 * un livre téléchargé n'est pas plus digne de confiance qu'une page web
 * quelconque. Le serveur nettoie déjà le HTML ; ce cadre est la seconde
 * protection, structurelle celle-là.
 */
const srcChapitre = computed(
  () => `/api/books/chapter?path=${url.value}&index=${chapitre.value}`,
)

const titreChapitre = computed(() => {
  const c = info.value?.chapters?.[chapitre.value]
  return c?.title || `Chapitre ${chapitre.value + 1}`
})

const dernier = computed(() => (info.value?.chapters?.length ?? 1) - 1)

async function charger() {
  erreur.value = null
  try {
    const res = await fetch(`/api/books/info?path=${url.value}`)
    const corps = await res.json()
    if (!res.ok) {
      erreur.value = corps.detail ?? 'Livre illisible.'
      return
    }
    info.value = corps
    // Reprendre où on s'était arrêté. Une liseuse qui rouvre toujours à la
    // page 1 oblige à retrouver sa place à la main, ce qui suffit à ne plus
    // s'en servir.
    const memoire = Number(localStorage.getItem(`livre:${props.path}`) ?? 0)
    chapitre.value = Number.isFinite(memoire) ? Math.max(0, memoire) : 0
  } catch {
    erreur.value = 'Serveur injoignable.'
  }
}

watch(chapitre, (n) => {
  try {
    localStorage.setItem(`livre:${props.path}`, String(n))
  } catch {
    // Navigation privée, quota plein : perdre la page courante n'est pas une
    // raison d'empêcher la lecture.
  }
})

function precedent() {
  if (chapitre.value > 0) chapitre.value -= 1
}

function suivant() {
  if (chapitre.value < dernier.value) chapitre.value += 1
}

function auClavier(e) {
  if (e.key === 'Escape') emit('close')
  else if (e.key === 'ArrowLeft') precedent()
  else if (e.key === 'ArrowRight') suivant()
}

onMounted(() => {
  charger()
  window.addEventListener('keydown', auClavier)
})
onUnmounted(() => window.removeEventListener('keydown', auClavier))
</script>

<template>
  <div class="voile" @click.self="emit('close')">
    <div class="liseuse">
      <header>
        <div class="identite">
          <strong>{{ info?.title ?? 'Chargement…' }}</strong>
          <span v-if="info?.author" class="auteur">{{ info.author }}</span>
          <span v-if="info?.series" class="serie">
            {{ info.series }}<template v-if="info.volume"> — tome {{ info.volume }}</template>
          </span>
        </div>
        <button class="small" @click="emit('close')">Fermer</button>
      </header>

      <p v-if="erreur" class="erreur">{{ erreur }}</p>

      <template v-else-if="info?.format === 'pdf'">
        <!-- Le navigateur sait afficher un PDF : lui imposer un lecteur maison
             serait moins bon et bien plus lourd. -->
        <iframe class="page" :src="`/api/books/file?path=${url}`" title="Livre"></iframe>
      </template>

      <template v-else-if="info?.readable">
        <iframe
          class="page"
          :src="srcChapitre"
          sandbox=""
          referrerpolicy="no-referrer"
          :title="titreChapitre"
        ></iframe>
        <footer>
          <button class="small" :disabled="chapitre === 0" @click="precedent">← Précédent</button>
          <select v-model.number="chapitre">
            <option v-for="c in info.chapters" :key="c.index" :value="c.index">
              {{ c.title || `Chapitre ${c.index + 1}` }}
            </option>
          </select>
          <span class="position">{{ chapitre + 1 }} / {{ info.chapters.length }}</span>
          <button class="small" :disabled="chapitre >= dernier" @click="suivant">Suivant →</button>
        </footer>
      </template>

      <p v-else-if="info" class="erreur">
        Format « {{ info.format }} » : Sortilège sait le ranger, pas l'ouvrir. Les liseuses
        et applications dédiées le lisent — le fichier est rangé, il suffit d'y accéder.
      </p>
    </div>
  </div>
</template>

<style scoped>
.voile {
  position: fixed; inset: 0; z-index: 60; display: flex;
  align-items: center; justify-content: center; padding: 24px;
  background: rgba(6, 6, 10, .82); backdrop-filter: blur(3px);
}
.liseuse {
  display: flex; flex-direction: column; width: min(880px, 100%); height: min(90vh, 100%);
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  overflow: hidden;
}
header {
  display: flex; align-items: center; gap: 12px;
  padding: 11px 14px; border-bottom: 1px solid var(--border);
}
.identite { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.identite strong { font-size: 13.5px; }
.auteur, .serie { font-size: 11.5px; color: var(--text-faint); }
header button { margin-left: auto; }
.page { flex: 1; width: 100%; border: 0; background: #14141b; }
footer {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 14px; border-top: 1px solid var(--border);
}
footer select {
  flex: 1; min-width: 0; font-size: 12px; padding: 4px 8px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text);
}
.position { font-size: 11.5px; color: var(--text-faint); font-family: var(--mono); }
.erreur { margin: auto; padding: 28px; font-size: 12.5px; color: var(--text-dim); max-width: 40em; line-height: 1.7; }
</style>
