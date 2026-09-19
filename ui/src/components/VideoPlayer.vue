<script>
/**
 * Ce que CE navigateur décode, mesuré une fois par chargement de la page.
 *
 * Le serveur ne peut pas le deviner, et c'est décisif : le réencodage produit
 * du HEVC 10 bits, que Firefox sur Mac décode par le matériel, et une
 * médiathèque contient des WebM VP9 et des MP4 AV1 que Firefox lit tels quels.
 * Sans cette déclaration, le NAS convertissait ce que le navigateur lit très
 * bien — un AV1 4K converti en logiciel sur un Celeron, pour une image moins
 * fidèle que le fichier.
 *
 * Exporté : la médiathèque s'en sert AUSSI pour décider si elle montre le
 * lecteur, et les deux écrans doivent poser au serveur la même question avec
 * les mêmes capacités — sinon l'un propose le lecteur quand l'autre le refuse.
 * Mesuré comme hls.js le fera (Media Source Extensions), avec le lecteur natif
 * en repli pour Safari sur iPhone.
 */
let capacites = null

export function capacitesDecodage() {
  if (capacites) return capacites
  const essai = (codecs) => {
    const type = `video/mp4; codecs="${codecs}"`
    try {
      const MS = window.ManagedMediaSource || window.MediaSource
      if (MS?.isTypeSupported?.(type)) return true
    } catch {
      // Une implémentation qui lève vaut un refus.
    }
    return document.createElement('video').canPlayType(type) === 'probably'
  }
  // « 10 » couvre aussi le 8 bits : un décodeur 10 bits lit les deux.
  const profondeur = (dixBits, huitBits) => (essai(dixBits) ? '10' : essai(huitBits) ? '8' : '0')
  capacites = {
    hevc: essai('hvc1.2.4.L123.B0') ? 'main10' : essai('hvc1.1.6.L123.B0') ? 'main' : '0',
    vp9: profondeur('vp09.02.10.10', 'vp09.00.10.08'),
    av1: profondeur('av01.0.08M.10', 'av01.0.08M.08'),
  }
  return capacites
}
</script>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { motif as motifServeur } from '../lib/langue.js'

/**
 * Le lecteur vidéo de l'application, partagé par la médiathèque et le
 * réencodage.
 *
 * Il est né d'un faux diagnostic : sur l'écran où l'on décide de remplacer un
 * original de vingt gigaoctets, Firefox a déclaré « corrompu » un réencodage
 * parfaitement sain — un H.264 10 bits, qu'aucun navigateur ne décode, servi
 * sans prise en charge des plages d'octets. Un faux « corrompu » fait jeter un
 * bon fichier ; un vrai défaut non vu fait remplacer un bon original par un
 * mauvais. D'où quatre règles :
 *
 * 1. Ne pas deviner : le serveur dit d'abord ce qu'est le fichier et comment
 *    le montrer (lecture directe, réemballage, aperçu converti), et on l'affiche
 *    en une ligne. Un aperçu converti en 720p ne doit jamais passer pour la
 *    qualité du fichier.
 * 2. Ne jamais laisser le message du navigateur tout seul : quand le DÉCODAGE
 *    échoue, le serveur décode lui-même le fichier à cinq endroits, et c'est sa
 *    réponse qui tranche. Une coupure réseau ou un segment libéré n'est pas un
 *    échec de décodage : on reprend, sans rien soupçonner.
 * 3. La position affichée est celle du fichier : le serveur lit dans le
 *    premier segment à quel instant du film correspond le début de l'aperçu,
 *    et dit quand l'endroit atteint n'est pas l'endroit demandé.
 * 4. Ne rien laisser tourner : fermer le lecteur arrête la conversion sur le
 *    NAS, tout de suite — même quand on le ferme pendant qu'elle démarre.
 */

const props = defineProps({
  /** « plan » (médiathèque) ou « transcode » (réencodage à vérifier). */
  genre: { type: String, required: true, validator: (v) => ['plan', 'transcode'].includes(v) },
  /** Identifiant du plan ou du travail : jamais un chemin. */
  ident: { type: String, required: true },
  /**
   * Lancer le contrôle de décodage dès l'ouverture, sans attendre un échec.
   * L'écran de réencodage en a besoin pour son verdict : c'est là qu'on jette
   * un original.
   */
  verifierDecodage: { type: Boolean, default: false },
})

