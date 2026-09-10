<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
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
const confirmingPrune = ref(false)
const open = ref(new Set())
const picking = ref(null)
const choosing = ref(false)
const filter = ref('all')

const KINDS = { movie: 'Film', episode: 'Série', anime: 'Anime' }

/**
 * Ce qui demande un arbitrage : les plans douteux ET les plans écartés.
 *
 * Les écartés n'étaient affichés NULLE PART. Ils comptaient dans « à traiter »,
 * la ligne apparaissait dans la liste, et l'ouvrir ne montrait rien : ni le
 * fichier, ni un lecteur, ni un bouton. Le fichier restait donc dans la source
 * pour toujours, sans que rien ne dise pourquoi ni quoi en faire.
 *
 * Un score bas dit l'incertitude de la MACHINE, pas celle de la personne qui
 * regarde. C'est exactement pour ces fichiers-là que le lecteur vidéo existe.
 */
function arbitrables(w) {
  return [...w.pending.review, ...(w.pending.rejected ?? [])]
}

const jobs = computed(() => data.value?.jobs ?? {})
const counts = computed(() => data.value?.counts ?? {})
const working = computed(
  () => jobs.value.scan?.running || jobs.value.plan?.running || jobs.value.index?.running,
)

/**
 * Deux onglets, parce que ce sont deux gestes qui n'ont rien à voir.
 *
 * « Source » répond à « qu'est-ce qui traîne et qu'il faut ranger ». On y vient
 * pour vider, et on en repart quand il est vide. « Ma médiathèque » répond à
 * « qu'est-ce que je possède, et qu'est-ce qui cloche dedans ». On y vient pour
 * inspecter, et elle n'est jamais vide.
 *
 * Les mélanger, c'était enterrer les quelques lignes actionnables sous des
 * centaines de lignes au repos — et ne pouvoir filtrer correctement ni les unes
 * ni les autres.
 */
const onglet = ref('source')
const reenc = ref(null)
const recherche = ref('')
const tri = ref('defaut')

const FILTERS = {
  all: () => true,
  // Onglet source
  ready: (w) => w.pending.ready.length > 0,
  review: (w) => w.pending.review.length > 0,
  unplanned: (w) => w.pending.unplanned_count > 0,
  // Onglet médiathèque
  gaps: (w) => (w.owned?.missing_count ?? 0) > 0,
  dupes: (w) => (w.owned?.duplicates?.length ?? 0) > 0,
  heavy: (w) => w.heaviness >= (data.value?.heavy_ratio ?? 2),
  offstrat: (w) => (w.off_strategy?.length ?? 0) > 0,
}

/** Sans accents ni casse : « Amelie » doit trouver « Amélie ». */
function pliage(texte) {
  return (texte ?? '')
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLowerCase()
}

const TRIS = {
  // L'ordre du serveur : ce qui demande une action d'abord. C'est le bon défaut
  // pour la source, où l'on vient pour agir.
  defaut: null,
  titre: (a, b) => a.title.localeCompare(b.title, 'fr'),
  poids: (a, b) => b.bytes_per_file - a.bytes_per_file,
  manquants: (a, b) => (b.owned?.missing_count ?? 0) - (a.owned?.missing_count ?? 0),
  recuperable: (a, b) => recuperable(b) - recuperable(a),
}

function recuperable(w) {
  return (w.off_strategy ?? []).reduce((somme, f) => somme + f.savings_bytes, 0)
}

// Filtre de type, indépendant de l'état : on veut pouvoir croiser « les séries »
// avec « celles qui pèsent lourd ».
const kind = ref('all')

/** L'onglet décide de ce qui entre dans la liste, avant tout autre filtre. */
const parOnglet = computed(() =>
  (data.value?.works ?? []).filter(
    onglet.value === 'source' ? (w) => w.pending.total > 0 : (w) => w.owned,
  ),
)

const works = computed(() => {
  const q = pliage(recherche.value.trim())
  const liste = parOnglet.value
    .filter(FILTERS[filter.value] ?? FILTERS.all)
    .filter((w) => kind.value === 'all' || w.kind === kind.value)
    .filter((w) => !q || pliage(w.title).includes(q))
  const ordre = TRIS[tri.value]
  return ordre ? [...liste].sort(ordre) : liste
})

