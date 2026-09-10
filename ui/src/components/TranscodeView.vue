<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

/**
 * Réencodage différé, sur son propre écran.
 *
 * Il vivait en onglet interne de la médiathèque, ce qui posait mal la question :
 * on n'y vient pas pour regarder ce qu'on possède, on y vient pour surveiller un
 * travail long. C'est un atelier, pas une vue de bibliothèque — et un atelier se
 * consulte le matin pour savoir ce que la nuit a produit.
 */
const donnees = ref(null)
const erreur = ref(null)
const chargement = ref(false)
const busy = ref(null)
const message = ref(null)
const playing = ref(null)

const ETATS = {
  queued: 'en attente',
  running: 'en cours',
  done: 'à vérifier',
  failed: 'échec',
  replaced: 'remplacé',
  discarded: 'jeté',
}

function gb(octets) {
  return ((octets ?? 0) / 1024 ** 3).toFixed(1)
}

async function charger() {
  chargement.value = true
  try {
    const res = await fetch('/api/transcode')
    // Une réponse 502 arrive en HTML : `json()` lèverait sans être attrapée, et
    // l'écran resterait figé sans un mot.
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    donnees.value = await res.json()
    erreur.value = null
  } catch (e) {
    // Ce qui est déjà affiché reste : la file tourne côté serveur, c'est
    // l'affichage qui manque. L'effacer ferait croire à une file vide, soit
    // l'inverse de ce qui se passe.
    erreur.value = e.message ?? 'sans réponse'
  } finally {
    chargement.value = false
  }
}

async function appel(url, corps = null) {
  message.value = null
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: corps ? JSON.stringify(corps) : '{}',
    })
    const lu = await res.json().catch(() => ({}))
    if (!res.ok) {
      message.value = { ok: false, texte: lu.detail ?? `Échec (${res.status}).` }
      return null
    }
    return lu
  } catch {
    message.value = { ok: false, texte: 'Serveur injoignable.' }
    return null
  }
}

async function enfiler() {
  busy.value = 'queue'
  try {
    const out = await appel('/api/transcode/queue', { all: true })
    if (out) {
      donnees.value = out
      message.value = {
        ok: true,
        texte:
          `${out.queued} fichier(s) en file.` +
          (out.rejected?.length ? ` ${out.rejected.length} ignoré(s).` : ''),
      }
    }
  } finally {
    busy.value = null
  }
}

/**
 * Installe le fichier réencodé à la place de l'original.
 *
 * L'original part en corbeille, donc le geste se défait — mais les détails
 * perdus à l'encodage, eux, ne reviennent pas. D'où la confirmation.
 */
async function remplacer(job) {
  busy.value = `replace:${job.id}`
  try {
    const out = await appel(`/api/transcode/${job.id}/replace`)
    if (out) {
      donnees.value = out
      message.value = { ok: true, texte: `Remplacé — ${gb(job.savings_bytes)} Go rendus.` }
    }
  } finally {
    busy.value = null
  }
}

async function jeter(job) {
  busy.value = `discard:${job.id}`
  try {
    const out = await appel(`/api/transcode/${job.id}/discard`)
    if (out) {
      donnees.value = out
      message.value = { ok: true, texte: 'Résultat jeté. L’original n’a jamais bougé.' }
    }
  } finally {
    busy.value = null
  }
}

async function retirer(job) {
  const out = await appel(`/api/transcode/${job.id}/cancel`)
  if (out) donnees.value = out
}

function basculerLecteur(id) {
  playing.value = playing.value === id ? null : id
}

const candidats = computed(() => donnees.value?.candidates ?? null)
const travaux = computed(() => donnees.value?.jobs ?? [])

/** Ce qui attend une décision humaine, et qui est la raison de venir ici. */
const aVerifier = computed(() => travaux.value.filter((j) => j.state === 'done').length)

