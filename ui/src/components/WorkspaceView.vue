<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import CandidatePicker from './CandidatePicker.vue'
import ImageZoom from './ImageZoom.vue'
import BookReader from './BookReader.vue'
import ConfirmAction from './ConfirmAction.vue'

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

const props = defineProps({
  /**
   * Quel espace afficher : `source` (ce qui traîne et qu'il faut ranger) ou
   * `library` (ce qu'on possède). Le choix se fait désormais dans la barre du
   * haut : ce sont deux intentions, pas deux filtres, et les enterrer dans des
   * onglets internes obligeait à entrer dans un écran pour découvrir qu'on
   * voulait l'autre.
   */
  espace: { type: String, default: 'source' },
})

const data = ref(null)
const error = ref(null)
const message = ref(null)
const busy = ref(null)

// Le retour d'une action sur doublons, par œuvre. Le bandeau du haut ne suffit
// pas quand la ligne concernée est au milieu de six cents autres.
const dupeMessage = ref({})
const menuOuvert = ref(false)
const lecture = ref(null)
const open = ref(new Set())
const picking = ref(null)
const choosing = ref(false)
const filter = ref('all')

const KINDS = { movie: 'Film', episode: 'Série', anime: 'Anime', book: 'Livre' }

/**
 * D'où vient l'identification. Deux plans à 82 % ne se valent pas selon qu'ils
 * viennent d'une fiche TMDB ou d'une supposition d'un modèle de langage : un
 * score nu demande de faire confiance sans savoir à qui.
 */
const ORIGINES = {
  tmdb: { court: 'TMDB', long: 'Fiche TMDB' },
  tvdb: { court: 'TVDB', long: 'Fiche TheTVDB' },
  anilist: { court: 'AniList', long: 'Fiche AniList' },
  ia: { court: 'IA', long: "Le résolveur a proposé le titre, la fiche vient du fournisseur" },
  memoire: { court: 'mémoire', long: 'Tu avais déjà tranché pour ce titre' },
  manuel: { court: 'ton choix', long: 'Candidat choisi à la main' },
  fichier: { court: 'fichier', long: 'Métadonnées lues dans le fichier lui-même' },
  nom: { court: 'nom', long: 'Deviné depuis le nom du fichier, rien de plus' },
}

function origine(plan) {
  return ORIGINES[plan.identified_by] ?? ORIGINES.nom
}

/**
 * Ce qui a identifié les fichiers d'une œuvre, résumé pour la ligne.
 *
 * Un seul fichier proposé par l'IA suffit à le signaler : c'est le maillon dont
 * on veut se méfier, et le noyer dans une majorité de fiches TMDB reviendrait à
 * ne pas le dire.
 */
function origines(w) {
  const tous = [...w.pending.ready, ...w.pending.review, ...(w.pending.rejected ?? [])]
  return {
    ia: tous.some((p) => p.identified_by === 'ia'),
    nom: tous.length > 0 && tous.every((p) => !p.identified_by || p.identified_by === 'nom'),
  }
}

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

/**
 * Ce qui empêche l'application de fonctionner. Calculé côté serveur — clé
 * absente, racine non montée, aucun scan — et jusqu'ici transporté nulle part :
 * une clé TMDB refusée se lisait comme « aucun candidat » sur trois cents
 * fichiers, sans qu'un seul écran ne prononce le mot « clé ».
 */
const blocages = computed(() => data.value?.blockers ?? [])

/**
 * La bibliothèque n'a jamais été indexée. Tous les compteurs de la sous-vue
 * « place » se calculent dessus : sans elle, ils valent zéro sans que rien ne
 * distingue « rien à récupérer » de « rien n'a été mesuré ».
 */
const sansIndex = computed(() => !(counts.value.library_works ?? 0))
const working = computed(
  () => jobs.value.scan?.running || jobs.value.plan?.running || jobs.value.index?.running,
)

/**
 * Ce que la liste montre à cet instant : l'espace choisi en haut, ou la file de
 * réencodage.
 *
 * « Source » et « Ma médiathèque » ont quitté cette rangée pour la barre du
 * haut. Le réencodage y reste, et c'est volontaire : ce n'est pas un troisième
 * espace mais une file d'attente qu'on ouvre pour vérifier un résultat de la
 * nuit, puis qu'on referme. Lui donner le même rang que les deux autres
 * laisserait croire à une troisième médiathèque.
 */
const onglet = ref(props.espace)

// La barre du haut fait foi. Sans ce report, changer d'entrée de navigation ne
// changerait rien à la liste — le composant étant réutilisé d'une entrée à
// l'autre, il n'est pas remonté et son état interne survivrait tel quel.
watch(() => props.espace, (espace) => { onglet.value = espace })

// La panne de la file de réencodage, séparée de son contenu. Les confondre
// dans un même `null` faisait passer un serveur muet pour un onglet vide.
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
  // Les trois causes réunies : c'est la question posée — « où sont mes 600 Go »
  // — et elle ne se décompose pas naturellement en trois listes.
  place: (w) =>
    (w.owned?.duplicates?.length ?? 0) > 0 ||
    w.heaviness >= (data.value?.heavy_ratio ?? 2) ||
    (w.off_strategy?.length ?? 0) > 0,
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

/**
 * Les types filtrables, dans l'ordre où on les cherche.
 *
 * Livres et animes circulaient depuis toujours dans les données — `w.kind` les
 * vaut, `KINDS` les nomme, le lecteur d'EPUB existe — mais la rangée n'offrait
 * que films et séries. Une médiathèque de mille livres se parcourait donc
 * mélangée aux films, sans aucun moyen de l'isoler.
 */
const KIND_FILTRES = [
  { id: 'movie', label: 'Films' },
  { id: 'episode', label: 'Séries' },
  { id: 'anime', label: 'Animes' },
  { id: 'book', label: 'Livres' },
]

/** L'onglet décide de ce qui entre dans la liste, avant tout autre filtre. */
const sousVue = ref('avoir')

const parOnglet = computed(() => {
  const tout = data.value?.works ?? []
  if (onglet.value === 'source') return tout.filter((w) => w.pending.total > 0)
  const possedees = tout.filter((w) => w.owned)
  return sousVue.value === 'place' ? possedees.filter(FILTERS.place) : possedees
})

/**
 * Ce que la sous-vue « place » promet, en octets : doublons plus réencodages.
 *
 * Les doublons se somment sur les œuvres CHARGÉES, pas sur toute la
 * bibliothèque — d'où le « ≈ » à l'affichage. Annoncer un total exact
 * demanderait un compteur serveur ; annoncer un total faux serait pire que de
 * ne rien annoncer.
 */
