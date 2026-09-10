<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import CandidatePicker from './CandidatePicker.vue'
import ImageZoom from './ImageZoom.vue'

/**
 * Vue unique de la médiathèque.
 *
 * Trois écrans montraient trois moitiés du même objet — les fichiers à ranger,
 * la collection rangée, la file d'arbitrage — et obligeaient à des allers-retours
 * pour répondre à une question simple : « où en est cette série ? ».
 *
 * Deux partis pris gouvernent ce composant :
 *
 * 1. **Rien n'attend la fin de rien.** L'état est relu périodiquement pendant
 *    qu'un travail tourne, et la liste se remplit. « Exécuter » agit sur ce qui
 *    est prêt à cet instant, y compris pendant que le calcul continue.
 * 2. **Ce qui demande une action passe devant.** Le tri est fait côté serveur
 *    et n'est pas alphabétique : quelques lignes actionnables ne doivent pas
 *    être enterrées sous des centaines de lignes au repos.
 */

const data = ref(null)
const error = ref(null)
const message = ref(null)
const busy = ref(null)

// Le retour d'une action sur doublons, par œuvre. Le bandeau du haut ne suffit
// pas quand la ligne concernée est au milieu de six cents autres.
const dupeMessage = ref({})
const confirmingDelete = ref(null)
const open = ref(new Set())
const picking = ref(null)
const choosing = ref(false)
const filter = ref('all')

const KINDS = { movie: 'Film', episode: 'Série', anime: 'Anime' }

const jobs = computed(() => data.value?.jobs ?? {})
const counts = computed(() => data.value?.counts ?? {})
const working = computed(
  () => jobs.value.scan?.running || jobs.value.plan?.running || jobs.value.index?.running,
)

const FILTERS = {
  all: () => true,
  todo: (w) => w.pending.total > 0,
  gaps: (w) => (w.owned?.missing_count ?? 0) > 0,
  dupes: (w) => (w.owned?.duplicates?.length ?? 0) > 0,
  heavy: (w) => w.heaviness >= (data.value?.heavy_ratio ?? 2),
}

// Filtre de type, indépendant de l'état : on veut pouvoir croiser « les séries »
// avec « celles qui pèsent lourd ».
const kind = ref('all')

const works = computed(() =>
  (data.value?.works ?? [])
    .filter(FILTERS[filter.value])
    .filter((w) => kind.value === 'all' || w.kind === kind.value),
)

const kindCounts = computed(() => {
  const out = { movie: 0, episode: 0, anime: 0 }
  for (const w of data.value?.works ?? []) if (w.kind in out) out[w.kind] += 1
  return out
})

/** Les plus lourds d'abord : c'est l'ordre utile quand on cherche de la place. */
const bySize = computed(() => [...works.value].sort((a, b) => b.bytes_per_file - a.bytes_per_file))
const listed = computed(() => (filter.value === 'heavy' ? bySize.value : works.value))

/** Ce que le travail en cours est en train de faire, en une ligne. */
const activity = computed(() => {
  const { scan, plan, index } = jobs.value
  if (scan?.running) return { label: 'Analyse des fichiers', ...progress(scan) }
  if (plan?.running) return { label: 'Identification', ...progress(plan) }
  if (index?.running) return { label: 'Lecture de la bibliothèque', ...progress(index) }
  return null
})

function progress(job) {
  const pct = job.total ? Math.min(100, Math.round((job.processed / job.total) * 100)) : 0
  return { processed: job.processed, total: job.total, pct, current: job.current }
}

// Nombre d'œuvres chargées. Croît par paliers plutôt que par pages : on
// parcourt une médiathèque en déroulant, pas en tournant des pages, et perdre
// les lignes précédentes obligerait à revenir en arrière pour comparer.
const PALIER = 200
const charge = ref(PALIER)

async function load() {
  try {
    data.value = await (await fetch(`/api/workspace?limit=${charge.value}`)).json()
    error.value = null
  } catch {
    error.value = 'Serveur injoignable.'
  }
}

async function chargerPlus() {
  charge.value += PALIER
  await load()
}

async function call(url, body = null) {
  error.value = null
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : '{}',
  })
  const parsed = await res.json()
  if (!res.ok) {
    error.value = parsed.detail ?? 'Échec.'
    return null
  }
  return parsed
}

// --- Actions globales ------------------------------------------------------

async function scan() {
  busy.value = 'scan'
  try {
    await call('/api/library/scan?deep=true')
    await load()
  } finally {
    busy.value = null
  }
}

async function plan({ reset = false } = {}) {
  busy.value = 'plan'
  try {
    await call(`/api/review/plan?limit=100&reset=${reset}`)
    await load()
  } finally {
    busy.value = null
  }
}

/**
 * Vérifie tout le trajet sans rien déplacer. Distinct d'« Exécuter » parce que
 * ce sont deux décisions : « est-ce que ça marcherait » et « fais-le ».
 */
async function simulate(ids = null) {
  busy.value = 'simulate'
  failures.value = []
  try {
    const out = await call('/api/review/apply', { plan_ids: ids, dry_run: true })
    if (out) {
      failures.value = out.results.filter((r) => !r.ok)
      message.value = `Simulation : ${out.applied} déplacement(s) possible(s), ${out.failed} bloqué(s).`
    }
  } finally {
    busy.value = null
  }
}

// --- Annulation ciblée ----------------------------------------------------
//
// « Tout annuler » suppose qu'on veuille défaire une session entière, alors
// qu'en pratique on veut défaire UNE série mal identifiée au milieu de sept
// cents déplacements corrects.
const journal = ref(null)
const showUndo = ref(false)
const undoing = ref(null)
const WORK_KINDS = { movie: 'Film', episode: 'Série', anime: 'Anime' }

async function loadJournal() {
  try {
    journal.value = await (await fetch('/api/review/journal')).json()
  } catch {
    journal.value = null
  }
}

async function toggleUndo() {
  showUndo.value = !showUndo.value
  if (showUndo.value) await loadJournal()
}

async function undoWork(work) {
  undoing.value = work.key
  try {
    const out = await call('/api/review/undo', { work: work.key })
    if (out) {
      message.value =
        `« ${work.title} » : ${out.undone} déplacement(s) annulé(s)` +
        (out.failed ? `, ${out.failed} en échec.` : '.')
      await Promise.all([load(), loadJournal()])
    }
  } finally {
    undoing.value = null
  }
}

async function undoAll(count) {
  const out = await call('/api/review/undo', { count })
  if (out) {
    message.value = `${out.undone} opération(s) annulée(s), ${out.failed} en échec.`
    await Promise.all([load(), loadJournal()])
  }
}

async function index() {
  busy.value = 'index'
  try {
    await call('/api/collection/build')
    await load()
  } finally {
    busy.value = null
  }
}

/**
 * `ids` absent = tout ce qui est prêt, à cet instant. C'est ce qui permet
 * d'exécuter pendant que le calcul continue : ce qui n'est pas encore identifié
 * le sera au prochain clic.
 */