/**
 * Pourquoi la mise en file est indisponible. La règle du projet : un bouton
 * grisé dit sa raison, à l'écran, jamais dans un attribut que le doigt et le
 * clavier ne voient pas.
 */
const raisonEnfiler = computed(() => {
  if (busy.value === 'queue') return 'Mise en file en cours…'
  if (!donnees.value) return 'La file n’a pas encore été lue.'
  if (!candidats.value?.count) return 'Aucun fichier ne dépasse ce que ta stratégie demande.'
  return ''
})

// Le réencodage dure des heures : un rafraîchissement toutes les cinq secondes
// suffit largement, là où la vue de rangement en demande un toutes les deux.
let minuteur
onMounted(() => {
  charger()
  minuteur = setInterval(charger, 5000)
})
onUnmounted(() => clearInterval(minuteur))
</script>

<template>
  <div v-if="!donnees && chargement" class="attente">
    <span class="pulsation"></span>
    Lecture de la file de réencodage…
  </div>

  <div v-else-if="!donnees" class="panne">
    <h2>La file de réencodage n'a pas pu être lue</h2>
    <p>{{ erreur ?? 'Aucune réponse de Sortilège.' }}</p>
    <p class="quoi-faire">
      Ce qui est déjà en file continue de tourner côté serveur : c'est l'affichage qui
      manque, pas le travail.
    </p>
    <button class="primary" :disabled="chargement" @click="charger">Réessayer</button>
  </div>

  <div v-else class="atelier">
    <header>
      <h2>Réencodage différé</h2>
      <p class="note">
        Ce que ta stratégie voudrait plus léger est encodé <strong>la nuit</strong>, un
        fichier à la fois, <strong>à côté</strong> de l'original. Rien n'est remplacé sans
        ton accord : le lendemain tu regardes le résultat et tu décides.
      </p>
      <p class="note attention">
        C'est la seule opération que rien ne défait. L'original part en corbeille et reste
        récupérable, mais les détails perdus à l'encodage, eux, ne reviennent pas.
      </p>
    </header>

    <p v-if="erreur" class="panne-inline">
      Dernière lecture échouée ({{ erreur }}) : cette liste date de la précédente.
      <button class="small" :disabled="chargement" @click="charger">Actualiser</button>
    </p>

    <div class="etat">
      <span class="puce" :class="donnees.ffmpeg ? 'ok' : 'ko'">
        {{ donnees.ffmpeg ? 'ffmpeg présent' : 'ffmpeg absent — rien ne pourra être encodé' }}
      </span>
      <span class="puce" :class="donnees.settings.enabled ? 'ok' : 'ko'">
        {{ donnees.settings.enabled ? 'activé' : 'désactivé dans les réglages' }}
      </span>
      <span class="puce">
        fenêtre {{ donnees.settings.start_hour }} h → {{ donnees.settings.end_hour }} h
        <template v-if="donnees.in_window">(on y est)</template>
        <template v-else>(hors plage, la file attend l'heure)</template>
      </span>
      <span class="puce">{{ donnees.settings.codec }} · CRF {{ donnees.settings.crf }}</span>
      <span v-if="aVerifier" class="puce a-voir">{{ aVerifier }} à vérifier</span>
    </div>

    <p v-if="message" :class="['retour', message.ok ? 'ok' : 'ko']">{{ message.texte }}</p>

    <div v-if="candidats?.count" class="lot">
      <span class="warn-text">
        {{ candidats.count }} fichier(s) ne respectent pas ta stratégie —
        environ {{ gb(candidats.recoverable_bytes) }} Go récupérables.
      </span>
      <button class="small" :disabled="!!raisonEnfiler" @click="enfiler">
        Tout mettre en file
      </button>
      <span v-if="raisonEnfiler" class="indispo" role="status">{{ raisonEnfiler }}</span>
    </div>

    <!-- « Rien à réencoder » est vrai mais inutile : quelqu'un qui vient de
         régler ses séries et n'en voit aucune proposée ne peut pas savoir si ses
         fichiers sont conformes ou si sa stratégie ne demandera jamais rien. Le
         cas le plus fréquent est le second. -->
    <div v-else class="rien">
      <p class="empty">Rien à réencoder pour l'instant.</p>
      <ul class="par-type">
        <li v-for="r in candidats?.by_kind ?? []" :key="r.kind">
          <strong>{{ r.label }}</strong>
          <span class="puce">{{ r.strategy }}</span>
          <span v-if="r.budget_mb" class="puce">{{ r.budget_mb }} Mo max</span>
          <span v-if="r.why" class="motif">{{ r.why }}</span>
          <span v-else class="motif ok">rien à réduire : les fichiers sont conformes</span>
        </li>
      </ul>
      <p class="hint">
        Une stratégie « Qualité maximale » ne propose jamais de réduire quoi que ce soit —
        c'est sa définition. Pour que des fichiers apparaissent ici : change la stratégie du
        type dans <em>Réglages → Bibliothèque</em>, ou fixe-lui un poids maximal.
      </p>
    </div>

    <ul v-if="travaux.length" class="jobs">
      <li v-for="j in travaux" :key="j.id" :class="j.state">
        <div class="ligne">
          <span class="etiquette-etat">{{ ETATS[j.state] ?? j.state }}</span>
          <span class="titre">{{ j.title || j.path }}</span>
          <span class="cible">→ {{ j.target }}</span>
          <span class="poids">
            {{ gb(j.source_bytes) }} Go
            <template v-if="j.output_bytes">
              → {{ gb(j.output_bytes) }} Go
              <strong class="gain">−{{ gb(j.savings_bytes) }} Go</strong>
            </template>
          </span>

          <span v-if="j.state === 'running'" class="barre">
            <span class="jauge" :style="{ width: `${Math.round(j.progress * 100)}%` }"></span>
          </span>

          <span class="actions">
            <button v-if="j.state === 'queued'" class="small" @click="retirer(j)">Retirer</button>
            <template v-if="j.state === 'done'">
              <button class="small play" @click="basculerLecteur(j.id)">
                {{ playing === j.id ? 'Fermer' : '▶ Vérifier' }}
              </button>
              <ConfirmAction
                label="Remplacer"
                confirm-label="Confirmer le remplacement"
                :detail="`L'original part en corbeille et le fichier réencodé prend sa place. `
                  + `${gb(j.savings_bytes)} Go rendus. Les détails perdus à l'encodage ne `
                  + `reviendront pas.`"
                :busy="busy === `replace:${j.id}`"
                :disabled="!!busy"
                :disabled-reason="busy ? 'Une opération est déjà en cours.' : ''"
                @confirm="remplacer(j)"
              />
              <ConfirmAction
                label="Jeter"
                confirm-label="Confirmer : jeter le résultat"
                detail="Efface le fichier réencodé. L'original n'a jamais bougé, et le
                  fichier repartira en file si tu le remets."
                :busy="busy === `discard:${j.id}`"
                :disabled="!!busy"
                :disabled-reason="busy ? 'Une opération est déjà en cours.' : ''"
                @confirm="jeter(j)"
              />
            </template>
          </span>
        </div>

        <p v-if="j.error" class="echec">{{ j.error }}</p>

        <!-- Le contrôle automatique attrape un encodage tronqué ; il ne dira
             jamais si l'image est devenue laide. Ça ne se voit qu'en regardant. -->
        <div v-if="playing === j.id" class="lecteur">
          <video controls preload="none" :src="`/api/media/transcode/${j.id}/remux`"></video>
          <p class="hint">
            Réemballé en MP4 à la volée. Regarde une scène sombre et une scène chargée :
            c'est là que la compression se voit.
          </p>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.attente, .panne {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 12px; min-height: 40vh; text-align: center; padding: 40px 20px;
}
.attente { color: var(--text-dim); font-size: var(--t-sm); }
.pulsation {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2px solid var(--border); border-top-color: var(--accent);
  animation: tourne 1s linear infinite;
}
@keyframes tourne { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .pulsation { animation: none; } }
.panne h2 { margin: 0; font-size: var(--t-md); }
.panne p { margin: 0; font-size: var(--t-sm); color: var(--text-dim); max-width: 46em; }
.panne .quoi-faire { color: var(--text-faint); }

.atelier { display: flex; flex-direction: column; gap: 14px; }
header h2 { margin: 0 0 6px; font-size: var(--t-lg); letter-spacing: -.01em; }
.note { margin: 0 0 8px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.65; max-width: 68ch; }
.note.attention { color: var(--warn); opacity: .9; }

.panne-inline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 0; padding: 9px 12px; border-radius: 8px; font-size: var(--t-sm);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 30%, transparent);
  color: var(--err);
}

