<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  automation: { type: Object, required: true },
})
const emit = defineEmits(['change'])

const status = ref(null)
const running = ref(false)
let poller = null

async function load() {
  try {
    status.value = await (await fetch('/api/automation')).json()
  } catch {
    status.value = null
  }
}

async function runNow() {
  running.value = true
  try {
    await fetch('/api/automation/run', { method: 'POST' })
  } finally {
    running.value = false
    await load()
  }
}

function patch(fields) {
  emit('change', fields)
}

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString('fr-FR') : '—')

onMounted(() => {
  load()
  poller = setInterval(load, 5000)
})
onUnmounted(() => clearInterval(poller))
</script>

<template>
  <section>
    <h3>Traitement automatique</h3>
    <p class="note">
      Détecte les nouveaux fichiers, scanne, identifie, et remplit la file — la même
      chaîne que les boutons, déclenchée par l'horloge. Un fichier n'est pris que
      lorsqu'il a cessé de grossir : traiter un téléchargement en cours déplacerait
      un fichier incomplet.
    </p>

    <label class="switch">
      <input
        type="checkbox"
        :checked="automation.enabled"
        @change="patch({ enabled: $event.target.checked })"
      />
      Activer la surveillance
    </label>

    <div v-if="automation.enabled" class="fields">
      <div class="row">
        <label for="a-interval">Intervalle</label>
        <div class="inline">
          <input
            id="a-interval"
            type="number"
            min="1"
            max="1440"
            :value="automation.interval_minutes"
            @change="patch({ interval_minutes: Number($event.target.value) })"
          />
          <span class="unit">minutes</span>
        </div>
      </div>

      <div class="row">
        <label for="a-quiet">Délai de stabilité</label>
        <div class="inline">
          <input
            id="a-quiet"
            type="number"
            min="0"
            max="3600"
            step="30"
            :value="automation.quiet_seconds"
            @change="patch({ quiet_seconds: Number($event.target.value) })"
          />
          <span class="unit">secondes sans modification</span>
        </div>
      </div>

      <label class="switch indent">
        <input
          type="checkbox"
          :checked="automation.apply_auto"
          @change="patch({ apply_auto: $event.target.checked })"
        />
        Déplacer réellement les fichiers sûrs
      </label>
      <p class="hint">
        Décoché, la boucle prépare la file et s'arrête là — tu gardes la main sur
        chaque déplacement. Coché, les plans au-dessus du seuil sont appliqués sans
        intervention. Le journal permet toujours de revenir en arrière.
      </p>
    </div>

    <div v-if="status" class="status">
      <div class="line">
        <span class="dot" :class="{ live: status.running }"></span>
        <span v-if="status.running">Cycle en cours…</span>
        <span v-else-if="status.enabled">
          Dernier passage {{ when(status.last_run) }} · prochain vers {{ when(status.next_run) }}
        </span>
        <span v-else class="off">Surveillance désactivée</span>
        <button class="run" :disabled="running || status.running" @click="runNow">
          {{ running ? 'En cours…' : 'Lancer maintenant' }}
        </button>
      </div>

      <p v-if="status.error" class="err">{{ status.error }}</p>

      <ul v-if="status.history?.length" class="history">
        <!-- Un cycle en échec se lisait comme les autres : même couleur, même
             ligne, seul le message changeait. Il se voit désormais. -->
        <li
          v-for="(h, i) in status.history.slice(0, 5)"
          :key="i"
          :class="{ echec: h.failed }"
        >
          <span class="time">{{ when(h.at) }}</span>
          <span v-if="h.failed" class="statut-echec">Échec</span>
          <span class="msg">{{ h.message }}</span>
          <span v-if="h.detected" class="counts">{{ h.detected }} détecté(s)</span>
        </li>
      </ul>
      <p v-else class="history-vide">Aucun passage depuis le démarrage du serveur.</p>
    </div>
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
.note { margin: 0 0 14px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint { margin: 4px 0 0 26px; font-size: var(--t-xs); color: var(--text-faint); line-height: 1.5; max-width: 560px; }

.switch { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
.switch input { width: auto; }
.switch.indent { margin-top: 10px; }

.fields { margin-top: 14px; }
.row { display: grid; grid-template-columns: 140px 1fr; gap: 11px; align-items: center; margin-bottom: 8px; }
.row label { font-size: 13px; color: var(--text-dim); }
.inline { display: flex; align-items: center; gap: 9px; }
.inline input { width: 90px; font-size: 12.5px; padding: 6px 10px; }
.unit { font-size: 12px; color: var(--text-faint); }

.status { margin-top: 16px; padding-top: 13px; border-top: 1px solid var(--border); }
.status .line { display: flex; align-items: center; gap: 9px; font-size: 12.5px; color: var(--text-dim); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); flex: none; }
.dot.live { background: var(--ok); }
.off { color: var(--text-faint); }
.run { margin-left: auto; font-size: var(--t-xs); padding: 3px 10px; }

.err { margin: 8px 0 0; font-size: 12px; color: var(--err); }

.history { list-style: none; margin: 11px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.history li { display: flex; gap: 10px; font-size: var(--t-xs); color: var(--text-faint); }
.history .time { font-family: var(--mono); flex: none; }
.history .counts { margin-left: auto; }
.history li.echec { color: var(--err); }
.history .statut-echec { flex: none; font-weight: 600; }
.history-vide { margin: 11px 0 0; font-size: var(--t-xs); color: var(--text-faint); }

@media (max-width: 700px) {
  .row { grid-template-columns: 1fr; gap: 4px; }
}
</style>