const placeRecuperable = computed(() => {
  const doublons = (data.value?.works ?? []).reduce(
    (somme, w) =>
      somme + (w.owned?.duplicates ?? []).reduce((s, d) => s + (d.wasted_bytes ?? 0), 0),
    0,
  )
  return doublons + (counts.value.recoverable_bytes ?? 0)
})

watch(sousVue, () => {
  filter.value = sousVue.value === 'place' ? 'place' : 'all'
  tri.value = sousVue.value === 'place' ? 'poids' : 'titre'
  charge.value = PALIER
})

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
  const out = Object.fromEntries(KIND_FILTRES.map((k) => [k.id, 0]))
  for (const w of data.value?.works ?? []) if (w.kind in out) out[w.kind] += 1
  return out
})

/**
 * Les types qu'aucune œuvre chargée ne porte.
 *
 * Leur filtre reste affiché et grisé plutôt que retiré : un bouton absent ne
 * dit rien, alors qu'un compte à zéro dit « c'est mesuré, et il n'y en a pas ».
 * Encore faut-il le DIRE — un bouton pâle tout seul se lit comme un bogue.
 */
const typesVides = computed(() =>
  KIND_FILTRES.filter((k) => !kindCounts.value[k.id]).map((k) => k.label.toLowerCase()),
)

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

/**
 * Pourquoi la barre d'actions est inerte, en une phrase.
 *
 * Vingt et un des vingt-quatre boutons de cet écran se grisent sans un mot, et
 * les trois qui s'expliquaient le faisaient par un attribut `title` — que le
 * doigt ne fait jamais apparaître et que le clavier n'atteint pas. Un bouton
 * pâle qui ne réagit pas se lit alors comme une panne.
 *
 * Une phrase et non un texte par bouton : la cause est presque toujours
 * commune à toute la barre, et la répéter vingt et une fois demanderait de la
 * chercher au lieu de la lire.
 *
 * L'ordre des cas n'est pas arbitraire — on nomme d'abord ce qui BLOQUE, qui se
 * terminera tout seul, puis ce qui MANQUE, qui demande un geste. L'inverse
 * enverrait relancer un scan pendant qu'un scan tourne.
 */
const raisonIndispo = computed(() => {
  if (busy.value) return "Une action est déjà en cours : les autres reprennent dès qu'elle rend la main."
  if (working.value) {
    return `${activity.value?.label ?? 'Un travail'} en cours : « Analyser les sources » et « Identifier » attendent la fin.`
  }
  const aIdentifier = counts.value.unplanned ?? 0
  const prets = counts.value.ready ?? 0
  if (!aIdentifier && !prets) {
    return "Rien à identifier ni à ranger : la source est vide. « Analyser les sources » la relit si tu viens d'y déposer des fichiers."
  }
  if (!aIdentifier) return "« Identifier » n'a plus rien à traiter : chaque fichier de la source a déjà un plan."
  if (!prets) return "« Ranger » attend un plan validé : aucun fichier n'est prêt. Lance « Identifier », puis arbitre ce qui est resté douteux."
  return null
})

// Nombre d'œuvres chargées. Croît par paliers plutôt que par pages : on
// parcourt une médiathèque en déroulant, pas en tournant des pages, et perdre
// les lignes précédentes obligerait à revenir en arrière pour comparer.
const PALIER = 200
const charge = ref(PALIER)
const chargement = ref(false)

// L'essai à blanc n'est pas une autre action : c'est le même appel serveur avec
// un drapeau. En faire un bouton distinct laissait croire à deux traitements.
const sansToucher = ref(false)

