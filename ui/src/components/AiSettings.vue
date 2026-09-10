<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'

const props = defineProps({
  ai: { type: Object, required: true },
  providers: { type: Array, required: true },
})
const emit = defineEmits(['change'])

// Saisie locale : la clé n'est jamais renvoyée par le serveur, on ne peut donc
// pas la lier au modèle. Vide = « ne change pas ».
const newKey = ref('')

// Modèles connus et diagnostic du résolveur arrivent dans la même réponse que
// le reste des préférences, mais la vue parente ne transmet que le bloc `ai` :
// on redemande ici plutôt que d'élargir son contrat pour deux champs qui ne
// servent qu'à ce composant.
const models = ref({})
const ready = ref(null)
const checking = ref(false)

async function loadContext() {
  checking.value = true
  try {
    const prefs = await (await fetch('/api/settings/preferences')).json()
    models.value = prefs.ai_models ?? {}
    ready.value = prefs.ai_ready ?? null
  } catch {
    // Serveur injoignable : pas de suggestions et pas de verdict, plutôt qu'un
    // diagnostic inventé qui enverrait chercher une panne au mauvais endroit.
    ready.value = null
  } finally {
    checking.value = false
  }
}

onMounted(loadContext)

// « Prêt » se calcule côté serveur et dépend de ce qu'on vient de changer (clé,
// modèle, fournisseur). Le parent remplace l'objet `ai` par la réponse de
// l'enregistrement : ce changement de référence est le seul signal fiable que
// la sauvegarde a abouti, donc le bon moment pour redemander le verdict.
watch(() => props.ai, loadContext)

const current = computed(
  () => props.providers.find((p) => p.key === props.ai.provider) ?? props.providers[0],
)

// Volontairement pas un <select> : la liste est figée à la publication de cette
// version, et un modèle sorti depuis doit rester saisissable sans attendre une
// mise à jour de Sortilège.
const currentModels = computed(() => models.value[props.ai.provider] ?? [])

const essai = ref(false)
const resultat = ref(null)

/** Un appel RÉEL au service, avec un cas que le déterministe ne sait pas lire. */
async function testerIa() {
  essai.value = true
  resultat.value = null
  try {
    const res = await fetch('/api/settings/ai/test', { method: 'POST' })
    const lu = await res.json().catch(() => ({}))
    if (!res.ok) {
      resultat.value = { ok: false, texte: lu.detail ?? `Échec (${res.status}).` }
      return
    }
    resultat.value = {
      ok: lu.ok,
      texte: lu.ok ? `Le service a répondu : ${lu.detail}` : lu.detail,
      envoye: lu.sent,
    }
  } catch {
    resultat.value = { ok: false, texte: 'Serveur injoignable.' }
  } finally {
    essai.value = false
  }
}

// Le modèle enregistré ne fait pas toujours partie de la liste : il peut venir
// d'une version antérieure, ou avoir été saisi à la main. Le menu bascule alors
// sur « Autre » plutôt que d'afficher un choix qui ne correspond pas à ce qui
// est réellement utilisé — ce qui serait pire que pas de menu du tout.
const modeleLibre = ref(false)
const champLibre = ref(null)

const choixModele = computed(() => {
  if (modeleLibre.value) return '__autre__'
  if (!props.ai.model) return ''
  return currentModels.value.includes(props.ai.model) ? props.ai.model : '__autre__'
})

watch(
  [() => props.ai.model, currentModels],
  ([modele, liste]) => {
    modeleLibre.value = Boolean(modele) && !liste.includes(modele)
  },
  { immediate: true },
)

function onModele(valeur) {
  if (valeur === '__autre__') {
    modeleLibre.value = true
    // Le champ n'existe pas encore au moment du choix : on attend le rendu.
    nextTick(() => champLibre.value?.focus())
    return
  }
  modeleLibre.value = false
  patch({ model: valeur })
}

const modelPlaceholder = computed(() => current.value?.default_model || 'nom du modèle')
const urlPlaceholder = computed(() => current.value?.base_url || 'https://…/v1')

function patch(fields) {
  emit('change', fields)
}

