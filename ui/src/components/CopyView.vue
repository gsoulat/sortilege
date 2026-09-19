<script>
/**
 * Ce qui survit à un changement d'écran.
 *
 * App.vue démonte la vue quand on passe au Journal : sans ce bloc, cocher
 * cinquante œuvres puis aller vérifier une ligne du Journal les décochait
 * toutes. Un `<script>` ordinaire s'exécute une fois par chargement de
 * l'application, pas à chaque montage — ce qu'il garde vit donc jusqu'au
 * rechargement de la page, et pas plus : rien n'est écrit dans le navigateur.
 */
const memoire = {
  selection: new Set(),
  disque: '',
  sidecars: true,
  debit: '',
  bilan: null,
  rapportMasque: null,
}
</script>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import ConfirmAction from './ConfirmAction.vue'
import {
  accord,
  constatServeur,
  motif,
  nombre,
  pluriel,
  serveurEnErreur,
  serveurMuet,
  verbe,
} from '../lib/langue.js'

/**
 * Copier des œuvres de la médiathèque vers un disque externe.
 *
 * Trois choix portent la page :
 *
 * 1. **Le disque dit ce qu'il a déjà.** On ne copie pas une liste d'œuvres, on
 *    copie ce qui MANQUE sur ce disque-là. Chaque ligne cochée porte donc son
 *    état relevé sur le disque (« déjà sur le disque », « partiel — 3 sur
 *    10 »), et le bouton ne compte que les fichiers qui partiront vraiment.
 * 2. **Un fichier à la fois.** Un port USB partagé entre deux transferts va
 *    moins vite que servi à un seul, et un disque qui chauffe décroche. Le
 *    serveur copie en file ; l'écran montre la file, fichier par fichier.
 * 3. **L'écran ne prétend jamais savoir.** Quand le contact est perdu en
 *    pleine copie, le serveur continue sans nous : on le dit, on relit son
 *    état, et on ne devine rien entre-temps.
 */

// --- Réglages de l'écran -----------------------------------------------------

/** Lignes rendues d'un coup. 1 200 cases à cocher rendues ensemble figent la
 *  frappe dans la recherche ; on déroule par paliers, comme Ma médiathèque. */
const PALIER = 200
/** Assez long pour cocher trois œuvres d'affilée sans relancer trois lectures
 *  du disque, assez court pour que l'état arrive avant qu'on le cherche. */
const DELAI_ANALYSE_MS = 700
const SONDAGE_MS = 1000
const TERMINES_MONTRES = 10
const A_VENIR_MONTRES = 20

// Même base que `gb()` de Ma médiathèque (octets / 1024³) : un « 1,0 Go » écrit
// en 10 s doit se lire « 102,4 Mo/s », pas un chiffre calculé autrement que
// la taille affichée juste à côté.
const GO = 1024 ** 3
const MO = 1024 ** 2
const KO = 1024

/** « TV » dans la bouche de l'utilisateur, ce sont les séries. */
const TYPES = [
  { id: 'movie', label: 'Films', aucun: 'Aucun film' },
  { id: 'episode', label: 'Séries', aucun: 'Aucune série' },
  { id: 'anime', label: 'Animes', aucun: 'Aucun anime' },
  { id: 'book', label: 'Livres', aucun: 'Aucun livre' },
]
const NATURE = { movie: 'Film', episode: 'Série', anime: 'Anime', book: 'Livre' }

const SYSTEMES = {
  exfat: 'exFAT',
  vfat: 'FAT32',
  fat: 'FAT32',
  fat32: 'FAT32',
  msdos: 'FAT32',
  ntfs: 'NTFS',
  ntfs3: 'NTFS',
  ext2: 'ext2',
  ext3: 'ext3',
  ext4: 'ext4',
  btrfs: 'Btrfs',
  xfs: 'XFS',
  hfsplus: 'HFS+',
  apfs: 'APFS',
}

const EXPLICATIONS = {
  conflict:
    "Un fichier différent porte déjà ce nom sur le disque. Il n'est pas écrasé : déplace ou renomme celui du disque si tu veux copier celui-ci.",
  too_large:
    "Le système de fichiers du disque refuse un fichier de cette taille (FAT32 s'arrête à 4 Go par fichier). Un disque en exFAT n'a pas cette limite.",
  name_invalid:
    'Le système de fichiers du disque refuse ce nom : un caractère interdit (\\ / : * ? " < > |) ou un nom trop long.',
  missing_source:
    "Le fichier n'est plus là où la médiathèque l'a vu. « Relire la médiathèque », dans Ma médiathèque, remet l'index à jour.",
  symlink:
    "Un lien symbolique est posé sur le disque à cet endroit. Sortilège ne suit jamais un lien : ce qui est derrière pourrait être n'importe où, jusque dans la médiathèque. Remplace le lien par un vrai dossier sur le disque.",
  other_volume:
    "Un autre volume est monté sous le disque à cet endroit : ce n'est plus le disque choisi. Sortilège n'y écrit pas et ne le compte pas.",
}

// --- Formats -----------------------------------------------------------------

function decimales(x, n) {
  return x.toLocaleString('fr-FR', { minimumFractionDigits: n, maximumFractionDigits: n })
}

/** Espace insécable : « 12,4 » ne part jamais seul en fin de ligne sans « Go ». */
const INSECABLE = ' '

/** « 4,0 Go », « 12,4 Go », « 184 Go », « 312 Mo ». Une décimale sous 100 Go :
 *  c'est là qu'un fichier avance et qu'une reprise se situe, et « 12 Go sur
 *  40 Go » ne dirait pas où reprend un fichier arrêté à 12,4. */
function taille(octets) {
  const v = Math.max(0, Number(octets) || 0)
  if (v >= GO) return `${decimales(v / GO, v >= 100 * GO ? 0 : 1)}${INSECABLE}Go`
  if (v >= MO) return `${decimales(v / MO, 0)}${INSECABLE}Mo`
  if (v > 0) return `${decimales(Math.max(1, v / KO), 0)}${INSECABLE}Ko`
  // Zéro n'a pas d'unité propre : « 0 Go sur 25 Mo » mélangeait deux échelles
  // dans la même phrase. Le Mo est l'unité où zéro se lit le plus naturellement.
  return `0${INSECABLE}Mo`
}

/** Un débit à zéro n'est pas une mesure : c'est l'absence de mesure. */
function debit(octetsParSeconde) {
  const v = Number(octetsParSeconde) || 0
  if (v <= 0) return 'mesure…'
  return `${decimales(v / MO, 1)}${INSECABLE}Mo/s`
}

function debitMoyen(f) {
  const v = Number(f?.speed_bps) || 0
  if (v > 0) return debit(v)
  const s = Number(f?.duration_s) || 0
  return s > 0 ? debit((Number(f?.bytes) || 0) / s) : 'débit non mesuré'
}

function duree(secondes) {
  const s = Number(secondes)
  if (secondes == null || !Number.isFinite(s) || s < 0) return null
  if (s < 60) return "moins d'une minute"
  const minutes = Math.round(s / 60)
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  if (!h) return `${m}${INSECABLE}min`
  return m ? [h, 'h', String(m).padStart(2, '0')].join(INSECABLE) : `${h}${INSECABLE}h`
}

/** Pourcentage flottant, borné : la largeur de barre veut la fraction exacte. */
function pourcent(fait, total) {
  const t = Number(total) || 0
  if (t <= 0) return 0
  return Math.min(100, Math.max(0, ((Number(fait) || 0) / t) * 100))
}

function nomSysteme(fs) {
  if (!fs) return ''
  return SYSTEMES[String(fs).toLowerCase()] ?? String(fs)
}

const base = (chemin) => (chemin ? String(chemin).split('/').filter(Boolean).pop() ?? String(chemin) : '')
const dossier = (chemin) => String(chemin ?? '').split('/').slice(0, -1).join('/')

/** Un chemin sur le disque, sans la racine du disque que tout le monde partage. */
function surDisque(chemin, racine) {
  if (!chemin) return ''
  return racine && chemin.startsWith(`${racine}/`) ? chemin.slice(racine.length + 1) : chemin
}

const HEURE = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit' })
const JOUR = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'long' })
const JOUR_COURT = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short' })

function lireDate(v) {
  if (v == null || v === '') return null
  const d = typeof v === 'number' ? new Date(v < 1e12 ? v * 1000 : v) : new Date(v)
  return Number.isNaN(d.getTime()) ? null : d
}

function quand(v) {
  const d = lireDate(v)
  if (!d) return ''
  const memeJour = d.toDateString() === new Date().toDateString()
  return memeJour ? `à ${HEURE.format(d)}` : `le ${JOUR.format(d)} à ${HEURE.format(d)}`
}

/** « le 18 sept. à 21 h 14 » : une date qu'on dit, pas un horodatage qu'on lit. */
function horodatage(v) {
  const d = lireDate(v)
  if (!d) return ''
  const heure = [d.getHours(), 'h', String(d.getMinutes()).padStart(2, '0')].join(INSECABLE)
  return `le ${JOUR_COURT.format(d).replace(' ', INSECABLE)} à ${heure}`
}

/** Sans accents ni casse : « amelie » trouve « Amélie », « dun » trouve « Les Dunes ». */
function pliage(texte) {
  return (texte ?? '')
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLowerCase()
}

// --- Appels au serveur -------------------------------------------------------

/**
 * Tout appel passe par ici, lecture comme action.
 *
 * `fetch` rejette quand le contact est perdu, et `res.json()` lève sur une page
 * d'erreur HTML (un 502 du proxy) : deux cas où, sans filet, l'écran se tait et
 * le bouton redevient cliquable. On rend donc un constat, jamais une exception,
 * et c'est l'appelant qui choisit la phrase — une lecture ratée et une commande
 * perdue ne se disent pas pareil.
 *
 * Sans `corps`, c'est une lecture (GET).
 */
async function appeler(url, corps) {
  const init =
    corps === undefined
      ? undefined
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(corps),
        }
  let res
  try {
    res = await fetch(url, init)
  } catch {
    return { ok: false, perdu: true, statut: 0, corps: null, illisible: false }
  }
  let lu = null
  let illisible = false
  try {
    lu = await res.json()
  } catch {
    illisible = true
  }
  if (res.ok && !illisible && lu && typeof lu === 'object') return { ok: true, corps: lu }
  return {
    ok: false,
    perdu: false,
    statut: res.status,
    corps: lu,
    illisible: illisible || !lu || typeof lu !== 'object',
  }
}

/** Une lecture ratée, en une phrase complète avec le geste à faire. */
function texteLecture(r) {
  if (r.perdu) return serveurMuet()
  if (r.statut < 300 && r.illisible) {
    return `Sortilège a répondu (réponse ${r.statut}) mais sa réponse est illisible. Réessaie ; si ça persiste, « docker logs sortilege » dit pourquoi.`
  }
  return motif(r.corps, r.statut, serveurEnErreur(r.statut))
}

/** Le même constat, sans le geste : pour un bandeau qui dit déjà quoi faire. */
function constatDe(r) {
  if (r.perdu) return constatServeur()
  if (r.statut < 300 && r.illisible) {
    return `Sortilège a répondu (réponse ${r.statut}) mais sa réponse est illisible.`
  }
  return motif(r.corps, r.statut, constatServeur(r.statut))
}

/**
 * Une commande qui n'a pas abouti. Quand le contact est perdu ou que la réponse
 * ne se lit pas, personne ici ne sait si le serveur a obéi : la phrase le dit,
 * et renvoie à l'état relu plutôt qu'à un second clic.
 */
function texteAction(r, geste) {
  if (r.perdu) {
    return `Contact perdu pendant « ${geste} » : impossible de savoir si le serveur a reçu la demande. L'état de la copie se relit tout seul — regarde-le avant de refaire le geste.`
  }
  const aUnMotif = typeof r.corps?.detail === 'string' && r.corps.detail
  if ((r.statut < 300 && r.illisible) || (r.statut >= 500 && !aUnMotif)) {
    return `Le serveur a répondu ${r.statut} sans message utilisable : impossible de savoir si « ${geste} » a été pris en compte. L'état de la copie se relit tout seul ; « docker logs sortilege » dit ce qui s'est passé.`
  }
  return motif(r.corps, r.statut)
}

/** Faux dès le démontage : une réponse tardive n'écrit plus dans un écran mort. */
let monte = true

// --- La médiathèque ----------------------------------------------------------

/** `shallowRef` : 1 200 œuvres rendues réactives en profondeur coûteraient à
 *  chaque frappe dans la recherche, pour des données qui ne changent jamais. */
const bibli = shallowRef(null)
const panneBibli = ref(null)
const chargementBibli = ref(false)

/** La sélection est remplacée à chaque geste, jamais modifiée sur place. */
const selection = shallowRef(new Set(memoire.selection))
watch(selection, (v) => { memoire.selection = v })