async function apply(ids = null) {
  busy.value = 'apply'
  failures.value = []
  try {
    const out = await call('/api/review/apply', { plan_ids: ids, dry_run: false })
    if (out) {
      failures.value = out.results.filter((r) => !r.ok)
      message.value =
        `${out.applied} fichier(s) rangé(s)` + (out.failed ? `, ${out.failed} en échec.` : '.')
      // La cause domine le compte : « 340 en échec » ne dit pas quoi faire,
      // « tous parce que la destination existe déjà » si.
      const [first] = failureGroups.value
      if (first) {
        const part = first.items.length === out.failed ? 'tous' : `dont ${first.items.length}`
        message.value += ` — ${part} : ${first.label.toLowerCase()}.`
      }
      await load()
    }
  } finally {
    busy.value = null
  }
}

// --- Pourquoi ça a échoué -------------------------------------------------
//
// Un compte d'échecs sans cause n'est pas exploitable : trois cents lignes
// disant chacune « déplacement impossible : /un/chemin/différent » se lisent
// exactement comme une seule.
const failures = ref([])
// Quel motif est deplie. Un compte et une explication ne suffisent pas : sans
// la liste, on sait POURQUOI ca a echoue mais pas SUR QUOI, et il n'y a rien
// a faire de cette information.
const ouvert = ref(null)
const MAX_LISTE = 100
const evacuating = ref(false)
const evacProgress = ref(null)

const REASONS = {
  destination_exists: {
    label: 'La destination existe déjà',
    fix: "Ces fichiers sont déjà rangés : seule une copie traîne encore dans les téléchargements. Rien n'a été écrasé — c'est volontaire. Tu peux évacuer ces copies vers la corbeille ; celles dont la taille diffère du fichier rangé seront refusées, car ce n'est alors pas le même fichier.",
    action: 'evacuate',
  },
  source_missing: {
    label: 'Fichier source introuvable',
    fix: 'Le fichier a bougé depuis le calcul du plan. Relance « Analyser les sources », puis « Identifier ».',
  },
  permission_denied: {
    label: 'Permission refusée',
    fix: "Sortilège n'a pas le droit d'écrire là où il doit agir — le plus souvent le dossier de TÉLÉCHARGEMENT, dont les fichiers appartiennent au client qui les a créés (JDownloader, un client torrent…). Aucun réglage de Sortilège ne peut le contourner : c'est une permission du NAS. Sur le NAS : « ls -ln » sur le dossier concerné pour voir l'UID propriétaire, puis aligne PUID / PGID du conteneur dessus, ou donne l'écriture au groupe partagé par les deux conteneurs.",
  },
  move_failed: {
    label: 'Déplacement impossible',
    fix: "Erreur système au moment du déplacement — le message exact ci-dessous dit laquelle : disque plein, volume en lecture seule, ou nom de fichier trop long.",
  },
  name_too_long: {
    label: 'Nom de fichier trop long',
    fix: "La corbeille aplatit le chemin d'origine dans le nom du fichier, et une release au titre à rallonge dépassait la limite du système. Ce n'est pas un problème de place. Corrigé : les noms trop longs sont désormais raccourcis en gardant leur fin, celle qui identifie le fichier.",
  },
  no_destination: {
    label: 'Aucune destination calculée',
    fix: "L'identification n'a rien donné pour ces fichiers.",
  },
  size_mismatch: {
    action: 'arbitrer',
    // Ce n'est PAS un échec, et l'appeler ainsi induit en erreur : c'est un
    // refus délibéré, et il demande une décision qu'aucun algorithme ne peut
    // prendre à ta place.
    label: 'Deux fichiers différents, pas un doublon',
    fix: "Le fichier rangé et la copie n'ont pas la même taille : ce sont deux encodages distincts de la même œuvre, pas deux exemplaires du même fichier. Rien n'a été touché — supprimer l'un des deux te ferait perdre une version. Compare-les avec « ▶ Voir », garde celui que tu préfères, et supprime l'autre toi-même.",
  },
  not_ranged: {
    label: "Le fichier n'est pas à destination",
    fix: "La copie ne peut pas être évacuée : rien ne prouve qu'elle existe ailleurs. Range-la d'abord avec « Exécuter ».",
  },
  no_trash: {
    label: 'Aucune corbeille configurée',
    fix: "La racine de bibliothèque n'est pas accessible en écriture — la corbeille y vit.",
  },
}

const failureGroups = computed(() => {
  const byReason = new Map()
  for (const r of failures.value) {
    const key = r.reason || 'move_failed'
    if (!byReason.has(key)) byReason.set(key, [])
    byReason.get(key).push(r)
  }
  return [...byReason.entries()]
    .map(([key, items]) => ({
      key,
      label: REASONS[key]?.label ?? 'Échec',
      fix: REASONS[key]?.fix ?? '',
      action: REASONS[key]?.action ?? null,
      items,
      sample: items[0].message,
    }))
    .sort((a, b) => b.items.length - a.items.length)
})

const evacPercent = computed(() => {
  const p = evacProgress.value
  if (!p || !p.total) return 0
  return Math.min(100, Math.round((p.processed / p.total) * 100))
})

let evacPoller = null

/**
 * Met en corbeille les copies dont le fichier est déjà rangé. Jamais une
 * suppression : l'opération est journalisée, donc annulable, et une source de
 * taille différente est refusée plutôt que confondue avec un doublon.
 */
const confirmDelete = ref(false)

async function evacuate(group, { mode = 'trash' } = {}) {
  // La suppression directe demande un second clic : elle est irréversible, et
  // un bouton irréversible qui part au premier clic est un piège.
  if (mode === 'delete' && !confirmDelete.value) {
    confirmDelete.value = true
    return
  }
  confirmDelete.value = false
  evacuating.value = true
  evacProgress.value = null

  const started = await call('/api/review/evacuate', {
    plan_ids: group.items.map((r) => r.plan_id),
    mode,
  })
  if (!started) {
    evacuating.value = false
    return
  }

  evacPoller = setInterval(async () => {
    try {
      const status = await (await fetch('/api/review/evacuate/status')).json()
      evacProgress.value = status
      if (status.error) error.value = status.error

      if (!status.running) {
        clearInterval(evacPoller)
        evacPoller = null
        evacuating.value = false
        evacProgress.value = null
        const verbe = mode === 'delete' ? 'supprimée(s)' : 'mise(s) en corbeille'
        message.value =
          `${status.evacuated} copie(s) ${verbe}` +
          (status.failed ? `, ${status.failed} refusée(s).` : '.')
        failures.value = status.results ?? []
        await load()
      }
    } catch {
      clearInterval(evacPoller)
      evacPoller = null
      evacuating.value = false
      error.value = 'Contact perdu avec le serveur pendant l\'évacuation.'
    }
  }, 700)
}

