<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import ConfirmAction from './ConfirmAction.vue'
import VideoPlayer from './VideoPlayer.vue'
import { motif, pluriel, refusSansMotif } from '../lib/langue.js'

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

/** Relu à chaque rafraîchissement : les durées affichées avancent avec la file. */
const maintenant = ref(Date.now())

async function charger() {
  chargement.value = true
  try {
    const res = await fetch('/api/transcode')
    // Une réponse 502 arrive en HTML : `json()` lèverait sans être attrapée, et
    // l'écran resterait figé sans un mot.
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    donnees.value = await res.json()
    maintenant.value = Date.now()
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
      message.value = { ok: false, texte: lu.detail ?? refusSansMotif(res.status) }
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
          `${pluriel(out.queued, 'fichier')} en file.` +
          (out.rejected?.length ? ` ${pluriel(out.rejected.length, 'ignoré')}.` : ''),
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

// --- Pause et reprise ----------------------------------------------------------
//
// Deux gestes sans perte : l'encodage suspendu reprend là où il s'était arrêté.
// Pas de confirmation, donc — mais un retour immédiat, et un bouton qui dit ce
// qu'il est en train de faire pendant l'appel.

/** « Reprendre » dès qu'une pause est enregistrée, même file vide (pause retrouvée
 *  après un redémarrage) ; « Mettre en pause » seulement s'il y a quelque chose à
 *  suspendre. */
const peutPauser = computed(
  () => !!donnees.value && !donnees.value.paused && (donnees.value.running || donnees.value.queued > 0),
)

async function basculerPause() {
  const reprendre = !!donnees.value?.paused
  busy.value = reprendre ? 'resume' : 'pause'
  try {
    const out = await appel(`/api/transcode/${reprendre ? 'resume' : 'pause'}`)
    if (out) {
      // La réponse porte l'état de la file, pas les candidats : on garde ceux
      // déjà affichés plutôt que de faire croire qu'il n'y en a plus.
      donnees.value = { ...donnees.value, ...out }
      maintenant.value = Date.now()
      message.value = {
        ok: true,
        texte: reprendre
          ? 'Réencodage repris.'
          : 'Réencodage en pause : rien n’est perdu, il reprendra là où il s’est arrêté.',
      }
    }
  } finally {
    busy.value = null
  }
}

const heurePause = computed(() => {
  const iso = donnees.value?.paused_since
  const quand = iso ? new Date(iso) : null
  if (!quand || Number.isNaN(quand.getTime())) return ''
  return `${quand.getHours()} h ${String(quand.getMinutes()).padStart(2, '0')}`
})

function dureeCourte(secondes) {
  const minutes = Math.round(secondes / 60)
  if (minutes < 1) return 'moins d’une minute'
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')}`
}

/**
 * Temps écoulé et restant d'un encodage, SANS le temps de pause : une nuit de
 * pause ne doit pas faire croire à un encodage de dix heures, ni gonfler
 * l'estimation de ce qui reste.
 */
function temps(j) {
  const debut = j.started_at ? Date.parse(j.started_at) : NaN
  if (Number.isNaN(debut)) return ''
  const ecoule = Math.max(0, (maintenant.value - debut) / 1000 - (j.paused_seconds ?? 0))
  let texte = `écoulé : ${dureeCourte(ecoule)}`
  if (j.progress > 0.02 && j.progress < 1) {
    texte += ` · reste environ ${dureeCourte((ecoule * (1 - j.progress)) / j.progress)}`
  }
  return texte
}

// --- La fiche comparée ---------------------------------------------------------
//
// C'est ici qu'on jette un original de vingt gigaoctets. Un H.264 10 bits, un HDR
// perdu, une piste disparue ne se voient pas dans trente secondes d'aperçu : ils
// se lisent, et la fiche les écrit AVANT le bouton « Remplacer ».

const comparaisons = ref({})
/** L'état du contrôle de décodage tel que le lecteur le suit, en direct. */
const controlesEnDirect = ref({})

async function chargerComparaison(id) {
  const avant = comparaisons.value[id]
  comparaisons.value = {
    ...comparaisons.value,
    [id]: { ...(avant ?? {}), etat: avant?.donnees ? 'ok' : 'chargement' },
  }
  const res = await fetch(`/api/media/transcode/${id}/comparaison`).catch(() => null)
  let entree
  if (!res) {
    entree = { etat: 'erreur', texte: 'Sortilège ne répond pas.' }
  } else {
    const corps = await res.json().catch(() => ({}))
    entree = res.ok ? { etat: 'ok', donnees: corps } : { etat: 'erreur', texte: motif(corps, res.status) }
  }
  comparaisons.value = { ...comparaisons.value, [id]: entree }
}

function surControle(id, etat) {
  controlesEnDirect.value = { ...controlesEnDirect.value, [id]: etat }
  // Un résultat final change le verdict : on relit la fiche.
  if (['sain', 'abime', 'indetermine'].includes(etat.etat)) chargerComparaison(id)
}

function fiche(id) {
  return comparaisons.value[id]?.donnees ?? null
}

function verdictLigne(id) {
  const c = comparaisons.value[id]
  if (!c || c.etat === 'chargement') return { classe: 'attente', texte: 'Analyse du résultat…' }
  if (c.etat === 'erreur') return { classe: 'attente', texte: `Fiche comparée indisponible : ${c.texte}` }
  return { classe: c.donnees.gravite, texte: c.donnees.phrase }
}

/** Ce qui doit être lu avant de confirmer : tout sauf les simples informations. */
function reserves(id) {
  return (fiche(id)?.verdicts ?? []).filter((v) => v.gravite !== 'info')
}

function detailRemplacer(j) {
  const base =
    `L'original part en corbeille et le fichier réencodé prend sa place. ` +
    `${gb(j.savings_bytes)} Go rendus. Les détails perdus à l'encodage ne reviendront pas.`
  const motifs = reserves(j.id).map((v) => v.texte)
  return motifs.length ? `Attention : ${motifs.join(' ')} ${base}` : base
}

function bits(a) {
  return a.video.profondeur ? `${a.video.profondeur} bits` : 'inconnue'
}

function hdr(a) {
  const lignes = [a.hdr.libelle]
  const dv = a.hdr.dolby_vision
  if (dv) lignes.push(dv.explication)
  const lmax = a.hdr.mastering?.luminance_max
  const cll = a.hdr.lumiere?.max_cll
  if (lmax) lignes.push(`mastering ${Math.round(lmax)} nits${cll ? ` · MaxCLL ${cll}` : ''}`)
  return lignes
}

function definition(a) {
  const { largeur, hauteur } = a.video
  return largeur && hauteur ? `${largeur}×${hauteur} (${a.video.definition})` : 'inconnue'
}

function pistes(liste) {
  return liste.length ? liste.map((p) => p.libelle) : ['aucune']
}

function lignesFiche(c) {
  const o = c.original
  const r = c.reencode
  const codes = new Set(c.verdicts.filter((v) => v.gravite === 'grave').map((v) => v.code))
  const reserve = new Set(c.verdicts.filter((v) => v.gravite === 'attention').map((v) => v.code))
  const niveau = (...liste) =>
    liste.some((x) => codes.has(x)) ? 'grave' : liste.some((x) => reserve.has(x)) ? 'attention' : ''
  return [
    { libelle: 'Codec et profil', o: [o.video.libelle_profil], r: [r.video.libelle_profil], niveau: niveau('h264_10_bits') },
    { libelle: 'Profondeur', o: [bits(o)], r: [bits(r)], niveau: niveau('h264_10_bits') },
    { libelle: 'HDR', o: hdr(o), r: hdr(r), niveau: niveau('hdr_perdu', 'dolby_vision_5', 'metadonnees_hdr') },
    { libelle: 'Définition', o: [definition(o)], r: [definition(r)], niveau: '' },
    { libelle: 'Durée', o: [o.duree_lisible], r: [r.duree_lisible], niveau: niveau('duree') },
    { libelle: 'Pistes audio', o: pistes(o.audio), r: pistes(r.audio), niveau: niveau('audio_perdu') },
    { libelle: 'Sous-titres', o: pistes(o.sous_titres), r: pistes(r.sous_titres), niveau: niveau('sous_titres_perdus') },
  ]
}

function controleDe(id) {
  return controlesEnDirect.value[id] ?? fiche(id)?.controle ?? null
}

function controleLigne(id) {
  const c = controleDe(id)
  if (!c || c.etat === 'aucun') return { classe: 'attente', texte: 'Contrôle de décodage pas encore lancé.' }
  if (c.etat === 'en_cours') {
    return { classe: 'attente', texte: 'Contrôle de décodage en cours : 5 endroits du fichier…' }
  }
  if (c.etat === 'attente' || c.etat === 'indisponible') return { classe: 'attente', texte: c.message }
  // Le message est une phrase complète, qui dit ce qui a été vérifié. Le
  // préfixer de son libellé (« Décodage : abîmé — … le fichier est abîmé »)
  // disait deux fois la même chose.
  return {
    classe: c.etat,
    texte: c.message,
    detail: c.etat === 'abime' ? c.premiere_erreur : '',
  }
}

/**
 * Les verdicts de la fiche, sauf celui du contrôle de décodage : la ligne du
 * contrôle, juste au-dessus, le dit déjà. Il reste dans `reserves()` — la
 * confirmation du remplacement doit le reprendre.
 */
function verdictsAffiches(id) {
  return (fiche(id)?.verdicts ?? []).filter(
    (v) => v.code !== 'decodage' && v.code !== 'decodage_indetermine',
  )
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

// Chaque résultat à vérifier a sa fiche comparée, chargée une fois.
watch(
  () => travaux.value.filter((j) => j.state === 'done').map((j) => j.id),
  (ids) => {
    for (const id of ids) if (!comparaisons.value[id]) chargerComparaison(id)
  },
  { immediate: true },
)

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
      <span v-if="donnees.paused" class="puce pause">
        {{ heurePause ? `en pause depuis ${heurePause}` : 'en pause' }}
      </span>
      <button
        v-if="donnees.paused"
        type="button"
        class="small"
        :disabled="busy === 'resume'"
        @click="basculerPause"
      >
        {{ busy === 'resume' ? 'Reprise en cours…' : 'Reprendre' }}
      </button>
      <button
        v-else-if="peutPauser"
        type="button"
        class="small"
        :disabled="busy === 'pause'"
        @click="basculerPause"
      >
        {{ busy === 'pause' ? 'Mise en pause en cours…' : 'Mettre en pause' }}
      </button>
    </div>

    <p v-if="donnees.paused && donnees.note" class="note-pause">{{ donnees.note }}</p>

    <p v-if="message" :class="['retour', message.ok ? 'ok' : 'ko']" role="status">
      {{ message.texte }}
    </p>

    <div v-if="candidats?.count" class="lot">
      <span class="warn-text">
        {{ pluriel(candidats.count, 'fichier') }} hors de ta stratégie —
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
        type dans <em>Réglages → Médiathèque</em>, ou fixe-lui un poids maximal.
      </p>
    </div>

    <ul v-if="travaux.length" class="jobs">
      <li v-for="j in travaux" :key="j.id" :class="j.state">
        <div class="ligne">
          <span class="etiquette-etat" :class="{ pause: j.paused }">
            {{ j.paused ? 'en pause' : (ETATS[j.state] ?? j.state) }}
          </span>
          <span class="titre">{{ j.title || j.path }}</span>
          <span class="cible">→ {{ j.target }}</span>
          <span class="poids">
            {{ gb(j.source_bytes) }} Go
            <template v-if="j.output_bytes">
              → {{ gb(j.output_bytes) }} Go
              <strong class="gain">−{{ gb(j.savings_bytes) }} Go</strong>
            </template>
          </span>

          <!-- Suspendu : la barre reste figée là où l'encodage s'est arrêté, et
               le dit, plutôt que de passer pour un encodage bloqué. -->
          <span v-if="j.state === 'running'" class="barre" :class="{ figee: j.paused }">
            <span class="jauge" :style="{ width: `${Math.round(j.progress * 100)}%` }"></span>
          </span>
          <span v-if="j.state === 'running' && temps(j)" class="temps">
            {{ Math.round(j.progress * 100) }} % · {{ temps(j) }}
          </span>

          <span v-if="j.state === 'queued'" class="actions">
            <button class="small" @click="retirer(j)">Retirer</button>
          </span>
        </div>

        <p v-if="j.error" class="echec">{{ j.error }}</p>

        <!-- « Jeter » n'existe que pour un résultat à vérifier : jamais pour un
             encodage en cours ou suspendu. -->
        <template v-if="j.state === 'done'">
          <!-- Le verdict AVANT les boutons : on doit l'avoir lu avant de
               remplacer, pas le découvrir après. -->
          <p class="verdict" :class="verdictLigne(j.id).classe" role="status">
            {{ verdictLigne(j.id).texte }}
          </p>
          <div class="decision">
            <button class="small play" @click="basculerLecteur(j.id)">
              {{ playing === j.id ? 'Fermer' : '▶ Vérifier' }}
            </button>
            <ConfirmAction
              label="Remplacer"
              :confirm-label="reserves(j.id).length
                ? 'Remplacer malgré l’avertissement'
                : 'Confirmer le remplacement'"
              :detail="detailRemplacer(j)"
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
          </div>

          <!-- Le contrôle automatique attrape un encodage tronqué ; il ne dira
               jamais si l'image est devenue laide. Ça ne se voit qu'en regardant
               — et ce que l'aperçu ne peut pas montrer (10 bits, HDR, pistes)
               se lit dans la fiche d'à côté. -->
          <div v-if="playing === j.id" class="verification">
            <div class="colonne-lecteur">
              <VideoPlayer
                genre="transcode"
                :ident="j.id"
                verifier-decodage
                @controle="(etat) => surControle(j.id, etat)"
              />
              <p class="hint">
                Regarde une scène sombre et une scène chargée : c'est là que la compression
                se voit — sauf si l'aperçu est converti, car l'image montrée n'est alors
                pas celle du fichier.
              </p>
            </div>

            <section class="fiche" aria-label="Original et réencodé comparés">
              <p v-if="!fiche(j.id)" class="hint" role="status">{{ verdictLigne(j.id).texte }}</p>
              <template v-else>
                <table>
                  <thead>
                    <tr>
                      <th scope="col"><span class="sr">Propriété</span></th>
                      <th scope="col">Original</th>
                      <th scope="col">Réencodé</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="l in lignesFiche(fiche(j.id))" :key="l.libelle" :class="l.niveau">
                      <th scope="row">{{ l.libelle }}</th>
                      <td><span v-for="(x, i) in l.o" :key="i">{{ x }}</span></td>
                      <td><span v-for="(x, i) in l.r" :key="i">{{ x }}</span></td>
                    </tr>
                  </tbody>
                </table>

                <div class="controle" :class="controleLigne(j.id).classe" role="status">
                  <p>{{ controleLigne(j.id).texte }}</p>
                  <code v-if="controleLigne(j.id).detail">{{ controleLigne(j.id).detail }}</code>
                </div>

                <ul v-if="verdictsAffiches(j.id).length" class="verdicts">
                  <li v-for="v in verdictsAffiches(j.id)" :key="v.code" :class="v.gravite">
                    {{ v.texte }}
                  </li>
                </ul>

                <p class="verdict final" :class="fiche(j.id).gravite">{{ fiche(j.id).phrase }}</p>
              </template>
            </section>
          </div>
        </template>
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
/* `.pulsation` — keyframes et garde de mouvement comprises — vit maintenant
   dans style.css : la regle etait identique a l'octet dans quatre vues. */
.panne h2 { margin: 0; font-size: var(--t-md); }
.panne p { margin: 0; font-size: var(--t-sm); color: var(--text-dim); max-width: 46em; }
.panne .quoi-faire { color: var(--text-faint); }

.atelier { display: flex; flex-direction: column; gap: 14px; }
header h2 { margin: 0 0 6px; font-size: var(--t-lg); letter-spacing: -.01em; }
.note { margin: 0 0 8px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.65; max-width: 68ch; }
.note.attention { color: var(--warn); opacity: .9; }

/* La boite elle-meme (fond, bordure, couleur, marge interne, interligne, taille
   de texte) vient de `.panne-inline` dans style.css. Ne reste ici que ce qui
   est propre a cet emplacement : pas de marge externe, et la mise en ligne du
   bouton « Actualiser » a cote du texte. */
.panne-inline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 0;
}

.etat { display: flex; gap: 7px; flex-wrap: wrap; }
.puce {
  font-size: var(--t-xs); padding: 2px 9px; border-radius: 20px;
  background: var(--surface-2); color: var(--text-dim);
}
.puce.ok { color: var(--accent); }
.puce.ko { color: var(--warn); }
.puce.a-voir { color: var(--ok); }
.puce.pause { color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, var(--surface-2)); }
.note-pause { margin: 0; font-size: var(--t-xs); color: var(--text-dim); line-height: 1.6; max-width: 68ch; }

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
.barre.figee .jauge { background: var(--warn); }
.etiquette-etat.pause { color: var(--warn); }
.temps { font-size: var(--t-xs); color: var(--text-faint); font-family: var(--mono); }
.actions { margin-left: auto; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }

.echec { margin: 6px 0 0; font-size: var(--t-xs); color: var(--warn); overflow-wrap: anywhere; }

/* Le verdict en une phrase, AVANT les boutons de décision. */
.verdict { margin: 8px 0 0; font-size: var(--t-sm); line-height: 1.55; color: var(--text-dim); }
.verdict.grave { color: var(--err); font-weight: 600; }
.verdict.attention { color: var(--warn); }
.verdict.ok { color: var(--ok); }
.decision { margin-top: 8px; display: flex; gap: 8px; align-items: flex-start; flex-wrap: wrap; }

/* Lecteur et fiche côte à côte quand la place le permet, l'un sous l'autre
   sinon. `minmax(0, …)` : sans lui, une ligne d'erreur de ffmpeg sans espace
   élargirait la colonne au-delà de l'écran. */
.verification {
  margin-top: 10px;
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
@media (max-width: 900px) { .verification { grid-template-columns: minmax(0, 1fr); } }
.colonne-lecteur { display: flex; flex-direction: column; gap: 6px; min-width: 0; }

.fiche { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.fiche table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: var(--t-xs); }
.fiche th, .fiche td {
  text-align: left; vertical-align: top; padding: 5px 6px;
  border-bottom: 1px solid var(--border); overflow-wrap: anywhere;
}
.fiche thead th { color: var(--text-faint); font-weight: 500; }
.fiche tbody th { color: var(--text-dim); font-weight: 500; width: 28%; }
.fiche td span { display: block; }
.fiche td span + span { color: var(--text-faint); }
.fiche tr.grave td:last-child { color: var(--err); font-weight: 600; }
.fiche tr.attention td:last-child { color: var(--warn); }
.sr {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}

.controle { font-size: var(--t-xs); line-height: 1.55; color: var(--text-dim); }
.controle p { margin: 0; }
.controle.sain p { color: var(--ok); }
.controle.abime p { color: var(--err); font-weight: 600; }
.controle.indetermine p { color: var(--warn); }
.controle code { display: block; margin-top: 3px; font-family: var(--mono); overflow-wrap: anywhere; }

.verdicts { margin: 0; padding-left: 18px; font-size: var(--t-xs); line-height: 1.55; color: var(--text-dim); }
.verdicts .grave { color: var(--err); }
.verdicts .attention { color: var(--warn); }
.verdict.final { margin: 0; }

button { font-size: var(--t-sm); }
button.small { font-size: var(--t-xs); padding: 7px 11px; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Sous 700 px, la ligne de travail se replie : les actions passent sous les
   informations plutôt que de rogner le titre du fichier. */
@media (max-width: 700px) {
  .ligne { align-items: flex-start; }
  .actions { margin-left: 0; width: 100%; }
  .etiquette-etat { min-width: 0; }
  .fiche tbody th { width: 30%; }
}
</style>