const recherche = ref('')
const type = ref('all')
const voirSelection = ref(false)
/** Les œuvres cochées au moment d'ouvrir « Voir la sélection ». Figées : une
 *  ligne qu'on décoche reste à l'écran, décochée, au lieu de disparaître sous
 *  le doigt qui allait peut-être la recocher. */
const selectionFigee = shallowRef(new Set())
const affiche = ref(PALIER)
const champRecherche = ref(null)

const parTitre = (a, b) =>
  String(a.title ?? '').localeCompare(String(b.title ?? ''), 'fr', { sensitivity: 'base' }) ||
  (Number(a.year) || 0) - (Number(b.year) || 0)

let premiereLecture = true

async function chargerBibli() {
  chargementBibli.value = true
  const r = await appeler('/api/copy/library')
  if (!monte) return
  chargementBibli.value = false
  if (!r.ok) {
    panneBibli.value = texteLecture(r)
    return
  }
  const works = Array.isArray(r.corps.works) ? r.corps.works : []
  // Le texte plié est calculé une fois ici, pas à chaque frappe sur 1 200 lignes.
  const preparees = works
    .filter((w) => w && w.key != null)
    .map((w) => ({ ...w, pli: pliage(`${w.title ?? ''} ${w.year ?? ''}`) }))
    .sort(parTitre)
  bibli.value = {
    built: r.corps.built !== false,
    works: preparees,
    parCle: new Map(preparees.map((w) => [w.key, w])),
  }
  panneBibli.value = null

  // Une œuvre cochée avant un changement d'écran peut avoir quitté l'index
  // entre-temps : la garder compterait des gigaoctets qui n'existent plus.
  const garde = new Set([...selection.value].filter((k) => bibli.value.parCle.has(k)))
  if (garde.size !== selection.value.size) selection.value = garde

  if (premiereLecture) {
    premiereLecture = false
    await nextTick()
    focaliserRecherche()
  }
}

/** Le champ prend le focus à l'arrivée — sauf si on a déjà cliqué DANS la page
 *  pendant la lecture : voler le focus d'un disque qu'on vient de choisir
 *  ferait taper dans le vide. Le bouton « Copie » de la barre du haut, lui,
 *  garde le focus après le clic qui a ouvert l'écran : il ne compte pas. */
const racine = ref(null)

function focaliserRecherche() {
  const actif = document.activeElement
  if (actif && racine.value?.contains(actif)) return
  champRecherche.value?.focus({ preventScroll: true })
}

function effacerRecherche() {
  recherche.value = ''
  champRecherche.value?.focus()
}

function surEchap(e) {
  if (!recherche.value) return
  e.preventDefault()
  recherche.value = ''
}

const rechercheNette = computed(() => recherche.value.trim())
const q = computed(() => pliage(rechercheNette.value))

/** Tout ce qui filtre, sauf le type : c'est sur cette liste que se comptent
 *  les boutons de type, pour qu'on voie où sont les résultats d'une recherche. */
const avantType = computed(() => {
  const toutes = bibli.value?.works ?? []
  const figee = selectionFigee.value
  const cle = q.value
  return toutes.filter(
    (w) => (!voirSelection.value || figee.has(w.key)) && (!cle || w.pli.includes(cle)),
  )
})

const compteParType = computed(() => {
  const c = { all: avantType.value.length }
  for (const w of avantType.value) c[w.kind] = (c[w.kind] ?? 0) + 1
  return c
})

const filtrees = computed(() =>
  type.value === 'all' ? avantType.value : avantType.value.filter((w) => w.kind === type.value),
)
const listees = computed(() => filtrees.value.slice(0, affiche.value))

// Un nouveau filtre repart du premier palier : garder « 1 000 lignes rendues »
// d'une liste précédente annulerait tout le bénéfice des paliers.
watch([recherche, type, voirSelection], () => { affiche.value = PALIER })

function basculer(cle) {
  const s = new Set(selection.value)
  s.has(cle) ? s.delete(cle) : s.add(cle)
  selection.value = s
}

/** « Tout cocher » ne coche que les lignes RENDUES : c'est ce qu'on a sous les
 *  yeux, et le bouton le dit. Cocher 1 000 œuvres jamais vues n'est pas un geste
 *  qu'on fait exprès. */
const aCocher = computed(() => listees.value.some((w) => !selection.value.has(w.key)))

function toutCocher() {
  const s = new Set(selection.value)
  for (const w of listees.value) s.add(w.key)
  selection.value = s
}

function toutDecocher() {
  selection.value = new Set()
}

function basculerVoirSelection() {
  if (voirSelection.value) {
    voirSelection.value = false
    return
  }
  // La recherche et le type en cours masqueraient une partie de ce qu'on a
  // demandé à voir : « Voir la sélection » montre TOUTE la sélection.
  selectionFigee.value = new Set(selection.value)
  recherche.value = ''
  type.value = 'all'
  voirSelection.value = true
}

const octetsSelection = computed(() => {
  const parCle = bibli.value?.parCle
  if (!parCle) return 0
  let total = 0
  for (const k of selection.value) total += Number(parCle.get(k)?.bytes) || 0
  return total
})

/** Ce que la recherche ou le filtre cache de la sélection. Le compteur compte
 *  tout ; cette ligne dit pourquoi on ne voit pas tout ce qu'il compte. */
const cocheesMasquees = computed(() => {
  let visibles = 0
  for (const w of filtrees.value) if (selection.value.has(w.key)) visibles += 1
  return Math.max(0, selection.value.size - visibles)
})

const aucunDuType = computed(() =>
  type.value === 'all' ? 'Aucun titre' : TYPES.find((t) => t.id === type.value)?.aucun ?? 'Aucun titre',
)

// --- Les disques -------------------------------------------------------------

const disques = shallowRef(null)
const panneDisques = ref(null)
const chargementDisques = ref(false)
const avisDisque = ref(null)
const disqueChoisi = ref(memoire.disque)

watch(disqueChoisi, (v) => {
  memoire.disque = v
  if (v) avisDisque.value = null
})

/**
 * Pourquoi un disque ne peut pas être choisi.
 *
 * Le motif du serveur d'abord : un disque débranché laisse derrière lui un
 * dossier vide, et y écrire remplirait la partition système du NAS. Les deux
 * gardes suivantes ne font que refuser ce que le serveur refuserait au premier
 * fichier.
 */
function refusDe(d) {
  if (d?.refusal) return d.refusal
  if (d?.mounted === false) {
    return "le disque n'est pas monté : écrire dans ce dossier remplirait le stockage du NAS lui-même."
  }
  if (d?.writable === false) {
    return "Sortilège n'a pas le droit d'y écrire (disque en lecture seule, ou droits insuffisants)."
  }
  return ''
}

/** Un motif en tête de phrase prend sa majuscule, qu'il vienne d'ici ou du serveur. */
const phrase = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : '')

const disqueCourant = computed(
  () => disques.value?.disks.find((d) => d.id === disqueChoisi.value) ?? null,
)

/** Le serveur désigne un disque tantôt par son identifiant (la reprise
 *  sauvegardée), tantôt par l'objet complet (l'état d'une copie). Les deux
 *  doivent donner le même libellé, jamais « [object Object] ». */
function libelleDisque(ref) {
  if (!ref) return 'disque inconnu'
  if (typeof ref === 'object') return ref.label ?? ref.id ?? 'disque inconnu'
  const d = disques.value?.disks.find((x) => x.id === ref || x.path === ref)
  return d?.label ?? String(ref)
}

function racineDe(ref) {
  if (ref && typeof ref === 'object') return ref.path ?? ''
  const d = disques.value?.disks.find((x) => x.id === ref || x.path === ref)
  return d?.path ?? ''
}

async function chargerDisques() {
  chargementDisques.value = true
  const r = await appeler('/api/copy/disks')
  if (!monte) return
  chargementDisques.value = false
  if (!r.ok) {
    panneDisques.value = texteLecture(r)
    return
  }
  disques.value = {
    ...r.corps,
    disks: Array.isArray(r.corps.disks) ? r.corps.disks : [],
  }
  panneDisques.value = null

  // Le disque choisi a pu être débranché depuis la dernière lecture. Le garder
  // choisi, c'est préparer une copie vers un dossier vide du NAS.
  if (disqueChoisi.value) {
    const d = disques.value.disks.find((x) => x.id === disqueChoisi.value)
    const nom = d?.label ?? disqueChoisi.value
    if (!d) {
      avisDisque.value = `« ${nom} » n'est plus proposé : il a sans doute été débranché ou démonté. Choisis un autre disque.`
      disqueChoisi.value = ''
    } else if (refusDe(d)) {
      avisDisque.value = `« ${nom} » n'est plus utilisable. ${phrase(refusDe(d))}`
      disqueChoisi.value = ''
    }
  }

  // Un seul disque utilisable : il est choisi d'office — la question n'a
  // qu'une réponse. Jamais après un avis : changer de destination en silence
  // juste après en avoir perdu une serait la pire surprise.
  if (!disqueChoisi.value && !avisDisque.value) {
    const possibles = disques.value.disks.filter((d) => !refusDe(d))
    if (possibles.length === 1) disqueChoisi.value = possibles[0].id
  }
}

/** Un disque rebranché pour reprendre une copie : l'état de la copie
 *  sauvegardée (`disk_available`) se relit APRÈS la liste — c'est la lecture
 *  des disques qui fait oublier au serveur sa dernière recherche. Lues en même
 *  temps, l'état pouvait passer avant et dire encore « pas branché », bouton
 *  « Reprendre » grisé devant un disque qui est là. */
async function relireDisques() {
  avisDisque.value = null
  await chargerDisques()
  if (monte) lireStatut()
}

const pctUtilise = (d) => pourcent((Number(d.total_bytes) || 0) - (Number(d.free_bytes) || 0), d.total_bytes)

// --- L'analyse : ce qui manque sur le disque ---------------------------------

const sidecars = ref(memoire.sidecars)
const debitMax = ref(memoire.debit)
watch(sidecars, (v) => { memoire.sidecars = v })
watch(debitMax, (v) => { memoire.debit = v })

const analyse = shallowRef(null)
const analyseEnCours = ref(false)
const panneAnalyse = ref(null)

/**
 * La question (disque, compagnons, sélection) dont le lancement a été refusé.
 *
 * Après un refus, l'écran relisait les disques et refaisait l'analyse de
 * lui-même : le contre-contrôle a montré qu'un disque débranché puis remplacé
 * par un dossier passait ainsi, en deux clics, pour une destination neuve. Un
 * refus s'affiche et s'arrête là ; l'analyse ne se refait que sur un geste
 * (« Refaire l'analyse ») ou un changement de sélection.
 */
const refusLancement = ref('')

/** Ce qui définit une analyse. Deux sélections dans un ordre différent sont la
 *  même question : les clés sont triées. */
const cleAnalyse = computed(() => {
  if (!disqueChoisi.value || !selection.value.size) return ''
  return [disqueChoisi.value, sidecars.value ? '1' : '0', ...[...selection.value].sort()].join('\n')
})

let minuteurAnalyse = null
let numeroAnalyse = 0

watch(cleAnalyse, (cle) => {
  clearTimeout(minuteurAnalyse)
  panneAnalyse.value = null
  if (!cle) {
    // Une réponse encore en route pour l'ancienne question sera ignorée.
    numeroAnalyse += 1
    analyseEnCours.value = false
    return
  }
  minuteurAnalyse = setTimeout(analyser, DELAI_ANALYSE_MS)
})

async function analyser() {
  clearTimeout(minuteurAnalyse)
  const cle = cleAnalyse.value
  if (!cle) return
  const numero = ++numeroAnalyse
  const disque = disqueChoisi.value
  const avecCompagnons = sidecars.value
  analyseEnCours.value = true
  panneAnalyse.value = null
  const r = await appeler('/api/copy/analyze', {
    disk: disque,
    works: [...selection.value],
    sidecars: avecCompagnons,
  })
  // Une réponse partie avant un clic arrive après lui : l'afficher donnerait
  // l'état d'une sélection qui n'est plus celle à l'écran.
  if (!monte || numero !== numeroAnalyse) return
  analyseEnCours.value = false
  if (r.ok) {
    analyse.value = {
      ...r.corps,
      works: Array.isArray(r.corps.works) ? r.corps.works : [],
      cle,
      disque,
      sidecars: avecCompagnons,
    }
    // L'analyse refusée vient d'être refaite : le bouton repart de ce qu'elle dit.
    if (refusLancement.value === cle) {
      refusLancement.value = ''
      erreurLancement.value = null
    }
  } else {
    panneAnalyse.value = texteLecture(r)
  }
}

const analyseAJour = computed(() =>
  Boolean(analyse.value && cleAnalyse.value && analyse.value.cle === cleAnalyse.value),
)