async function load() {
  chargement.value = true
  try {
    const res = await fetch(`/api/workspace?limit=${charge.value}`)
    // Une réponse 502 arrive en HTML : `res.json()` lèverait, la promesse
    // remonterait sans être attrapée, et l'écran resterait figé sans un mot.
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    data.value = await res.json()
    error.value = null
  } catch (e) {
    error.value = `Serveur injoignable (${e.message ?? 'sans réponse'}).`
  } finally {
    chargement.value = false
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
  // Un scan qui trouve des fichiers appelle une identification : les séparer
  // obligeait à revenir cliquer une fois l'analyse finie, sans que rien ne le
  // dise. On enchaîne, et l'utilisateur peut arrêter quand il veut.
  if (counts.value.unplanned) await plan()
}

/**
 * Identifie, par lots de cent, jusqu'au bout.
 *
 * Le serveur travaille par lot borné — sur une bibliothèque constituée, tout
 * planifier d'un tenant, c'est des heures d'appels au fournisseur pendant
 * lesquelles rien n'est applicable. Mais rendre la main entre deux lots faisait
 * porter le rythme à l'utilisateur : mille fichiers, dix clics, sans que le
 * bouton dise jamais qu'il en restait.
 *
 * La boucle enchaîne donc les lots elle-même, et s'arrête à trois conditions :
 * plus rien à identifier, un lot qui n'avance plus (le serveur refuse ou
 * échoue — sans cette garde on tournerait à l'infini), ou une demande d'arrêt.
 */
const stopPlan = ref(false)

async function plan({ reset = false } = {}) {
  busy.value = 'plan'
  stopPlan.value = false
  try {
    let premier = true
    while (premier || (!stopPlan.value && counts.value.unplanned > 0)) {
      const restantAvant = counts.value.unplanned
      const out = await call(`/api/review/plan?limit=100&reset=${premier && reset}`)
      await load()
      premier = false
      if (!out) break
      if (counts.value.unplanned >= restantAvant) {
        // Le lot n'a rien retiré de la file : insister ne ferait que répéter
        // le même appel. Mieux vaut s'arrêter et le dire.
        message.value =
          `Identification interrompue : ${counts.value.unplanned} fichier(s) n'ont pas pu ` +
          `être planifiés. Le bandeau ci-dessus dit ce qui bloque.`
        break
      }
    }
  } finally {
    busy.value = null
    stopPlan.value = false
  }
}

/**
 * Vérifie tout le trajet sans rien déplacer. Distinct d'« Exécuter » parce que
 * ce sont deux décisions : « est-ce que ça marcherait » et « fais-le ».
 */
// L'annulation vit désormais sur sa propre page, où chaque ligne se défait
// seule. Elle était ici en panneau replié derrière un bouton de barre : deux
// chemins vers le même geste obligent à se demander lequel fait foi, et le
// panneau ne montrait jamais que les dernières opérations.

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
/**
 * Supprime les dossiers vides laissés par le rangement.
 *
 * Silencieux quand il n'y a rien à faire — c'est le cas le plus fréquent, et
 * annoncer « 0 dossier supprimé » après chaque rangement serait du bruit. Un
 * échec, lui, se dit : il signale presque toujours un problème de droits.
 */
async function nettoyerVides() {
  try {
    const res = await fetch('/api/library/empty-dirs/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    })
    if (!res.ok) return
    const out = await res.json()
    if (out.removed) {
      message.value += ` ${out.removed} dossier(s) vide(s) supprimé(s).`
    }
    if (out.failed?.length) {
      message.value += ` ${out.failed.length} dossier(s) n'ont pas pu être supprimés.`
    }
  } catch {
    // Le rangement, lui, a réussi : un ménage raté ne doit pas le faire passer
    // pour un échec.
  }
}

async function apply(ids = null) {
  busy.value = 'apply'
  failures.value = []
  const blanc = sansToucher.value
  try {
    const out = await call('/api/review/apply', { plan_ids: ids, dry_run: blanc })
    if (out) {
      failures.value = out.results.filter((r) => !r.ok)
      message.value = blanc
        ? `Essai : ${out.applied} déplacement(s) possible(s), ${out.failed} bloqué(s).`
        : `${out.applied} fichier(s) rangé(s)` + (out.failed ? `, ${out.failed} en échec.` : '.')
      // La cause domine le compte : « 340 en échec » ne dit pas quoi faire,
      // « tous parce que la destination existe déjà » si.
      const [first] = failureGroups.value
      if (first) {
        const part = first.items.length === out.failed ? 'tous' : `dont ${first.items.length}`
        message.value += ` — ${part} : ${first.label.toLowerCase()}.`
      }
      await load()
      // Ranger laisse derrière lui le dossier de la release, vide. Le proposer
      // dans un écran de réglages revenait à demander un second geste pour
      // finir le premier — et personne ne va le chercher. Un dossier vide ne
      // contient rien à récupérer : le supprimer ne peut rien coûter.
      if (!blanc) await nettoyerVides()
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

/**
 * Ce qu'on dit d'un échec, et dans quel ordre.
 *
 * `fix` est ce qu'il y a À FAIRE, en une phrase visible. `detail` est le
 * pourquoi, replié dans un `<details>` : sept lignes d'explication posées
 * au-dessus de l'action à mener enterrent l'action, et on relit l'explication
 * une fois, jamais dix.
 */
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
    fix: "Sur le NAS : « ls -ln » sur le dossier concerné donne l'UID propriétaire, puis aligne PUID / PGID du conteneur Sortilège dessus — ou donne l'écriture au groupe que les deux conteneurs partagent.",
    detail:
      "Sortilège n'a pas le droit d'écrire là où il doit agir, le plus souvent dans le dossier de TÉLÉCHARGEMENT : ses fichiers appartiennent au client qui les a créés — JDownloader, un client torrent. Aucun réglage de Sortilège ne contourne cela, c'est une permission du NAS. Le droit qui manque porte d'ailleurs sur le DOSSIER, jamais sur le fichier : sous Unix, déplacer un fichier exige d'écrire dans le répertoire qui le contient.",
  },
  move_failed: {
    label: 'Déplacement impossible',
    fix: "Erreur système au moment du déplacement — le message exact ci-dessous dit laquelle : disque plein, volume en lecture seule, ou nom de fichier trop long.",
  },
  name_too_long: {
    label: 'Nom de fichier trop long',
    fix: "Raccourcis le nom de ces fichiers dans la source, puis relance « Analyser les sources » et « Identifier ».",
    detail:
      "La corbeille aplatit le chemin d'origine dans le nom du fichier — c'est ce qui permet de le remettre exactement d'où il vient — et une release au titre à rallonge fait dépasser la limite du système de fichiers. Ce n'est pas un problème de place disque : libérer des gigaoctets n'y changera rien.",
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
      detail: REASONS[key]?.detail ?? null,
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
async function evacuate(group, { mode = 'trash' } = {}) {
  // Le second clic est demandé par ConfirmAction, dans le gabarit. Le
  // redemander ici en ferait trois, dont un sans aucune trace à l'écran.
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
    const res = await fetch(`/api/media/plan/${id}/positions`)
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    preview.value = await res.json()
  } catch (e) {
    // Distinguer « ce fichier ne se lit pas » de « le serveur n'a pas répondu ».
    // Les confondre accusait le fichier d'un défaut qui venait du réseau, et
    // envoyait chercher la panne au mauvais endroit.
    preview.value = {
      available: false,
      playable_in_browser: false,
      panne: e.message ?? 'serveur injoignable',
    }
  }
}

// Image agrandie, ou null. Une jaquette de trente pixels ne permet pas de
// distinguer deux saisons d'une meme serie — et c'est pourtant sur elle qu'on
// tranche.
const zoom = ref(null)

function agrandir(src, legende) {
  if (src) zoom.value = { src, legende }
}

/**
 * Entrée et Espace sur une image déclarée bouton.
 *
 * Un `<img tabindex="0">` reçoit le focus mais n'a aucune activation native :
 * sans ce relais, on l'atteint au clavier sans jamais pouvoir l'ouvrir — pire
 * qu'un élément inatteignable, qui au moins ne promet rien. Espace demande le
 * `preventDefault` : c'est le raccourci de défilement de la page.
 */
function agrandirTouche(e, src, legende) {
  if (e.key !== 'Enter' && e.key !== ' ') return
  e.preventDefault()
  agrandir(src, legende)
}

// Remise a zero de l'etat de travail. En deux clics : ce qui part est
// reconstructible, mais recalculer six mille plans coute des heures d'appels.
async function remiseAZero() {
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

// --- Fermeture du menu « Entretien » ---------------------------------------
//
// Un menu qui ne se referme qu'en recliquant son propre bouton reste posé
// par-dessus la liste dès qu'on regarde ailleurs, et il n'existait aucun geste
// au clavier pour en sortir : une fois entré dedans au Tab, on ne pouvait que
// le traverser.
const menuRef = ref(null)

function fermerSiDehors(e) {
  if (!menuOuvert.value) return
  if (!menuRef.value?.contains(e.target)) menuOuvert.value = false
}

function surEchap(e) {
  if (e.key !== 'Escape' || !menuOuvert.value) return
  menuOuvert.value = false
  // Le focus revient sur le bouton qui a ouvert le menu. Sans cela il retombe
  // sur le document, et la tabulation suivante repart du haut de la page — on
  // perd l'endroit où on travaillait pour avoir fermé un menu.
  menuRef.value?.querySelector('button')?.focus()
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
  // `pointerdown` et non `click` : le menu doit être parti quand le clic
  // atteint ce qu'il visait, sinon on ferme le menu ET on active ce qui était
  // dessous, ou l'inverse selon l'ordre de propagation.
  document.addEventListener('pointerdown', fermerSiDehors)
  document.addEventListener('keydown', surEchap)
})
onUnmounted(() => {
  clearInterval(poller)
  document.removeEventListener('pointerdown', fermerSiDehors)
  document.removeEventListener('keydown', surEchap)
})
</script>

<template>
  <!-- Trois etats, et non « donnees ou rien ». Le message d'erreur etait
       enferme dans la condition qu'il devait remplacer : serveur injoignable,
       et l'ecran restait vide, sans un mot et sans bouton pour reessayer. -->
  <div v-if="!data && chargement" class="attente">
    <span class="pulsation"></span>
    Chargement de la médiathèque…
  </div>

  <div v-else-if="!data" class="panne">
    <h2>Le serveur ne répond pas</h2>
    <p>{{ error ?? 'Aucune réponse de Sortilège.' }}</p>
    <p class="quoi-faire">
      Vérifie que le conteneur tourne (<code>docker ps</code>), puis réessaie. Si la page
      reste blanche, les journaux disent pourquoi : <code>docker logs sortilege</code>.
    </p>
    <button class="primary" :disabled="chargement" @click="load">Réessayer</button>
  </div>

  <div v-else class="workspace">
    <!-- Barre d'action : les compteurs portent sur TOUT, pas sur la page -->
    <!-- Elle appartient au RANGEMENT. Dans la médiathèque, aucun de ces
         boutons n'a d'objet : on n'y analyse pas une source, on y regarde ce
         qu'on possède — et les voir là laissait croire qu'ils portaient sur
         elle. -->
    <div v-if="espace === 'source'" class="toolbar">
      <button :disabled="busy || working" @click="scan">Analyser les sources</button>
      <!-- Pendant la boucle, le bouton devient sa propre sortie : une action
           longue sans moyen de l'arrêter oblige à recharger la page. -->
      <button v-if="busy === 'plan'" class="ghost" @click="stopPlan = true">
        {{ stopPlan ? 'Arrêt après ce lot…' : `Arrêter (${counts.unplanned} restants)` }}
      </button>
      <button v-else :disabled="busy || working || !counts.unplanned" @click="plan">
        Identifier {{ counts.unplanned ? `(${counts.unplanned})` : '' }}
      </button>
      <button class="primary" :disabled="busy || !counts.ready" @click="apply()">
        {{ sansToucher ? 'Essayer' : 'Ranger' }} {{ counts.ready }} prêt{{ counts.ready > 1 ? 's' : '' }}
      </button>
      <!-- « Simuler » etait un bouton a part pour le MEME appel serveur, avec
           un drapeau different. Devenu une case attachee au bouton principal :
           c'est une variante de l'execution, pas une autre action. -->
      <label class="essai" :title="'Ne déplace rien, montre seulement ce qui serait fait'">
        <input type="checkbox" v-model="sansToucher" />
        essai à blanc
      </label>
      <span class="spacer"></span>

      <!-- Quatre boutons de même poids visuel ne disaient pas lequel sert tous
           les jours. Ceux d'entretien passent derrière un menu : on les cherche
           quand on en a besoin, ils n'encombrent pas le reste du temps. -->
      <div ref="menuRef" class="menu-entretien">
        <button
          class="ghost"
          :class="{ active: menuOuvert }"
          :aria-expanded="menuOuvert"
          @click="menuOuvert = !menuOuvert"
        >
          Entretien ▾
        </button>
        <div v-if="menuOuvert" class="tiroir" @click="menuOuvert = false">
          <button :disabled="busy || working" @click="index">
            Relire la bibliothèque
            <span class="quoi">reconstruit l'index depuis le disque</span>
          </button>
          <button
            v-if="counts.works"
            :disabled="busy || working"
            @click="plan({ reset: true })"
          >
            Recommencer l'identification
            <span class="quoi">vide la file de plans et repart du premier fichier</span>
          </button>
          <ConfirmAction
            v-if="counts.works"
            label="Tout effacer"
            confirm-label="Confirmer : tout effacer"
            :detail="`Vide la liste — ${counts.works} œuvre(s), plans et aperçus. Ton journal `
              + `d'annulation (${data.journal_size}) et tes identifications retenues sont `
              + `conservés, et aucun fichier n'est déplacé.`"
            :busy="busy === 'reset'"
            :disabled="busy || working"
            :disabled-reason="raisonIndispo"
            @confirm="remiseAZero"
          />
        </div>
      </div>
    </div>

    <!-- Pourquoi la barre ne répond pas, sous la barre. Une seule ligne : la
         cause est commune à presque tous ces boutons, et un `title` par bouton
         n'existe ni au doigt ni au clavier. `role="status"` pour qu'un
         changement d'état soit annoncé sans voler le focus. -->
    <p v-if="espace === 'source' && raisonIndispo" class="indispo" role="status">
      {{ raisonIndispo }}
    </p>

    <!-- Une seule action porte sur la médiathèque : la relire. Le reste de
         l'entretien vit avec le rangement, qui le produit. -->
    <div v-if="espace === 'library'" class="toolbar">
      <button :disabled="busy || working" @click="index">Relire la bibliothèque</button>
      <span class="quoi-inline">reconstruit l'index depuis le disque</span>
    </div>

    <!-- Au FUTUR, et en disant ce qu'il reste à faire. La formulation au
         présent laissait croire que l'action avait déjà eu lieu, alors que le
         bouton attend un second clic. -->


    <!-- Ce qui a été rangé, par œuvre, avec une annulation par ligne -->

    <!-- Ce qui tourne, quand quelque chose tourne -->
    <div v-if="activity" class="activity">
      <div class="bar"><div class="fill" :style="{ width: activity.pct + '%' }"></div></div>
      <div class="stats">
        <span class="label">{{ activity.label }}</span>
        <span v-if="activity.total">{{ activity.processed }} / {{ activity.total }}</span>
        <span v-if="activity.current" class="current">{{ activity.current }}</span>
      </div>
    </div>

    <!-- Ce qui empeche l'application de fonctionner, AVANT la liste. Ces
         diagnostics etaient calcules cote serveur depuis toujours ; ils
         n'etaient transportes jusqu'a aucun ecran. Une cle absente se lisait
         donc comme « aucun candidat » sur trois cents fichiers. -->
    <section v-if="blocages.length" class="blocages">
      <h3>À régler avant d'aller plus loin</h3>
      <ul>
        <li v-for="b in blocages" :key="b.code">
          <span class="quoi">{{ b.message }}</span>
          <span v-if="b.where" class="ou">→ {{ b.where }}</span>
        </li>
      </ul>
    </section>

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
          <!-- Le pourquoi replié, et natif : un `<details>` s'ouvre au clavier,
               retient son état, et n'a besoin d'aucun script. Déplié par
               défaut, il repoussait l'action à mener sous sept lignes
               d'explication qu'on ne relit jamais. -->
          <details v-if="g.detail" class="pourquoi">
            <summary>Pourquoi ?</summary>
            <p>{{ g.detail }}</p>
          </details>
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
            <ConfirmAction
              label="Supprimer sans passer par la corbeille"
              :confirm-label="`Confirmer : supprimer ces ${g.items.length} copies`"
              :detail="`${g.items.length} fichier(s) effacé(s) définitivement. Rien ne les `
                + `rendra : l'exemplaire déjà rangé, lui, ne bouge pas.`"
              :busy="evacuating"
              :disabled="evacuating"
              disabled-reason="Une évacuation est déjà en cours."
              @confirm="evacuate(g, { mode: 'delete' })"
            />
          </div>

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

    <template v-else-if="onglet === 'library'">
      <!-- Deux questions distinctes, longtemps mélangées dans une seule rangée
           de filtres : « qu'est-ce que je possède » et « où sont mes 600 Go ».
           La seconde est la raison d'être de l'outil, elle méritait son écran. -->
      <div class="sous-vues">
        <button :class="{ actif: sousVue === 'avoir' }" @click="sousVue = 'avoir'">
          Ce que je possède
        </button>
        <button :class="{ actif: sousVue === 'place' }" @click="sousVue = 'place'">
          Récupérer de la place
          <span v-if="placeRecuperable" class="gain">≈ {{ gb(placeRecuperable) }} Go</span>
        </button>
      </div>

      <div v-if="sousVue === 'avoir'" class="filters">
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
          Tout ({{ parOnglet.length }})
        </button>
        <button v-if="counts.missing" :class="{ active: filter === 'gaps' }" @click="filter = 'gaps'">
          Épisodes manquants ({{ counts.missing }})
        </button>
      </div>

      <!-- Ces filtres restent VISIBLES a zero. Ils disparaissaient quand leur
           compteur tombait a zero, ce qui rendait « il manque le filtre
           doublons » indiscernable de « il n'y a pas de doublons » — et une
           bibliotheque jamais indexee affiche zero partout. -->
      <div v-else class="filters">
        <button :class="{ active: filter === 'place' }" @click="filter = 'place'">
          Tout ce qui pèse pour rien ({{ parOnglet.length }})
        </button>
        <button
          :class="{ active: filter === 'dupes', muet: !counts.duplicates }"
          :disabled="!counts.duplicates"
          :title="counts.duplicates ? '' : sansIndex
            ? 'La bibliothèque n\'a pas encore été lue : rien à comparer'
            : 'Aucun doublon détecté'"
          @click="filter = 'dupes'"
        >
          Doublons ({{ counts.duplicates ?? 0 }})
        </button>
        <button
          class="heavy-filter"
          :class="{ active: filter === 'heavy', muet: !counts.heavy }"
          :disabled="!counts.heavy"
          :title="counts.heavy
            ? `Au moins ${data.heavy_ratio} fois le poids habituel de leur type`
            : 'Aucun fichier anormalement lourd'"
          @click="filter = 'heavy'"
        >
          Surpoids ({{ counts.heavy ?? 0 }})
        </button>
        <button
          :class="{ active: filter === 'offstrat', muet: !counts.off_strategy }"
          :disabled="!counts.off_strategy"
          :title="'Fichiers dont la résolution ne suit pas la stratégie choisie pour leur type'"
          @click="filter = 'offstrat'"
        >
          Hors stratégie ({{ counts.off_strategy ?? 0 }})
          <span v-if="counts.recoverable_bytes" class="gain">
            −{{ gb(counts.recoverable_bytes) }} Go
          </span>
        </button>
      </div>

      <!-- Le cas qui explique tous les zeros d'un coup. -->
      <p v-if="sansIndex" class="hint-index">
        La bibliothèque n'a jamais été lue : doublons, surpoids et écarts à la stratégie
        se calculent dessus, donc tout affiche zéro.
        <button class="small" :disabled="busy || working" @click="index">
          Lire la bibliothèque maintenant
        </button>
      </p>

      <!-- Un écran vide qui ne dit rien laisse croire à une panne. Ici il dit
           ce qui a été cherché, et ce qui reste à régler pour trouver mieux. -->
      <p v-if="sousVue === 'place' && !parOnglet.length" class="rien-a-gagner">
        Rien à récupérer : aucun doublon, aucun fichier anormalement lourd, et tout
        respecte la stratégie de son type. Les dossiers vides et la corbeille se vident
        dans <em>Réglages → Bibliothèque</em> et <em>Réglages → Système</em>.
      </p>
    </template>

    <div v-if="onglet === 'library' && filter === 'dupes' && counts.duplicates" class="lot">
      <span class="warn-text">
        Ne garde qu'un exemplaire par emplacement : celui que la stratégie de son type
        désigne. Porte sur TOUTE la bibliothèque, pas seulement sur les lignes affichées.
      </span>
      <ConfirmAction
        label="Nettoyer tous les doublons"
        :confirm-label="`Confirmer — ${counts.duplicates} emplacement(s)`"
        :detail="`Supprime les exemplaires en trop de ${counts.duplicates} emplacement(s) sur `
          + `toute la bibliothèque. Celui que la stratégie désigne reste en place.`"
        :busy="busy === 'prune'"
        :disabled="busy === 'prune'"
        disabled-reason="Un nettoyage est déjà en cours."
        @confirm="pruneDuplicates"
      />
    </div>

    <div class="outils">
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

    <!-- Livres et animes rejoignent films et séries. Le type existait dans les
         données depuis toujours ; seuls deux des quatre avaient un bouton, et
         rien ne disait que les deux autres étaient filtrables. -->
    <div class="filters kinds">
      <button :class="{ active: kind === 'all' }" @click="kind = 'all'">Tous types</button>
      <button
        v-for="k in KIND_FILTRES"
        :key="k.id"
        :class="{ active: kind === k.id, muet: !kindCounts[k.id] }"
        :disabled="!kindCounts[k.id]"
        @click="kind = k.id"
      >
        {{ k.label }} ({{ kindCounts[k.id] }})
      </button>
      <span v-if="counts.total_bytes" class="total">
        {{ gb(counts.total_bytes) }} Go en bibliothèque
      </span>
    </div>

    <!-- La raison des boutons grisés, écrite plutôt que cachée dans un
         `title`. Elle nomme la LISTE et non la médiathèque : le type existe
         peut-être ailleurs, il n'est simplement pas ici. -->
    <p v-if="typesVides.length" class="indispo">
      Aucun résultat de ce type dans cette liste : {{ typesVides.join(', ') }}. Le filtre
      reste affiché — un compte à zéro dit qu'on a mesuré, un bouton absent ne dit rien.
    </p>

    <p
      v-if="!works.length && !(onglet === 'library' && sousVue === 'place')"
      class="empty"
    >
      <template v-if="recherche">Aucun titre ne correspond à « {{ recherche }} ».</template>
      <template v-else-if="onglet === 'source'">
        Rien ne traîne dans la source. C'est l'état recherché — lance « Analyser les sources »
        si tu viens d'ajouter des fichiers.
      </template>
      <template v-else>
        La bibliothèque est vide pour ce filtre. « Relire la bibliothèque » la reconstruit.
      </template>
    </p>

    <ul class="works">
      <li v-for="w in listed" :key="w.key" :class="{ open: open.has(w.key) }">
        <!-- La jaquette a QUITTÉ le bouton qui l'enveloppait. Un élément
             cliquable imbriqué dans un bouton est invalide, et le navigateur
             s'en sortait en n'en rendant qu'un seul atteignable : la jaquette
             n'existait ni au Tab ni pour un lecteur d'écran, alors que c'est
             sur elle qu'on tranche entre deux saisons.
             Elle passe donc en tête de ligne, sœur du bouton et non son
             enfant ; le chevron la suit et reste aligné, les jaquettes ayant
             toutes la même largeur, vides comprises. -->
        <div class="row-wrap">
          <img
            v-if="w.poster_url"
            class="thumb zoomable"
            :src="w.poster_url"
            :alt="`Jaquette de ${w.title}`"
            loading="lazy"
            role="button"
            tabindex="0"
            :aria-label="`Agrandir la jaquette de ${w.title}`"
            @click="agrandir(w.poster_url, `${w.title}${w.year ? ` (${w.year})` : ''}`)"
            @keydown="(e) => agrandirTouche(e, w.poster_url, `${w.title}${w.year ? ` (${w.year})` : ''}`)"
          />
          <span v-else class="thumb empty"></span>

          <button class="row" :aria-expanded="open.has(w.key)" @click="toggle(w.key)">
            <span class="chev" :class="{ closed: !open.has(w.key) }">▾</span>

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
              <!-- Sur la LIGNE, pas seulement dans le détail : c'est là qu'on
                   décide d'ouvrir. Une identification proposée par un modèle de
                   langage mérite un coup d'œil que la même à 100 % venue de TMDB
                   ne demande pas. -->
              <span v-if="origines(w).ia" class="badge ia" title="Titre proposé par le résolveur IA, fiche confirmée par le fournisseur">
                IA
              </span>
              <span v-else-if="origines(w).nom" class="badge devine" title="Deviné depuis le nom du fichier, sans fournisseur">
                nom seul
              </span>
            </span>
          </button>
        </div>

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
                  <span class="origine" :title="origine(p).long">{{ origine(p).court }}</span>
                  <code class="from">{{ shortPath(p.source) }}</code>
                  <span class="arrow">→</span>
                  <code class="to">{{ shortPath(p.destination) }}</code>
                  <button class="small play" title="Vérifier avant de ranger"
                          @click="togglePlayer(p.id)">
                    {{ playing === p.id ? 'Fermer' : '▶' }}
                  </button>
                  <!-- Un choix ne se verrouille pas. Une fois le plan passé en
                       « prêt », le bouton d'arbitrage disparaissait : une
                       identification manuelle erronée n'était plus corrigeable,
                       et il fallait tout effacer pour revenir dessus. -->
                  <button
                    class="small"
                    :title="p.manual ? 'Revenir sur ton choix' : 'Choisir une autre œuvre'"
                    @click="picking = picking === p.id ? null : p.id"
                  >
                    {{ p.manual ? 'Changer' : "Ce n'est pas ça" }}
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
                      <!-- Déclarées boutons et atteignables au Tab : ces
                           vignettes sont l'aperçu, pas une illustration — on
                           les agrandit pour trancher, et un `<img>` cliquable
                           nu n'existe pour aucun autre dispositif que la
                           souris. -->
                      <img
                        v-for="pos in preview.positions"
                        :key="pos"
                        :src="`/api/media/plan/${p.id}/thumb?at=${pos}`"
                        :alt="`Aperçu à ${Math.round(pos * 100)} % du fichier`"
                        loading="lazy"
                        role="button"
                        tabindex="0"
                        :aria-label="`Agrandir l'aperçu à ${Math.round(pos * 100)} %`"
                        class="zoomable"
                        @click="agrandir(
                          `/api/media/plan/${p.id}/thumb?at=${pos}`,
                          `${shortPath(p.source)} — à ${Math.round(pos * 100)} % du fichier`,
                        )"
                        @keydown="(e) => agrandirTouche(
                          e,
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
                  <p v-else-if="preview?.panne" class="format-warn">
                    Aperçu indisponible : {{ preview.panne }}. Le fichier n'est pas en cause.
                  </p>
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
                  <span class="origine" :title="origine(p).long">{{ origine(p).court }}</span>
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
                      <!-- Déclarées boutons et atteignables au Tab : ces
                           vignettes sont l'aperçu, pas une illustration — on
                           les agrandit pour trancher, et un `<img>` cliquable
                           nu n'existe pour aucun autre dispositif que la
                           souris. -->
                      <img
                        v-for="pos in preview.positions"
                        :key="pos"
                        :src="`/api/media/plan/${p.id}/thumb?at=${pos}`"
                        :alt="`Aperçu à ${Math.round(pos * 100)} % du fichier`"
                        loading="lazy"
                        role="button"
                        tabindex="0"
                        :aria-label="`Agrandir l'aperçu à ${Math.round(pos * 100)} %`"
                        class="zoomable"
                        @click="agrandir(
                          `/api/media/plan/${p.id}/thumb?at=${pos}`,
                          `${shortPath(p.source)} — à ${Math.round(pos * 100)} % du fichier`,
                        )"
                        @keydown="(e) => agrandirTouche(
                          e,
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
                  <p v-else-if="preview?.panne" class="format-warn">
                    Aperçu indisponible : {{ preview.panne }}. Le fichier n'est pas en cause.
                  </p>
                  <p v-else-if="preview" class="format-warn">
                    Aperçu impossible : ffmpeg absent, ou fichier illisible.
                  </p>
                  <p v-else class="thumb-note">Chargement de l'aperçu…</p>
                </li>
              </template>
            </ul>
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
          <!-- Le sélecteur vit au niveau du détail, pas dans une section :
               il sert aux plans PRÊTS comme aux douteux, et ceux-ci vivent dans
               deux blocs distincts. L'enfermer dans l'un des deux privait
               l'autre de toute possibilité de correction. -->
          <template v-for="p in [...w.pending.ready, ...arbitrables(w)]" :key="`pick-${p.id}`">
            <CandidatePicker
              v-if="picking === p.id"
              :candidates="p.alternatives ?? []"
              :busy="choosing"
              :plan-id="p.id"
              :plan-kind="p.kind || w.kind"
              @choose="(c) => choose(p.id, c)"
              @close="picking = null"
            />
          </template>

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
              <ConfirmAction
                label="Supprimer selon la stratégie"
                confirm-label="Confirmer la suppression"
                :detail="`Garde l'exemplaire que la stratégie de ce type désigne et supprime `
                  + `les autres, définitivement. Le serveur vérifie que celui qu'on garde `
                  + `existe avant chaque suppression.`"
                :busy="busy === 'delete'"
                :disabled="busy === 'delete'"
                disabled-reason="Une suppression est déjà en cours."
                @confirm="deleteDuplicates(w)"
              />
              <span v-if="dupeMessage[w.key]" class="dupe-msg">{{ dupeMessage[w.key] }}</span>
            </div>
            <!-- Un livre rangé n'est pas un livre lu : sans lecteur, vérifier
                 qu'un fichier est bien ce qu'il prétend demanderait de le
                 télécharger et d'ouvrir une autre application. -->
            <ul v-if="w.owned.books?.length" class="fichiers livres">
              <li v-for="b in w.owned.books" :key="b.path">
                <button class="small play" @click="lecture = b.path">Lire</button>
                <span class="poids">{{ (b.size_bytes / 1024 ** 2).toFixed(1) }} Mo</span>
                <code>{{ b.path }}</code>
              </li>
            </ul>

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

    <BookReader v-if="lecture" :path="lecture" @close="lecture = null" />

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
.quoi-inline { font-size: var(--t-xs); color: var(--text-faint); }
.badge.ia {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  color: var(--accent);
}
.badge.devine { background: var(--surface-2); color: var(--text-faint); }
/* Un filtre a zero reste lisible : le griser sans l'effacer dit « mesuré, et
   il n'y en a pas », la ou son absence ne dit rien du tout. */
.filters button.muet { opacity: .55; cursor: default; }
.hint-index {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 2px 0 0; font-size: 12px; color: var(--text-dim);
}
.sous-vues { display: flex; gap: 6px; }
.sous-vues button {
  font-size: 12.5px; padding: 6px 13px; border-radius: 7px;
  background: var(--surface); border: 1px solid var(--border); color: var(--text-dim);
  display: flex; align-items: center; gap: 8px;
}
.sous-vues button.actif { border-color: var(--accent); color: var(--text); }
.sous-vues .gain { font-family: var(--mono); font-size: 11px; color: var(--accent); }
.rien-a-gagner {
  font-size: 12.5px; color: var(--text-dim); line-height: 1.7;
  max-width: 60em; margin: 4px 0 0;
}
.menu-entretien { position: relative; }
.tiroir {
  position: absolute; right: 0; top: calc(100% + 5px); z-index: 20;
  display: flex; flex-direction: column; min-width: 250px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 5px; box-shadow: 0 8px 24px rgba(0, 0, 0, .35);
}
.tiroir button {
  display: flex; flex-direction: column; align-items: flex-start; gap: 1px;
  text-align: left; border: 0; background: transparent; padding: 7px 9px;
  border-radius: 6px; font-size: 12.5px; width: 100%;
}
.tiroir button:hover:not(:disabled) { background: var(--surface-2); }
.tiroir button .quoi { font-size: 11px; color: var(--text-faint); }
.tiroir button.danger { color: var(--warn); }
/* --- Les trois etats de la page ---------------------------------------- */
.attente, .panne {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 12px; min-height: 40vh; text-align: center; padding: 40px 20px;
}
.attente { color: var(--text-dim); font-size: 13px; }
.pulsation {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2px solid var(--border); border-top-color: var(--accent);
  animation: tourne 1s linear infinite;
}
@keyframes tourne { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .pulsation { animation: none; } }
.panne h2 { margin: 0; font-size: 17px; }
.panne p { margin: 0; font-size: 13px; color: var(--text-dim); max-width: 46em; }
.panne .quoi-faire { color: var(--text-faint); font-size: 12.5px; }
.panne code { font-family: var(--mono); font-size: 12px; }

/* --- Ce qui bloque, avant tout le reste -------------------------------- */
.blocages {
  padding: 12px 14px; border-radius: 8px;
  background: color-mix(in srgb, var(--warn) 9%, transparent);
  border: 1px solid color-mix(in srgb, var(--warn) 32%, transparent);
}
.blocages h3 {
  margin: 0 0 8px; font-size: 11px; text-transform: uppercase;
  letter-spacing: .07em; color: var(--warn);
}
.blocages ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.blocages li { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; font-size: 13px; }
.blocages .ou { font-size: 11.5px; color: var(--text-dim); }

.essai { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--text-dim); }
.essai input { accent-color: var(--accent); }
.origine {
  font-size: 10px; padding: 1px 7px; border-radius: 20px; white-space: nowrap;
  background: var(--surface-2); color: var(--text-faint);
  text-transform: uppercase; letter-spacing: .04em;
}
.rien { display: flex; flex-direction: column; gap: 10px; }
.par-type { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.par-type li { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; font-size: 12px; }
.par-type strong { min-width: 70px; }
.par-type .puce {
  font-size: 10.5px; padding: 1px 8px; border-radius: 20px;
  background: var(--surface-2); color: var(--text-dim);
}
.par-type .motif { font-size: 11.5px; color: var(--text-faint); }
.par-type .motif.ok { color: var(--accent); }
.fichiers.livres code { color: var(--text-dim); }
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
.onglets { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 4px; }
.onglet-retour { font-size: 11.5px; color: var(--text-faint); }
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
button.small.danger { color: var(--err); }
button.small.danger:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--warn) 40%, transparent);
}
.workspace { display: flex; flex-direction: column; gap: 14px; }