function onProvider(key) {
  // Le modèle et l'URL du fournisseur précédent n'ont aucun sens pour le
  // suivant : on les remet à vide pour retomber sur les valeurs par défaut.
  patch({ provider: key, model: '', base_url: '' })
}

function saveKey() {
  if (!newKey.value) return
  patch({ api_key: newKey.value })
  newKey.value = ''
}
</script>

<template>
  <section>
    <h3>Résolveur IA</h3>
    <p class="note">
      Sollicité uniquement quand le score descend sous le seuil ci-dessous. Il propose
      un titre corrigé qui relance une recherche chez TheMovieDB — il ne décide jamais
      seul, sa réponse repasse par le même calcul de confiance.
    </p>

    <label class="switch">
      <input
        type="checkbox"
        :checked="ai.enabled"
        @change="patch({ enabled: $event.target.checked })"
      />
      Activer le résolveur
    </label>

    <p v-if="checking" class="etat attente">
      <span class="dot"></span>
      Vérification…
    </p>
    <p v-else-if="ai.enabled && ready" :class="['etat', ready.ok ? 'ok' : 'ko']">
      <span class="dot"></span>
      <span v-if="ready.ok">Résolveur opérationnel.</span>
      <span v-else>Inutilisable en l'état : {{ ready.reason }}.</span>
    </p>

    <!-- « Opérationnel » ne dit que la forme des réglages. Une clé peut être
         valide et refusée par le service, un modèle avoir disparu, un serveur
         local ne pas répondre — et comme le résolveur dégrade vers la revue
         manuelle par construction, une configuration morte ressemble à une
         configuration qui n'a rien eu à faire. Seul un appel réel tranche. -->
    <div v-if="ai.enabled" class="essai">
      <button :disabled="essai || !ready?.ok" @click="testerIa">
        {{ essai ? 'Interrogation…' : "Tester l'IA" }}
      </button>
      <span v-if="!ready?.ok" class="indispo">
        Rien à tester tant que les réglages ne sont pas complets.
      </span>
      <span v-else-if="!resultat" class="indispo">
        Soumet un vrai cas difficile au service et montre sa réponse.
      </span>
      <span v-else :class="['resultat', resultat.ok ? 'ok' : 'ko']">{{ resultat.texte }}</span>
    </div>
    <p v-if="resultat?.envoye" class="precision">
      Cas soumis : <code>{{ resultat.envoye }}</code> — un nom abîmé, sans année. Ce qui
      compte n'est pas que la réponse soit juste, c'est que le service réponde.
    </p>

    <p class="precision">
      Opérationnel ne veut pas dire actif : le résolveur n'est appelé que pour les
      fichiers ambigus, ceux dont le score passe sous le seuil. Un lot entièrement
      identifié n'en déclenche aucun, et c'est le comportement attendu.
    </p>

    <div v-if="ai.enabled" class="fields">
      <div class="row">
        <label for="ai-provider">Fournisseur</label>
        <select id="ai-provider" :value="ai.provider" @change="onProvider($event.target.value)">
          <option v-for="p in providers" :key="p.key" :value="p.key">{{ p.label }}</option>
        </select>
      </div>
      <p class="hint">{{ current?.hint }}</p>

      <div class="row">
        <label for="ai-model">Modèle</label>
        <!-- Un vrai menu déroulant, et non une liste de suggestions : un
             datalist ne se voit pas tant qu'on ne tape rien, ce qui revient à
             une case vide devant laquelle on doit deviner un nom de modèle. -->
        <select id="ai-model" :value="choixModele" @change="onModele($event.target.value)">
          <option value="">Modèle par défaut ({{ current?.default_model || 'aucun' }})</option>
          <option v-for="m in currentModels" :key="m" :value="m">{{ m }}</option>
          <option value="__autre__">Autre — saisir le nom…</option>
        </select>
      </div>

      <!-- La liste est figée à la publication de cette version : un modèle
           sorti depuis doit rester utilisable sans attendre une mise à jour. -->
      <div v-if="modeleLibre" class="row">
        <label for="ai-model-libre">Nom exact</label>
        <input
          id="ai-model-libre"
          ref="champLibre"
          autocomplete="off"
          :value="ai.model"
          :placeholder="modelPlaceholder"
          spellcheck="false"
          @change="patch({ model: $event.target.value })"
        />
      </div>
      <p class="hint">
        Les modèles connus du fournisseur sont proposés. « Autre » laisse saisir le nom
        exact d'un modèle plus récent que cette version de Sortilège.
      </p>

      <div v-if="ai.provider !== 'anthropic'" class="row">
        <label for="ai-url">URL de base</label>
        <input
          id="ai-url"
          :value="ai.base_url"
          :placeholder="urlPlaceholder"
          spellcheck="false"
          @change="patch({ base_url: $event.target.value })"
        />
      </div>

      <div v-if="current?.needs_key" class="row">
        <label for="ai-key">Clé d'API</label>
        <div class="key">
          <input
            id="ai-key"
            v-model="newKey"
            type="password"
            autocomplete="off"
            :placeholder="ai.api_key_set ? '•••••••• (déjà enregistrée)' : 'coller la clé'"
            @keyup.enter="saveKey"
          />
          <button :disabled="!newKey" @click="saveKey">Enregistrer la clé</button>
        </div>
        <p class="hint">
          Stockée sur le NAS, jamais renvoyée au navigateur. Laisse le champ vide pour
          modifier le reste sans la ressaisir.
        </p>
      </div>

      <div class="row">
        <label for="ai-threshold">Seuil</label>
        <div class="threshold">
          <input
            id="ai-threshold"
            type="range"
            min="0"
            max="1"
            step="0.05"
            :value="ai.threshold"
            @change="patch({ threshold: Number($event.target.value) })"
          />
          <span class="value">{{ Math.round(ai.threshold * 100) }} %</span>
        </div>
      </div>
      <p class="hint">
        En dessous, le résolveur est appelé. Au-dessus, le résultat automatique est déjà
        bon et un appel n'apporterait rien — c'est ce qui borne la dépense.
      </p>
    </div>
  </section>