const emit = defineEmits(['controle'])

/** De quoi vérifier un film en trois endroits sans faire fondre le NAS. */
const SAUTS = [0, 0.1, 0.25, 0.5, 0.75, 0.9]
const ETATS_FINIS = ['sain', 'abime', 'indetermine']
/** Reprises automatiques tolérées par minute après une coupure, avant de s'arrêter. */
const REPRISES_MAX = 2

const base = computed(() => `/api/media/${props.genre}/${encodeURIComponent(props.ident)}`)
const decodage = capacitesDecodage()

const video = ref(null)
const plan = ref(null)
/**
 * Ce qui est en lecture : { mode, resume, motif, avertissements, url, session,
 * at, debut, premiere_image, fin, alerte }.
 */
const courant = ref(null)
/** analyse · preparation · lecture · verification · echec · veille · impossible */
const etape = ref('analyse')
/** Ce qui a cassé, côté navigateur ou côté session. Jamais présenté seul. */
const panne = ref('')
const controle = ref(null)
const position = ref(0)
const reprise = ref(0)
const relanceEnCours = ref(false)
/** Déjà repassé en aperçu converti après un échec : on ne boucle pas. */
const converti = ref(false)
const relanceConvertie = ref(false)
/** Fin de lecture directe avant la durée annoncée : il n'y a pas de session pour le dire. */
const alerteLocale = ref('')

let hls = null
let signeDeVie = null
let suiviControle = null
let fini = false
let recuperationTentee = false
let positionEchec = 0
let reprises = []
/**
 * Change à chaque démarrage, réinitialisation ou fermeture. Une réponse qui
 * arrive après ne vaut plus rien : sa session n'a plus de spectateur.
 */
let jeton = 0
/** Change quand le fichier montré change ou que le lecteur se ferme. */
let generation = 0

async function lireJson(res) {
  try {
    return await res.json()
  } catch {
    return {}
  }
}