.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }
.toolbar .ghost { color: var(--text-faint); }
.toolbar .ghost.active { border-color: var(--accent); color: var(--text); }
.toolbar .ghost.danger:hover:not(:disabled) { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }

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
.pourquoi { margin: 5px 0 0; max-width: 680px; }
.pourquoi summary {
  font-size: 11.5px; color: var(--text-faint); cursor: pointer;
  width: fit-content; padding: 2px 0;
}
.pourquoi summary:hover { color: var(--text-dim); }
.pourquoi summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.pourquoi p { margin: 5px 0 0; font-size: 12px; color: var(--text-faint); line-height: 1.65; }
.failures .actions { display: flex; gap: 8px; margin: 9px 0 0; flex-wrap: wrap; }
.failures .act { font-size: 12px; padding: 4px 12px; }
.failures .act.danger { color: var(--text-faint); }
.failures .act.danger:hover:not(:disabled) { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }
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

/* Le rembourrage et le survol vivent sur l'enveloppe, plus sur le bouton : la
   jaquette est sortie de celui-ci et doit rester dans la même bande cliquable,
   à la même hauteur. */
.row-wrap { display: flex; align-items: center; gap: 11px; padding: 8px 12px; }
.row-wrap:hover { background: var(--surface-2); }
.row {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: 11px; padding: 0;
  border: none; background: none; text-align: left; cursor: pointer;
}
/* Le liseré rentre à l'intérieur : la carte est en `overflow: hidden` et un
   `outline-offset` positif serait rogné sur ses bords — la ligne au clavier
   deviendrait invisible précisément là où on en a besoin. */