.etat { display: flex; gap: 7px; flex-wrap: wrap; }
.puce {
  font-size: var(--t-xs); padding: 2px 9px; border-radius: 20px;
  background: var(--surface-2); color: var(--text-dim);
}
.puce.ok { color: var(--accent); }
.puce.ko { color: var(--warn); }
.puce.a-voir { color: var(--ok); }

.retour { margin: 0; font-size: var(--t-sm); }
.retour.ok { color: var(--ok); }
.retour.ko { color: var(--err); }

.lot {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 9px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--warn) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
}
.warn-text { font-size: var(--t-sm); color: var(--text-dim); }
.indispo { font-size: var(--t-xs); color: var(--text-faint); }

.rien { display: flex; flex-direction: column; gap: 10px; }
.empty { margin: 0; font-size: var(--t-sm); color: var(--text-dim); font-style: italic; }
.par-type { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.par-type li { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; font-size: var(--t-sm); }
.par-type strong { min-width: 70px; }
.par-type .motif { font-size: var(--t-xs); color: var(--text-faint); }
.par-type .motif.ok { color: var(--accent); }
.hint { margin: 0; font-size: var(--t-xs); color: var(--text-faint); line-height: 1.6; max-width: 68ch; }

.jobs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.jobs > li {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 13px;
}
.jobs > li.done { border-color: color-mix(in srgb, var(--accent) 35%, transparent); }
.jobs > li.failed { border-color: color-mix(in srgb, var(--warn) 35%, transparent); }

.ligne { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: var(--t-sm); }
.etiquette-etat {
  font-size: var(--t-xs); text-transform: uppercase; letter-spacing: .05em;
  color: var(--text-faint); min-width: 76px;
}
.ligne .titre { font-weight: 500; overflow-wrap: anywhere; }
.ligne .cible { font-family: var(--mono); font-size: var(--t-xs); color: var(--accent); }
.ligne .poids { color: var(--text-dim); }
.ligne .gain { color: var(--accent); font-family: var(--mono); }
.barre {
  flex: 1; min-width: 90px; height: 4px; background: var(--surface-2);
  border-radius: 3px; overflow: hidden;
}
.jauge { display: block; height: 100%; background: var(--accent); transition: width .4s linear; }
@media (prefers-reduced-motion: reduce) { .jauge { transition: none; } }
.actions { margin-left: auto; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }

.echec { margin: 6px 0 0; font-size: var(--t-xs); color: var(--warn); overflow-wrap: anywhere; }

.lecteur { margin-top: 9px; display: flex; flex-direction: column; gap: 6px; }
.lecteur video { width: 100%; max-height: 58vh; background: #000; border-radius: 6px; }

button { font-size: var(--t-sm); }
button.small { font-size: var(--t-xs); padding: 7px 11px; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Sous 700 px, la ligne de travail se replie : les actions passent sous les
   informations plutôt que de rogner le titre du fichier. */
@media (max-width: 700px) {
  .ligne { align-items: flex-start; }
  .actions { margin-left: 0; width: 100%; }
  .etiquette-etat { min-width: 0; }
}
</style>