// Changer d'onglet remet les filtres à zéro : « Épisodes manquants » n'a aucun
// sens côté source, et laisser un filtre actif d'un onglet à l'autre donnerait
// une liste vide sans qu'on comprenne pourquoi.
watch(onglet, () => {
  filter.value = 'all'
  tri.value = onglet.value === 'source' ? 'defaut' : 'titre'
  recherche.value = ''
  charge.value = PALIER
  // Sans cet appel, l'onglet réencodage resterait vide jusqu'au prochain
  // rafraîchissement automatique : deux secondes d'écran blanc pour rien.
  load()
})

const kindCounts = computed(() => {
  const out = { movie: 0, episode: 0, anime: 0 }
  for (const w of data.value?.works ?? []) if (w.kind in out) out[w.kind] += 1
  return out
})

/** Les plus lourds d'abord : c'est l'ordre utile quand on cherche de la place. */
const bySize = computed(() => [...works.value].sort((a, b) => b.bytes_per_file - a.bytes_per_file))
const listed = computed(() =>
  filter.value === 'heavy' && tri.value === 'defaut' ? bySize.value : works.value,
)

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
  // La file de réencodage n'est chargée que quand on la regarde : elle recalcule
  // les candidats depuis l'index, et le faire toutes les deux secondes sur une
  // médiathèque entière coûterait cher pour un onglet fermé.
  if (onglet.value === 'transcode') {
    try {
      reenc.value = await (await fetch('/api/transcode')).json()
    } catch {
      reenc.value = null
    }
  }
}

async function enfiler(paths = null) {
  busy.value = 'queue'
  try {
    const out = await call('/api/transcode/queue', paths ? { paths } : { all: true })
    if (out) {
      message.value = `${out.queued} fichier(s) en file.` +
        (out.rejected?.length ? ` ${out.rejected.length} ignoré(s).` : '')
      reenc.value = out
    }
  } finally {
    busy.value = null
  }
}

/**
 * Installe le fichier réencodé. L'original part en corbeille, jamais à la
 * suppression : un réencodage peut être visuellement décevant sans que le
 * contrôle automatique l'ait vu — cela ne se découvre qu'en regardant.
 */
async function remplacer(job) {
  busy.value = 'replace'
  try {
    const out = await call(`/api/transcode/${job.id}/replace`)
    if (out) {
      reenc.value = out
      message.value = `Remplacé — ${gb(job.savings_bytes)} Go rendus.`
      await load()
    }
  } finally {
    busy.value = null
  }
}

async function jeter(job) {
  const out = await call(`/api/transcode/${job.id}/discard`)
  if (out) reenc.value = out
}

async function retirer(job) {
  const out = await call(`/api/transcode/${job.id}/cancel`)
  if (out) reenc.value = out
}