.row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: 6px; }
.chev { font-size: 10px; color: var(--text-faint); transition: transform .15s; flex: none; }
.chev.closed { transform: rotate(-90deg); }

.thumb { width: 30px; height: 45px; border-radius: 3px; object-fit: cover; background: var(--surface-2); flex: none; }
.thumb.empty { border: 1px dashed var(--border); }
.zoomable { cursor: zoom-in; }
.zoomable:hover { outline: 1px solid var(--accent); outline-offset: 1px; }
/* Deux pixels pleins et non le liseré de survol : le survol suggère, le focus
   doit affirmer — c'est le seul repère de qui ne voit pas le curseur. */
.zoomable:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

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
/* `overflow-wrap: anywhere` comme les autres blocs de chemin : un chemin n'a
   aucune coupure naturelle, et sans cela il pousse la ligne au-delà de l'écran
   au lieu de se replier. C'était le seul bloc `code` à ne pas l'avoir. */
.files code {
  font-family: var(--mono); font-size: 10.5px; color: var(--text-faint);
  overflow-wrap: anywhere;
}
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

/* La raison d'une indisponibilité : ton d'appoint, jamais celui d'une alerte.
   Ce n'est pas une erreur — c'est l'état normal de l'application dit à voix
   haute, et l'écrire en rouge apprendrait à ignorer le rouge. */