/** L'état de chaque œuvre ne dépend que du disque et des compagnons, pas du
 *  reste de la sélection : cocher une œuvre de plus ne doit pas effacer l'état
 *  des vingt autres le temps de la relecture. */
const etats = computed(() => {
  const a = analyse.value
  if (!a || a.disque !== disqueChoisi.value || a.sidecars !== sidecars.value) return new Map()
  return new Map(a.works.map((w) => [w.key, presenter(w)]))
})

function grouperProblemes(issues) {
  const m = new Map()
  for (const i of Array.isArray(issues) ? issues : []) {
    const k = i?.reason || 'autre'
    if (!m.has(k)) m.set(k, [])
    m.get(k).push(i)
  }
  return [...m].map(([raison, items]) => ({ raison, items }))
}

function presenter(e) {
  const aCopier = Number(e.files_to_copy) || 0
  const problemes = grouperProblemes(e.issues)
  let classe
  let texte
  if (e.state === 'present') {
    classe = 'present'
    texte = 'déjà sur le disque'
  } else if (e.state === 'partial') {
    classe = 'partiel'
    texte = `partiel — ${nombre(e.files_present)} sur ${nombre(e.files_total)}`
    if (aCopier) texte += `, ${taille(e.bytes_to_copy)} à copier`
    else if (problemes.length) texte += ', le reste est bloqué'
  } else {
    classe = 'absent'
    texte = aCopier ? `à copier — ${taille(e.bytes_to_copy)}` : 'absent du disque'
    if (!aCopier && problemes.length) texte += ', rien de copiable'
  }
  return { classe, texte, dejaLa: e.existing_dir || '', problemes }
}

const fsCourant = computed(() => nomSysteme(analyse.value?.disk?.fs ?? disqueCourant.value?.fs))

function libelleProbleme(raison, n, fs) {
  switch (raison) {
    case 'conflict':
      return `${pluriel(n, 'conflit')} de nom`
    case 'too_large':
      return `${pluriel(n, 'fichier trop gros', 'fichiers trop gros')} pour ${fs === 'FAT32' ? 'FAT32' : 'ce disque'}`
    case 'name_invalid':
      return `${pluriel(n, 'nom refusé', 'noms refusés')} par le système de fichiers`
    case 'missing_source':
      return `${pluriel(n, 'fichier introuvable', 'fichiers introuvables')} dans la médiathèque`
    case 'symlink':
      return `${pluriel(n, 'fichier derrière un lien', 'fichiers derrière un lien')} sur le disque`
    case 'other_volume':
      return pluriel(n, 'fichier sur un autre volume', 'fichiers sur un autre volume')
    default:
      return pluriel(n, 'problème')
  }
}

const totaux = computed(() => (analyseAJour.value ? analyse.value.totals ?? {} : null))
const disqueAnalyse = computed(() => analyse.value?.disk ?? disqueCourant.value)
const tient = computed(() => analyse.value?.fits !== false)
const libreApres = computed(
  () => (Number(disqueAnalyse.value?.free_bytes) || 0) - (Number(totaux.value?.bytes_to_copy) || 0),
)

/** Les fichiers écartés, toutes œuvres confondues, par cause. */
const ecartes = computed(() => {
  if (!analyseAJour.value) return []
  const m = new Map()
  for (const w of analyse.value.works) {
    for (const i of Array.isArray(w.issues) ? w.issues : []) {
      const k = i?.reason || 'autre'
      m.set(k, (m.get(k) ?? 0) + 1)
    }
  }
  return [...m].map(([raison, n]) => ({ raison, n }))
})

const erreursLecture = computed(() =>
  analyseAJour.value && Array.isArray(analyse.value.scan_errors) ? analyse.value.scan_errors : [],
)

const texteErreurLecture = (e) =>
  typeof e === 'string' ? e : [e?.path, e?.message].filter(Boolean).join(' — ') || 'erreur sans détail'

/** Le disque tel qu'il sera après la copie, sur sa jauge. */
function projection(d) {
  if (!analyseAJour.value || d.id !== disqueChoisi.value) return null
  const total = Number(d.total_bytes) || 0
  if (!total) return null
  const libre = Number(d.free_bytes) || 0
  const prevu = Math.min(Number(totaux.value.bytes_to_copy) || 0, libre)
  return { pct: pourcent(prevu, total), deborde: !tient.value }
}

// --- Suivi de la copie -------------------------------------------------------

const statut = shallowRef(null)
const panneSuivi = ref(null)
const action = ref(null)
const erreurLancement = ref(null)
const erreurPilotage = ref(null)
const erreurReprise = ref(null)
const messageReprise = ref(null)
const rapportMasque = ref(memoire.rapportMasque)
const bilan = shallowRef(memoire.bilan)
const listeFichiers = ref(null)

/**
 * Le débit d'un fichier REPRIS, mesuré ici entre deux lectures.
 *
 * Un fichier repris à 12,4 Go a déjà 12,4 Go « faits » à la première lecture :
 * un débit calculé depuis son départ afficherait des centaines de Mo/s pour
 * rien d'écrit. L'écart entre deux lectures successives ne compte, lui, que ce
 * qui vient d'être écrit — la part reprise n'y entre jamais. Lissé, pour qu'un
 * compteur serveur qui avance par blocs ne fasse pas danser le chiffre.
 */
let echantillon = null
const debitMesure = ref(0)

function mesurerDebit(s) {
  const c = s?.current
  if (!c || !s.running || s.paused) {
    echantillon = null
    debitMesure.value = 0
    return
  }
  const cible = c.target || c.source || ''
  const octets = Number(c.bytes_done) || 0
  const t = performance.now()
  if (echantillon && echantillon.cible === cible && octets >= echantillon.octets && t > echantillon.t) {
    const instant = (octets - echantillon.octets) / ((t - echantillon.t) / 1000)
    debitMesure.value = debitMesure.value > 0 ? debitMesure.value * 0.6 + instant * 0.4 : instant
  } else {
    debitMesure.value = 0
  }
  echantillon = { cible, octets, t }
}

/**
 * Le fichier qui devait reprendre au point de sauvegarde, pour vérifier qu'il
 * reprend bien. S'il repart de zéro, l'utilisateur doit le savoir : un fichier
 * à 80 % qui recommence sans explication passe pour une panne.
 *
 * L'attente dure jusqu'au premier état qui porte un fichier en cours : la
 * réponse de « Reprendre » arrive souvent avant que le serveur ait ouvert le
 * fichier (`current` vaut alors null), et la trancher là ne comparait rien.
 * Elle cesse aussi quand la copie ne tourne plus : aucun fichier ne viendra.
 */
let repriseAttendue = null
const avisRepartZero = ref(null)

function verifierReprise(s) {
  if (!repriseAttendue) return
  const c = s?.current
  if (!c) {
    if (s && !s.running) repriseAttendue = null
    return
  }
  const cible = c.target || c.source || ''
  if (cible === repriseAttendue.cible && repriseAttendue.depuis > 0 && !(Number(c.resumed_from_bytes) > 0)) {
    avisRepartZero.value =
      `« ${base(cible)} » devait reprendre à ${taille(repriseAttendue.depuis)} : ` +
      'le serveur le recopie depuis le début.'
  }
  repriseAttendue = null
}

let minuteurSuivi = null
let lectureEnVol = false

function suivre() {
  if (minuteurSuivi === null) minuteurSuivi = setInterval(lireStatut, SONDAGE_MS)
}

function cesserSuivi() {
  if (minuteurSuivi !== null) {
    clearInterval(minuteurSuivi)
    minuteurSuivi = null
  }
}

async function lireStatut() {
  // Une lecture lente ne doit pas en empiler une par seconde derrière elle.
  if (lectureEnVol) return
  lectureEnVol = true
  try {
    const r = await appeler('/api/copy/status')
    if (!monte) return
    if (r.ok) {
      panneSuivi.value = null
      appliquerStatut(r.corps)
    } else {
      // Le sondage continue : le suivi reprend seul quand le contact revient.
      panneSuivi.value = constatDe(r)
    }
  } finally {
    lectureEnVol = false
  }
}

function appliquerStatut(s) {
  if (!s || typeof s !== 'object') return
  const avant = Boolean(statut.value?.running)
  statut.value = s
  mesurerDebit(s)
  verifierReprise(s)
  if (s.running) {
    suivre()
  } else {
    cesserSuivi()
    if (avant) apresCopie()
  }
}

/** Ce qui vient d'arriver sur le disque change la place libre et l'état des
 *  lignes : les deux se relisent, plutôt que d'afficher un « à copier » sur
 *  des fichiers qui y sont maintenant. */
function apresCopie() {
  chargerDisques()
  if (cleAnalyse.value) analyser()
}

const cleRapport = computed(() =>
  statut.value ? String(statut.value.started_at ?? statut.value.finished_at ?? '') : '',
)

const suiviVisible = computed(() => {
  const s = statut.value
  if (!s) return false
  if (s.running) return true
  return Boolean(s.finished_at) && rapportMasque.value !== cleRapport.value
})

function masquerRapport() {
  rapportMasque.value = cleRapport.value
  memoire.rapportMasque = cleRapport.value
}

const titreSuivi = computed(() => {
  const s = statut.value
  if (!s) return ''
  if (s.running) {
    if (s.stopping) return 'Arrêt en cours'
    if (s.paused) return 'Copie en pause'
    return 'Copie en cours'
  }
  if (s.stopped_by_user) return 'Copie arrêtée à ta demande'
  if (s.aborted) return 'Copie interrompue avant la fin'
  const n = Array.isArray(s.errors) ? s.errors.length : 0
  return n ? `Copie terminée, avec ${pluriel(n, 'erreur')}` : 'Copie terminée'
})

const racineSuivi = computed(() => racineDe(statut.value?.disk))
const courant = computed(() => statut.value?.current ?? null)
const pctTotal = computed(() => pourcent(statut.value?.bytes_done, statut.value?.bytes_total))
const pctFichier = computed(() => pourcent(courant.value?.bytes_done, courant.value?.bytes_total))

/** `done` ne garde que les 50 derniers fichiers : les COMPTES viennent des
 *  totaux du serveur (`files_done`, et les octets publiés = `bytes_done` moins
 *  le fichier en cours), la liste ne sert qu'à montrer les plus récents. */
const termines = computed(() => {
  const liste = Array.isArray(statut.value?.done) ? statut.value.done : []
  const total = Number(statut.value?.files_done) || liste.length
  const octets = Math.max(
    0,
    (Number(statut.value?.bytes_done) || 0) - (Number(statut.value?.current?.bytes_done) || 0),
  )
  return {
    total,
    octets,
    recents: liste.slice(-TERMINES_MONTRES),
    caches: Math.max(0, total - Math.min(liste.length, TERMINES_MONTRES)),
  }
})

/** `queue_next` est une liste d'objets ; une chaîne nue reste lisible. */
const aVenir = computed(() =>
  (Array.isArray(statut.value?.queue_next) ? statut.value.queue_next : [])
    .slice(0, A_VENIR_MONTRES)
    .map((x) =>
      typeof x === 'string'
        ? { target: x, bytes: null }
        : { target: x?.target ?? x?.source ?? '', bytes: x?.bytes ?? null },
    ),
)

const erreursCopie = computed(() => (Array.isArray(statut.value?.errors) ? statut.value.errors : []))

/** Prévus, ni copiés ni en erreur : ceux qu'un arrêt ou une panne a laissés. */
const nonCopies = computed(() =>
  Math.max(0, (Number(statut.value?.files_total) || 0) - termines.value.total - erreursCopie.value.length),
)

const resteAVenir = computed(() => {
  const total = Number(statut.value?.files_total) || 0
  const vus = termines.value.total + erreursCopie.value.length + (courant.value ? 1 : 0) + aVenir.value.length
  return Math.max(0, total - vus)
})

/** Ce que le fichier en cours avait déjà sur le disque quand il a repris. */
const reprisA = computed(() => Number(courant.value?.resumed_from_bytes) || 0)
const pctRepris = computed(() => pourcent(reprisA.value, courant.value?.bytes_total))

/** Débit instantané du fichier en cours. Repris, il se mesure ici — voir
 *  `mesurerDebit` : seul ce qui est écrit depuis la reprise y compte. */
const debitFichier = computed(() => {
  const c = courant.value
  if (!c) return 0
  return reprisA.value > 0 ? debitMesure.value : Number(c.speed_bps) || 0
})

/** Temps restant pour CE fichier, au débit instantané. */
const resteFichier = computed(() => {
  const c = courant.value
  const v = debitFichier.value
  if (!c || v <= 0) return null
  return duree(((Number(c.bytes_total) || 0) - (Number(c.bytes_done) || 0)) / v)
})