async function choose(planId, candidate) {
  choosing.value = true
  try {
    const out = await call(`/api/review/${planId}/choose`, {
      provider: candidate.provider,
      external_id: candidate.external_id,
    })
    if (out) {
      picking.value = null
      message.value = `Identifié comme « ${candidate.title} »` +
        (out.corrected > 1 ? ` — ${out.corrected} épisodes corrigés.` : '.')
      await load()
    }
  } finally {
    choosing.value = false
  }
}

/**
 * Les exemplaires en trop partent à la CORBEILLE, jamais à la suppression.
 * Un algorithme qui se trompe sur un doublon fait perdre le seul exemplaire ;
 * c'est le genre d'erreur qu'on ne peut pas rattraper.
 */
async function trashDuplicates(work) {
  const paths = work.owned.duplicates.flatMap((d) => d.redundant)
  if (!paths.length) return
  busy.value = 'trash'
  try {
    const out = await call('/api/collection/duplicates/trash', { paths })
    // Le retour s'affiche À CÔTÉ DU BOUTON, pas seulement dans le bandeau du
    // haut : quand on agit sur une ligne au milieu d'une liste de six cents,
    // un message hors de l'écran équivaut à pas de message du tout.
    dupeMessage.value[work.key] = out
      ? `${out.trashed} en corbeille` + (out.failed ? `, ${out.failed} en échec` : '')
      : error.value ?? 'Échec.'
    if (out) await load()
  } finally {
    busy.value = null
  }
}

/**
 * Supprime sans passer par la corbeille. Deux clics : le premier arme, le
 * second exécute — parce que rien ne défera celui-ci.
 *
 * La corbeille reste le geste par défaut. Mais quand on cherche de la place,
 * déplacer six cents gigaoctets vers une corbeille qu'il faudra vider ensuite
 * double le travail sans rien protéger de plus : l'exemplaire gardé est là, et
 * le serveur vérifie sa présence avant chaque suppression.
 */
async function deleteDuplicates(work) {
  if (confirmingDelete.value !== work.key) {
    confirmingDelete.value = work.key
    return
  }
  const groups = work.owned.duplicates
    .filter((d) => d.redundant.length)
    .map((d) => ({ keep: d.keep, paths: d.redundant }))
  if (!groups.length) return
  busy.value = 'delete'
  try {
    const out = await call('/api/collection/duplicates/delete', { groups, confirm: true })
    dupeMessage.value[work.key] = out
      ? `${out.deleted} supprimé(s), ${gb(out.freed_bytes)} Go libérés` +
        (out.failed ? `, ${out.failed} en échec` : '')
      : error.value ?? 'Échec.'
    if (out) await load()
  } finally {
    confirmingDelete.value = null
    busy.value = null
  }
}

/**
 * Valide l'identification proposée. C'était le geste symétrique manquant : on
 * ne pouvait que dire « ce n'est pas ça », jamais « c'est bon ». Un plan à 78 %
 * est très souvent correct — le score dit l'incertitude de la MACHINE, pas
 * celle de la personne qui regarde.
 */
async function confirm(plan, { ids = null } = {}) {
  choosing.value = true
  try {
    // Les identifiants exacts plutôt qu'un « toute la série » déduit du titre :
    // une même œuvre peut avoir été lue sous deux orthographes, et l'interface
    // sait précisément ce qu'elle affiche.
    const out = await call(`/api/review/${plan.id}/confirm`, { plan_ids: ids })
    if (out) {
      message.value =
        out.confirmed > 1
          ? `${out.confirmed} fichiers confirmés — prêts à ranger.`
          : 'Confirmé — prêt à ranger.'
      await load()
    }
  } finally {
    choosing.value = false
  }
}

// Quel fichier a son aperçu ouvert. Un seul à la fois : plusieurs extractions
// en parallèle sur un NAS saturent le processeur pour rien.
const playing = ref(null)
const preview = ref(null)

/**
 * Aucun navigateur courant ne lit le MKV, qui est le format majoritaire d'une
 * bibliothèque constituée. Un lecteur noir sur neuf fichiers sur dix ne répond
 * pas à la question posée — « est-ce le bon fichier ? ».
 *
 * Le serveur dit donc d'abord ce qu'il peut faire : lecture directe quand le
 * format s'y prête, sinon des images extraites par ffmpeg, qui marchent sur
 * tout. Une image noire ou figée en fin de fichier révèle en prime un
 * téléchargement incomplet, ce qu'une lecture du début ne montrerait jamais.
 */
async function togglePlayer(id) {
  if (playing.value === id) {
    playing.value = null
    preview.value = null
    return
  }
  playing.value = id
  preview.value = null
  try {
    preview.value = await (await fetch(`/api/media/plan/${id}/positions`)).json()
  } catch {
    preview.value = { available: false, playable_in_browser: false }
  }
}

// Image agrandie, ou null. Une jaquette de trente pixels ne permet pas de
// distinguer deux saisons d'une meme serie — et c'est pourtant sur elle qu'on
// tranche.
const zoom = ref(null)

function agrandir(src, legende) {
  if (src) zoom.value = { src, legende }
}

// Remise a zero de l'etat de travail. En deux clics : ce qui part est
// reconstructible, mais recalculer six mille plans coute des heures d'appels.
const confirmReset = ref(false)

async function remiseAZero() {
  if (!confirmReset.value) {
    confirmReset.value = true
    return
  }
  confirmReset.value = false
  busy.value = 'reset'
  failures.value = []
  try {
    const out = await call('/api/library/reset', { confirm: true })
    if (out) {
      message.value =
        `Liste effacée : ${out.cleared_plans} plan(s) et ${out.cleared_thumbnails} aperçu(s). ` +
        'Lance « Analyser les sources » pour repartir.'
      charge.value = PALIER
      await load()
    }
  } finally {
    busy.value = null
  }
}

function toggle(key) {
  const next = new Set(open.value)
  next.has(key) ? next.delete(key) : next.add(key)
  open.value = next
}

const shortPath = (p) => (p ? p.split('/').slice(-2).join('/') : '—')
const gb = (bytes) => (bytes / 1024 ** 3).toFixed(1)

/** « ×2,4 » se lit d'un coup d'œil là où « 8,3 Go » demande de comparer. */
const heavyLabel = (w) => `×${w.heaviness.toFixed(1).replace('.', ',')}`

// L'affichage progressif tient à ce seul intervalle : le serveur répond ce
// qu'il sait, et il en sait un peu plus à chaque appel. Pas de flux ouvert,
// pas d'état partagé.
let poller = null
onMounted(() => {
  load()
  poller = setInterval(() => {
    if (working.value || document.visibilityState === 'visible') load()
  }, 2000)
})
onUnmounted(() => clearInterval(poller))
</script>