.indispo {
  margin: -6px 0 0; font-size: 12px; color: var(--text-faint);
  line-height: 1.65; max-width: 720px;
}

/* Une panne locale : le reste de l'écran fonctionne, seul ce panneau est
   aveugle. D'où le cadre plutôt qu'un bandeau en haut de page — l'erreur est
   là où elle s'est produite, avec le bouton qui la relance. */
.panne-inline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 0; padding: 9px 12px; border-radius: 8px;
  font-size: 12px; line-height: 1.6; color: var(--err);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.panne-inline button { color: var(--text); flex: none; }

/* --- Écrans étroits ------------------------------------------------------ *
 * Ce fichier n'avait aucune règle de largeur : deux mille lignes pensées pour
 * un écran large, et rien pour un téléphone — alors que « où sont mes 600 Go »
 * est exactement la question qu'on se pose depuis le canapé.
 *
 * Deux dégâts précis, invisibles au bureau :
 *
 * - la ligne d'œuvre ne se repliait pas, et les badges — `flex: none` et
 *   `white-space: nowrap` dans une carte en `overflow: hidden` — étaient
 *   COUPÉS net par le bord. Pas rétrécis, pas empilés : absents. Le compte de
 *   doublons et le « à arbitrer » disparaissaient donc en silence, et ce sont
 *   les deux seules raisons d'ouvrir une ligne.
 * - les boutons `.small` faisaient vingt pixels de haut. On les vise, on les
 *   rate, on repose le téléphone. */