/** Ce que le serveur dit du fichier en cours, tel quel — c'est lui qui sait
 *  pourquoi un fichier repart de zéro. */
const noteCourant = computed(() => {
  const c = courant.value
  const n = c?.resume_note ?? c?.message ?? c?.note ?? c?.info
  return typeof n === 'string' && n ? n : ''
})

/** La même erreur sur quarante épisodes se lit une fois, avec son compte. */
const erreursGroupees = computed(() => {
  const m = new Map()
  for (const e of erreursCopie.value) {
    const k = e?.message || 'Erreur sans message du serveur'
    if (!m.has(k)) m.set(k, [])
    m.get(k).push(e)
  }
  return [...m]
    .map(([message, items]) => ({ message, items }))
    .sort((a, b) => b.items.length - a.items.length)
})

const detailArret = computed(() => {
  const n = termines.value.total
  const c = courant.value
  const nom = c ? ` (« ${base(c.target || c.source)} », ${taille(c.bytes_done)} écrits)` : ''
  const deja = n
    ? `${accord(n, 'fichier', 'déjà copié')} ${verbe(n, 'reste', 'restent')} sur le disque`
    : "aucun fichier n'a encore été copié jusqu'au bout"
  return (
    `Le fichier en cours${nom} est mis de côté tel quel : la copie pourra reprendre là où elle ` +
    `s'est arrêtée, depuis le bandeau « Copie interrompue ». Rien n'est perdu, et ${deja}.`
  )
})

// --- Copie interrompue, reprenable -------------------------------------------

/** Une copie sauvegardée n'a de sens que si aucune ne tourne. */
const reprise = computed(() => {
  const r = statut.value?.resumable
  return r && typeof r === 'object' && !statut.value.running ? r : null
})

const checkpoint = computed(() => Number(reprise.value?.current?.checkpoint_bytes) || 0)

const raisonReprise = computed(() => {
  if (!reprise.value) return ''
  if (action.value) return 'Une commande est déjà partie vers le serveur : les boutons attendent sa réponse.'
  if (reprise.value.disk_available === false) {
    return `Le disque « ${libelleDisque(reprise.value.disk)} » n'est pas branché : rebranche-le, puis « Relire les disques ».`
  }
  return ''
})

const detailAbandon = computed(() => {
  const c = reprise.value?.current
  const partiel = c?.target
    ? ` (« ${base(c.target)} », ${taille(checkpoint.value)} écrits)`
    : ''
  return (
    `Les fichiers déjà copiés restent sur le disque. Seul le fichier partiel laissé par Sortilège${partiel} ` +
    "est supprimé, rien d'autre. La copie ne pourra plus reprendre : il faudra la relancer depuis la sélection."
  )
})

/** Disque absent et fichier partiel sur lui : « Abandonner » ne pourrait pas le
 *  retirer (le serveur refuse), seul « Oublier » reste — et il le dit. */
const oubliSeulement = computed(() =>
  Boolean(reprise.value?.current && reprise.value.disk_available === false),
)

/** Ce que « Oublier » promet, et que le serveur tient : il ne supprime jamais
 *  rien, et refuse d'oublier si le disque est en fait branché — l'écran le dit
 *  alors, avec « Reprendre » à portée de main (voir `abandonner`). */
const detailOubli = computed(() => {
  const c = reprise.value?.current
  const nom = c?.target ? `« ${base(c.target)}.sortilege-part »` : 'le fichier partiel'
  return (
    `Le disque « ${libelleDisque(reprise.value?.disk)} » n'est pas branché : ${nom} (${taille(checkpoint.value)}) ` +
    'ne peut pas en être retiré et y restera — tu pourras le supprimer à la main. ' +
    "La copie ne pourra plus reprendre. Pour qu'il soit retiré, rebranche plutôt le disque, puis « Relire les disques ». " +
    "Si le disque est en fait branché, rien n'est oublié : l'écran le dira et proposera « Reprendre »."
  )
})

/** Le disque de la copie, retrouvé sous un autre nom (rebranché ailleurs). */
const repriseAilleurs = computed(() => {
  const r = reprise.value
  if (!r?.disk_available || !r.disk_found || r.disk_found === r.disk) return ''
  return `Son disque est branché sous « ${libelleDisque(r.disk_found)} » : Sortilège l'a reconnu à son repère, la copie reprendra là.`
})

/** Pourquoi Pause et Reprendre ne répondent pas : une commande est partie. */
const raisonPilotage = computed(() => {
  if (!['pause', 'resume', 'stop'].includes(action.value)) return ''
  const nom = { pause: 'Pause', resume: 'Reprendre', stop: 'Arrêter' }[action.value] ?? action.value
  return `« ${nom} » est parti vers le serveur : les boutons attendent sa réponse.`
})

const bilanVisible = computed(() => {
  const b = bilan.value
  if (!b || !statut.value) return false
  return b.debut == null || String(b.debut) === String(statut.value.started_at)
})

// Le fichier en cours reste dans le cadre de la liste : à chaque changement de
// fichier, la liste se recale dessus, avec deux terminés visibles au-dessus.
watch(
  () => courant.value?.target,
  async (cible) => {
    if (!cible) return
    await nextTick()
    const boite = listeFichiers.value
    const ligne = boite?.querySelector('[data-en-cours]')
    if (!boite || !ligne) return
    boite.scrollTop = Math.max(0, ligne.offsetTop - boite.clientHeight / 3)
  },
)

// --- Les gestes --------------------------------------------------------------

const raisonCopie = computed(() => {
  if (action.value === 'start') return 'Lancement de la copie…'
  if (statut.value?.running) {
    return `Une copie tourne déjà vers « ${libelleDisque(statut.value.disk)} » : une seule à la fois, pour ne pas partager le port USB. Attends sa fin, ou arrête-la.`
  }
  // Le serveur refuse aussi (409) : une nouvelle copie écraserait l'état de
  // celle-ci, et son fichier partiel resterait sur le disque sans que plus rien
  // ne le désigne.
  if (reprise.value) {
    return "Une copie interrompue attend : reprends-la ou abandonne-la d'abord, dans le bandeau « Copie interrompue »."
  }
  if (!disqueChoisi.value) {
    return disques.value?.disks.some((d) => !refusDe(d))
      ? "Choisis d'abord un disque de destination."
      : 'Aucun disque utilisable : branche et monte un disque, puis « Relire les disques ».'
  }
  if (!selection.value.size) return 'Coche au moins une œuvre dans la médiathèque.'
  if (panneAnalyse.value) {
    return "L'analyse du disque n'a pas abouti : sans elle, rien ne dit ce qui manque. Réessaie-la ci-dessus."
  }
  if (!analyseAJour.value) {
    return "Analyse du disque en cours : le bouton s'active dès qu'elle a dit ce qui manque."
  }
  if (refusLancement.value && refusLancement.value === cleAnalyse.value) {
    return "Le serveur a refusé de lancer cette copie (motif ci-dessous) : « Refaire l'analyse » relit le disque avant tout nouvel essai."
  }
  const n = Number(totaux.value.files_to_copy) || 0
  if (!n) {
    return ecartes.value.length
      ? 'Rien de copiable : ce qui manque est bloqué — le détail est sur les lignes cochées.'
      : "Tout est déjà sur le disque : il n'y a rien à copier."
  }
  if (!tient.value) {
    return `Ça ne tient pas : il manque ${taille(analyse.value.missing_bytes)} sur le disque. Décoche des œuvres, ou choisis un disque plus grand.`
  }
  return ''
})

const texteBouton = computed(() => {
  if (!analyseAJour.value) return 'Copier'
  const t = totaux.value
  return `Copier ${pluriel(t.files_to_copy, 'fichier')} (${taille(t.bytes_to_copy)})`
})

async function lancer() {
  if (raisonCopie.value) return
  const a = analyse.value
  const cle = cleAnalyse.value
  action.value = 'start'
  erreurLancement.value = null
  erreurPilotage.value = null
  erreurReprise.value = null
  const r = await appeler('/api/copy/start', {
    disk: disqueChoisi.value,
    works: [...selection.value],
    sidecars: sidecars.value,
    max_mb_per_s: debitMax.value ? Number(debitMax.value) : null,
  })
  if (!monte) return
  action.value = null

  if (r.ok && r.corps.started !== false) {
    refusLancement.value = ''
    avisRepartZero.value = null
    messageReprise.value = null
    repriseAttendue = null
    // Ce que l'analyse a écarté ne revient pas dans l'état de la copie : on le
    // garde pour le compte rendu, qui doit dire ce qui a été ignoré et pourquoi.
    bilan.value = {
      debut: r.corps.started_at ?? null,
      presents: Number(a?.totals?.files_present) || 0,
      ecartes: ecartes.value.map((p) => ({ ...p })),
      fs: fsCourant.value,
    }
    memoire.bilan = bilan.value
    rapportMasque.value = null
    memoire.rapportMasque = null
    appliquerStatut(r.corps)
    // Même si la réponse de lancement ne portait pas encore `running` : la
    // prochaine lecture tranche.
    suivre()
    return
  }

  erreurLancement.value = r.ok
    ? motif(r.corps, 200, "Le serveur n'a pas lancé la copie, sans dire pourquoi. Rien n'a été copié.")
    : texteAction(r, 'Copier')
  // Un 409 veut souvent dire « une copie tourne déjà » ; un contact perdu,
  // qu'elle tourne peut-être. Dans les deux cas, l'état du serveur tranche.
  lireStatut()
  if (!r.ok && (r.perdu || r.illisible || r.statut >= 500)) suivre()
  // Un refus s'affiche, et c'est tout : ni les disques ni l'analyse ne se
  // relisent d'eux-mêmes (voir `refusLancement`). Le bouton attend un geste.
  if (r.statut === 409) refusLancement.value = cle
}

/** Le geste qui suit un refus : relire les disques, puis refaire l'analyse de
 *  la même sélection. Ce que le disque est devenu s'affiche avant d'être copié. */
async function refaireAnalyse() {
  await chargerDisques()
  if (monte && cleAnalyse.value) analyser()
}

async function piloter(geste) {
  const libelles = { pause: 'Pause', resume: 'Reprendre', stop: 'Arrêter' }
  action.value = geste
  erreurPilotage.value = null
  const r = await appeler(`/api/copy/${geste}`, {})
  if (!monte) return
  action.value = null
  if (r.ok) {
    appliquerStatut(r.corps)
    return
  }
  erreurPilotage.value = texteAction(r, libelles[geste])
  lireStatut()
}

/** Une réponse d'action qui porte l'état de la copie, et pas autre chose. */
const estUnEtat = (corps) => corps && typeof corps === 'object' && 'running' in corps

async function reprendre() {
  if (raisonReprise.value) return
  const sauvegarde = reprise.value
  action.value = 'continue'
  erreurReprise.value = null
  erreurPilotage.value = null
  messageReprise.value = null
  const r = await appeler('/api/copy/continue', {})
  if (!monte) return
  action.value = null
  if (r.ok) {
    avisRepartZero.value = null
    repriseAttendue = sauvegarde?.current?.target
      ? { cible: sauvegarde.current.target, depuis: Number(sauvegarde.current.checkpoint_bytes) || 0 }
      : null
    // Le compte rendu de l'analyse d'origine ne vaut plus : la sélection et le
    // disque ont pu changer depuis l'interruption.
    bilan.value = null
    memoire.bilan = null
    rapportMasque.value = null
    memoire.rapportMasque = null
    if (estUnEtat(r.corps)) appliquerStatut(r.corps)
    suivre()
    lireStatut()
    return
  }
  erreurReprise.value = texteAction(r, 'Reprendre la copie')
  lireStatut()
  if (r.perdu || r.illisible || r.statut >= 500) suivre()
}

/** Ce que « Abandonner » a réellement fait, d'après `removed` et `not_removed`
 *  — jamais d'après ce qu'on espérait. */
function texteAbandon(corps, oubli) {
  const retires = Array.isArray(corps?.removed) ? corps.removed : []
  const absents = Array.isArray(corps?.absent) ? corps.absent : []
  const restes = Array.isArray(corps?.not_removed) ? corps.not_removed : []
  const debut = oubli ? 'Copie interrompue oubliée' : 'Copie interrompue abandonnée'
  const copies = 'les fichiers déjà copiés restent sur le disque.'
  if (restes.length) {
    const n = restes.length
    const noms = restes.map((p) => `« ${base(p)} »`).join(', ')
    return (
      `${debut}, mais ${verbe(n, 'ce fichier partiel', 'ces fichiers partiels')} ` +
      `${verbe(n, "n'a pas été retiré", "n'ont pas été retirés")} du disque : ${noms}. ` +
      `${verbe(n, "S'il y est encore, supprime-le", "S'ils y sont encore, supprime-les")} à la main ; ${copies}`
    )
  }
  if (retires.length) return `${debut} : le fichier partiel a été supprimé du disque, et ${copies}`
  if (absents.length) {
    return `${debut} : son fichier partiel n'était déjà plus sur le disque, et ${copies}`
  }
  return `${debut} : elle n'avait laissé aucun fichier partiel, et ${copies}`
}

