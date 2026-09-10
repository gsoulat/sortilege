<script setup>
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'

/**
 * Bouton d'action destructrice en deux temps : premier clic pour armer,
 * second pour exécuter.
 *
 * L'application avait six mécanismes de confirmation écrits à la main
 * (`confirmReset`, `confirmDelete`, `confirmingDelete`, `confirmingPrune`,
 * `confirmingAll`, `confirmSuppr`), chacun avec ses propres mots, sa propre
 * mise en forme et sa propre idée de ce qu'il fallait dire — et quatre autres
 * actions qui effaçaient au premier clic sans rien demander. Six formulations
 * pour un seul geste, c'est six occasions d'apprendre la même chose. Une
 * seule ici.
 *
 * Trois règles portent ce composant :
 *
 * 1. Aucun état muet. Un bouton désactivé DIT pourquoi, à l'écran et pas
 *    seulement dans un `title` que personne ne survole — sur écran tactile
 *    ou au clavier, un `title` n'existe pas. Un bouton gris sans explication
 *    passe pour cassé, pas pour indisponible.
 * 2. Rouge au repos. Le bouton qui efface doit se voir AVANT qu'on le
 *    survole, pas après.
 * 3. L'armement expire. Un bouton rouge armé oublié au milieu d'une liste est
 *    un piège : dix secondes plus tard il redevient inoffensif tout seul.
 */

const props = defineProps({
  /** Texte au repos. Un verbe : « Supprimer », « Tout vider ». */
  label: { type: String, required: true },
  /** Texte une fois armé. À défaut, `label` est préfixé de « Confirmer — ». */
  confirmLabel: { type: String, default: '' },
  /**
   * Ce qui va réellement se passer, en une phrase, affiché seulement une fois
   * armé. C'est le seul moment où la question se pose vraiment : avant le
   * premier clic personne ne lit, après le second il est trop tard.
   */
  detail: { type: String, default: '' },
  /** Action en cours côté serveur : le bouton se verrouille et le dit. */
  busy: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  /** Pourquoi c'est indisponible. Sans elle, `disabled` est une impasse. */
  disabledReason: { type: String, default: '' },
})

const emit = defineEmits(['confirm'])

/** Assez long pour lire le détail et décider, assez court pour qu'un bouton
 *  armé puis abandonné ne survive pas à un changement d'écran. */
const DELAI_DESARMEMENT_MS = 10_000

const arme = ref(false)
let minuteur = null

const idDetail = useId()
const idRaison = useId()

function stopperMinuteur() {
  if (minuteur !== null) {
    clearTimeout(minuteur)
    minuteur = null
  }
}

function desarmer() {
  arme.value = false
  stopperMinuteur()
}

function cliquer() {
  if (props.disabled || props.busy) return

  if (!arme.value) {
    arme.value = true
    stopperMinuteur()
    minuteur = setTimeout(desarmer, DELAI_DESARMEMENT_MS)
    return
  }

  // Désarmer AVANT d'émettre : si le parent démonte la ligne en réponse,
  // le minuteur est déjà éteint et ne réveille pas un composant disparu.
  desarmer()
  emit('confirm')
}

// Si le parent désactive l'action pendant qu'elle est armée — la sélection a
// changé, la liste s'est vidée — l'armement porte sur quelque chose qui n'est
// plus là. On le laisse tomber plutôt que de le garder pointé dans le vide.
watch(
  () => props.disabled,
  (off) => { if (off) desarmer() },
)

// Un minuteur qui survit au démontage rappelle un composant détruit : Vue
// n'en meurt pas, mais on garde en vie une fermeture et son état pour rien.
onBeforeUnmount(stopperMinuteur)

const texte = computed(() => {
  if (props.busy) return `${props.label}…`
  if (!arme.value) return props.label
  return props.confirmLabel || `Confirmer — ${props.label}`
})

/** La raison ne s'affiche que si elle sert : désactivé ET expliqué. */
const raisonVisible = computed(() =>
  props.disabled && props.disabledReason ? props.disabledReason : '',
)