</template>

<style scoped>
.essai { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 10px 0 0 26px; }
.essai .indispo { font-size: 11.5px; color: var(--text-faint); }
.essai .resultat { font-size: 12px; }
.essai .resultat.ok { color: var(--ok); }
.essai .resultat.ko { color: var(--warn); }
section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px;
}
h3 {
  margin: 0 0 10px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}
.note { margin: 0 0 14px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint { margin: 2px 0 10px 101px; font-size: 11.5px; color: var(--text-faint); line-height: 1.5; max-width: 520px; }

.switch { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
.switch input { width: auto; accent-color: var(--accent); }

/* Collé sous la case à cocher, pas relégué en bas de section : c'est en cochant
   « activer » qu'on se demande pourquoi rien ne se passe. */
.etat { display: flex; align-items: center; gap: 8px; margin: 9px 0 0 26px; font-size: 12px; }
.etat .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }
.etat.ok { color: var(--ok); }
.etat.ko { color: var(--warn); }
.etat.attente { color: var(--text-faint); }
.precision { margin: 7px 0 0 26px; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 620px; }

.fields { margin-top: 14px; display: flex; flex-direction: column; }
.row { display: grid; grid-template-columns: 90px 1fr; gap: 11px; align-items: center; margin-bottom: 4px; }
.row label { font-size: 13px; color: var(--text-dim); }
.row input, .row select { font-size: 12.5px; padding: 6px 10px; width: 100%; }
.row select { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); }

.key { display: flex; gap: 8px; }
.key input { flex: 1; font-family: var(--mono); }
.key button { white-space: nowrap; font-size: 12px; }

.threshold { display: flex; align-items: center; gap: 11px; }
.threshold input { flex: 1; padding: 0; }
.value { font-family: var(--mono); font-size: 12.5px; color: var(--accent); min-width: 44px; }

@media (max-width: 700px) {
  .row { grid-template-columns: 1fr; gap: 4px; }
  .hint { margin-left: 0; }
  .etat, .precision { margin-left: 0; }
}
</style>