async function abandonner(oubli = false) {
  action.value = 'discard'
  erreurReprise.value = null
  messageReprise.value = null
  const r = await appeler('/api/copy/discard', oubli ? { forget: true } : {})
  if (!monte) return
  action.value = null
  if (r.ok) {
    messageReprise.value = texteAbandon(r.corps, oubli)
    if (estUnEtat(r.corps)) appliquerStatut(r.corps)
    lireStatut()
    // La place libre et l'état des lignes ont changé avec le fichier partiel.
    chargerDisques()
    if (cleAnalyse.value) analyser()
    return
  }
  erreurReprise.value = texteAction(r, oubli ? 'Oublier cette copie' : 'Abandonner')
  // Un refus d'« Oublier » veut dire que le disque est en fait branché : l'état
  // relu le montre, et « Reprendre » redevient possible.
  lireStatut()
}

// --- Cycle de vie ------------------------------------------------------------

onMounted(async () => {
  // Une copie lancée avant de changer d'écran tourne toujours : la page la
  // reprend là où elle en est.
  lireStatut()
  chargerBibli()
  await chargerDisques()
  if (monte && cleAnalyse.value) analyser()
})

onUnmounted(() => {
  monte = false
  cesserSuivi()
  clearTimeout(minuteurAnalyse)
})
</script>