const detailVisible = computed(() => (arme.value && props.detail ? props.detail : ''))

const decritPar = computed(() => {
  const ids = []
  if (detailVisible.value) ids.push(idDetail)
  if (raisonVisible.value) ids.push(idRaison)
  return ids.length ? ids.join(' ') : undefined
})

defineExpose({ desarmer })
</script>

<template>
  <div class="confirm-action" :class="{ arme, indispo: disabled }">
    <div class="ligne">
      <button
        type="button"
        class="declencheur"
        :class="{ arme }"
        :disabled="disabled || busy"
        :aria-describedby="decritPar"
        @click="cliquer"
      >
        {{ texte }}
      </button>

      <!-- Sortie de secours explicite. Attendre l'expiration marche aussi,
           mais il faut pouvoir dire non tout de suite. -->
      <button v-if="arme && !busy" type="button" class="renoncer" @click="desarmer">
        Renoncer
      </button>

      <!-- La règle centrale : un bouton désactivé dit pourquoi, ici, visible. -->
      <span v-if="raisonVisible" :id="idRaison" class="raison" role="note">
        {{ raisonVisible }}
      </span>
    </div>

    <p v-if="detailVisible" :id="idDetail" class="detail" role="alert">
      {{ detailVisible }}
    </p>
  </div>
</template>

<style scoped>
.confirm-action {
  display: inline-flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
}

.ligne {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* Rouge au repos, pas seulement au survol : sur tactile et au clavier le
   survol n'arrive jamais, et le bouton qui efface resterait le plus terne de
   la rangée. Contraste du rouge sur ce fond : 5,21:1 — au-dessus du seuil AA
   de 4,5:1. */
.declencheur {
  font-size: var(--t-sm);
  padding: 5px 12px;
  color: var(--err);
  border: 1px solid color-mix(in srgb, var(--err) 45%, var(--border));
  background: color-mix(in srgb, var(--err) 8%, var(--surface-2));
}
.declencheur:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--err) 70%, transparent);
  background: color-mix(in srgb, var(--err) 16%, var(--surface-2));
}

/* Armé : le bouton change de poids, pas seulement de mots. Deux états qui ne
   diffèrent que par le texte se confondent au coin de l'œil, et c'est
   précisément au coin de l'œil qu'on clique deux fois de suite. */
.declencheur.arme {
  color: #fff;
  font-weight: 600;
  border-color: var(--err);
  background: color-mix(in srgb, var(--err) 62%, #000);
}
.declencheur.arme:hover:not(:disabled) {
  background: color-mix(in srgb, var(--err) 74%, #000);
}

.declencheur:disabled {
  color: var(--text-faint);
  border-color: var(--border);
  background: var(--surface-2);
  opacity: 1; /* La raison affichée explique l'état ; le griser en plus le rendrait illisible. */
  cursor: not-allowed;
}

.renoncer {
  font-size: var(--t-sm);
  padding: 5px 10px;
  color: var(--text-dim);
  background: transparent;
  border: 1px solid var(--border);
}
.renoncer:hover {
  color: var(--text);
  border-color: var(--accent-dim);
}

/* Pourquoi c'est indisponible. En --text-dim et non --text-faint : cette
   phrase est la seule information de la zone, elle n'a pas à être le texte le
   plus pâle de l'écran. 6,55:1 sur --surface-2. */
.raison {
  font-size: var(--t-xs);
  color: var(--text-dim);
  line-height: 1.5;
  max-width: 46ch;
}

/* Ce qui va réellement se passer. En --warn : c'est un avertissement, il
   arrive au moment où il change encore quelque chose. */
.detail {
  margin: 0;
  font-size: var(--t-xs);
  line-height: 1.55;
  color: var(--warn);
  max-width: 60ch;
  border-left: 2px solid color-mix(in srgb, var(--warn) 55%, transparent);
  padding-left: 9px;
}
</style>
