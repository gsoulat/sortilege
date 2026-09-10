<script setup>
import { computed, onMounted, ref, watch } from 'vue'

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
        <input
          id="ai-model"
          list="ai-modeles"
          autocomplete="off"
          :value="ai.model"
          :placeholder="modelPlaceholder"
          spellcheck="false"
          @change="patch({ model: $event.target.value })"
        />
        <datalist id="ai-modeles">
          <option v-for="m in currentModels" :key="m" :value="m"></option>
        </datalist>
      </div>
      <p class="hint">
        Les modèles connus du fournisseur sont proposés à la saisie. Le champ reste
        libre : un modèle plus récent que cette version de Sortilège s'y tape
        directement. Vide, le modèle par défaut du fournisseur est utilisé.
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