<template>
  <div ref="racine" class="copie">
    <header class="entete">
      <h2>Copie vers un disque externe</h2>
      <p class="lead">
        Coche ce que tu veux emporter et choisis le disque. Sortilège regarde ce que le disque
        contient déjà et ne copie que ce qui manque — <strong>un fichier à la fois</strong>, pour ne
        pas saturer le port USB. Ce qui est déjà sur le disque n'est ni effacé ni écrasé, et la
        médiathèque n'est jamais modifiée.
      </p>
    </header>

    <!-- L'état de la copie n'a pas pu être lu, et rien d'autre ne le dit. -->
    <p v-if="panneSuivi && !statut" class="panne-inline bandeau" role="alert">
      L'état de la copie n'a pas pu être lu : {{ panneSuivi }} Une copie tourne peut-être déjà.
      <button class="petit" @click="lireStatut">Réessayer</button>
    </p>

    <!-- ============ Suivi de la copie, puis compte rendu ============ -->
    <section
      v-if="suiviVisible"
      class="suivi"
      :class="{ pause: statut.running && statut.paused, fini: !statut.running }"
      aria-labelledby="titre-suivi"
    >
      <div class="suivi-tete">
        <h3 id="titre-suivi">{{ titreSuivi }}</h3>
        <span class="vers">vers « {{ libelleDisque(statut.disk) }} »</span>
        <span v-if="!statut.running && quand(statut.finished_at)" class="vers">
          {{ quand(statut.finished_at) }}
        </span>
      </div>
      <!-- L'état est annoncé quand il change, pas les chiffres : un lecteur
           d'écran qui lirait le débit chaque seconde serait inutilisable. -->
      <p class="visuellement-cache" role="status">{{ titreSuivi }}</p>

      <p v-if="panneSuivi" class="panne-inline" role="alert">
        Le suivi est coupé : {{ panneSuivi }} La copie continue peut-être côté serveur, mais cet écran
        ne peut plus le dire — ce qui suit est le dernier état connu. Il se remet à jour tout seul
        dès que le contact revient.
      </p>

      <template v-if="statut.running">
        <p v-if="statut.stopping" class="note-etat">
          Arrêt demandé : le fichier en cours est mis de côté tel quel, et la copie pourra reprendre
          là où elle s'est arrêtée. Les fichiers déjà copiés restent sur le disque.
        </p>
        <p v-else-if="statut.paused" class="note-etat">
          En pause. « Reprendre » relance la copie ; « Arrêter » la met de côté, reprenable.
        </p>
        <p v-if="avisRepartZero" class="note-etat" role="status">{{ avisRepartZero }}</p>

        <!-- L'ensemble d'abord : c'est la question qu'on se pose en revenant. -->
        <div class="ensemble">
          <div class="barre-ligne">
            <span
              class="barre grande"
              role="progressbar"
              aria-label="Avancement de la copie entière"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="Math.floor(pctTotal)"
              :aria-valuetext="`${taille(statut.bytes_done)} sur ${taille(statut.bytes_total)}`"
            >
              <span class="remplissage" :style="{ width: `${pctTotal}%` }"></span>
            </span>
            <span class="pct">{{ Math.floor(pctTotal) }} %</span>
          </div>
          <p class="chiffres-ligne">
            <span>{{ nombre(statut.files_done) }} sur {{ pluriel(statut.files_total, 'fichier') }}</span>
            <span>{{ taille(statut.bytes_done) }} sur {{ taille(statut.bytes_total) }}</span>
            <span>
              débit global
              <strong>{{ statut.paused ? 'en pause' : debit(statut.speed_bps) }}</strong>
            </span>
            <span v-if="!statut.paused">
              <template v-if="duree(statut.eta_s)">reste environ <strong>{{ duree(statut.eta_s) }}</strong></template>
              <template v-else>temps restant : mesure…</template>
            </span>
          </p>
        </div>

        <!-- La file, dans son ordre : terminés, en cours, à venir. Une barre
             par fichier, et la liste se recale sur celui qui avance. -->
        <ol ref="listeFichiers" class="fichiers-copie" aria-label="Fichiers de la copie, dans l'ordre de la file">
          <li v-if="termines.caches" class="autres">
            … et {{ pluriel(termines.caches, 'autre terminé', 'autres terminés') }} avant
          </li>

          <li v-for="(f, i) in termines.recents" :key="`fait-${termines.caches + i}`" class="fichier fait">
            <span class="nom">{{ base(f.target) }}</span>
            <span
              class="barre"
              role="progressbar"
              :aria-label="`${base(f.target)} : copié`"
              aria-valuemin="0"
              aria-valuemax="100"
              aria-valuenow="100"
            ><span class="remplissage" style="width: 100%"></span></span>
            <span class="infos">{{ taille(f.bytes) }} · {{ debitMoyen(f) }}</span>
          </li>

          <li v-if="courant" data-en-cours class="fichier courant">
            <span class="nom">{{ base(courant.target || courant.source) }}</span>
            <!-- Un fichier repris démarre à la part déjà écrite : elle est
                 dessinée d'un ton plus sourd, pour qu'on voie ce qui avance
                 MAINTENANT et ce qui était déjà là. -->
            <span class="barre-ligne">
              <span
                class="barre"
                role="progressbar"
                :aria-label="`${base(courant.target || courant.source)} : en cours de copie`"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="Math.floor(pctFichier)"
                :aria-valuetext="`${taille(courant.bytes_done)} sur ${taille(courant.bytes_total)}`
                  + (reprisA ? `, repris à ${taille(reprisA)}` : '')"
              >
                <span class="remplissage" :style="{ width: `${pctFichier}%` }"></span>
                <span v-if="pctRepris" class="repris" :style="{ width: `${Math.min(pctRepris, pctFichier)}%` }"></span>
              </span>
              <span class="pct">{{ Math.floor(pctFichier) }} %</span>
            </span>
            <span class="infos-courant">
              <span>{{ taille(courant.bytes_done) }} sur {{ taille(courant.bytes_total) }}</span>
              <span v-if="reprisA" class="mention-reprise">repris à {{ taille(reprisA) }}</span>
              <span class="vitesse">{{ statut.paused ? 'en pause' : debit(debitFichier) }}</span>
              <span v-if="!statut.paused">
                {{ resteFichier ? `reste ${resteFichier} pour ce fichier` : 'temps restant : mesure…' }}
              </span>
            </span>
            <span v-if="noteCourant" class="note-courant">{{ noteCourant }}</span>
            <span v-if="courant.target" class="dest">
              dans <code>{{ surDisque(dossier(courant.target), racineSuivi) || '/' }}</code>
            </span>
          </li>
          <li v-else class="autres">
            {{ statut.paused ? 'En pause avant le fichier suivant.' : 'Préparation du fichier suivant…' }}
          </li>

          <li v-for="(f, i) in aVenir" :key="`venir-${i}-${f.target}`" class="fichier attend">
            <span class="nom">{{ base(f.target) }}</span>
            <span
              class="barre"
              role="progressbar"
              :aria-label="`${base(f.target)} : à venir`"
              aria-valuemin="0"
              aria-valuemax="100"
              aria-valuenow="0"
            ></span>
            <span class="infos">{{ f.bytes != null ? taille(f.bytes) : 'taille inconnue' }}</span>
          </li>

          <li v-if="resteAVenir" class="autres">
            … et {{ pluriel(resteAVenir, 'autre à venir', 'autres à venir') }}
          </li>
        </ol>

        <div class="pilotage">
          <button
            v-if="!statut.paused && !statut.stopping"
            :disabled="!!action"
            @click="piloter('pause')"
          >
            {{ action === 'pause' ? 'Pause…' : 'Pause' }}
          </button>
          <button
            v-if="statut.paused && !statut.stopping"
            class="primary"
            :disabled="!!action"
            @click="piloter('resume')"
          >
            {{ action === 'resume' ? 'Reprise…' : 'Reprendre' }}
          </button>
          <ConfirmAction
            v-if="!statut.stopping"
            label="Arrêter (reprenable)"
            confirm-label="Confirmer l'arrêt"
            :detail="detailArret"
            :busy="action === 'stop'"
            :disabled="!!action && action !== 'stop'"
            @confirm="piloter('stop')"
          />
        </div>
        <p v-if="raisonPilotage" class="indispo" role="status">{{ raisonPilotage }}</p>
        <p v-if="erreurPilotage" class="panne-inline" role="alert">{{ erreurPilotage }}</p>
      </template>

      <!-- Le compte rendu : ce qui est sur le disque, ce qui n'y est pas, et
           pourquoi. Les chiffres sont ceux du serveur, jamais une estimation. -->
      <template v-else>
        <p class="bilan-principal">
          {{ accord(termines.total, 'fichier', 'copié') }} ({{ taille(termines.octets) }}) sur
          {{ nombre(statut.files_total) }} {{ verbe(statut.files_total, 'prévu', 'prévus') }}.
        </p>
        <p v-if="nonCopies" class="bilan-note">
          {{ pluriel(nonCopies, 'fichier') }}
          {{ verbe(nonCopies, "n'a pas été copié", "n'ont pas été copiés") }} :
          <template v-if="statut.stopped_by_user">l'arrêt est intervenu avant.</template>
          <template v-else-if="statut.aborted">la copie s'est interrompue avant (la cause est ci-dessous).</template>
          <template v-else>le serveur {{ nonCopies > 1 ? 'ne les a pas traités' : "ne l'a pas traité" }}.</template>
          <template v-if="reprise">
            Le bandeau « Copie interrompue » reprend la copie là où elle s'est arrêtée.
          </template>
          <template v-else>
            Relancer la copie {{ nonCopies > 1 ? 'les' : 'le' }} reprendra : ce qui est déjà sur le
            disque n'est pas recopié.
          </template>
        </p>

        <div v-if="bilanVisible && (bilan.ecartes.length || bilan.presents)" class="bilan-note">
          <p v-if="bilan.presents" class="bilan-note">
            {{ accord(bilan.presents, 'fichier', 'ignoré') }} : {{ verbe(bilan.presents, 'il était', 'ils étaient') }}
            déjà sur le disque.
          </p>
          <p v-if="bilan.ecartes.length" class="bilan-note">
            Écartés avant la copie :
            {{ bilan.ecartes.map((p) => libelleProbleme(p.raison, p.n, bilan.fs)).join(', ') }}.
            Rien de ceux-là n'a été écrit sur le disque.
          </p>
        </div>

        <details v-if="termines.total" class="copies">
          <summary>Voir {{ termines.total > 1 ? `les ${nombre(termines.total)} fichiers copiés` : 'le fichier copié' }}</summary>
          <ul>
            <li v-for="(f, i) in (statut.done ?? []).slice(0, 200)" :key="`copie-${i}`">
              <code>{{ surDisque(f.target, racineSuivi) }}</code>
              <span class="infos">{{ taille(f.bytes) }} · {{ debitMoyen(f) }}</span>
            </li>
          </ul>
          <p v-if="termines.total > 200" class="note">
            Et {{ pluriel(termines.total - 200, 'autre') }}, sur le disque aussi.
          </p>
        </details>

        <p class="ejecter">
          <strong>Avant de débrancher le disque, éjecte-le depuis l'interface du NAS</strong>
          (stockage externe → éjecter). Débranché sans ça, les dernières écritures peuvent ne pas être
          arrivées sur le disque.
        </p>

        <div class="pilotage">
          <button class="petit" @click="masquerRapport">Fermer ce compte rendu</button>
        </div>
      </template>

      <!-- Les erreurs arrivent au fil de l'eau, groupées par message : la même
           panne sur quarante épisodes se lit une fois, avec son compte. -->
      <section v-if="erreursGroupees.length" class="erreurs" aria-live="polite">
        <h4>
          {{ statut.running ? 'Erreurs en cours de route' : 'Ce qui a échoué' }}
          <span class="compte">{{ pluriel(erreursCopie.length, 'fichier') }}</span>
        </h4>
        <ul>
          <li v-for="g in erreursGroupees" :key="g.message">
            <span class="message">{{ g.message }}</span>
            <template v-if="g.items.length === 1">
              <code>{{ g.items[0].target || g.items[0].source }}</code>
            </template>
            <details v-else>
              <summary>{{ pluriel(g.items.length, 'fichier') }} concernés</summary>
              <ul>
                <li v-for="(e, i) in g.items.slice(0, 50)" :key="i">
                  <code>{{ e.target || e.source }}</code>
                </li>
              </ul>
              <p v-if="g.items.length > 50" class="note">Et {{ pluriel(g.items.length - 50, 'autre') }}.</p>
            </details>
          </li>
        </ul>
      </section>
    </section>

    <div class="grille">
      <!-- ============ La médiathèque ============ -->
      <section class="biblio" aria-labelledby="titre-biblio">
        <h3 id="titre-biblio" class="titre-col">Médiathèque</h3>

        <div v-if="!bibli && chargementBibli" class="attente-col">
          <span class="pulsation"></span>
          Lecture de la médiathèque…
        </div>

        <div v-else-if="!bibli" class="etat-col">
          <p class="panne-inline" role="alert">
            La médiathèque n'a pas pu être lue. {{ panneBibli ?? serveurMuet() }}
          </p>
          <button class="primary" :disabled="chargementBibli" @click="chargerBibli">Réessayer</button>
        </div>

        <div v-else-if="!bibli.built" class="etat-col vide">
          <p>
            <strong>La médiathèque n'a jamais été lue</strong> : Sortilège ne sait pas encore ce
            qu'elle contient, il n'a donc rien à proposer ici.
          </p>
          <p class="quoi-faire">
            Va dans <strong>Ma médiathèque</strong> et lance <strong>Relire la médiathèque</strong>,
            puis reviens ici.
          </p>
          <button class="petit" :disabled="chargementBibli" @click="chargerBibli">
            {{ chargementBibli ? 'Relecture…' : 'Réessayer' }}
          </button>
        </div>

        <div v-else-if="!bibli.works.length" class="etat-col vide">
          <p>
            La médiathèque a été lue, et elle est vide : il n'y a rien à copier. « Relire la
            médiathèque », dans Ma médiathèque, la reconstruit si tu viens d'y ranger des fichiers.
          </p>
        </div>

        <template v-else>
          <div class="recherche">
            <label for="copie-recherche" class="recherche-libelle">Chercher un titre ou une année</label>
            <div class="recherche-champ">
              <input
                id="copie-recherche"
                ref="champRecherche"
                v-model="recherche"
                type="search"
                autocomplete="off"
                spellcheck="false"
                placeholder="dune, sev, 2021…"
                @keydown.esc="surEchap"
              />
              <button v-if="recherche" type="button" class="petit" @click="effacerRecherche">Effacer</button>
            </div>
          </div>

          <!-- Les compteurs suivent la recherche : taper « dun » dit tout de
               suite s'il faut regarder les films ou les séries. -->
          <div class="types" role="group" aria-label="Filtrer par type">
            <button :class="{ actif: type === 'all' }" :aria-pressed="type === 'all'" @click="type = 'all'">
              Tous ({{ nombre(compteParType.all) }})
            </button>
            <button
              v-for="t in TYPES"
              :key="t.id"
              :class="{ actif: type === t.id, muet: !compteParType[t.id] }"
              :aria-pressed="type === t.id"
              @click="type = t.id"
            >
              {{ t.label }} ({{ nombre(compteParType[t.id] ?? 0) }})
            </button>
          </div>

          <div class="barre-selection">
            <span class="compte-selection" role="status">
              {{ accord(selection.size, 'œuvre', 'cochée') }} · {{ taille(octetsSelection) }}
            </span>
            <button
              v-if="selection.size || voirSelection"
              class="lien"
              :aria-pressed="voirSelection"
              @click="basculerVoirSelection"
            >
              {{ voirSelection ? 'Tout voir' : 'Voir la sélection' }}
            </button>
            <button v-if="aCocher" class="petit" @click="toutCocher">
              Tout cocher ({{ pluriel(listees.length, 'affichée') }})
            </button>
            <span v-else-if="listees.length" class="deja">Toutes les œuvres affichées sont cochées.</span>
            <button v-if="selection.size" class="petit" @click="toutDecocher">Tout décocher</button>
          </div>
          <p v-if="cocheesMasquees" class="note">
            Dont {{ pluriel(cocheesMasquees, 'masquée') }} par la recherche ou le filtre :
            {{ verbe(cocheesMasquees, 'elle reste cochée', 'elles restent cochées') }}.
          </p>

          <div v-if="!filtrees.length" class="vide-liste">
            <template v-if="rechercheNette">
              <p>
                {{ aucunDuType }} ne contient « {{ rechercheNette }} »<template v-if="voirSelection">
                  dans la sélection</template>.
              </p>
              <p v-if="type !== 'all' && compteParType.all" class="note">
                Les autres types en comptent {{ nombre(compteParType.all) }}.
              </p>
              <div class="boutons">
                <button v-if="type !== 'all' && compteParType.all" class="petit" @click="type = 'all'">
                  Tous les types
                </button>
                <button class="petit" @click="effacerRecherche">Effacer la recherche</button>
              </div>
            </template>
            <template v-else-if="voirSelection">
              <p>{{ aucunDuType }} dans la sélection.</p>
              <div class="boutons">
                <button class="petit" @click="basculerVoirSelection">Tout voir</button>
              </div>
            </template>
            <p v-else>{{ aucunDuType }} dans la médiathèque.</p>
          </div>

          <ul v-else class="oeuvres">
            <li
              v-for="w in listees"
              :key="w.key"
              class="oeuvre"
              :class="{ cochee: selection.has(w.key) }"
            >
              <!-- Toute la ligne coche : c'est un vrai `label`, et la case reste
                   une vraie case — atteignable au Tab, cochée à l'espace. -->
              <label class="ligne">
                <input type="checkbox" :checked="selection.has(w.key)" @change="basculer(w.key)" />
                <span class="titre">
                  {{ w.title }}<span v-if="w.year" class="annee"> ({{ w.year }})</span>
                </span>
                <span class="meta">
                  {{ NATURE[w.kind] ?? w.kind }} · {{ pluriel(w.files, 'fichier') }} · {{ taille(w.bytes) }}
                </span>
              </label>

              <div v-if="selection.has(w.key) && disqueChoisi" class="etat">
                <template v-if="etats.get(w.key)">
                  <span class="pastille" :class="etats.get(w.key).classe">{{ etats.get(w.key).texte }}</span>
                  <span v-if="etats.get(w.key).dejaLa && etats.get(w.key).classe !== 'absent'" class="ou">
                    sur le disque :
                    <code>{{ surDisque(etats.get(w.key).dejaLa, disqueCourant?.path) || '/' }}</code>
                  </span>
                  <details v-for="p in etats.get(w.key).problemes" :key="p.raison" class="probleme">
                    <summary>{{ libelleProbleme(p.raison, p.items.length, fsCourant) }}</summary>
                    <p v-if="EXPLICATIONS[p.raison]" class="explique">{{ EXPLICATIONS[p.raison] }}</p>
                    <ul>
                      <li v-for="(i, n) in p.items" :key="n">
                        <code>{{ i.path }}</code>
                        <span v-if="i.message" class="message"> — {{ i.message }}</span>
                      </li>
                    </ul>
                  </details>
                </template>
                <span v-else class="attente-ligne">
                  {{ panneAnalyse ? "état inconnu : l'analyse du disque n'a pas abouti" : 'analyse du disque…' }}
                </span>
              </div>
            </li>
          </ul>

          <div v-if="filtrees.length > listees.length" class="plus">
            <button @click="affiche += PALIER">
              Afficher {{ pluriel(Math.min(PALIER, filtrees.length - listees.length), 'œuvre') }} de plus
            </button>
            <span class="note">{{ nombre(listees.length) }} sur {{ nombre(filtrees.length) }}</span>
          </div>
        </template>
      </section>

      <!-- ============ Le disque ============ -->
      <section class="disques" aria-labelledby="titre-disques">
        <!-- Une copie interrompue passe avant le choix d'une nouvelle : c'est
             la première chose à trancher en arrivant, et elle se tranche ici,
             à côté du disque qu'il faut peut-être rebrancher. -->
        <div v-if="reprise" class="reprise" role="group" aria-labelledby="titre-reprise">
          <h4 id="titre-reprise">Copie interrompue</h4>
          <p>
            Une copie vers « {{ libelleDisque(reprise.disk) }} » a été interrompue
            {{ horodatage(reprise.saved_at) }} :
            {{ pluriel(reprise.files_remaining, 'fichier restant', 'fichiers restants') }},
            {{ taille(reprise.bytes_remaining) }}.
            <template v-if="reprise.current && checkpoint">
              Le fichier en cours reprendra à {{ taille(checkpoint) }} sur
              {{ taille(reprise.current.bytes_total) }}, après contrôle : identité de la source,
              dernier segment relu en entier, échantillons ailleurs. Sinon il repartira de zéro, et
              l'écran le dira.
            </template>
            <template v-else-if="reprise.current">
              Le fichier en cours repartira de zéro : rien n'en avait été écrit.
            </template>
          </p>
          <div class="boutons-reprise">
            <button class="primary" :disabled="!!raisonReprise" @click="reprendre">
              {{ action === 'continue' ? 'Reprise…' : 'Reprendre la copie' }}
            </button>
            <ConfirmAction
              v-if="!oubliSeulement"
              label="Abandonner"
              confirm-label="Confirmer l'abandon"
              :detail="detailAbandon"
              :busy="action === 'discard'"
              :disabled="!!action && action !== 'discard'"
              @confirm="abandonner(false)"
            />
            <ConfirmAction
              v-else
              label="Oublier cette copie"
              confirm-label="Confirmer l'oubli"
              :detail="detailOubli"
              :busy="action === 'discard'"
              :disabled="!!action && action !== 'discard'"
              @confirm="abandonner(true)"
            />
          </div>
          <p v-if="raisonReprise" class="indispo" role="status">{{ raisonReprise }}</p>
          <p v-else-if="repriseAilleurs" class="note" role="status">{{ repriseAilleurs }}</p>
        </div>
        <!-- Hors du bandeau : un refus de « Reprendre » (rien à reprendre, par
             exemple) peut faire disparaître le bandeau, et son message avec. -->
        <p v-if="erreurReprise" class="panne-inline" role="alert">{{ erreurReprise }}</p>
        <p v-if="messageReprise && !reprise" class="ok-msg" role="status">{{ messageReprise }}</p>

        <div class="tete-col">
          <h3 id="titre-disques" class="titre-col">Disque de destination</h3>
          <!-- Un disque branché à l'instant n'apparaît qu'après une relecture :
               le bouton est toujours là pour ça. -->
          <button class="petit" :disabled="chargementDisques" @click="relireDisques">
            {{ chargementDisques ? 'Relecture…' : 'Relire les disques' }}
          </button>
        </div>

        <div v-if="!disques && chargementDisques" class="attente-col">
          <span class="pulsation"></span>
          Recherche des disques branchés…
        </div>

        <div v-else-if="!disques" class="etat-col">
          <p class="panne-inline" role="alert">
            Les disques n'ont pas pu être lus. {{ panneDisques ?? serveurMuet() }}
          </p>
        </div>

        <template v-else>
          <p v-if="panneDisques" class="panne-inline" role="alert">
            Cette liste date de la dernière lecture réussie : la relecture a échoué. {{ panneDisques }}
          </p>
          <p v-if="avisDisque" class="avis" role="status">{{ avisDisque }}</p>

          <div v-if="!disques.disks.length" class="aucun-disque">
            <p><strong>Aucun disque externe trouvé.</strong></p>
            <p v-if="disques.hint" class="hint">{{ disques.hint }}</p>
            <p class="note">
              Pour apparaître ici, un disque doit être monté <em>dans le conteneur</em> de Sortilège,
              à l'intérieur d'un dossier de <code>{{ disques.root || '/externes' }}</code> : le dossier
              parent des disques, monté avec <code>:rslave</code> (par exemple sur
              <code>{{ disques.root || '/externes' }}/usb</code>). Le brancher sur le NAS ne suffit
              pas si ce dossier n'est pas relié au conteneur, et un disque monté directement sur
              <code>{{ disques.root || '/externes' }}/&lt;nom&gt;</code> est refusé.
              <template v-if="disques.root_exists === false">
                Ce dossier n'existe pas dans le conteneur : aucun volume n'y est relié.
              </template>
              Un disque branché à l'instant apparaît après « Relire les disques ».
            </p>
          </div>

          <!-- Des boutons radio natifs : un seul choix, et les flèches du
               clavier passent d'un disque à l'autre en sautant les refusés. -->
          <div v-else class="liste-disques" role="radiogroup" aria-labelledby="titre-disques">
            <label
              v-for="d in disques.disks"
              :key="d.id"
              class="disque"
              :class="{ choisi: disqueChoisi === d.id, refuse: !!refusDe(d) }"
            >
              <input
                v-model="disqueChoisi"
                type="radio"
                name="copie-disque"
                :value="d.id"
                :disabled="!!refusDe(d)"
              />
              <span class="disque-corps">
                <span class="disque-tete">
                  <span class="disque-nom">{{ d.label || d.id }}</span>
                  <span v-if="nomSysteme(d.fs)" class="fs">{{ nomSysteme(d.fs) }}</span>
                </span>
                <template v-if="Number(d.total_bytes) > 0">
                  <span class="jauge" aria-hidden="true">
                    <span class="utilise" :style="{ width: `${pctUtilise(d)}%` }"></span>
                    <span
                      v-if="projection(d)"
                      class="prevu"
                      :class="{ deborde: projection(d).deborde }"
                      :style="{ width: `${projection(d).pct}%` }"
                    ></span>
                  </span>
                  <span class="place">
                    {{ taille(d.free_bytes) }} libres sur {{ taille(d.total_bytes) }}
                  </span>
                </template>
                <span v-else-if="!d.refusal" class="place">Place inconnue : le disque n'a pas donné sa taille.</span>
                <span v-if="d.max_file_bytes" class="limite">
                  Fichiers de {{ taille(d.max_file_bytes) }} au plus sur ce disque.
                </span>
                <span v-if="refusDe(d)" class="refus"><strong>Indisponible.</strong> {{ phrase(refusDe(d)) }}</span>
              </span>
            </label>
          </div>
        </template>
      </section>

      <!-- ============ Ce qui sera copié ============ -->
      <section class="recap" aria-labelledby="titre-recap">
        <h3 id="titre-recap" class="titre-col">Ce qui sera copié</h3>
        <p class="rappel">
          {{ accord(selection.size, 'œuvre', 'cochée') }} · {{ taille(octetsSelection) }}
          <template v-if="disqueCourant"> → « {{ disqueCourant.label || disqueCourant.id }} »</template>
        </p>

        <template v-if="disqueChoisi && selection.size">
          <div v-if="!analyseAJour && !panneAnalyse" class="attente-inline" role="status">
            <span class="pulsation petite"></span>
            Sortilège regarde ce que le disque contient déjà…
          </div>
          <p v-if="panneAnalyse" class="panne-inline" role="alert">
            L'analyse du disque n'a pas abouti. {{ panneAnalyse }}
            <button class="petit" :disabled="analyseEnCours" @click="analyser">Réessayer</button>
          </p>

          <template v-if="analyseAJour">
            <dl class="chiffres">
              <div>
                <dt>Fichiers à copier</dt>
                <dd>{{ nombre(totaux.files_to_copy) }}</dd>
              </div>
              <div>
                <dt>Volume à copier</dt>
                <dd>{{ taille(totaux.bytes_to_copy) }}</dd>
              </div>
              <div>
                <dt>Déjà sur le disque</dt>
                <dd>{{ pluriel(totaux.files_present, 'fichier') }}</dd>
              </div>
              <div>
                <dt>Place libre après copie</dt>
                <dd :class="{ ko: !tient }">{{ tient ? taille(libreApres) : 'insuffisante' }}</dd>
              </div>
            </dl>

            <p v-if="!tient" class="manque" role="alert">
              Il manque {{ taille(analyse.missing_bytes) }} sur « {{ disqueCourant?.label || disqueChoisi }} ».
            </p>

            <p v-if="ecartes.length" class="ecartes">
              Écartés : {{ ecartes.map((p) => libelleProbleme(p.raison, p.n, fsCourant)).join(', ') }}.
              Le détail est sur chaque ligne cochée.
            </p>

            <div v-if="erreursLecture.length" class="lecture-partielle">
              <p>
                Le disque n'a pas pu être lu entièrement : ce qui y est déjà peut être sous-compté, et un
                fichier présent risque d'être proposé à la copie.
              </p>
              <ul>
                <li v-for="(e, i) in erreursLecture.slice(0, 10)" :key="i"><code>{{ texteErreurLecture(e) }}</code></li>
              </ul>
            </div>
          </template>
        </template>

        <div class="options">
          <label class="option">
            <input v-model="sidecars" type="checkbox" />
            <span>Copier aussi les sous-titres, fiches et affiches</span>
          </label>
          <label class="option-debit">
            <span>Débit maximal</span>
            <select v-model="debitMax">
              <option value="">sans limite</option>
              <option value="20">20 Mo/s</option>
              <option value="50">50 Mo/s</option>
              <option value="100">100 Mo/s</option>
            </select>
          </label>
          <p class="note">
            La copie se fait de toute façon un fichier à la fois : un seul transfert occupe le port USB.
            Limiter le débit laisse en plus de la marge au NAS pour ses autres tâches.
          </p>
          <p v-if="statut?.running" class="note">
            Ces options valent pour la prochaine copie, pas pour celle en cours.
          </p>
        </div>

        <button
          class="primary copier"
          :disabled="!!raisonCopie"
          :aria-describedby="raisonCopie ? 'copie-raison' : undefined"
          @click="lancer"
        >
          {{ texteBouton }}
        </button>
        <p v-if="raisonCopie" id="copie-raison" class="indispo" role="status">{{ raisonCopie }}</p>
        <p v-if="erreurLancement" class="panne-inline" role="alert">
          {{ erreurLancement }}
          <button
            v-if="refusLancement && refusLancement === cleAnalyse"
            class="petit"
            :disabled="analyseEnCours || chargementDisques"
            @click="refaireAnalyse"
          >
            {{ analyseEnCours ? 'Analyse…' : "Refaire l'analyse" }}
          </button>
        </p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.copie { display: flex; flex-direction: column; gap: 18px; }