@media (max-width: 700px) {
  /* La ligne se replie, et les badges prennent leur propre rang plutôt que de
     disputer une largeur qui n'existe pas. */
  .row-wrap { align-items: flex-start; }
  .row { flex-wrap: wrap; row-gap: 5px; }
  .badges { flex: 1 0 100%; justify-content: flex-start; }
  .badge { white-space: normal; }

  /* 32 px de cible, obtenus au rembourrage. Grossir la police à la place
     déplacerait toute la hiérarchie typographique de l'écran pour résoudre un
     problème de doigt. */
  button.small { min-height: 32px; padding: 7px 12px; }
  .filters button { min-height: 32px; padding: 6px 12px; }

  /* Le champ de recherche prend la ligne : à 180 px il partageait le rang avec
     le tri et les deux devenaient illisibles. */
  .recherche { max-width: none; flex: 1 0 100%; }
  .outils { row-gap: 8px; }

  /* Le tiroir d'entretien débordait par la droite : ancré au bord de l'écran,
     il gardait ses 250 px minimum et poussait la page en largeur. */
  .tiroir { min-width: 0; width: min(280px, calc(100vw - 40px)); }

  /* Un chemin monospace ne se coupe nulle part : sans cela il impose sa
     largeur à la carte entière. */
  .fautifs .chemin, .refus code, 
    .detail { padding: 4px 10px 12px; }
}
</style>
