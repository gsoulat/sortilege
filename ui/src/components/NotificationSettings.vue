<script setup>
import { ref } from 'vue'

const props = defineProps({
  notifications: { type: Object, required: true },
})
const emit = defineEmits(['change'])

// L'URL n'est jamais renvoyée par le serveur : elle vaut un droit d'écriture
// sur le canal. Le champ reste donc vide même quand un webhook est enregistré,
// et `webhook_set` dit s'il y en a un.
const draft = ref('')
const testing = ref(false)
const result = ref(null)

function patch(fields) {
  emit('change', fields)
}

// Enregistrer un webhook ET activer d'un seul geste. Séparer les deux laissait
// la case « Activer » grisée tant qu'aucune URL n'était enregistrée : on ne
// pouvait pas la cocher, et rien ne disait clairement pourquoi. Coller une URL
// de webhook, c'est vouloir des notifications — le second clic n'apprenait rien
// à personne.
function saveUrl() {
  const value = draft.value.trim()
  if (!value) return
  patch({ webhook_url: value, enabled: true })
  draft.value = ''
  result.value = null
}

function clearUrl() {
  // « - » vide explicitement le champ côté serveur : une chaîne vide y signifie
  // « ne touche pas », sans quoi couper les notifications l'effacerait.
  patch({ webhook_url: '-', enabled: false })
  result.value = null
}

/** Un webhook peut être valide de forme et révoqué chez Discord. Seul un envoi
 *  réel le dit — le bouton est là pour ça, et rapporte l'échec tel quel. */
async function test() {
  testing.value = true
  result.value = null
  try {
    const res = await fetch('/api/settings/notifications/test', { method: 'POST' })
    const body = await res.json()
    result.value = res.ok
      ? { ok: true, text: 'Message envoyé. Regarde ton canal Discord.' }
      : { ok: false, text: body.detail ?? 'Échec.' }
  } catch {
    result.value = { ok: false, text: 'Serveur injoignable.' }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <section>
    <h3>Notifications Discord</h3>
    <p class="note">
      Le rangement automatique tourne sans personne devant l'écran. Sortilège dit dans
      un canal ce qu'il a fait — et surtout ce qui a échoué, puisque c'est précisément
      ce que personne ne va voir autrement.
    </p>
    <p class="note">
      <strong>Un message uniquement quand un fichier a été rangé.</strong> Un cycle qui
      détecte sans pouvoir ranger, ou qui met des fichiers en attente d'arbitrage, ne dit
      rien : ça s'attend dans l'application, et un message toutes les quinze minutes
      finit par ne plus être lu du tout.
    </p>

    <div class="field">
      <label for="webhook">URL du webhook</label>
      <div class="row">
        <input
          id="webhook"
          type="password"
          autocomplete="off"
          :placeholder="notifications.webhook_set ? '•••••• enregistré' : 'https://discord.com/api/webhooks/…'"
          v-model="draft"
          @keyup.enter="saveUrl"
        />
        <button :disabled="!draft.trim()" @click="saveUrl">Enregistrer</button>
        <button v-if="notifications.webhook_set" class="clear" @click="clearUrl">Retirer</button>
      </div>
      <p class="hint">
        Dans Discord : <em>Paramètres du salon → Intégrations → Webhooks → Nouveau webhook</em>,
        puis « Copier l'URL ». Elle donne le droit d'écrire dans ce salon, donc elle est
        stockée comme une clé : saisie une fois, jamais réaffichée.
      </p>
    </div>

    <label class="switch" :class="{ bloque: !notifications.webhook_set }">
      <input
        type="checkbox"
        :checked="notifications.enabled"
        :disabled="!notifications.webhook_set"
        @change="patch({ enabled: $event.target.checked })"
      />
      Activer les notifications
      <span v-if="!notifications.webhook_set" class="hint">
        — impossible tant qu'aucun webhook n'est enregistré : il n'y aurait nulle part où écrire
      </span>
    </label>

    <label class="switch sub">
      <input
        type="checkbox"
        :checked="notifications.on_failure"
        @change="patch({ on_failure: $event.target.checked })"
      />
      Prévenir aussi quand un cycle échoue
    </label>

    <div v-if="notifications.webhook_set" class="test">
      <button :disabled="testing" @click="test">
        {{ testing ? 'Envoi…' : 'Envoyer un message d\'essai' }}
      </button>
      <span v-if="result" :class="['result', result.ok ? 'ok' : 'err']">{{ result.text }}</span>
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

.switch { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 8px; }
.switch.sub { margin-left: 22px; color: var(--text-dim); }
.switch input { accent-color: var(--accent); }
/* Une case qu'on ne peut pas cocher doit se VOIR : sans cela, elle passe pour
   cassée plutôt que pour indisponible. */
.switch.bloque { color: var(--text-faint); cursor: not-allowed; }
.switch.bloque input { cursor: not-allowed; }
.switch .hint { margin: 0; }

.field { margin-top: 14px; }
.field > label { display: block; font-size: 11.5px; color: var(--text-dim); margin-bottom: 5px; }
.row { display: flex; gap: 8px; align-items: center; }
.row input {
  flex: 1; min-width: 0; font-size: 12.5px; padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}
.clear { color: var(--text-faint); }
.clear:hover { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }

.hint { margin: 7px 0 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.switch .hint { margin: 0; }

.test { margin-top: 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.result { font-size: 12px; }
.result.ok { color: var(--ok); }
.result.err { color: var(--err); }
</style>