.entete h2 {
  margin: 0 0 6px; font-size: var(--t-lg); font-weight: 600;
  letter-spacing: -.01em; color: var(--text-title);
}
.lead { margin: 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.65; max-width: 76ch; }
.lead strong { color: var(--text-dim); }

.visuellement-cache {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}

/* La boîte vient de `.panne-inline` (style.css) ; ne reste que la mise en
   ligne d'un bouton « Réessayer » à côté du texte. */
.panne-inline { margin: 0; overflow-wrap: anywhere; }
.panne-inline .petit { margin-left: 6px; color: var(--text); }

/* --- Grille ----------------------------------------------------------------- *
 * Trois zones et non deux colonnes : sur un téléphone, l'ordre utile est
 * disque, médiathèque, puis ce qui sera copié — le bouton juste après la
 * liste où l'on vient de cocher. Sur un écran large, les deux dernières
 * zones s'empilent à droite, et le récapitulatif suit le défilement. */
.grille {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  grid-template-areas:
    "biblio disques"
    "biblio recap";
  grid-template-rows: auto 1fr;
  gap: 18px 28px;
  align-items: start;
}
.biblio { grid-area: biblio; }
.disques { grid-area: disques; }
.recap { grid-area: recap; }
.biblio, .disques, .recap { min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.disques, .recap {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}

@media (min-width: 901px) {
  /* Le bouton de copie reste à portée pendant qu'on descend dans 1 200
     œuvres. Plafonné à la hauteur de l'écran : un bloc collant plus haut que
     la fenêtre cacherait justement son bas, c'est-à-dire le bouton. */
  .recap { position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow-y: auto; }
}

@media (max-width: 900px) {
  .grille {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "disques" "biblio" "recap";
    grid-template-rows: auto;
  }
}

.titre-col {
  margin: 0; font-size: var(--t-xs); font-weight: 600; text-transform: uppercase;
  letter-spacing: .07em; color: var(--text-title);
}
.tete-col { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }

.attente-col {
  display: flex; align-items: center; gap: 12px; padding: 24px 4px;
  font-size: var(--t-sm); color: var(--text-dim);
}
.etat-col { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
.etat-col.vide {
  padding: 18px; border-radius: 10px; background: var(--surface); border: 1px solid var(--border);
}
.etat-col p { margin: 0; font-size: var(--t-sm); color: var(--text-dim); line-height: 1.6; }
.etat-col .quoi-faire { color: var(--text-faint); }
.etat-col strong { color: var(--text); }

button.petit { font-size: var(--t-xs); padding: 5px 11px; color: var(--text-dim); min-height: 30px; }
button.lien {
  background: none; border: none; padding: 4px 2px; min-height: 30px;
  font-size: var(--t-xs); color: var(--accent);
  text-decoration: underline; text-underline-offset: 3px;
}
button.lien[aria-pressed="true"] { color: var(--text); }

.note { margin: 0; font-size: var(--t-xs); color: var(--text-faint); line-height: 1.6; }
.note code, .aucun-disque code { font-family: var(--mono); font-size: var(--t-xs); overflow-wrap: anywhere; }

/* La raison d'un bouton inerte : ton d'appoint, jamais celui d'une alerte. */
.indispo {
  margin: 0; font-size: var(--t-xs); line-height: 1.6; color: var(--text-dim);
  border-left: 2px solid var(--accent-dim); padding-left: 10px;
}

/* --- Recherche et filtres --------------------------------------------------- */

.recherche { display: flex; flex-direction: column; gap: 5px; }
.recherche-libelle { font-size: var(--t-xs); color: var(--text-dim); }
.recherche-champ { display: flex; gap: 8px; align-items: stretch; }
.recherche-champ input {
  flex: 1; min-width: 0; font-size: var(--t-md); padding: 8px 12px;
  background: var(--surface-2);
}
/* Le bouton « Effacer » fait déjà ce travail, et dit son nom. */
.recherche-champ input::-webkit-search-cancel-button { -webkit-appearance: none; appearance: none; }

.types { display: flex; flex-wrap: wrap; gap: 6px; }
.types button { font-size: var(--t-xs); padding: 5px 12px; min-height: 30px; color: var(--text-dim); }
.types button.actif { border-color: var(--accent); color: var(--text); background: var(--surface); }
/* Un type à zéro reste lisible et cliquable : « Livres (0) » dit « mesuré, il
   n'y en a pas », là où un bouton absent ne dirait rien. */
.types button.muet:not(.actif) { color: var(--text-faint); }

.barre-selection {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
}
.compte-selection {
  margin-right: auto; font-size: var(--t-sm); color: var(--text);
  font-variant-numeric: tabular-nums;
}
.deja { font-size: var(--t-xs); color: var(--text-faint); }

.vide-liste {
  padding: 22px 16px; border-radius: 10px; text-align: center;
  background: var(--surface); border: 1px solid var(--border);
  display: flex; flex-direction: column; align-items: center; gap: 8px;
}
.vide-liste p { margin: 0; font-size: var(--t-sm); color: var(--text-dim); overflow-wrap: anywhere; }
.vide-liste .boutons { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }

/* --- Lignes d'œuvre --------------------------------------------------------- */

.oeuvres { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.oeuvre { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.oeuvre.cochee { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }

/* 40 px de haut : une ligne se coche au doigt du premier coup, sans viser la
   petite case. */
.ligne {
  display: flex; align-items: center; flex-wrap: wrap; gap: 3px 10px;
  min-height: 40px; padding: 7px 12px; border-radius: 8px; cursor: pointer;
}
.ligne:hover { background: var(--surface-2); }
.ligne input {
  width: 16px; height: 16px; margin: 0; flex: none;
  accent-color: var(--accent); cursor: pointer;
}
.ligne .titre { flex: 1 1 12rem; min-width: 0; font-size: var(--t-sm); color: var(--text); overflow-wrap: anywhere; }
.annee { color: var(--text-faint); }
.meta { margin-left: auto; font-size: var(--t-xs); color: var(--text-faint); font-variant-numeric: tabular-nums; }

.etat {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px;
  padding: 0 12px 9px 38px; font-size: var(--t-xs); line-height: 1.5;
}
.pastille { padding: 1px 8px; border-radius: 20px; border: 1px solid var(--border); color: var(--text-dim); }
.pastille.present {
  color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, transparent);
  background: color-mix(in srgb, var(--ok) 9%, transparent);
}
.pastille.partiel {
  color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 9%, transparent);
}
.pastille.absent { color: var(--accent); border-color: var(--accent-dim); }
.ou { min-width: 0; color: var(--text-faint); overflow-wrap: anywhere; }
.ou code, .probleme code { font-family: var(--mono); font-size: var(--t-xs); overflow-wrap: anywhere; }
.attente-ligne { color: var(--text-faint); font-style: italic; }

.probleme { flex-basis: 100%; color: var(--warn); }
.probleme summary { cursor: pointer; min-height: 24px; }
.probleme .explique { margin: 4px 0; color: var(--text-dim); max-width: 70ch; }
.probleme ul { margin: 4px 0 0; padding-left: 16px; display: flex; flex-direction: column; gap: 2px; }
.probleme li { color: var(--text-faint); overflow-wrap: anywhere; }
.probleme .message { color: var(--text-dim); }

.plus { display: flex; align-items: center; justify-content: center; gap: 12px; flex-wrap: wrap; padding: 4px 0 8px; }

/* --- Disques ---------------------------------------------------------------- */

.reprise {
  display: flex; flex-direction: column; gap: 8px; padding: 11px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--warn) 8%, var(--surface));
  border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--border));
}
.reprise h4 {
  margin: 0; font-size: var(--t-xs); font-weight: 600; text-transform: uppercase;
  letter-spacing: .06em; color: var(--warn);
}
.reprise p { margin: 0; font-size: var(--t-sm); color: var(--text); line-height: 1.6; overflow-wrap: anywhere; }
.reprise .indispo { font-size: var(--t-xs); color: var(--text-dim); }
.boutons-reprise { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 8px 10px; }
.boutons-reprise > button { font-size: var(--t-sm); min-height: 32px; }
.ok-msg { margin: 0; font-size: var(--t-sm); color: var(--ok); line-height: 1.6; }

