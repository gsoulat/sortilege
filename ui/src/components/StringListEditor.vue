<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  items: { type: Array, required: true },
  placeholder: { type: String, default: '' },

  // Le serveur refuse un nom de dossier contenant un séparateur, et ce refus
  // remonte comme une erreur d'enregistrement globale, loin du champ fautif.
  // On le dit ici, sous la saisie, avant de l'envoyer.
  interditChemin: { type: Boolean, default: false },

  vide: { type: String, required: true },
})
const emit = defineEmits(['update'])

const draft = ref('')

/** La comparaison se fait en minuscules parce que le scanner compare ainsi :
 *  ajouter « Sample » à côté de « sample » donnerait deux entrées pour une
 *  seule règle, et laisserait croire qu'elles font deux choses différentes. */
const doublon = computed(() => {
  const v = draft.value.trim().toLowerCase()
  return Boolean(v) && props.items.some((i) => i.toLowerCase() === v)
})

// Ce qui empêche l'ajout, en toutes lettres. Un bouton grisé qui ne dit pas
// pourquoi laisse chercher la panne dans le navigateur.
const blocage = computed(() => {
  const v = draft.value.trim()
  if (!v) return null
  if (props.interditChemin && /[/\\]/.test(v)) {
    return `« ${v} » est un chemin. Indique un NOM de dossier : l'exclusion s'applique ` +
      'à ce nom partout où il apparaît sous les sources.'
  }
  if (doublon.value) return `« ${v} » est déjà dans la liste.`
  return null
})

function ajouter() {
  const v = draft.value.trim()
  if (!v || blocage.value) return
  emit('update', [...props.items, v])
  draft.value = ''
}

function retirer(index) {
  emit('update', props.items.filter((_, i) => i !== index))
}
</script>

<template>
  <div class="liste">
    <label :for="`ajout-${label}`">{{ label }}</label>

    <ul v-if="items.length" class="puces">
      <li v-for="(valeur, i) in items" :key="`${valeur}-${i}`">
        <code>{{ valeur }}</code>
        <button class="retirer" :aria-label="`Retirer ${valeur}`" @click="retirer(i)">×</button>
      </li>
    </ul>
    <!-- Une liste vide n'est pas un vide : elle a un effet, qu'on énonce. -->
    <p v-else class="hint">{{ vide }}</p>

    <div class="row">
      <input
        :id="`ajout-${label}`"
        type="text"
        spellcheck="false"
        :placeholder="placeholder"
        v-model="draft"
        @keyup.enter="ajouter"
      />
      <button :disabled="!draft.trim() || Boolean(blocage)" @click="ajouter">Ajouter</button>
    </div>

    <p v-if="blocage" class="hint refus">{{ blocage }}</p>
    <p v-else-if="!draft.trim()" class="hint">
      « Ajouter » attend une valeur. Rien n'est enregistré tant que le champ est vide.
    </p>
  </div>
</template>

<style scoped>
.liste { display: flex; flex-direction: column; gap: 8px; }
.liste > label { font-size: 11.5px; color: var(--text-dim); }

.puces { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 7px; }
.puces li {
  display: flex; align-items: center; gap: 4px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; padding: 2px 4px 2px 8px;
}
.puces code {
  font-family: var(--mono); font-size: 11.5px; color: var(--text-dim);
  background: none; padding: 0;
}
.retirer {
  font-size: 14px; line-height: 1; padding: 1px 6px 3px;
  color: var(--text-faint); border: none; background: none;
}
.retirer:hover { color: var(--err); }

.row { display: flex; gap: 8px; align-items: center; }
.row input {
  flex: 1; min-width: 0; max-width: 320px; font-size: 12.5px; padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}

.hint { margin: 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 640px; }
.hint.refus { color: var(--warn); opacity: .9; }
</style>