<template>
  <div v-if="data" class="workspace">
    <!-- Barre d'action : les compteurs portent sur TOUT, pas sur la page -->
    <div class="toolbar">
      <button :disabled="busy || working" @click="scan">Analyser les sources</button>
      <button :disabled="busy || working || !counts.unplanned" @click="plan">
        Identifier {{ counts.unplanned ? `(${Math.min(100, counts.unplanned)})` : '' }}
      </button>
      <button class="primary" :disabled="busy || !counts.ready" @click="apply()">
        Exécuter {{ counts.ready }} prêt{{ counts.ready > 1 ? 's' : '' }}
      </button>
      <button class="ghost" :disabled="busy || !counts.ready" @click="simulate()">
        Simuler
      </button>
      <span class="spacer"></span>
      <button class="ghost" :disabled="busy || working" @click="index">
        Relire la bibliothèque
      </button>
      <button
        v-if="data.journal_size"
        class="ghost"
        :class="{ active: showUndo }"
        @click="toggleUndo"
      >Annuler… ({{ data.journal_size }})</button>
      <button
        v-if="counts.unplanned === 0 && counts.works"
        class="ghost"
        :disabled="busy || working"
        title="Vide la file de plans et repart du premier fichier"
        @click="plan({ reset: true })"
      >Recommencer</button>
      <button
        v-if="counts.works"
        class="ghost danger"
        :disabled="busy || working"
        title="Efface la liste entière — le journal d'annulation et les identifications retenues sont conservés"
        @click="remiseAZero"
      >{{ confirmReset ? 'Confirmer : tout effacer' : 'Tout effacer' }}</button>
    </div>

    <!-- Au FUTUR, et en disant ce qu'il reste à faire. La formulation au
         présent laissait croire que l'action avait déjà eu lieu, alors que le
         bouton attend un second clic. -->
    <p v-if="confirmReset" class="reset-avert">
      <strong>Rien n'est encore effacé.</strong> Clique à nouveau sur
      « Confirmer : tout effacer » pour vider la liste — plans, avancement, aperçus —
      et repartir d'un scan neuf.
      <br />
      Ton journal d'annulation ({{ data.journal_size }}) et tes identifications retenues
      seront <strong class="garde">conservés</strong>, et aucun fichier ne sera déplacé
      ni supprimé.
      <button class="renoncer" @click="confirmReset = false">Renoncer</button>
    </p>

    <!-- Ce qui a été rangé, par œuvre, avec une annulation par ligne -->
    <section v-if="showUndo" class="undo-panel">
      <div class="head">
        <h3>Annuler un rangement</h3>
        <button v-if="data.journal_size" class="danger" @click="undoAll(data.journal_size)">
          Tout annuler ({{ data.journal_size }})
        </button>
      </div>
      <p class="note">
        Les fichiers retournent à leur emplacement d'origine. Rien n'est supprimé, et une
        origine déjà occupée fait échouer le retour plutôt que d'écraser.
      </p>

      <p v-if="!journal" class="empty">Lecture du journal…</p>
      <p v-else-if="!journal.works.length" class="empty">Aucun déplacement à annuler.</p>

      <ul v-else class="undo-works">
        <li v-for="wk in journal.works" :key="wk.key">
          <div class="body">
            <div class="title">
              {{ wk.title }}
              <span v-if="wk.work_kind" class="kind">
                {{ WORK_KINDS[wk.work_kind] ?? wk.work_kind }}
              </span>
            </div>
            <div class="meta">
              {{ wk.files }} fichier{{ wk.files > 1 ? 's' : '' }}
              <span v-if="wk.companions">+ {{ wk.companions }} associé{{ wk.companions > 1 ? 's' : '' }}</span>
              <code>{{ shortPath(wk.sample) }}</code>
            </div>
          </div>
          <button class="small" :disabled="undoing === wk.key" @click="undoWork(wk)">
            {{ undoing === wk.key ? 'Annulation…' : 'Annuler' }}
          </button>
        </li>
      </ul>
    </section>

    <!-- Ce qui tourne, quand quelque chose tourne -->
    <div v-if="activity" class="activity">
      <div class="bar"><div class="fill" :style="{ width: activity.pct + '%' }"></div></div>
      <div class="stats">
        <span class="label">{{ activity.label }}</span>
        <span v-if="activity.total">{{ activity.processed }} / {{ activity.total }}</span>
        <span v-if="activity.current" class="current">{{ activity.current }}</span>
      </div>
    </div>

    <p v-if="error" class="err-msg">{{ error }}</p>
    <p v-if="message" class="ok-msg">{{ message }}</p>

    <!-- Pourquoi ça a échoué. En haut, pas enfoui : chercher la cause sous
         trois cents lignes revient à ne pas la donner. -->
    <section v-if="failureGroups.length" class="failures">
      <h3>Ce qui a bloqué</h3>
      <ul>
        <li v-for="g in failureGroups" :key="g.key">
          <button class="head" @click="ouvert = ouvert === g.key ? null : g.key">
            <span class="chev" :class="{ closed: ouvert !== g.key }">▾</span>
            <span class="count">{{ g.items.length }}</span>
            <span class="label">{{ g.label }}</span>
            <span class="voir">{{ ouvert === g.key ? 'masquer' : 'voir les fichiers' }}</span>
          </button>
          <p class="fix">{{ g.fix }}</p>
          <!-- Arbitrage par la taille : deux encodages, il faut choisir. -->
          <div v-if="g.action === 'arbitrer'" class="actions">
            <button class="act" :disabled="evacuating" @click="evacuate(g, { mode: 'keep_smaller' })">
              Garder le plus petit ({{ g.items.length }})
            </button>
            <button class="act" :disabled="evacuating" @click="evacuate(g, { mode: 'keep_larger' })">
              Garder le plus gros
            </button>
          </div>
          <p v-if="g.action === 'arbitrer'" class="fix">
            Le fichier écarté part en <strong>corbeille</strong>, pas à la poubelle, et
            l'opération est journalisée — « Annuler… » la défait comme n'importe quel
            rangement.
          </p>

          <div v-if="g.action === 'evacuate'" class="actions">
            <button class="act" :disabled="evacuating" @click="evacuate(g)">
              {{ evacuating ? 'En cours…' : `Mettre ces ${g.items.length} copies en corbeille` }}
            </button>
            <button class="act danger" :disabled="evacuating" @click="evacuate(g, { mode: 'delete' })">
              {{ confirmDelete
                ? `Confirmer : supprimer ces ${g.items.length} copies`
                : 'Supprimer sans passer par la corbeille' }}
            </button>
          </div>
          <p v-if="confirmDelete" class="fix warn-strong">
            Irréversible. Chaque fichier est tout de même vérifié avant : présent à
            destination et de même taille, sinon il est refusé.
          </p>
          <div v-if="g.action === 'evacuate' && evacProgress" class="evac">
            <div class="bar"><div class="fill" :style="{ width: evacPercent + '%' }"></div></div>
            <div class="stats">
              <span>{{ evacProgress.processed }} / {{ evacProgress.total }}</span>
              <span class="ok-count">{{ evacProgress.evacuated }} en corbeille</span>
              <span v-if="evacProgress.failed" class="ko-count">{{ evacProgress.failed }} refusée(s)</span>
              <span v-if="evacProgress.current" class="current">{{ evacProgress.current }}</span>
            </div>
            <!-- Les motifs de refus, pendant l'opération et non après : sur
                 trois cents fichiers, découvrir à la fin que tout a été refusé
                 pour une même raison fait perdre l'attente entière. -->
            <ul v-if="evacProgress.results?.length" class="refus">
              <li v-for="(r, i) in evacProgress.results.slice(0, 5)" :key="i">
                <code>{{ shortPath(r.source) }}</code>
                <span>{{ r.message }}</span>
              </li>
              <li v-if="evacProgress.results.length > 5" class="more">
                … et {{ evacProgress.results.length - 5 }} autres refus
              </li>
            </ul>
          </div>
          <code v-if="ouvert !== g.key" class="sample">{{ g.sample }}</code>

          <ul v-else class="fautifs">
            <li v-for="(r, i) in g.items.slice(0, MAX_LISTE)" :key="i">
              <code class="chemin">{{ shortPath(r.source) }}</code>
              <span class="motif">{{ r.message }}</span>
            </li>
            <li v-if="g.items.length > MAX_LISTE" class="more">
              … et {{ g.items.length - MAX_LISTE }} autres
            </li>
          </ul>
        </li>
      </ul>
    </section>

    <div class="filters">
      <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
        Tout ({{ counts.works }})
      </button>
      <button v-if="counts.ready + counts.review + counts.unplanned" class="warn"
              :class="{ active: filter === 'todo' }" @click="filter = 'todo'">
        À traiter ({{ counts.ready + counts.review + counts.unplanned }})
      </button>
      <button v-if="counts.missing" :class="{ active: filter === 'gaps' }" @click="filter = 'gaps'">
        Épisodes manquants ({{ counts.missing }})
      </button>
      <button v-if="counts.duplicates" :class="{ active: filter === 'dupes' }" @click="filter = 'dupes'">
        Doublons ({{ counts.duplicates }})
      </button>
      <button v-if="counts.heavy" class="heavy-filter" :class="{ active: filter === 'heavy' }"
              @click="filter = 'heavy'"
              :title="`Au moins ${data.heavy_ratio} fois le poids habituel de leur type`">
        Anormalement lourds ({{ counts.heavy }})
      </button>
    </div>

    <div class="filters kinds">
      <button :class="{ active: kind === 'all' }" @click="kind = 'all'">Tous types</button>
      <button v-if="kindCounts.movie" :class="{ active: kind === 'movie' }" @click="kind = 'movie'">
        Films ({{ kindCounts.movie }})
      </button>
      <button v-if="kindCounts.episode" :class="{ active: kind === 'episode' }" @click="kind = 'episode'">
        Séries ({{ kindCounts.episode }})
      </button>
      <button v-if="kindCounts.anime" :class="{ active: kind === 'anime' }" @click="kind = 'anime'">
        Animes ({{ kindCounts.anime }})
      </button>
      <span v-if="counts.total_bytes" class="total">
        {{ gb(counts.total_bytes) }} Go en bibliothèque
      </span>
    </div>

    <p v-if="!works.length" class="empty">
      Rien ici. Lance « Analyser les sources » pour commencer.
    </p>

    <ul class="works">
      <li v-for="w in listed" :key="w.key" :class="{ open: open.has(w.key) }">
        <button class="row" @click="toggle(w.key)">
          <span class="chev" :class="{ closed: !open.has(w.key) }">▾</span>
          <img
            v-if="w.poster_url"
            class="thumb zoomable"
            :src="w.poster_url"
            :alt="w.title"
            loading="lazy"
            title="Agrandir"
            @click.stop="agrandir(w.poster_url, `${w.title}${w.year ? ` (${w.year})` : ''}`)"
          />
          <span v-else class="thumb empty"></span>

          <span class="title">
            {{ w.title }}<span v-if="w.year" class="year"> ({{ w.year }})</span>
            <span v-if="w.kind" class="kind">{{ KINDS[w.kind] ?? w.kind }}</span>
          </span>

          <span class="badges">
            <span v-if="w.owned" class="badge own">{{ w.owned.file_count }} fichier{{ w.owned.file_count > 1 ? 's' : '' }}</span>
            <span v-if="w.owned?.total_bytes" class="badge size">{{ gb(w.owned.total_bytes) }} Go</span>
            <span v-if="w.heaviness >= (data.heavy_ratio ?? 2)" class="badge heavy"
                  :title="`${gb(w.bytes_per_file)} Go par fichier, contre ${(w.bytes_per_file / w.heaviness / 1024 ** 3).toFixed(1)} Go en médiane`">
              {{ heavyLabel(w) }} le poids habituel
            </span>
            <span v-if="w.owned?.missing_count" class="badge gap">{{ w.owned.missing_count }} manquant{{ w.owned.missing_count > 1 ? 's' : '' }}</span>
            <span v-if="w.owned?.duplicates?.length" class="badge dupe">{{ w.owned.duplicates.length }} doublon{{ w.owned.duplicates.length > 1 ? 's' : '' }}</span>
            <span v-if="w.pending.ready.length" class="badge ready">{{ w.pending.ready.length }} prêt{{ w.pending.ready.length > 1 ? 's' : '' }}</span>
            <span v-if="w.pending.review.length" class="badge review">{{ w.pending.review.length }} à arbitrer</span>
            <span v-if="w.pending.unplanned_count" class="badge wait">{{ w.pending.unplanned_count }} en attente</span>
          </span>
        </button>

        <div v-if="open.has(w.key)" class="detail">
          <!-- Prêts : exécutables pour cette œuvre seule -->
          <section v-if="w.pending.ready.length" class="block">
            <div class="block-head">
              <h4>Prêts à ranger</h4>
              <button class="primary small" :disabled="busy"
                      @click="apply(w.pending.ready.map((p) => p.id))">
                Exécuter ces {{ w.pending.ready.length }}
              </button>
            </div>
            <ul class="files">
              <template v-for="p in w.pending.ready" :key="p.id">
                <li>
                  <span class="score ok">{{ (p.score * 100).toFixed(0) }}</span>
                  <code class="from">{{ shortPath(p.source) }}</code>
                  <span class="arrow">→</span>
                  <code class="to">{{ shortPath(p.destination) }}</code>
                  <button class="small play" title="Vérifier avant de ranger"
                          @click="togglePlayer(p.id)">
                    {{ playing === p.id ? 'Fermer' : '▶' }}
                  </button>
                </li>
                <li v-if="playing === p.id" class="player">
                  <template v-if="preview?.playable_in_browser">
                    <video controls preload="metadata" :src="`/api/media/plan/${p.id}`"></video>
                  </template>
                  <!-- Conteneur illisible mais image lisible : on réemballe à la
                       volée, sans réencoder. Une release H.264 dans un MKV est
                       parfaitement lisible une fois dans un MP4. -->
                  <template v-else-if="preview?.remux?.possible">
                    <video controls preload="none" :src="`/api/media/plan/${p.id}/remux`"></video>
                    <p class="thumb-note">
                      Réemballé en MP4 à la volée — l'image n'est pas réencodée. La barre de
                      progression ne permet pas de sauter : le flux est produit au fil de la
                      lecture.
                    </p>
                  </template>
                  <template v-else-if="preview?.available">
                    <div class="thumbs">
                      <img
                        v-for="pos in preview.positions"
                        :key="pos"
                        :src="`/api/media/plan/${p.id}/thumb?at=${pos}`"
                        :alt="`à ${Math.round(pos * 100)} %`"
                        loading="lazy"
                        title="Agrandir"
                        class="zoomable"
                        @click="agrandir(
                          `/api/media/plan/${p.id}/thumb?at=${pos}`,
                          `${shortPath(p.source)} — à ${Math.round(pos * 100)} % du fichier`,
                        )"
                      />
                    </div>
                    <p class="thumb-note">
                      <template v-if="preview?.remux?.video">
                        Vidéo en {{ preview.remux.video }} : la réemballer ne suffirait pas, il
                        faudrait la réencoder — trop cher pour vérifier trois secondes.
                      </template>
                      Images prises tout au long du fichier — la dernière est à 90 %, une image
                      noire ou figée à cet endroit trahit un téléchargement incomplet.
                    </p>
                  </template>
                  <p v-else-if="preview" class="format-warn">
                    Aperçu impossible : ffmpeg absent, ou fichier illisible.
                  </p>
                  <p v-else class="thumb-note">Chargement de l'aperçu…</p>
                </li>
              </template>
            </ul>
          </section>

          <!-- Arbitrage : les jaquettes tranchent en une seconde -->
          <section v-if="w.pending.review.length" class="block">
            <div class="block-head">
              <h4>À arbitrer</h4>
              <!-- La portée est écrite sur le bouton. Un bouton par ligne qui
                   agirait en douce sur toute la série serait pire qu'absent :
                   on ne saurait pas ce qu'on vient de valider. -->
              <button
                class="small ok"
                :disabled="choosing"
                @click="confirm(w.pending.review[0], { ids: w.pending.review.map((p) => p.id) })"
              >
                C'est bon pour {{ w.pending.review.length > 1
                  ? `les ${w.pending.review.length}`
                  : 'celui-ci' }}
              </button>
            </div>
            <ul class="files">
              <template v-for="p in w.pending.review" :key="p.id">
                <li class="reviewable">
                  <span class="score warn">{{ (p.score * 100).toFixed(0) }}</span>
                  <code class="from">{{ shortPath(p.source) }}</code>
                  <button class="small play" title="Vérifier le contenu"
                          @click="togglePlayer(p.id)">
                    {{ playing === p.id ? 'Fermer' : '▶ Voir' }}
                  </button>
                  <button
                    class="small ok"
                    :disabled="choosing"
                    title="Confirme ce fichier seul"
                    @click="confirm(p)"
                  >
                    C'est bon
                  </button>
                  <button class="small" @click="picking = picking === p.id ? null : p.id">
                    Ce n'est pas ça
                  </button>
                </li>
                <li v-if="playing === p.id" class="player">
                  <template v-if="preview?.playable_in_browser">
                    <video controls preload="metadata" :src="`/api/media/plan/${p.id}`"></video>
                  </template>
                  <!-- Conteneur illisible mais image lisible : on réemballe à la
                       volée, sans réencoder. Une release H.264 dans un MKV est
                       parfaitement lisible une fois dans un MP4. -->
                  <template v-else-if="preview?.remux?.possible">
                    <video controls preload="none" :src="`/api/media/plan/${p.id}/remux`"></video>
                    <p class="thumb-note">
                      Réemballé en MP4 à la volée — l'image n'est pas réencodée. La barre de
                      progression ne permet pas de sauter : le flux est produit au fil de la
                      lecture.
                    </p>
                  </template>
                  <template v-else-if="preview?.available">
                    <div class="thumbs">
                      <img
                        v-for="pos in preview.positions"
                        :key="pos"
                        :src="`/api/media/plan/${p.id}/thumb?at=${pos}`"
                        :alt="`à ${Math.round(pos * 100)} %`"
                        loading="lazy"
                        title="Agrandir"
                        class="zoomable"
                        @click="agrandir(
                          `/api/media/plan/${p.id}/thumb?at=${pos}`,
                          `${shortPath(p.source)} — à ${Math.round(pos * 100)} % du fichier`,
                        )"
                      />
                    </div>
                    <p class="thumb-note">
                      <template v-if="preview?.remux?.video">
                        Vidéo en {{ preview.remux.video }} : la réemballer ne suffirait pas, il
                        faudrait la réencoder — trop cher pour vérifier trois secondes.
                      </template>
                      Images prises tout au long du fichier — la dernière est à 90 %, une image
                      noire ou figée à cet endroit trahit un téléchargement incomplet.
                    </p>
                  </template>
                  <p v-else-if="preview" class="format-warn">
                    Aperçu impossible : ffmpeg absent, ou fichier illisible.
                  </p>
                  <p v-else class="thumb-note">Chargement de l'aperçu…</p>
                </li>
              </template>
            </ul>
            <template v-for="p in w.pending.review" :key="`pick-${p.id}`">
              <CandidatePicker
                v-if="picking === p.id"
                :candidates="p.alternatives ?? []"
                :busy="choosing"
                :plan-id="p.id"
                @choose="(c) => choose(p.id, c)"
                @close="picking = null"
              />
            </template>
          </section>

          <!-- Pas encore identifiés : la ligne existe dès le scan -->
          <section v-if="w.pending.unplanned_count" class="block">
            <div class="block-head"><h4>En attente d'identification</h4></div>
            <ul class="files plain">
              <li v-for="(f, i) in w.pending.unplanned" :key="i"><code>{{ f }}</code></li>
              <li v-if="w.pending.unplanned_count > w.pending.unplanned.length" class="more">
                … et {{ w.pending.unplanned_count - w.pending.unplanned.length }} autres
              </li>
            </ul>
          </section>

          <!-- Ce qu'on possède -->
          <section v-if="w.owned" class="block">
            <div class="block-head">
              <h4>En bibliothèque</h4>
              <span class="size">
                {{ gb(w.owned.total_bytes) }} Go — {{ gb(w.bytes_per_file) }} Go par fichier
                <template v-if="w.heaviness">({{ heavyLabel(w) }} la médiane de son type)</template>
              </span>
            </div>
            <ul class="seasons">
              <li v-for="s in w.owned.seasons" :key="s.number">
                <span class="season-num">{{ s.number ? `Saison ${s.number}` : 'Hors saison' }}</span>
                <span class="have">{{ s.owned.length }} épisode{{ s.owned.length > 1 ? 's' : '' }}</span>
                <span v-if="s.missing.length" class="missing">
                  manque {{ s.missing.join(', ') }}
                </span>
                <span v-else-if="s.complete" class="complete">complète</span>
              </li>
            </ul>
            <!-- « 2,5 fois le poids habituel » sur neuf épisodes ne dit pas
                 LEQUEL. On nomme les fichiers : c'est sur eux qu'on agit. -->
            <ul v-if="w.heavy_files?.length" class="fichiers surpoids">
              <li v-for="f in w.heavy_files" :key="f.path">
                <span class="etiquette">×{{ f.ratio }}</span>
                <span class="poids">{{ gb(f.size_bytes) }} Go</span>
                <code>{{ f.path }}</code>
              </li>
            </ul>
            <ul v-if="w.off_strategy?.length" class="fichiers hors-strategie">
              <li v-for="f in w.off_strategy" :key="f.path">
                <span class="etiquette cible">{{ f.resolution }} → {{ f.target }}</span>
                <span class="poids">
                  {{ gb(f.size_bytes) }} Go — environ {{ gb(f.savings_bytes) }} Go récupérables
                </span>
                <code>{{ f.path }}</code>
              </li>
            </ul>

            <div v-if="w.owned.duplicates.length" class="dupe-head">
              <span class="warn-text">
                La corbeille se vide ensuite ; la suppression ne se rattrape pas.
              </span>
              <!-- Un seul bouton désactivé par « busy » global restait inerte
                   pendant un scan, sans rien dire : le clic ne faisait rien et
                   rien n'expliquait pourquoi. Chaque bouton ne se bloque plus
                   que sur SA propre action. -->
              <button class="small" :disabled="busy === 'trash'" @click="trashDuplicates(w)">
                {{ busy === 'trash' ? 'Déplacement…' : 'Mettre en corbeille' }}
              </button>
              <button
                class="small danger"
                :disabled="busy === 'delete'"
                @click="deleteDuplicates(w)"
              >
                {{ confirmingDelete === w.key ? 'Confirmer la suppression' : 'Supprimer' }}
              </button>
              <button
                v-if="confirmingDelete === w.key"
                class="small"
                @click="confirmingDelete = null"
              >
                Renoncer
              </button>
              <span v-if="dupeMessage[w.key]" class="dupe-msg">{{ dupeMessage[w.key] }}</span>
            </div>
            <ul v-if="w.owned.duplicates.length" class="dupes">
              <li v-for="d in w.owned.duplicates" :key="d.label">
                <span class="label">{{ d.label }}</span>
                <span class="wasted">{{ gb(d.wasted_bytes) }} Go en double</span>
                <code>garde {{ d.keep }}</code>
              </li>
            </ul>
          </section>
        </div>
      </li>
    </ul>

    <ImageZoom
      v-if="zoom"
      :src="zoom.src"
      :legende="zoom.legende"
      @close="zoom = null"
    />

    <div v-if="data.has_more" class="plus">
      <button :disabled="busy" @click="chargerPlus">
        Charger {{ Math.min(PALIER, data.total - data.shown) }} œuvres de plus
      </button>
      <span class="compte">{{ data.shown }} sur {{ data.total }}</span>
    </div>
  </div>
