<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

/**
 * Intégration : la clé d'API de l'installation, et l'état du dernier appel
 * reçu d'un client de téléchargement.
 *
 * Le serveur renvoie ses refus 401 vers « Réglages → Système → Intégration » :
 * ce titre de section est une adresse, il ne se renomme pas sans elle.
 */

const props = defineProps({
  /**
   * Bloc `integration` de `GET /api/settings/preferences` :
   * `{ api_key_masked, api_key_set, header }`. La clé n'y est que masquée —
   * cette réponse finit dans le cache du navigateur ; en clair, elle ne sort
   * que sur un geste explicite, par `GET /api/settings/api-key`.
   */
  integration: { type: Object, default: null },
})

/** `regenerated` : la clé vient d'être remplacée. Le parent doit relire les
 *  préférences — sinon, cette section remontée à l'ouverture de l'onglet
 *  repartirait du masque de la clé révoquée. */
const emit = defineEmits(['regenerated'])

/** Clé en clair, rendue par le serveur sur demande : `{ api_key, header,
 *  masked, example }`. Null tant qu'on n'a rien demandé, ou après « Masquer ». */
const cle = ref(null)
const lecture = ref(false)
const regeneration = ref(false)
const regeneree = ref(false)
const panneCle = ref(null)

// La version masquée suit la clé réellement en place : après une régénération,
// celle des préférences chargées à l'ouverture est déjà périmée.
const masquee = ref(props.integration?.api_key_masked ?? '')
watch(
  () => props.integration?.api_key_masked,
  (v) => {
    masquee.value = v ?? ''
  },
)

const entete = computed(() => cle.value?.header ?? props.integration?.header ?? '')

/** Une 502 arrive en HTML : lire le corps sans filet ferait lever, et le
 *  message d'échec n'arriverait jamais à l'écran. */
async function lireJson(res) {
  const brut = await res.text()
  try {
    return JSON.parse(brut)
  } catch {
    return null
  }
}

async function afficherCle() {
  lecture.value = true
  panneCle.value = null
  try {
    const res = await fetch('/api/settings/api-key')
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      panneCle.value =
        corps?.detail ?? `Le serveur n'a pas rendu la clé (réponse ${res.status}).`
      return
    }
    cle.value = corps
    masquee.value = corps.masked ?? masquee.value
    regeneree.value = false
  } catch {
    panneCle.value = "Serveur injoignable : la clé n'a pas pu être lue."
  } finally {
    lecture.value = false
  }
}

function masquerCle() {
  cle.value = null
  regeneree.value = false
}

async function regenerer() {
  regeneration.value = true
  panneCle.value = null
  try {
    const res = await fetch('/api/settings/api-key/regenerate', { method: 'POST' })
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      panneCle.value =
        (corps?.detail ?? `Régénération refusée (réponse ${res.status}).`) +
        " L'ancienne clé reste valable."
      return
    }
    cle.value = corps
    masquee.value = corps.masked ?? ''
    regeneree.value = true
    emit('regenerated')
  } catch {
    panneCle.value =
      "Serveur injoignable : impossible de savoir si la clé a été remplacée. « Afficher la clé » " +
      'montrera celle qui est réellement en place dès que le serveur répondra.'
  } finally {
    regeneration.value = false
  }
}

// --- Dernier appel reçu -------------------------------------------------------

const etat = ref(null)
const chargementEtat = ref(false)
const panneEtat = ref(null)

async function lireEtat() {
  chargementEtat.value = true
  panneEtat.value = null
  try {
    const res = await fetch('/api/integration')
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      panneEtat.value =
        corps?.detail ?? `Le serveur n'a pas rendu l'état de l'intégration (réponse ${res.status}).`
      return
    }
    etat.value = corps
  } catch {
    panneEtat.value = "Serveur injoignable : l'état de l'intégration n'a pas pu être lu."
  } finally {
    chargementEtat.value = false
  }
}

onMounted(lireEtat)

const quand = (ts) => (ts ? new Date(ts * 1000).toLocaleString('fr-FR') : '')
const appels = (n) => (n > 1 ? `${n} appels acceptés` : `${n} appel accepté`)
</script>