function temps(secondes) {
  const total = Math.max(0, Math.round(secondes || 0))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = String(total % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

/** Arrête une session précise, sans attendre la réponse. */
function arreterSid(sid) {
  if (!sid) return
  fetch(`/api/media/lecture/${sid}/stop`, { method: 'POST', keepalive: true }).catch(() => {})
}

// --- Plan de lecture et sessions ---------------------------------------------

async function chargerPlan() {
  const moi = generation
  etape.value = 'analyse'
  panne.value = ''
  const res = await fetch(`${base.value}/lecture?${new URLSearchParams(decodage)}`).catch(
    () => null,
  )
  if (fini || moi !== generation) return
  if (!res) {
    etape.value = 'impossible'
    panne.value = 'Sortilège ne répond pas : le fichier n’est pas en cause.'
    return
  }
  const corps = await lireJson(res)
  if (fini || moi !== generation) return
  if (!res.ok) {
    etape.value = 'impossible'
    panne.value = motifServeur(corps, res.status)
    return
  }
  plan.value = corps
  if (corps.mode === 'impossible') {
    etape.value = 'impossible'
    panne.value = corps.motif
    // ffprobe n'a rien pu lire : seul un décodage dira si le fichier est abîmé.
    if (corps.controler) lancerControle()
    return
  }
  await demarrer(0, false)
}

/**
 * Ouvre une session à `at` secondes. Le serveur remplace de lui-même la
 * session précédente du même fichier : un saut ne laisse pas deux ffmpeg.
 *
 * Si le lecteur est fermé (ou relancé ailleurs) pendant que la session
 * s'ouvre, la session reçue est arrêtée aussitôt : sans cela, ffmpeg tournait
 * encore une minute pour personne.
 */
async function demarrer(at, conversion) {
  const moi = ++jeton
  relanceEnCours.value = true
  panne.value = ''
  alerteLocale.value = ''
  detacher()
  try {
    const params = new URLSearchParams({ at: String(Math.max(0, at || 0)), ...decodage })
    if (conversion) params.set('conversion', 'true')
    const res = await fetch(`${base.value}/session?${params}`, { method: 'POST' }).catch(() => null)
    const corps = res ? await lireJson(res) : {}
    if (fini || moi !== jeton) {
      arreterSid(corps.session)
      return
    }
    if (!res) {
      echouerSansControle('Sortilège ne répond pas : le fichier n’est pas en cause.')
      return
    }
    if (!res.ok) {
      echouerSansControle(motifServeur(corps, res.status))
      return
    }
    courant.value = { ...corps, at: corps.at ?? at }
    position.value = courant.value.at

    if (!corps.session) {
      etape.value = 'lecture'
      await attacher(corps.url, false, courant.value.at)
      return
    }

    etape.value = 'preparation'
    const pret = await attendrePret(corps.session, moi)
    if (fini || moi !== jeton) {
      arreterSid(corps.session)
      return
    }
    if (pret.fin) {
      sessionFinie(pret.fin, courant.value.at)
      return
    }
    if (!pret.ok) {
      // Personne ne lira cette session : ne pas la laisser tourner une minute.
      arreterSid(corps.session)
      etape.value = 'lecture'
      echouer(`La préparation de l'aperçu a échoué : ${pret.erreur}`)
      return
    }
    majSession(pret.etat)
    position.value = courant.value.debut ?? courant.value.at
    etape.value = 'lecture'
    await attacher(corps.url, true, 0)
    demarrerSignesDeVie()
  } finally {
    if (moi === jeton) relanceEnCours.value = false
  }
}

/**
 * Attend le premier segment. Un 4K converti met plusieurs secondes à sortir.
 * Chaque interrogation est un signe de vie pour le serveur : une préparation
 * longue n'est pas une session abandonnée.
 */
async function attendrePret(sid, moi) {
  const limite = Date.now() + 90_000
  while (!fini && moi === jeton && Date.now() < limite) {
    const res = await fetch(`/api/media/lecture/${sid}`).catch(() => null)
    if (res && (res.status === 404 || res.status === 410)) return { fin: await lireJson(res) }
    if (res?.ok) {
      const etat = await lireJson(res)
      if (etat.pret) return { ok: true, etat }
      if (!etat.vivant && etat.erreur) return { ok: false, erreur: etat.erreur }
      if (!etat.vivant) return { ok: false, erreur: 'ffmpeg s’est arrêté sans rien produire.' }
    }
    await new Promise((r) => setTimeout(r, 700))
  }
  return { ok: false, erreur: 'rien de prêt au bout de 90 secondes.' }
}

/** Ce que le serveur sait de la session : origine réelle, fin réelle, alerte. */
function majSession(etat) {
  if (!courant.value || !etat || etat.session !== courant.value.session) return
  const { debut, premiere_image, fin, termine, alerte } = etat
  courant.value = { ...courant.value, debut, premiere_image, fin, termine, alerte }
}

async function attacher(url, estHls, debut) {
  const v = video.value
  if (!v) return
  recuperationTentee = false
  if (!estHls) {
    v.src = url
    if (debut > 0) {
      v.addEventListener('loadedmetadata', () => { v.currentTime = debut }, { once: true })
    }
    return
  }
  // Safari lit le HLS lui-même : c'est le lecteur le plus sûr sur Apple.
  if (v.canPlayType('application/vnd.apple.mpegurl')) {
    v.src = url
    return
  }
  let Hls
  try {
    // Embarqué dans le build (aucun CDN), et chargé seulement quand il sert.
    const module = await import('hls.js/light')
    Hls = module.default
  } catch {
    echouerSansControle('Le module de lecture HLS n’a pas pu être chargé : recharge la page.')
    return
  }
  if (fini) return
  if (!Hls.isSupported()) {
    echouerSansControle('Ce navigateur ne sait lire ni le HLS ni les Media Source Extensions.')
    return
  }
  // Le serveur peut retenir la liste le temps du premier segment : les délais
  // par défaut de hls.js (10 s) couperaient avant qu'un 4K converti ne sorte.
  const politique = {
    default: {
      maxTimeToFirstByteMs: 30_000,
      maxLoadTimeMs: 60_000,
      timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
      errorRetry: { maxNumRetry: 3, retryDelayMs: 1000, maxRetryDelayMs: 4000 },
    },
  }
  hls = new Hls({
    // Une liste EVENT se lirait sinon « en direct », depuis la fin.
    startPosition: 0,
    enableWorker: false,
    maxBufferLength: 30,
    backBufferLength: 60,
    manifestLoadPolicy: politique,
    playlistLoadPolicy: politique,
  })
  hls.on(Hls.Events.ERROR, (_evenement, donnees) => {
    if (!donnees.fatal) return
    // Un segment disparu (404, 410), une liste introuvable, le réseau coupé :
    // rien de tout cela ne dit quoi que ce soit du fichier.
    if (donnees.type === Hls.ErrorTypes.NETWORK_ERROR) {
      const code = donnees.response?.code
      surCoupure(`${donnees.details}${code ? `, réponse ${code}` : ''}`)
      return
    }
    // Une erreur de décodage isolée se rattrape une fois ; au-delà, on enquête.
    if (donnees.type === Hls.ErrorTypes.MEDIA_ERROR && !recuperationTentee) {
      recuperationTentee = true
      hls.recoverMediaError()
      return
    }
    surEchec(`erreur de lecture (${donnees.details})`)
  })
  hls.loadSource(url)
  hls.attachMedia(v)
}

function detacher() {
  arreterSignesDeVie()
  if (hls) {
    hls.destroy()
    hls = null
  }
  const v = video.value
  if (v) {
    v.pause()
    v.removeAttribute('src')
    v.load()
  }
}

// --- Signes de vie -------------------------------------------------------------
//
// Le serveur arrête une session sans nouvelles depuis une minute. On n'en
// envoie QUE pendant la lecture : un lecteur en pause oublié dans un onglet ne
// doit pas tenir une conversion 4K ouverte toute la soirée. Il reprendra d'un
// clic, là où il en était.

function demarrerSignesDeVie() {
  arreterSignesDeVie()
  signeDeVie = setInterval(async () => {
    const v = video.value
    const sid = courant.value?.session
    if (!sid || !v || v.paused) return
    const res = await fetch(`/api/media/lecture/${sid}/ping`, { method: 'POST' }).catch(() => null)
    if (courant.value?.session !== sid) return
    if (res && (res.status === 404 || res.status === 410)) {
      sessionFinie(await lireJson(res), position.value)
    } else if (res?.ok) {
      majSession(await lireJson(res))
    }
  }, 20_000)
}

function arreterSignesDeVie() {
  if (signeDeVie) clearInterval(signeDeVie)
  signeDeVie = null
}

/** Reprise explicite : la lecture repart d'un clic sur « lecture », si la session s'est endormie. */
async function surLecture() {
  const sid = courant.value?.session
  if (!sid || etape.value !== 'lecture') return
  const res = await fetch(`/api/media/lecture/${sid}/ping`, { method: 'POST' }).catch(() => null)
  if (res && (res.status === 404 || res.status === 410) && courant.value?.session === sid) {
    demarrer(position.value, converti.value)
  }
}

function mettreEnVeille(texte, pos) {
  detacher()
  courant.value = courant.value ? { ...courant.value, session: null } : null
  reprise.value = pos
  panne.value = texte
  etape.value = 'veille'
}

function sessionFinie(corps, pos) {
  mettreEnVeille(corps?.detail ?? 'Aperçu interrompu.', pos)
}

/**
 * La lecture est arrivée au bout. Si le bout est avant la durée annoncée, le
 * dire : c'est exactement ce qu'on vient chercher en sautant vers la fin.
 */
async function surFin() {
  const c = courant.value
  if (!c) return
  if (!c.session) {
    const v = video.value
    if (v && duree.value && duree.value - v.currentTime > 2) {
      alerteLocale.value =
        `La lecture s’arrête à ${temps(v.currentTime)} alors que le fichier ` +
        `annonce ${temps(duree.value)}.`
    }
    return
  }
  const res = await fetch(`/api/media/lecture/${c.session}`).catch(() => null)
  if (res?.ok) majSession(await lireJson(res))
}

// --- Quand la lecture échoue -----------------------------------------------------

const MESSAGES_NAVIGATEUR = {
  3: 'le navigateur n’a pas su décoder la vidéo',
  4: 'le navigateur refuse ce format',
}

function surErreurVideo() {
  // Les erreurs d'un élément qu'on vient de vider ne disent rien du fichier.
  if (relanceEnCours.value || !courant.value || hls) return
  const code = video.value?.error?.code
  // 1 : lecture interrompue par nous-mêmes ; 2 : le réseau. Ni l'un ni
  // l'autre ne parle du fichier.
  if (code === 1) return
  if (code === 2) {
    surCoupure('le réseau a lâché en cours de lecture')
    return
  }
  surEchec(MESSAGES_NAVIGATEUR[code] ?? 'la lecture a échoué')
}

/**
 * Coupure réseau ou segment libéré du cache : on reprend la lecture là où
 * elle en était, sans contrôle de décodage ni conversion forcée — qui
 * répondraient à une question que personne n'a posée, en faisant travailler
 * le NAS. Si la session elle-même est finie, le serveur dit pourquoi. Au-delà
 * de deux reprises par minute, on s'arrête et on le dit.
 */
async function surCoupure(detail) {
  if (fini || etape.value !== 'lecture') return
  const moi = jeton
  const pos = position.value
  const sid = courant.value?.session
  detacher()
  if (sid) {
    const res = await fetch(`/api/media/lecture/${sid}`).catch(() => null)
    if (fini || moi !== jeton) return
    if (!res) {
      mettreEnVeille('Sortilège ne répond plus : le fichier n’est pas en cause.', pos)
      return
    }
    if (res.status === 404 || res.status === 410) {
      sessionFinie(await lireJson(res), pos)
      return
    }
  }
  const maintenant = Date.now()
  reprises = reprises.filter((t) => maintenant - t < 60_000)
  if (reprises.length >= REPRISES_MAX) {
    mettreEnVeille(
      `La lecture a été coupée plusieurs fois (${detail}) : le réseau ou le NAS, pas le fichier.`,
      pos,
    )
    return
  }
  reprises.push(maintenant)
  demarrer(pos, converti.value)
}

/**
 * Un échec de DÉCODAGE n'est pas un verdict. On demande d'abord au serveur si
 * la session a simplement fini (mise en veille, remplacée par une autre
 * conversion), puis on fait décoder le fichier par ffmpeg.
 */
async function surEchec(ceQuiACasse) {
  if (fini || etape.value !== 'lecture') return
  etape.value = 'verification'
  const pos = position.value
  const sid = courant.value?.session
  detacher()
  let detail = ''
  if (sid) {
    const res = await fetch(`/api/media/lecture/${sid}`).catch(() => null)
    if (res && (res.status === 404 || res.status === 410)) {
      sessionFinie(await lireJson(res), pos)
      return
    }
    if (res?.ok) {
      const etat = await lireJson(res)
      if (etat.erreur) detail = `la préparation de l'aperçu a échoué (${etat.erreur})`
    }
  }
  positionEchec = pos
  echouer(`Lecture impossible : ${detail || ceQuiACasse}.`)
}

function echouer(texte) {
  panne.value = texte
  etape.value = 'echec'
  lancerControle()
}

/** Une panne qui ne vient pas du fichier (serveur muet, module absent) : rien à contrôler. */
function echouerSansControle(texte) {
  panne.value = texte
  etape.value = 'impossible'
}

// --- Le contrôle de décodage côté serveur ---------------------------------------

async function lancerControle() {
  if (suiviControle) return
  if (ETATS_FINIS.includes(controle.value?.etat)) {
    reagirAuControle()
    return
  }
  const moi = generation
  const res = await fetch(`${base.value}/controle`, { method: 'POST' }).catch(() => null)
  if (fini || moi !== generation) return
  if (!res) {
    majControle({ etat: 'indisponible', message: 'Sortilège ne répond pas : contrôle impossible.' })
    return
  }
  const corps = await lireJson(res)
  if (res.status === 409) {
    // Un seul contrôle à la fois sur le NAS : on attend notre tour, et on le dit.
    majControle({ etat: 'attente', message: corps.detail })
    suiviControle = setTimeout(() => {
      suiviControle = null
      lancerControle()
    }, 4000)
    return
  }
  if (!res.ok) {
    majControle({ etat: 'indisponible', message: motifServeur(corps, res.status) })
    return
  }
  majControle(corps)
  if (corps.etat === 'en_cours') suivreControle()
}

function suivreControle() {
  const moi = generation
  suiviControle = setTimeout(async () => {
    suiviControle = null
    const res = await fetch(`${base.value}/controle`).catch(() => null)
    if (fini || moi !== generation) return
    const corps = res?.ok ? await lireJson(res) : null
    if (!corps) {
      suivreControle()
      return
    }
    majControle(corps)
    if (corps.etat === 'en_cours') suivreControle()
    else if (corps.etat === 'aucun') lancerControle()
  }, 2000)
}

function majControle(corps) {
  controle.value = corps
  emit('controle', corps)
  if (ETATS_FINIS.includes(corps.etat)) reagirAuControle()
}

/**
 * Aucune erreur aux points testés mais le navigateur a échoué à DÉCODER : on
 * lui donne ce qu'il sait lire à coup sûr, une fois. Abîmé : on n'insiste
 * pas, c'est la réponse. (Une coupure réseau n'arrive jamais ici.)
 */
function reagirAuControle() {
  if (etape.value !== 'echec') return
  if (
    controle.value?.etat === 'sain' &&
    courant.value?.mode !== 'transcode' &&
    !converti.value
  ) {
    converti.value = true
    relanceConvertie.value = true
    demarrer(positionEchec, true)
  }
}

// --- Sauts ------------------------------------------------------------------------

const duree = computed(() => plan.value?.duree ?? null)

const raisonSauts = computed(() => {
  if (etape.value === 'analyse') return 'Analyse du fichier en cours…'
  if (relanceEnCours.value || etape.value === 'preparation') return 'Relance de l’aperçu en cours…'
  if (etape.value === 'verification') return 'Vérification en cours…'
  if (plan.value?.mode === 'impossible' || !plan.value) return 'Aperçu indisponible pour ce fichier.'
  if (!duree.value) return 'Durée inconnue : impossible de viser un endroit du fichier.'
  return ''
})

/** Lecture directe : le navigateur saute seul (plages d'octets). Sinon, nouvelle session. */
function sauter(fraction) {
  if (raisonSauts.value) return
  const cible = duree.value * fraction
  if (etape.value === 'lecture' && courant.value && !courant.value.session && video.value) {
    alerteLocale.value = ''
    video.value.currentTime = cible
    return
  }
  demarrer(cible, converti.value)
}

function suivrePosition() {
  const v = video.value
  if (!v) return
  const c = courant.value
  // En HLS, la vidéo commence à 0 à chaque session. Ce 0 n'est PAS l'endroit
  // demandé : c'est l'instant du fichier que le serveur a lu dans le premier
  // segment. Sur un fichier tronqué, « 90 % » affichait 1:48 sur des images
  // de 1:10 ; l'instant réel, lui, ne ment pas.
  position.value = c?.session ? (c.debut ?? c.at) + v.currentTime : v.currentTime
}

// --- Ce qui s'affiche ----------------------------------------------------------------

function majuscule(texte) {
  return texte ? texte[0].toUpperCase() + texte.slice(1) : ''
}

const ligne = computed(() => {
  switch (etape.value) {
    case 'analyse':
      return 'Analyse du fichier…'
    case 'preparation':
      return courant.value?.mode === 'transcode'
        ? 'Préparation de l’aperçu converti sur le NAS…'
        : 'Préparation du réemballage…'
    case 'verification':
      return 'La lecture a échoué — vérification…'
    case 'impossible':
      return 'Lecture impossible'
    default:
      return majuscule(courant.value?.resume ?? plan.value?.resume ?? '')
  }
})

const classeLigne = computed(() => {
  const mode = courant.value?.mode ?? plan.value?.mode
  if (etape.value === 'impossible') return 'ko'
  return mode === 'transcode' ? 'converti' : mode === 'direct' ? 'direct' : ''
})

const motifAffiche = computed(() => courant.value?.motif ?? plan.value?.motif ?? '')

const avertissements = computed(
  () => courant.value?.avertissements ?? plan.value?.avertissements ?? [],
)

/** L'endroit atteint n'est pas l'endroit demandé, ou le fichier finit avant ce qu'il annonce. */
const alerte = computed(() => {
  if (!['lecture', 'echec', 'veille'].includes(etape.value)) return ''
  return courant.value?.alerte || alerteLocale.value
})

const positionLisible = computed(() => {
  if (!duree.value || !['lecture', 'echec', 'veille'].includes(etape.value)) return ''
  return `${temps(position.value)} sur ${temps(duree.value)}`
})

/** La conclusion, quand la lecture a échoué : c'est elle qui compte, pas le navigateur. */
const conclusion = computed(() => {
  const c = controle.value
  if (!c) return null
  switch (c.etat) {
    case 'sain':
      // « Aucune erreur aux points testés » n'est pas « intact » : la phrase
      // dit ce qui a été vérifié, et en tire la conclusion la plus probable.
      return {
        classe: 'navigateur',
        texte:
          `${c.message.replace(/\.$/, '')} : l’échec vient très probablement du navigateur, ` +
          'pas du fichier.' +
          (relanceConvertie.value && etape.value !== 'echec'
            ? ' L’aperçu repart en version convertie.'
            : ''),
      }
    case 'abime':
      return { classe: 'abime', texte: c.message, detail: c.premiere_erreur }
    case 'indetermine':
      return {
        classe: 'incertain',
        texte:
          `Le contrôle n’a pas pu conclure (${c.message.replace(/\.$/, '')}) : impossible de ` +
          'dire si le fichier ou le navigateur est en cause.',
      }
    case 'en_cours':
      return { classe: 'attente', texte: 'Décodage du fichier par le serveur à 5 endroits…' }
    case 'attente':
    case 'indisponible':
      return { classe: 'attente', texte: c.message }
    default:
      return null
  }
})

/** Le diagnostic s'affiche seulement si la lecture a échoué — ou si l'on n'a pas pu lire. */
const diagnosticVisible = computed(
  () => relanceConvertie.value || ['echec', 'verification', 'impossible'].includes(etape.value),
)

// --- Cycle de vie ---------------------------------------------------------------------

function arreterSession() {
  arreterSid(courant.value?.session)
}

/** L'onglet se ferme : `fetch` n'aurait pas le temps, une balise si. */
function surDepart() {
  const sid = courant.value?.session
  if (sid && navigator.sendBeacon) navigator.sendBeacon(`/api/media/lecture/${sid}/stop`)
}

function reinitialiser() {
  jeton++
  generation++
  arreterSession()
  detacher()
  if (suiviControle) clearTimeout(suiviControle)
  suiviControle = null
  plan.value = null
  courant.value = null
  controle.value = null
  converti.value = false
  relanceConvertie.value = false
  alerteLocale.value = ''
  relanceEnCours.value = false
  position.value = 0
  reprises = []
}

watch(
  () => [props.genre, props.ident],
  () => {
    reinitialiser()
    chargerPlan()
    if (props.verifierDecodage) lancerControle()
  },
)

onMounted(() => {
  window.addEventListener('pagehide', surDepart)
  chargerPlan()
  if (props.verifierDecodage) lancerControle()
})

// AVANT le démontage, et non après : une fois démonté, la référence à
// l'élément <video> est déjà vide, et un lecteur HLS natif (Safari) restait
// branché sur sa liste, à télécharger des segments pour personne.
onBeforeUnmount(() => {
  fini = true
  jeton++
  generation++
  window.removeEventListener('pagehide', surDepart)
  if (suiviControle) clearTimeout(suiviControle)
  arreterSession()
  detacher()
})
</script>

<template>
  <div class="video-player">
    <video
      ref="video"
      controls
      playsinline
      preload="metadata"
      @error="surErreurVideo"
      @timeupdate="suivrePosition"
      @play="surLecture"
      @ended="surFin"
    ></video>

    <p class="ligne" role="status" aria-live="polite">
      <span class="mode" :class="classeLigne">{{ ligne }}</span>
      <span v-if="positionLisible" class="position">{{ positionLisible }}</span>
    </p>

    <p v-if="alerte" class="alerte" role="status" aria-live="polite">{{ alerte }}</p>

    <p v-if="motifAffiche && etape !== 'analyse'" class="motif">{{ motifAffiche }}</p>
    <ul v-if="avertissements.length" class="avertissements">
      <li v-for="(a, i) in avertissements" :key="i">{{ a }}</li>
    </ul>

    <div class="sauts" role="group" aria-label="Aller à un endroit du fichier">
      <span class="etiquette">Aller à</span>
      <button
        v-for="f in SAUTS"
        :key="f"
        type="button"
        class="saut"
        :disabled="!!raisonSauts"
        :aria-label="`Aller à ${Math.round(f * 100)} % du fichier`"
        @click="sauter(f)"
      >
        {{ Math.round(f * 100) }} %
      </button>
      <span v-if="raisonSauts" class="raison" role="status">{{ raisonSauts }}</span>
    </div>

    <div v-if="etape === 'veille'" class="veille" role="status">
      <span>{{ panne }}</span>
      <button type="button" class="small" @click="demarrer(reprise, converti)">
        Reprendre à {{ temps(reprise) }}
      </button>
    </div>

    <div
      v-if="diagnosticVisible && (panne || conclusion)"
      class="diagnostic"
      :class="conclusion?.classe"
      role="status"
      aria-live="polite"
    >
      <p v-if="panne && etape !== 'veille'" class="panne">{{ panne }}</p>
      <p v-if="conclusion" class="conclusion">{{ conclusion.texte }}</p>
      <code v-if="conclusion?.detail" class="erreur">{{ conclusion.detail }}</code>
    </div>
  </div>
</template>

<style scoped>
.video-player { display: flex; flex-direction: column; gap: 7px; min-width: 0; }

video {
  display: block;
  width: 100%;
  max-height: 58vh;
  background: #000;
  border-radius: 6px;
}

.ligne {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  align-items: baseline;
  font-size: var(--t-sm);
}
.mode { color: var(--text-dim); font-weight: 500; }
/* L'aperçu converti se voit d'un coup d'œil : c'est lui qu'on risque de
   prendre pour la qualité du fichier. */
.mode.converti { color: var(--warn); }
.mode.direct { color: var(--ok); }
.mode.ko { color: var(--err); }
.position { font-family: var(--mono); font-size: var(--t-xs); color: var(--text-faint); }

/* L'endroit atteint n'est pas l'endroit demandé : c'est souvent LA réponse
   qu'on venait chercher en sautant vers la fin. */
.alerte {
  margin: 0;
  font-size: var(--t-sm);
  font-weight: 600;
  color: var(--warn);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.motif {
  margin: 0;
  font-size: var(--t-xs);
  color: var(--text-faint);
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.avertissements {
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: var(--t-xs);
  color: var(--warn);
  line-height: 1.55;
}

.sauts { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.etiquette { font-size: var(--t-xs); color: var(--text-faint); }
.saut {
  font-size: var(--t-xs);
  font-family: var(--mono);
  padding: 6px 10px;
  min-width: 3.4em;
}
.raison { font-size: var(--t-xs); color: var(--text-dim); }

.veille {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: var(--t-sm);
  color: var(--text-dim);
}
button.small { font-size: var(--t-xs); padding: 7px 11px; }

.diagnostic {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 9px 12px;
  border-radius: 8px;
  font-size: var(--t-sm);
  line-height: 1.55;
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.diagnostic p { margin: 0; }
.diagnostic .panne { color: var(--text-dim); font-size: var(--t-xs); }
.diagnostic.navigateur { border-color: color-mix(in srgb, var(--ok) 40%, transparent); }
.diagnostic.navigateur .conclusion { color: var(--ok); }
.diagnostic.abime {
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border-color: color-mix(in srgb, var(--err) 35%, transparent);
}
.diagnostic.abime .conclusion { color: var(--err); font-weight: 600; }
.diagnostic.incertain { border-color: color-mix(in srgb, var(--warn) 40%, transparent); }
.diagnostic.incertain .conclusion { color: var(--warn); }
.diagnostic.attente .conclusion { color: var(--text-dim); }
.erreur {
  font-family: var(--mono);
  font-size: var(--t-xs);
  color: var(--text-dim);
  overflow-wrap: anywhere;
}
</style>