</template>

<style scoped>
.fichiers { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.fichiers li { display: flex; align-items: center; gap: 8px; font-size: 11.5px; flex-wrap: wrap; }
.fichiers code {
  font-family: var(--mono); font-size: 11px; color: var(--text-faint);
  overflow-wrap: anywhere;
}
.fichiers .etiquette {
  font-size: 10.5px; padding: 1px 6px; border-radius: 4px;
  background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn);
}
.fichiers .etiquette.cible {
  background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent);
  font-family: var(--mono);
}
.fichiers .poids { color: var(--text-dim); }
.dupe-msg { font-size: 11.5px; color: var(--text-dim); }
button.small.danger { color: var(--warn); }
button.small.danger:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--warn) 40%, transparent);
}
.workspace { display: flex; flex-direction: column; gap: 14px; }

.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }
.toolbar .ghost { color: var(--text-faint); }
.toolbar .ghost.active { border-color: var(--accent); color: var(--text); }
.toolbar .ghost.danger:hover:not(:disabled) { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }
.reset-avert { margin: 0; font-size: 12px; color: var(--warn); line-height: 1.7; max-width: 720px; }
.reset-avert strong { color: var(--warn); }
.reset-avert strong.garde { color: var(--ok); }
.renoncer { margin-left: 10px; font-size: 11.5px; padding: 2px 10px; color: var(--text-faint); }

