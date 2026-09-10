<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  metadata: { type: Object, required: true },
})
const emit = defineEmits(['change'])

// La clé n'est jamais renvoyée par le serveur : elle vaut un droit d'appel sur
// un quota nominatif, et cette réponse finit dans le cache du navigateur. Le
// champ reste donc vide même quand une clé est enregistrée — `tmdb_key_set` et
// `tmdb_key_from_env` disent d'où elle vient.
const draft = ref('')
const testing = ref(false)
const result = ref(null)

// Cinq langues, et pas la liste entière de TheMovieDB : au-delà, le menu
// devient un formulaire à part entière alors que le besoin réel est de choisir
// la langue de sa bibliothèque une fois pour toutes.
const LANGUES = [
  { code: 'fr-FR', label: 'Français (fr-FR)' },
  { code: 'en-US', label: 'Anglais (en-US)' },
  { code: 'es-ES', label: 'Espagnol (es-ES)' },
  { code: 'de-DE', label: 'Allemand (de-DE)' },
  { code: 'it-IT', label: 'Italien (it-IT)' },
]

/** Une langue posée hors de l'interface — fichier de préférences édité à la
 *  main, « ja-JP » — ne correspondrait à aucune option : le menu s'afficherait
 *  vide et le premier choix l'écraserait sans que personne ait voulu en
 *  changer. On l'ajoute donc à la liste plutôt que de la faire disparaître. */
const choixLangues = computed(() => {
  const courante = props.metadata.language
  if (!courante || LANGUES.some((l) => l.code === courante)) return LANGUES
  return [{ code: courante, label: `${courante} (enregistrée)` }, ...LANGUES]
})

const aUneCle = computed(
  () => props.metadata.tmdb_key_set || props.metadata.tmdb_key_from_env,
)

const placeholder = computed(() => {
  if (props.metadata.tmdb_key_set) return '•••••• enregistrée ici'
  if (props.metadata.tmdb_key_from_env) return '•••••• fournie par TMDB_API_KEY'
  return 'Colle ta clé v3 TheMovieDB'
})

function patch(fields) {
  emit('change', fields)
}

function saveKey() {
  const value = draft.value.trim()
  if (!value) return
  patch({ tmdb_api_key: value })
  draft.value = ''
  result.value = null
}

function clearKey() {
  // « - » vide explicitement le champ côté serveur : une chaîne vide y signifie
  // « ne touche pas », sans quoi changer de langue effacerait la clé.
  patch({ tmdb_api_key: '-' })
  result.value = null
}

/** Une clé peut être bien formée, enregistrée, et refusée — la confusion entre
 *  la clé v3 et le jeton v4 se lit comme un 401 muet qui ne se manifeste que
 *  plus tard, sous la forme d'un scan qui n'identifie rien. Le serveur
 *  interroge réellement TheMovieDB ; son verdict est repris tel quel, parce
 *  qu'il nomme la cause au lieu de dire « échec ». */