<template>
  <section>
    <h3>Intégration</h3>
    <p class="note">
      La clé d'API permet à un script ou à un client de téléchargement (qBittorrent, SABnzbd…)
      d'appeler Sortilège sans session ouverte — typiquement pour annoncer « ce transfert vient de
      finir, range-le ».
      <template v-if="entete">Elle se passe dans l'en-tête <code>{{ entete }}</code>.</template>
    </p>
    <p class="note capital">
      <strong>Cette clé ouvre toute l'API : elle vaut le mot de passe.</strong> Qui la détient peut
      lire et modifier les réglages, ranger, supprimer, restaurer une sauvegarde. Ne la colle que
      dans un outil de confiance, jamais dans un dépôt public ni dans un message.
    </p>

    <div class="cle-bloc">
      <span class="etiquette">Clé en place</span>
      <code v-if="cle" class="cle">{{ cle.api_key }}</code>
      <code v-else-if="masquee">{{ masquee }}</code>
      <span v-else-if="integration && !integration.api_key_set" class="hint inline">
        Aucune clé n'existe encore : « Afficher la clé » en crée une.
      </span>
      <span v-else class="hint inline">
        Le serveur n'a pas transmis l'état de la clé : « Afficher la clé » le demande.
      </span>
    </div>

    <div class="actions">
      <button v-if="!cle" type="button" :disabled="lecture || regeneration" @click="afficherCle">
        {{ lecture ? 'Lecture…' : 'Afficher la clé' }}
      </button>
      <button v-else type="button" @click="masquerCle">Masquer la clé</button>
      <ConfirmAction
        label="Régénérer la clé"
        confirm-label="Confirmer — remplacer la clé"
        detail="L'ancienne clé cesse de valoir immédiatement : tout script ou client de téléchargement qui l'utilise sera refusé jusqu'à ce que tu y colles la nouvelle."
        :busy="regeneration"
        :disabled="lecture"
        disabled-reason="Attends la fin de la lecture de la clé."
        @confirm="regenerer"
      />
    </div>
    <p v-if="lecture || regeneration" class="indispo" role="status">
      {{ lecture ? 'Lecture de la clé…' : 'Remplacement de la clé…' }}
    </p>

    <p v-if="panneCle" class="panne-inline" role="alert">{{ panneCle }}</p>
    <template v-if="cle">
      <p v-if="regeneree" class="hint attention" role="status">
        <strong>Nouvelle clé en place.</strong> L'ancienne est déjà refusée : remplace-la partout où
        elle servait.
      </p>
      <p class="hint">
        Exemple d'appel, à coller tel quel en remplaçant <code>NAS</code> par l'adresse du serveur :
      </p>
      <pre class="exemple">{{ cle.example }}</pre>
      <p class="hint">Masque la clé avant de partager une capture de cet écran.</p>
    </template>

    <h3 class="sous-titre">Dernier appel reçu</h3>
    <p v-if="chargementEtat && !etat" class="indispo" role="status">Lecture de l'état…</p>
    <div v-else-if="panneEtat" class="panne-inline" role="alert">
      <p>{{ panneEtat }}</p>
      <button type="button" @click="lireEtat">Réessayer</button>
    </div>
    <template v-else-if="etat">
      <p v-if="!etat.last_at" class="hint">
        Aucun appel reçu depuis le démarrage du serveur : aucun client n'est branché, ou il n'a
        encore rien terminé. Un appel refusé faute de clé valide n'arrive pas jusqu'ici et n'est
        donc pas compté.
      </p>
      <template v-else>
        <p class="summary">
          Dernier appel le <strong>{{ quand(etat.last_at) }}</strong
          ><template v-if="etat.last_source">, pour <code>{{ etat.last_source }}</code></template>.
        </p>
        <p v-if="etat.last_result" class="hint">Résultat : {{ etat.last_result }}</p>
        <p class="hint">{{ appels(etat.accepted ?? 0) }} depuis le démarrage du serveur.</p>
      </template>
      <p v-if="etat.pending" class="hint attention">
        Un traitement est programmé : il partira après {{ etat.grace_seconds }} s sans nouvel
        appel, pour laisser une rafale se terminer.
      </p>
      <div class="actions">
        <button type="button" :disabled="chargementEtat" @click="lireEtat">
          {{ chargementEtat ? 'Lecture…' : "Relire l'état" }}
        </button>
      </div>
    </template>
  </section>
</template>

<style scoped>
section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px;
}
h3 {
  margin: 0 0 10px; font-size: var(--t-xs); font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-title);
}
.sous-titre { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }

.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }
.note.capital strong { color: var(--warn); }

.cle-bloc { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-top: 4px; }
.etiquette { font-size: var(--t-sm); color: var(--text-dim); }
.cle { color: var(--text); user-select: all; word-break: break-all; }

.actions { margin-top: 12px; display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.actions > button { font-size: var(--t-sm); padding: 5px 12px; }

.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.hint strong { color: var(--text-dim); }
.hint.attention, .hint.attention strong { color: var(--warn); }
.hint.inline { margin: 0; }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.exemple {
  margin: 6px 0 0; padding: 9px 11px; max-width: 680px; overflow-x: auto;
  font-family: var(--mono); font-size: var(--t-xs); line-height: 1.6; color: var(--text-dim);
  white-space: pre-wrap; word-break: break-all;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px;
}

.summary { margin: 0; font-size: var(--t-md); color: var(--text-dim); }
.summary strong { color: var(--text); }

.indispo {
  margin: 10px 0 0; font-size: var(--t-xs); color: var(--text-faint);
  line-height: 1.6; max-width: 680px;
}

.panne-inline {
  margin: 12px 0 0; padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-sm); line-height: 1.6; color: var(--err); max-width: 680px;
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.panne-inline p { margin: 0 0 8px; }
.panne-inline button { font-size: var(--t-sm); }

@media (max-width: 700px) {
  .actions > button { min-height: 32px; padding: 7px 12px; }
}
</style>