.undo-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.undo-panel .head { display: flex; align-items: center; gap: 12px; }
.undo-panel .head h3 {
  margin: 0; flex: 1; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}
.undo-panel .danger { font-size: 11.5px; padding: 3px 10px; color: var(--text-faint); }
.undo-panel .danger:hover { color: var(--err); border-color: color-mix(in srgb, var(--err) 30%, transparent); }
.undo-panel .note { margin: 9px 0 12px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.undo-panel .empty { margin: 0; font-size: 12.5px; color: var(--text-faint); font-style: italic; }
.undo-works { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
.undo-works li { display: flex; align-items: center; gap: 12px; }
.undo-works .body { flex: 1; min-width: 0; }
.undo-works .title { font-size: 13px; display: flex; align-items: baseline; gap: 8px; }
.undo-works .kind {
  font-size: 10px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--text-faint); border: 1px solid var(--border); border-radius: 3px; padding: 0 5px;
}
.undo-works .meta { display: flex; gap: 9px; align-items: baseline; margin-top: 2px; font-size: 11px; color: var(--text-faint); flex-wrap: wrap; }
.undo-works .meta code { font-family: var(--mono); font-size: 10.5px; }

.activity { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.activity .bar { height: 3px; background: var(--surface-2); border-radius: 2px; overflow: hidden; }
.activity .fill { height: 100%; background: var(--accent); transition: width .3s; }
.activity .stats { display: flex; gap: 12px; align-items: baseline; margin-top: 7px; font-size: 11.5px; color: var(--text-faint); }
.activity .label { color: var(--text-dim); }
.activity .current { font-family: var(--mono); font-size: 10.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.err-msg { margin: 0; font-size: 12.5px; color: var(--err); }
.ok-msg { margin: 0; font-size: 12.5px; color: var(--ok); }

.failures {
  background: var(--surface); border: 1px solid color-mix(in srgb, var(--err) 25%, var(--border));
  border-radius: 10px; padding: 14px 16px;
}
.failures > h3 {
  margin: 0 0 12px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--err);
}
.failures > ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 14px; }
.failures .head {
  display: flex; align-items: center; gap: 9px; font-size: 13.5px;
  width: 100%; border: none; background: none; padding: 0;
  color: var(--text); text-align: left; cursor: pointer;
}
.failures .head:hover .label { color: var(--text); }
.failures .chev { font-size: 10px; color: var(--text-faint); transition: transform .15s; }
.failures .chev.closed { transform: rotate(-90deg); }
.failures .voir { margin-left: auto; font-size: 11px; color: var(--text-faint); }

.fautifs { list-style: none; margin: 9px 0 0; padding: 0; display: flex; flex-direction: column; gap: 5px; max-height: 340px; overflow-y: auto; }
.fautifs li { display: flex; flex-direction: column; gap: 1px; }
.fautifs .chemin { font-family: var(--mono); font-size: 11px; color: var(--text-dim); }
.fautifs .motif { font-size: 10.5px; color: var(--text-faint); }
.fautifs .more { font-size: 11.5px; color: var(--text-faint); font-style: italic; }
.failures .count {
  font-family: var(--mono); font-size: 11.5px; padding: 1px 7px; border-radius: 4px;
  background: color-mix(in srgb, var(--err) 18%, transparent); color: var(--err);
}
.failures .fix { margin: 6px 0 0; font-size: 12px; color: var(--text-dim); line-height: 1.6; max-width: 680px; }
.failures .actions { display: flex; gap: 8px; margin: 9px 0 0; flex-wrap: wrap; }
.failures .act { font-size: 12px; padding: 4px 12px; }
.failures .act.danger { color: var(--text-faint); }
.failures .act.danger:hover:not(:disabled) { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }
.failures .warn-strong { color: var(--err); }
.failures .act:hover:not(:disabled) { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, transparent); }
.failures .sample { display: block; margin: 6px 0 0; font-family: var(--mono); font-size: 11px; color: var(--text-faint); }
.evac { margin: 10px 0 0; max-width: 680px; }
.evac .bar { height: 3px; background: var(--surface-2); border-radius: 2px; overflow: hidden; }
.evac .fill { height: 100%; background: var(--warn); transition: width .3s; }
.evac .stats { display: flex; gap: 12px; align-items: baseline; margin-top: 6px; font-size: 11.5px; color: var(--text-faint); flex-wrap: wrap; }
.evac .ok-count { color: var(--ok); }
.evac .ko-count { color: var(--err); }
.evac .current { font-family: var(--mono); font-size: 10.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.refus { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.refus li { display: flex; gap: 9px; align-items: baseline; font-size: 11px; color: var(--err); flex-wrap: wrap; }
.refus code { font-family: var(--mono); font-size: 10.5px; color: var(--text-faint); }
.refus .more { color: var(--text-faint); font-style: italic; }

.filters { display: flex; gap: 7px; flex-wrap: wrap; }
.filters button { font-size: 11.5px; padding: 3px 11px; }
.filters button.active { border-color: var(--accent); color: var(--text); }

.empty { font-size: 13px; color: var(--text-faint); font-style: italic; }

.works { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.works > li { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.works > li.open { border-color: color-mix(in srgb, var(--accent) 30%, var(--border)); }

.row {
  width: 100%; display: flex; align-items: center; gap: 11px; padding: 8px 12px;
  border: none; background: none; text-align: left; cursor: pointer;
}
.row:hover { background: var(--surface-2); }
.chev { font-size: 10px; color: var(--text-faint); transition: transform .15s; flex: none; }
.chev.closed { transform: rotate(-90deg); }

.thumb { width: 30px; height: 45px; border-radius: 3px; object-fit: cover; background: var(--surface-2); flex: none; }
.thumb.empty { border: 1px dashed var(--border); }
.zoomable { cursor: zoom-in; }
.zoomable:hover { outline: 1px solid var(--accent); outline-offset: 1px; }

.title { flex: 1; min-width: 0; font-size: 13.5px; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.year { color: var(--text-faint); }
.kind { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--text-faint); border: 1px solid var(--border); border-radius: 3px; padding: 0 5px; }

.badges { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; flex: none; }
.badge { font-size: 10.5px; padding: 1px 7px; border-radius: 3px; background: var(--surface-2); color: var(--text-faint); white-space: nowrap; }
.badge.ready { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.badge.review { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
.badge.gap { background: color-mix(in srgb, var(--warn) 12%, transparent); color: var(--warn); }
.badge.dupe { background: color-mix(in srgb, var(--err) 12%, transparent); color: var(--err); }
.badge.size { font-family: var(--mono); font-size: 10px; }
.badge.heavy { background: color-mix(in srgb, var(--err) 18%, transparent); color: var(--err); }
.filters.kinds { margin-top: -6px; align-items: baseline; }
.filters .total { margin-left: auto; font-size: 11.5px; color: var(--text-faint); }

.detail { padding: 4px 12px 12px 12px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 14px; }
.block-head { display: flex; align-items: center; gap: 10px; margin: 10px 0 7px; }
.block-head h4 { margin: 0; flex: 1; font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--text-dim); }
.block-head .size { font-size: 11px; color: var(--text-faint); }
button.small { font-size: 11px; padding: 2px 9px; }

.files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.files li { display: flex; align-items: center; gap: 8px; font-size: 11.5px; flex-wrap: wrap; }
.files code { font-family: var(--mono); font-size: 10.5px; color: var(--text-faint); }
.files .to { color: var(--ok); }
.files .arrow { color: var(--text-faint); }
.files .more { color: var(--text-faint); font-style: italic; }
.score { font-family: var(--mono); font-size: 10.5px; padding: 0 5px; border-radius: 3px; flex: none; }
.score.ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.score.warn { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
button.small.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 28%, transparent); }
button.small.play { color: var(--text-faint); }

.files li.player { display: block; margin: 6px 0 10px; }
.files li.player video { width: 100%; max-width: 620px; border-radius: 6px; background: #000; display: block; }
.format-warn { margin: 6px 0 0; font-size: 11.5px; color: var(--warn); max-width: 620px; line-height: 1.5; }
.thumbs { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 4px; }
.thumbs img { height: 118px; width: auto; border-radius: 4px; background: #000; flex: none; }
.thumb-note { margin: 7px 0 0; font-size: 11.5px; color: var(--text-faint); max-width: 680px; line-height: 1.5; }

.seasons { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.seasons li { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.season-num { min-width: 90px; color: var(--text-dim); }
.have { color: var(--text-faint); }
.missing { color: var(--warn); }
.complete { color: var(--ok); }

.dupe-head { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.dupe-head .warn-text { flex: 1; font-size: 11.5px; color: var(--text-faint); }
.dupes { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; }
.dupes li { display: flex; gap: 9px; align-items: baseline; flex-wrap: wrap; }
.dupes .wasted { color: var(--err); }

.plus { display: flex; align-items: center; gap: 12px; justify-content: center; padding: 4px 0 8px; }
.plus .compte { font-size: 11.5px; color: var(--text-faint); }
</style>