.avis {
  margin: 0; font-size: var(--t-xs); line-height: 1.6; color: var(--warn);
  border-left: 2px solid var(--warn); padding-left: 10px;
}
.aucun-disque { display: flex; flex-direction: column; gap: 8px; }
.aucun-disque p { margin: 0; font-size: var(--t-sm); color: var(--text-dim); line-height: 1.6; }
.aucun-disque .hint { color: var(--text); overflow-wrap: anywhere; }

.liste-disques { display: flex; flex-direction: column; gap: 8px; }
.disque {
  display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2);
  cursor: pointer;
}
.disque:hover { border-color: var(--accent-dim); }
.disque input { margin: 3px 0 0; width: 16px; height: 16px; flex: none; accent-color: var(--accent); cursor: pointer; }
.disque.choisi { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, var(--surface-2)); }
.disque.refuse { cursor: not-allowed; background: var(--surface); }
.disque.refuse:hover { border-color: var(--border); }
.disque.refuse input { cursor: not-allowed; }
.disque-corps { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.disque-tete { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.disque-nom { font-size: var(--t-sm); font-weight: 500; color: var(--text); overflow-wrap: anywhere; }
.fs {
  font-family: var(--mono); font-size: var(--t-xs); color: var(--text-faint);
  border: 1px solid var(--border); border-radius: 4px; padding: 0 6px;
}
.jauge { display: flex; height: 6px; border-radius: 3px; overflow: hidden; background: var(--bg); }
.jauge .utilise { background: var(--text-faint); }
.jauge .prevu { background: var(--accent); }
.jauge .prevu.deborde { background: var(--err); }
.place { font-size: var(--t-xs); color: var(--text-dim); font-variant-numeric: tabular-nums; }
.limite { font-size: var(--t-xs); color: var(--text-faint); }
.refus { font-size: var(--t-xs); color: var(--err); line-height: 1.5; }

/* --- Récapitulatif ---------------------------------------------------------- */

.rappel { margin: 0; font-size: var(--t-sm); color: var(--text-dim); overflow-wrap: anywhere; }
.attente-inline { display: flex; align-items: center; gap: 10px; font-size: var(--t-sm); color: var(--text-dim); }
.pulsation.petite { width: 16px; height: 16px; flex: none; }

.chiffres { margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.chiffres div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.chiffres dt { font-size: var(--t-xs); color: var(--text-faint); }
.chiffres dd { margin: 0; font-size: var(--t-md); color: var(--text); font-variant-numeric: tabular-nums; }
.chiffres dd.ko { color: var(--err); }

.manque { margin: 0; font-size: var(--t-sm); font-weight: 600; color: var(--err); }
.ecartes { margin: 0; font-size: var(--t-xs); color: var(--warn); line-height: 1.6; }
.lecture-partielle { font-size: var(--t-xs); color: var(--warn); line-height: 1.6; }
.lecture-partielle p { margin: 0; }
.lecture-partielle ul { margin: 4px 0 0; padding-left: 16px; }
.lecture-partielle code { font-family: var(--mono); color: var(--text-faint); overflow-wrap: anywhere; }

.options { display: flex; flex-direction: column; gap: 8px; padding-top: 10px; border-top: 1px solid var(--border); }
.option {
  display: flex; align-items: flex-start; gap: 9px; min-height: 32px; padding: 4px 0;
  font-size: var(--t-sm); color: var(--text); cursor: pointer;
}
.option input { width: 16px; height: 16px; margin: 2px 0 0; flex: none; accent-color: var(--accent); }
.option-debit { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--t-sm); color: var(--text); }
.option-debit select {
  font: inherit; font-size: var(--t-sm); min-height: 32px; padding: 5px 8px;
  color: var(--text); background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px;
}

.copier { width: 100%; padding: 10px 14px; font-size: var(--t-md); font-weight: 600; }

/* --- Suivi de la copie ------------------------------------------------------ */

.bandeau { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }

.suivi {
  display: flex; flex-direction: column; gap: 12px; padding: 14px 16px;
  background: var(--surface); border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border));
}
.suivi.pause { border-color: color-mix(in srgb, var(--warn) 45%, var(--border)); }
.suivi.fini { border-color: var(--border); }
.suivi-tete { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; }
.suivi-tete h3 { margin: 0; font-size: var(--t-md); font-weight: 600; color: var(--text-title); }
.vers { font-size: var(--t-xs); color: var(--text-dim); overflow-wrap: anywhere; }
.note-etat { margin: 0; font-size: var(--t-sm); color: var(--warn); line-height: 1.6; }

.barre {
  display: block; position: relative; flex: 1; min-width: 0; height: 6px;
  background: var(--bg); border-radius: 3px; overflow: hidden;
  border: 1px solid var(--border);
}
/* La part reprise, posée par-dessus le début du remplissage : plus sourde que
   ce qui s'écrit maintenant, pour que l'avancée du jour se lise à part. */
.repris {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: color-mix(in srgb, var(--accent) 40%, var(--bg));
  border-right: 1px solid var(--bg);
}
.pause .repris { background: color-mix(in srgb, var(--warn) 40%, var(--bg)); }
.mention-reprise {
  font-size: var(--t-xs); color: var(--text-dim); padding: 0 7px; border-radius: 20px;
  border: 1px solid var(--border);
}
.note-courant { font-size: var(--t-xs); color: var(--warn); line-height: 1.5; overflow-wrap: anywhere; }
.barre.grande { height: 12px; border-radius: 6px; }
.remplissage {
  display: block; height: 100%; background: var(--accent);
  transition: width .6s linear;
}
.fait .remplissage { background: var(--ok); }
.pause .ensemble .remplissage, .pause .courant .remplissage { background: var(--warn); }
/* Une barre qui glisse en continu dans le champ de vision est exactement ce
   que ce réglage système demande de faire cesser. Aucune rayure animée non
   plus : la largeur suffit à dire que ça avance. */
@media (prefers-reduced-motion: reduce) { .remplissage { transition: none; } }

.ensemble { display: flex; flex-direction: column; gap: 6px; }
.barre-ligne { display: flex; align-items: center; gap: 10px; min-width: 0; }
.pct {
  flex: none; min-width: 4.5ch; text-align: right;
  font-family: var(--mono); font-size: var(--t-sm); color: var(--text);
}
.chiffres-ligne {
  margin: 0; display: flex; flex-wrap: wrap; gap: 4px 18px;
  font-size: var(--t-sm); color: var(--text-dim);
}
.chiffres-ligne strong { color: var(--text-title); font-weight: 600; font-variant-numeric: tabular-nums; }

.fichiers-copie {
  list-style: none; margin: 0; padding: 4px; position: relative;
  max-height: 24rem; overflow-y: auto;
  display: flex; flex-direction: column; gap: 2px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
}
.fichier {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(80px, 150px) auto;
  align-items: center; gap: 4px 12px; padding: 6px 10px; border-radius: 6px;
  font-size: var(--t-xs);
}
.fichier .nom { min-width: 0; color: var(--text-dim); overflow-wrap: anywhere; }
.fichier.attend .nom { color: var(--text-faint); }
.fichier .infos {
  color: var(--text-faint); text-align: right; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.fichier.courant {
  grid-template-columns: minmax(0, 1fr); gap: 6px; padding: 10px 12px;
  background: color-mix(in srgb, var(--accent) 9%, var(--surface));
  border: 1px solid var(--accent-dim);
}
.pause .fichier.courant {
  background: color-mix(in srgb, var(--warn) 7%, var(--surface));
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border));
}
.fichier.courant .nom { font-size: var(--t-sm); color: var(--text); }
.fichier.courant .barre { height: 10px; border-radius: 5px; }
.infos-courant {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px;
  font-size: var(--t-sm); color: var(--text-dim); font-variant-numeric: tabular-nums;
}
/* Le débit instantané est le chiffre qu'on vient chercher : il se lit de loin. */
.vitesse { font-size: var(--t-lg); font-weight: 600; color: var(--text-title); }
.dest { font-size: var(--t-xs); color: var(--text-faint); overflow-wrap: anywhere; }
.dest code { font-family: var(--mono); }
.autres { padding: 5px 10px; font-size: var(--t-xs); color: var(--text-faint); font-style: italic; }

.pilotage { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 10px; }
.pilotage > button { font-size: var(--t-sm); min-height: 32px; }

.bilan-principal { margin: 0; font-size: var(--t-md); color: var(--text); }
.bilan-note { margin: 0; font-size: var(--t-sm); color: var(--text-dim); line-height: 1.6; }
div.bilan-note { display: flex; flex-direction: column; gap: 4px; }

.copies summary { cursor: pointer; font-size: var(--t-sm); color: var(--text-dim); min-height: 28px; }
.copies ul { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.copies li { display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: var(--t-xs); }
.copies code { font-family: var(--mono); color: var(--text-dim); overflow-wrap: anywhere; min-width: 0; }
.copies .infos { color: var(--text-faint); font-variant-numeric: tabular-nums; }

.ejecter {
  margin: 0; padding: 9px 12px; font-size: var(--t-sm); line-height: 1.6; color: var(--text);
  border-left: 2px solid var(--warn); border-radius: 0 6px 6px 0;
  background: color-mix(in srgb, var(--warn) 7%, transparent);
}
.ejecter strong { color: var(--warn); }

.erreurs {
  display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--err) 28%, var(--border));
}
.erreurs h4 {
  margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px;
  font-size: var(--t-xs); font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
  color: var(--err);
}
.erreurs h4 .compte { text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--text-faint); }
.erreurs > ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.erreurs li { display: flex; flex-direction: column; gap: 2px; font-size: var(--t-xs); line-height: 1.5; }
.erreurs .message { color: var(--err); overflow-wrap: anywhere; }
.erreurs code { font-family: var(--mono); color: var(--text-faint); overflow-wrap: anywhere; }
.erreurs summary { cursor: pointer; color: var(--text-dim); }
.erreurs details ul { margin: 4px 0 0; padding-left: 16px; }

/* --- Écrans étroits --------------------------------------------------------- */
@media (max-width: 700px) {
  /* Le poids passe sous le titre plutôt que de lui disputer la ligne. */
  .meta { flex-basis: 100%; margin-left: 0; padding-left: 26px; }
  .etat { padding-left: 38px; }

  /* 32 px de cible, obtenus au rembourrage et non en grossissant le texte. */
  button.petit, button.lien, .types button { min-height: 32px; padding-top: 7px; padding-bottom: 7px; }

  /* Le nom prend sa ligne, la barre et les chiffres la suivante. */
  .fichier { grid-template-columns: minmax(0, 1fr) auto; }
  .fichier .nom { grid-column: 1 / -1; }
  .fichier.courant { grid-template-columns: minmax(0, 1fr); }

  .chiffres-ligne { gap: 4px 14px; }
}
</style>