const ETATS = {
  queued: 'en attente',
  running: 'en cours',
  done: 'à vérifier',
  failed: 'échec',
  replaced: 'remplacé',
  discarded: 'jeté',
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
 * Nettoie TOUS les doublons de la bibliothèque d'un coup.
 *
 * Le serveur travaille sur son propre index, pas sur ce que la page affiche :
 * une liste tronquée à deux cents œuvres ferait oublier les autres, sans que
 * rien ne le signale. Et il ne refait pas l'arbitrage — l'exemplaire gardé est
 * celui que la stratégie du type a déjà désigné.
 */
async function pruneDuplicates() {
  if (!confirmingPrune.value) {
    confirmingPrune.value = true
    return
  }
  busy.value = 'prune'
  try {
    const out = await call('/api/collection/duplicates/prune', { confirm: true })
    if (out) {
      message.value =
        `${out.deleted} exemplaire(s) en trop supprimé(s), ${gb(out.freed_bytes)} Go libérés` +
        (out.failed ? `, ${out.failed} en échec.` : '.')
      await load()
    }
  } finally {
    confirmingPrune.value = false
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

    <!-- Deux gestes distincts : vider la source, inspecter la médiathèque. -->
    <div class="onglets">
      <button :class="{ actif: onglet === 'source' }" @click="onglet = 'source'">
        Source <span class="pastille">{{ counts.source_works ?? 0 }}</span>
      </button>
      <button :class="{ actif: onglet === 'library' }" @click="onglet = 'library'">
        Ma médiathèque <span class="pastille">{{ counts.library_works ?? 0 }}</span>
      </button>
      <button :class="{ actif: onglet === 'transcode' }" @click="onglet = 'transcode'">
        Réencodage
        <span v-if="counts.off_strategy" class="pastille">{{ counts.off_strategy }}</span>
      </button>
    </div>

    <!-- Réencodage : la nuit, un fichier à la fois, sans rien remplacer. -->
    <section v-if="onglet === 'transcode'" class="reenc">
      <div class="reenc-head">
        <div>
          <h3>Réencodage différé</h3>
          <p class="note">
            Ce que ta stratégie voudrait plus léger est encodé <strong>la nuit</strong>, un
            fichier à la fois, <strong>à côté</strong> de l'original. Rien n'est remplacé sans
            ton accord : le lendemain tu regardes le résultat et tu décides. C'est la seule
            opération que rien ne défait — l'original part en corbeille, mais les détails
            perdus à l'encodage ne reviennent pas.
          </p>
        </div>
      </div>

      <div v-if="reenc" class="reenc-etat">
        <span class="puce" :class="reenc.ffmpeg ? 'ok' : 'ko'">
          {{ reenc.ffmpeg ? 'ffmpeg présent' : 'ffmpeg absent — rien ne pourra être encodé' }}
        </span>
        <span class="puce" :class="reenc.settings.enabled ? 'ok' : 'ko'">
          {{ reenc.settings.enabled ? 'activé' : 'désactivé dans les réglages' }}
        </span>
        <span class="puce">
          fenêtre {{ reenc.settings.start_hour }} h → {{ reenc.settings.end_hour }} h
          <template v-if="reenc.in_window">(on y est)</template>
          <template v-else>(hors plage, la file attend l'heure)</template>
        </span>
        <span class="puce">{{ reenc.settings.codec }} · CRF {{ reenc.settings.crf }}</span>
      </div>

      <div v-if="reenc?.candidates?.count" class="lot">
        <span class="warn-text">
          {{ reenc.candidates.count }} fichier(s) ne respectent pas ta stratégie —
          environ {{ gb(reenc.candidates.recoverable_bytes) }} Go récupérables.
        </span>
        <button class="small" :disabled="busy === 'queue'" @click="enfiler()">
          Tout mettre en file
        </button>
      </div>
      <p v-else-if="reenc" class="empty">
        Rien à réencoder : tous tes fichiers respectent la stratégie de leur type.
      </p>

      <ul v-if="reenc?.jobs?.length" class="jobs">
        <li v-for="j in reenc.jobs" :key="j.id" :class="j.state">
          <div class="job-line">
            <span class="etat">{{ ETATS[j.state] ?? j.state }}</span>
            <span class="job-titre">{{ j.title || j.path }}</span>
            <span class="etiquette cible">→ {{ j.target }}</span>
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

            <span class="job-actions">
              <button v-if="j.state === 'queued'" class="small" @click="retirer(j)">Retirer</button>
              <template v-if="j.state === 'done'">
                <button class="small play" @click="togglePlayer(`re:${j.id}`)">
                  {{ playing === `re:${j.id}` ? 'Fermer' : '▶ Vérifier' }}
                </button>
                <button class="small ok" :disabled="busy === 'replace'" @click="remplacer(j)">
                  Remplacer
                </button>
                <button class="small" @click="jeter(j)">Jeter</button>
              </template>
            </span>
          </div>
          <p v-if="j.error" class="format-warn">{{ j.error }}</p>
          <!-- Le contrôle automatique attrape un encodage tronqué ; il ne dira
               jamais si l'image est devenue laide. Ça ne se voit qu'en regardant. -->
          <div v-if="playing === `re:${j.id}`" class="player">
            <video controls preload="none" :src="`/api/media/transcode/${j.id}/remux`"></video>
            <p class="thumb-note">
              Réemballé en MP4 à la volée. Regarde une scène sombre et une scène chargée :
              c'est là que la compression se voit.
            </p>
          </div>
        </li>
      </ul>
    </section>

    <div v-if="onglet === 'source'" class="filters">
      <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
        Tout ({{ parOnglet.length }})
      </button>
      <button v-if="counts.ready" class="warn" :class="{ active: filter === 'ready' }"
              @click="filter = 'ready'">
        Prêts à ranger ({{ counts.ready }})
      </button>
      <button v-if="counts.review" :class="{ active: filter === 'review' }" @click="filter = 'review'">
        À arbitrer ({{ counts.review }})
      </button>
      <button v-if="counts.unplanned" :class="{ active: filter === 'unplanned' }"
              @click="filter = 'unplanned'">
        Pas encore identifiés ({{ counts.unplanned }})
      </button>
    </div>

    <div v-else-if="onglet === 'library'" class="filters">
      <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
        Tout ({{ parOnglet.length }})
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
        Surpoids ({{ counts.heavy }})
      </button>
      <button v-if="counts.off_strategy" :class="{ active: filter === 'offstrat' }"
              @click="filter = 'offstrat'"
              :title="'Fichiers dont la résolution ne suit pas la stratégie choisie pour leur type'">
        Hors stratégie ({{ counts.off_strategy }})
        <span v-if="counts.recoverable_bytes" class="gain">
          −{{ gb(counts.recoverable_bytes) }} Go
        </span>
      </button>
    </div>

    <div v-if="onglet === 'library' && filter === 'dupes' && counts.duplicates" class="lot">
      <span class="warn-text">
        Ne garde qu'un exemplaire par emplacement : celui que la stratégie de son type
        désigne. Porte sur TOUTE la bibliothèque, pas seulement sur les lignes affichées.
      </span>
      <button class="small danger" :disabled="busy === 'prune'" @click="pruneDuplicates">
        {{ confirmingPrune ? `Confirmer — ${counts.duplicates} emplacement(s)` : 'Nettoyer tous les doublons' }}
      </button>
      <button v-if="confirmingPrune" class="small" @click="confirmingPrune = false">Renoncer</button>
    </div>

    <div v-if="onglet !== 'transcode'" class="outils">
      <input
        v-model="recherche"
        type="search"
        class="recherche"
        :placeholder="onglet === 'source' ? 'Chercher dans la source…' : 'Chercher un titre…'"
      />
      <label class="tri">
        Trier par
        <select v-model="tri">
          <option value="defaut">Ce qui demande une action</option>
          <option value="titre">Titre</option>
          <option value="poids">Poids par fichier</option>
          <option v-if="onglet === 'library'" value="manquants">Épisodes manquants</option>
          <option v-if="onglet === 'library'" value="recuperable">Place récupérable</option>
        </select>
      </label>
      <span v-if="recherche && works.length" class="compte">{{ works.length }} résultat(s)</span>
    </div>

    <div v-if="onglet !== 'transcode'" class="filters kinds">
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

    <p v-if="!works.length && onglet !== 'transcode'" class="empty">
      <template v-if="recherche">Aucun titre ne correspond à « {{ recherche }} ».</template>
      <template v-else-if="onglet === 'source'">
        Rien ne traîne dans la source. C'est l'état recherché — lance « Analyser les sources »
        si tu viens d'ajouter des fichiers.
      </template>
      <template v-else>
        La bibliothèque est vide pour ce filtre. « Relire la bibliothèque » la reconstruit.
      </template>
    </p>

    <ul v-if="onglet !== 'transcode'" class="works">
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
            <span v-if="arbitrables(w).length" class="badge review">{{ arbitrables(w).length }} à arbitrer</span>
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
          <section v-if="arbitrables(w).length" class="block">
            <div class="block-head">
              <h4>
                À arbitrer
                <span v-if="w.pending.rejected?.length" class="sous-titre">
                  dont {{ w.pending.rejected.length }} écarté(s) par le score — à vérifier
                  soi-même
                </span>
              </h4>
              <!-- La portée est écrite sur le bouton. Un bouton par ligne qui
                   agirait en douce sur toute la série serait pire qu'absent :
                   on ne saurait pas ce qu'on vient de valider. -->
              <button
                class="small ok"
                :disabled="choosing"
                @click="confirm(arbitrables(w)[0], { ids: arbitrables(w).map((p) => p.id) })"
              >
                C'est bon pour {{ arbitrables(w).length > 1
                  ? `les ${arbitrables(w).length}`
                  : 'celui-ci' }}
              </button>
            </div>
            <ul class="files">
              <template v-for="p in arbitrables(w)" :key="p.id">
                <li class="reviewable">
                  <span class="score" :class="p.decision === 'reject' ? 'bad' : 'warn'"
                        :title="p.decision === 'reject'
                          ? 'Écarté automatiquement : score trop bas'
                          : 'Score de confiance'">
                    {{ (p.score * 100).toFixed(0) }}
                  </span>
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
                title="Garde l'exemplaire que la stratégie de ce type désigne, supprime les autres"
                @click="deleteDuplicates(w)"
              >
                {{
                  confirmingDelete === w.key
                    ? 'Confirmer la suppression'
                    : 'Supprimer selon la stratégie'
                }}
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
                <div class="dupe-line">
                  <span class="label">{{ d.label }}</span>
                  <span class="wasted">{{ gb(d.wasted_bytes) }} Go en double</span>
                  <span v-if="d.strategy" class="strat">stratégie « {{ d.strategy }} »</span>
                </div>
                <!-- Chaque exemplaire avec ce qu'il vaut : « garde celui-ci »
                     sans dire ce que valent les autres demande une confiance
                     aveugle juste avant une suppression. -->
                <ul class="exemplaires">
                  <li v-for="f in d.files ?? []" :key="f.path" :class="{ garde: f.kept }">
                    <span class="etiquette" :class="{ cible: f.kept }">
                      {{ f.resolution || 'résolution inconnue' }}
                    </span>
                    <span class="poids">{{ gb(f.size_bytes) }} Go</span>
                    <span class="verdict">{{ f.kept ? 'gardé' : 'en trop' }}</span>
                    <code>{{ f.path }}</code>
                  </li>
                </ul>
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
.reenc { display: flex; flex-direction: column; gap: 12px; }
.reenc h3 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim); }
.reenc-etat { display: flex; gap: 7px; flex-wrap: wrap; }
.reenc-etat .puce {
  font-size: 11px; padding: 2px 9px; border-radius: 20px;
  background: var(--surface-2); color: var(--text-dim);
}
.reenc-etat .puce.ok { color: var(--accent); }
.reenc-etat .puce.ko { color: var(--warn); }
.jobs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.jobs > li { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px; }
.jobs > li.done { border-color: color-mix(in srgb, var(--accent) 35%, transparent); }
.jobs > li.failed { border-color: color-mix(in srgb, var(--warn) 35%, transparent); }
.job-line { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 12px; }
.job-line .etat { font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--text-faint); min-width: 74px; }
.job-line .job-titre { font-weight: 500; }
.job-line .gain { color: var(--accent); font-family: var(--mono); }
.job-line .barre { flex: 1; min-width: 90px; height: 4px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
.job-line .jauge { display: block; height: 100%; background: var(--accent); transition: width .4s linear; }
.job-actions { margin-left: auto; display: flex; gap: 6px; }
.block-head .sous-titre {
  font-size: 11px; font-weight: 400; text-transform: none; letter-spacing: 0;
  color: var(--text-faint); margin-left: 8px;
}
.score.bad {
  background: color-mix(in srgb, var(--warn) 22%, transparent);
  color: var(--warn);
}
.lot {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 9px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--warn) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
}
.dupe-line { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.dupe-line .strat { font-size: 11px; color: var(--text-faint); }
.exemplaires { list-style: none; margin: 5px 0 0 14px; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.exemplaires li { display: flex; gap: 8px; align-items: center; font-size: 11.5px; flex-wrap: wrap; }
.exemplaires code { font-family: var(--mono); font-size: 10.5px; color: var(--text-faint); overflow-wrap: anywhere; }
.exemplaires .verdict { font-size: 10.5px; color: var(--text-faint); }
.exemplaires li.garde .verdict { color: var(--ok, var(--accent)); }
.onglets { display: flex; gap: 4px; margin-bottom: 4px; }
.onglets button {
  font-size: 13px; padding: 7px 15px; border-radius: 8px 8px 0 0;
  border-bottom: 2px solid transparent; background: transparent;
}
.onglets button.actif {
  color: var(--text); border-bottom-color: var(--accent);
  background: var(--surface);
}
.onglets .pastille {
  font-size: 10.5px; margin-left: 6px; padding: 1px 6px; border-radius: 20px;
  background: var(--surface-2); color: var(--text-dim);
}
.outils { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: -2px; }
.recherche {
  flex: 1; min-width: 180px; max-width: 320px; font-size: 12.5px; padding: 5px 10px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text);
}
.tri { font-size: 11.5px; color: var(--text-faint); display: flex; gap: 6px; align-items: center; }
.tri select {
  font-size: 11.5px; padding: 4px 8px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text);
}
.outils .compte { font-size: 11.5px; color: var(--text-faint); }
.filters .gain { color: var(--accent); margin-left: 5px; font-family: var(--mono); font-size: 10.5px; }
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