async function test() {
  testing.value = true
  result.value = null
  try {
    const res = await fetch('/api/settings/metadata/test', { method: 'POST' })
    const body = await res.json()
    result.value = res.ok
      ? {
          ok: true,
          text: `Clé acceptée. Recherche d'essai en ${body.language} : « ${body.sample} ».`,
        }
      : { ok: false, text: body.detail ?? 'Échec sans motif renvoyé.' }
  } catch {
    result.value = { ok: false, text: 'Serveur injoignable.' }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <section>
    <h3>Clé TheMovieDB</h3>
    <p class="note capital">
      <strong>Sans cette clé, Sortilège ne propose aucun candidat et n'identifie rien.</strong>
      C'est le seul réglage qui imposait jusqu'ici d'éditer un fichier et de redémarrer le
      conteneur : il se pose maintenant ici, et prend effet au prochain scan sans redémarrage.
    </p>
    <p class="note">
      La clé se crée sur <em>themoviedb.org → Paramètres du compte → API</em>. Prends la
      <strong>clé « API Read Access » version 3</strong>, une trentaine de caractères — pas le
      jeton v4, beaucoup plus long, que TheMovieDB refuse ici. Elle est stockée comme un
      secret : saisie une fois, jamais réaffichée.
    </p>
    <p class="note">
      La variable d'environnement <code>TMDB_API_KEY</code> reste lue <strong>en repli</strong>,
      quand aucune clé n'est enregistrée ici. Une clé posée sur cet écran a la priorité.
    </p>

    <div class="field">
      <label for="tmdb">Clé d'API</label>
      <div class="row">
        <input
          id="tmdb"
          type="password"
          autocomplete="off"
          spellcheck="false"
          :placeholder="placeholder"
          v-model="draft"
          @keyup.enter="saveKey"
        />
        <button :disabled="!draft.trim()" @click="saveKey">Enregistrer</button>
        <button v-if="metadata.tmdb_key_set" class="clear" @click="clearKey">Retirer</button>
      </div>
      <!-- Un bouton grisé sans raison affichée passe pour cassé. -->
      <p v-if="!draft.trim()" class="hint">
        « Enregistrer » attend une clé : le champ vide signifie « ne change rien », il
        n'efface pas celle qui est déjà en place.
      </p>
      <p v-if="metadata.tmdb_key_set" class="hint">
        « Retirer » efface la clé enregistrée ici. L'application retombe alors sur
        <code>TMDB_API_KEY</code> si le déploiement en porte une, et cesse d'identifier sinon.
      </p>
      <p v-else-if="metadata.tmdb_key_from_env" class="hint">
        La clé active vient de <code>TMDB_API_KEY</code>. En saisir une ici la remplacera,
        sans toucher au fichier d'environnement.
      </p>
      <p v-else class="hint attention">
        Aucune clé, ni ici ni dans l'environnement : tous les fichiers scannés resteront
        non identifiés.
      </p>
    </div>

    <div class="field">
      <label for="tmdb-lang">Langue des métadonnées</label>
      <select
        id="tmdb-lang"
        :value="metadata.language"
        @change="patch({ language: $event.target.value })"
      >
        <option v-for="l in choixLangues" :key="l.code" :value="l.code">{{ l.label }}</option>
      </select>
      <p class="hint">
        Langue des titres et des résumés récupérés. Ce n'est pas un choix cosmétique : le
        titre renvoyé est celui qui est comparé au nom du fichier. Une bibliothèque nommée
        en anglais mais interrogée en français fait chuter la similarité, donc le score, et
        remplit la file de revue d'identifications pourtant justes.
      </p>
    </div>

    <div class="test">
      <button :disabled="testing || !aUneCle" @click="test">
        {{ testing ? 'Interrogation…' : 'Tester la clé' }}
      </button>
      <span v-if="!aUneCle" class="hint inline">
        Rien à tester tant qu'aucune clé n'est enregistrée : saisis-la et enregistre d'abord.
      </span>
      <span v-else-if="result" :class="['result', result.ok ? 'ok' : 'err']">{{ result.text }}</span>
    </div>
    <p v-if="aUneCle && !result && !testing" class="hint">
      L'essai interroge réellement TheMovieDB et rapporte son refus mot pour mot — il sait
      dire qu'une clé v3 était attendue et qu'un jeton v4 a été reçu, ce qu'aucun scan raté
      ne dira jamais.
    </p>
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
.note { margin: 0 0 12px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.note strong { color: var(--text-dim); }
/* Ce réglage n'est pas un réglage parmi d'autres : sans lui, rien ne marche. */
.note.capital strong { color: var(--warn); }

.field { margin-top: 16px; }
.field > label { display: block; font-size: 11.5px; color: var(--text-dim); margin-bottom: 5px; }
.row { display: flex; gap: 8px; align-items: center; }
.row input {
  flex: 1; min-width: 0; font-size: 12.5px; padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}
select {
  font-size: 12.5px; padding: 5px 9px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text);
}
.clear { color: var(--text-faint); }
.clear:hover { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }

.hint { margin: 7px 0 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint.attention { color: var(--warn); opacity: .9; }
.hint.inline { margin: 0; }
code {
  font-family: var(--mono); font-size: 11.5px;
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.test { margin-top: 18px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.result { font-size: 12px; line-height: 1.55; max-width: 520px; }
.result.ok { color: var(--ok); }
.result.err { color: var(--err); }
</style>
