<script setup>
import { computed, ref } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

const props = defineProps({
  subtitles: { type: Object, required: true },
})
const emit = defineEmits(['change'])

// Ni la clé ni le jeton ne sont renvoyés par le serveur : ils valent un droit
// d'appel sur un quota nominatif, et cette réponse finit dans le cache du
// navigateur. Les champs restent donc vides même quand une clé est enregistrée
// — `opensubtitles_key_set` et `opensubtitles_token_set` disent ce qui est en
// place.
const cleBrouillon = ref('')
const jetonBrouillon = ref('')
const langueBrouillon = ref('')

const essai = ref(false)
const resultat = ref(null)

const langues = computed(() => props.subtitles.languages ?? [])

/** La clé d'API est OBLIGATOIRE. Le jeton VIP relève un quota, il n'ouvre pas
 *  l'accès à lui seul : le serveur refuse d'activer la recherche, et de la
 *  tester, avec un jeton sans clé. « Clé ou jeton » laissait croire le
 *  contraire. */
const aUneCle = computed(() => Boolean(props.subtitles.opensubtitles_key_set))

/** Ce qui empêche d'activer la recherche, en toutes lettres. Le serveur refuse
 *  `enabled: true` sans langue ou sans clé, et ce refus s'affiche loin d'ici,
 *  au niveau de l'enregistrement : on tranche donc à côté de l'interrupteur.
 *  Rien ne bloque la DÉSACTIVATION — couper doit toujours rester possible. */
const blocageActivation = computed(() => {
  if (props.subtitles.enabled) return null
  const manques = []
  if (!aUneCle.value) {
    manques.push(
      props.subtitles.opensubtitles_token_set
        ? "une clé d'API OpenSubtitles (le jeton VIP enregistré ne suffit pas seul)"
        : "une clé d'API OpenSubtitles",
    )
  }
  if (!langues.value.length) manques.push('au moins une langue')
  if (!manques.length) return null
  return (
    `Impossible d'activer : il manque ${manques.join(' et ')}. Le serveur refuserait ` +
    "l'enregistrement, et la recherche échouerait à chaque rangement."
  )
})

/** La forme courte que cet écran exige : deux lettres, éventuellement suivies
 *  d'une région de deux lettres — « fr », « en », « pt-BR ». Le serveur, lui,
 *  accepte aussi « eng » ou « fre » et les convertit ; mais une ligne « eng »
 *  posée à côté de « en » disparaissait alors à l'enregistrement sans un mot. */
const FORME_CODE = /^[a-z]{2}(-[a-z]{2})?$/i

/** Codes à trois lettres (ISO 639-2) et noms tapés par habitude, avec la forme
 *  courte attendue. Ne sert qu'à PROPOSER la bonne saisie dans le refus : le
 *  verdict reste au serveur. */
const FORMES_COURTES = {
  fre: 'fr', fra: 'fr', french: 'fr', francais: 'fr', 'français': 'fr', eng: 'en',
  english: 'en', ger: 'de', deu: 'de', spa: 'es', ita: 'it', por: 'pt', pob: 'pt-BR',
  dut: 'nl', nld: 'nl', jpn: 'ja', chi: 'zh', zho: 'zh', kor: 'ko', rus: 'ru', ara: 'ar',
  hin: 'hi', pol: 'pl', swe: 'sv', dan: 'da', nor: 'no', fin: 'fi', tur: 'tr', gre: 'el',
  ell: 'el', heb: 'he', cze: 'cs', ces: 'cs', ukr: 'uk', rum: 'ro', ron: 'ro', hun: 'hu',
  vie: 'vi', tha: 'th', ind: 'id', may: 'ms', msa: 'ms', bul: 'bg', hrv: 'hr', srp: 'sr',
  slo: 'sk', slk: 'sk', slv: 'sl', est: 'et', lav: 'lv', lit: 'lt', cat: 'ca', fas: 'fa',
  per: 'fa',
}

// Les régions que le serveur CONSERVE : ailleurs il jette la région, « fr-FR »
// et « fr-CA » désignant le même sous-titre pour un spectateur. Reproduit ici
// pour une seule raison : détecter le doublon AVANT l'envoi. Ajouter « fr-FR »
// à côté de « fr » ferait sinon disparaître la saisie en silence à
// l'enregistrement, ce qui se lit comme un bouton cassé.
const REGIONS_CONSERVEES = ['pt-br', 'zh-cn', 'zh-tw', 'es-mx']

function normaliser(brut) {
  const bas = brut.trim().toLowerCase().replace('_', '-')
  if (!bas) return ''
  if (REGIONS_CONSERVEES.includes(bas)) return bas
  return bas.split('-', 1)[0]
}

const blocageLangue = computed(() => {
  const v = langueBrouillon.value.trim()
  if (!v) return null
  if (!FORME_CODE.test(v)) {
    const court = FORMES_COURTES[v.toLowerCase()]
    if (court) {
      return `« ${v} » : cet écran n'accepte que la forme courte (fr, pt-BR). Écris « ${court} ».`
    }
    if (/^[a-z]{2}_[a-z]{2}$/i.test(v)) {
      const [langue, region] = v.split('_')
      return (
        `« ${v} » n'est pas accepté : la région se sépare par un tiret. ` +
        `Écris « ${langue.toLowerCase()}-${region.toUpperCase()} ».`
      )
    }
    return (
      `« ${v} » n'est pas un code accepté. Attendu deux lettres, éventuellement suivies ` +
      "d'une région de deux lettres : « fr », « en », « pt-BR »."
    )
  }
  const code = normaliser(v)
  const deja = langues.value.find((l) => normaliser(l) === code)
  if (deja) {
    return deja.toLowerCase() === v.toLowerCase()
      ? `« ${v} » est déjà dans la liste.`
      : `« ${v} » désigne la même langue que « ${deja} », déjà dans la liste.`
  }
  return null
})

function patch(champs) {
  emit('change', champs)
}

function enregistrerCle() {
  const valeur = cleBrouillon.value.trim()
  if (!valeur) return
  patch({ opensubtitles_api_key: valeur })
  cleBrouillon.value = ''
  resultat.value = null
}

/** « - » efface explicitement côté serveur : une chaîne vide y signifie « ne
 *  touche pas », sans quoi changer de langue effacerait la clé.
 *
 *  Retirer la clé alors que la recherche tourne ferait refuser
 *  l'enregistrement (`enabled` sans clé — le jeton ne la remplace pas) : on
 *  coupe donc la recherche dans le même geste, et c'est annoncé sous le champ
 *  avant le clic plutôt que découvert après. */
function effacerCle() {
  patch(
    props.subtitles.enabled
      ? { opensubtitles_api_key: '-', enabled: false }
      : { opensubtitles_api_key: '-' },
  )
  resultat.value = null
}

function enregistrerJeton() {
  const valeur = jetonBrouillon.value.trim()
  if (!valeur) return
  patch({ opensubtitles_token: valeur })
  jetonBrouillon.value = ''
  resultat.value = null
}

/** Le jeton est facultatif : le retirer ne coupe jamais la recherche. */
function effacerJeton() {
  patch({ opensubtitles_token: '-' })
  resultat.value = null
}

function ajouterLangue() {
  const v = langueBrouillon.value.trim()
  if (!v || blocageLangue.value) return
  patch({ languages: [...langues.value, v] })
  langueBrouillon.value = ''
  resultat.value = null
}

function retirerLangue(index) {
  const restantes = langues.value.filter((_, i) => i !== index)
  // Même raison qu'en effaçant la dernière clé : une liste vide avec la
  // recherche active est refusée par le serveur.
  patch(
    !restantes.length && props.subtitles.enabled
      ? { languages: restantes, enabled: false }
      : { languages: restantes },
  )
  resultat.value = null
}

/** L'ordre est un ordre de PRÉFÉRENCE : la première langue disponible pour une
 *  vidéo est celle qu'on dépose. Deux boutons plutôt qu'un glisser-déposer —
 *  un glissement ne s'attrape ni au clavier ni au doigt sur une ligne de
 *  vingt pixels de haut. */
function deplacer(index, pas) {
  const cible = index + pas
  if (cible < 0 || cible >= langues.value.length) return
  const copie = [...langues.value]
  ;[copie[index], copie[cible]] = [copie[cible], copie[index]]
  patch({ languages: copie })
}

/** Un appel RÉEL à OpenSubtitles. Une clé peut être bien formée, enregistrée,
 *  et refusée ; un quota peut être épuisé ; le conteneur peut ne pas sortir sur
 *  le réseau. Ces trois-là appellent trois gestes différents, et aucun rangement
 *  raté ne les distinguera : le serveur nomme la cause, on la reprend mot pour
 *  mot au lieu d'afficher « échec ». */
async function tester() {
  essai.value = true
  resultat.value = null
  try {
    const res = await fetch('/api/settings/subtitles/test', { method: 'POST' })
    const corps = await res.json().catch(() => ({}))
    if (!res.ok) {
      resultat.value = { ok: false, texte: corps.detail ?? `Échec (${res.status}).` }
      return
    }
    resultat.value = {
      ok: true,
      exemple: corps.sample ?? '',
      rendues: corps.languages ?? [],
      demandees: corps.wanted ?? [],
    }
  } catch {
    resultat.value = { ok: false, texte: 'Serveur injoignable.' }
  } finally {
    essai.value = false
  }
}

/** Une langue demandée qu'OpenSubtitles n'a pas rendue sur cette recherche : le
 *  cas qu'on confond avec « la clé ne marche pas », alors que la clé marche et
 *  que c'est la langue qui manque pour ce titre. */
const languesAbsentes = computed(() => {
  if (!resultat.value?.ok) return []
  const rendues = resultat.value.rendues.map((l) => l.toLowerCase())
  return resultat.value.demandees.filter((l) => !rendues.includes(l.toLowerCase()))
})

// --- Compléter la bibliothèque déjà rangée ------------------------------------

/** Taille d'un lot. Le serveur plafonne à 500 ; à 200, un lot rend la main en
 *  quelques minutes même quand chaque vidéo coûte un appel au service, et le
 *  quota quotidien ne se vide pas d'un seul clic. */
const LOT = 200

const lotEnCours = ref(false)
/** Compte rendu du dernier lot, tel que le serveur l'a rendu. */
const lot = ref(null)
/** Totaux de la série commencée par « Compléter la bibliothèque ». */
const serie = ref(null)
const panneLot = ref(null)
/** Où reprendre après une panne, ou un refus en cours de série : le lot qui
 *  n'a pas rendu son compte. */
const offsetEchoue = ref(null)

/** Une 502 arrive en HTML : lire le corps sans filet ferait lever, et le
 *  message d'échec n'arriverait jamais à l'écran. */
async function lireJson(res) {
  const brut = await res.text()
  try {
    return JSON.parse(brut)
  } catch {
    return null
  }
}

async function completerBibliotheque(offset = 0) {
  lotEnCours.value = true
  panneLot.value = null
  offsetEchoue.value = null
  try {
    const res = await fetch(`/api/review/subtitles-library?offset=${offset}&limit=${LOT}`, {
      method: 'POST',
    })
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      // 409 (un lot tourne déjà) et 400 (recherche coupée) : le serveur nomme
      // la cause, elle est reprise telle quelle. Le même lot se relance après
      // une panne du serveur, et après tout refus en cours de série : le
      // compte rendu d'avant n'est plus affiché, et sans ce point de reprise
      // il ne restait qu'à repartir du début.
      panneLot.value = corps?.detail ?? `Le serveur a refusé ce lot (réponse ${res.status}).`
      if (offset > 0 || res.status >= 500 || !corps) offsetEchoue.value = offset
      return
    }
    lot.value = corps
    // La série ne repart à zéro qu'une fois son premier lot rendu : remise à
    // zéro avant l'appel, un refus effaçait les totaux des lots déjà faits.
    if (offset === 0) {
      serie.value = { lots: 1, examined: corps.examined ?? 0, written: corps.written ?? 0 }
    } else if (serie.value) {
      serie.value = {
        lots: serie.value.lots + 1,
        examined: serie.value.examined + (corps.examined ?? 0),
        written: serie.value.written + (corps.written ?? 0),
      }
    }
  } catch {
    panneLot.value =
      "Serveur injoignable : le compte rendu de ce lot manque. Ce qui a été déposé avant la " +
      "coupure reste en place ; « Relancer ce lot » reprend au même endroit."
    offsetEchoue.value = offset
  } finally {
    lotEnCours.value = false
  }
}

const raisonLot = computed(() =>
  props.subtitles.enabled
    ? ''
    : "Active d'abord la recherche ci-dessus : le serveur refuse de parcourir la bibliothèque " +
      "tant qu'elle est coupée.",
)

const detailLot = computed(
  () =>
    `Parcourt les vidéos déjà rangées par lots de ${LOT} et cherche, pour chacune, le ` +
    'sous-titre qui manque dans la première langue disponible. Chaque recherche compte dans le ' +
    "quota quotidien d'OpenSubtitles. " +
    (props.subtitles.overwrite
      ? 'Les sous-titres déjà présents seront REMPLACÉS : la case est cochée plus haut.'
      : "Un sous-titre déjà présent n'est pas remplacé."),
)

const pluriel = (n, mot) => `${n} ${mot}${n > 1 ? 's' : ''}`
const accord = (n, nom, participe) => `${pluriel(n, nom)} ${participe}${n > 1 ? 's' : ''}`
</script>

<template>
  <section>
    <h3>Sous-titres manquants</h3>
    <p class="note">
      Après un rangement, Sortilège peut aller chercher chez OpenSubtitles les sous-titres
      que la vidéo n'a pas, et les déposer à côté d'elle.
      <strong>Désactivé par défaut :</strong> cela ajoute des appels vers un service tiers
      et écrit des fichiers dans la bibliothèque — les deux se demandent.
    </p>

    <label class="switch" :class="{ bloque: Boolean(blocageActivation) }">
      <input
        type="checkbox"
        :checked="subtitles.enabled"
        :disabled="Boolean(blocageActivation)"
        @change="patch({ enabled: $event.target.checked })"
      />
      Rechercher les sous-titres manquants
    </label>
    <!-- Une case qu'on ne peut pas cocher sans un mot passe pour cassée. -->
    <p v-if="blocageActivation" class="hint bloquant">{{ blocageActivation }}</p>

    <div class="field">
      <label for="os-cle">Clé d'API OpenSubtitles <span class="option">— obligatoire</span></label>
      <div class="row">
        <input
          id="os-cle"
          v-model="cleBrouillon"
          type="password"
          autocomplete="off"
          spellcheck="false"
          :placeholder="
            subtitles.opensubtitles_key_set
              ? '•••••• enregistrée'
              : `Colle ta clé d'API OpenSubtitles`
          "
          @keyup.enter="enregistrerCle"
        />
        <button type="button" :disabled="!cleBrouillon.trim()" @click="enregistrerCle">
          Enregistrer
        </button>
        <button
          v-if="subtitles.opensubtitles_key_set"
          type="button"
          class="clear"
          @click="effacerCle"
        >
          Retirer
        </button>
      </div>
      <p v-if="!cleBrouillon.trim()" class="hint">
        « Enregistrer » attend une clé : un champ vide ne change rien<template
          v-if="subtitles.opensubtitles_key_set"
        >, il n'efface pas la clé en place — c'est « Retirer » qui le fait</template>.
      </p>
      <p class="hint">
        La clé se crée sur
        <a href="https://www.opensubtitles.com/consumers" target="_blank" rel="noopener noreferrer">
          opensubtitles.com/consumers
        </a>
        — un compte gratuit, puis « New consumer ». Elle est stockée comme un secret :
        saisie une fois, jamais réaffichée.
      </p>
      <p v-if="subtitles.enabled && subtitles.opensubtitles_key_set" class="hint attention">
        « Retirer » efface la clé <strong>et coupe la recherche</strong> : la clé est
        obligatoire, et le jeton VIP ne la remplace pas.
      </p>
      <p
        v-if="!subtitles.opensubtitles_key_set && (subtitles.opensubtitles_token_set || subtitles.enabled)"
        class="hint attention"
      >
        Aucune clé d'API enregistrée<template v-if="subtitles.opensubtitles_token_set"
          >, seulement un jeton VIP</template
        >. <strong>La clé est obligatoire</strong> : le jeton seul ne suffit pas. Sans elle, la
        recherche ne peut être ni activée ni testée<template v-if="subtitles.enabled"
          >, et chaque recherche lancée au rangement sera refusée</template
        >.
      </p>
    </div>

    <div class="field">
      <label for="os-jeton">Jeton VIP <span class="option">— facultatif</span></label>
      <div class="row">
        <input
          id="os-jeton"
          v-model="jetonBrouillon"
          type="password"
          autocomplete="off"
          spellcheck="false"
          :placeholder="
            subtitles.opensubtitles_token_set ? '•••••• enregistré' : `Jeton d'un compte VIP`
          "
          @keyup.enter="enregistrerJeton"
        />
        <button type="button" :disabled="!jetonBrouillon.trim()" @click="enregistrerJeton">
          Enregistrer
        </button>
        <button
          v-if="subtitles.opensubtitles_token_set"
          type="button"
          class="clear"
          @click="effacerJeton"
        >
          Retirer
        </button>
      </div>
      <p class="hint">
        Il ne sert qu'à <strong>relever le quota quotidien</strong> d'un compte VIP, et
        <strong>ne remplace pas la clé</strong> : un jeton seul ne permet ni d'activer la
        recherche ni de la tester. Sans jeton, la recherche fonctionne avec le quota gratuit —
        ce qui suffit largement à une bibliothèque qui ne bouge plus.
      </p>
      <p v-if="!jetonBrouillon.trim()" class="hint">
        « Enregistrer » attend un jeton : un champ vide ne change rien<template
          v-if="subtitles.opensubtitles_token_set"
        >, il n'efface pas le jeton en place — c'est « Retirer » qui le fait</template>.
      </p>
    </div>

    <div class="field">
      <span id="os-langues-titre" class="titre-liste">Langues, par ordre de préférence</span>
      <p class="hint souligne">
        <strong>L'ordre compte.</strong> Ce n'est pas un ensemble de langues acceptées :
        pour chaque vidéo, la <strong>première langue disponible</strong> de cette liste
        est celle qu'on dépose, et la recherche s'arrête là. Mettre l'anglais avant le
        français donne un fichier anglais chaque fois que les deux existent.
      </p>

      <ol v-if="langues.length" class="langues" aria-labelledby="os-langues-titre">
        <li v-for="(code, i) in langues" :key="`${code}-${i}`">
          <span class="rang">{{ i + 1 }}</span>
          <code>{{ code }}</code>
          <span v-if="i === 0" class="premiere">déposée en priorité</span>
          <span class="fleches">
            <button
              type="button"
              class="fleche"
              :disabled="i === 0"
              :aria-label="`Monter ${code} d'un rang`"
              @click="deplacer(i, -1)"
            >
              ▲
            </button>
            <button
              type="button"
              class="fleche"
              :disabled="i === langues.length - 1"
              :aria-label="`Descendre ${code} d'un rang`"
              @click="deplacer(i, 1)"
            >
              ▼
            </button>
          </span>
          <button
            type="button"
            class="retirer"
            :aria-label="`Retirer ${code} de la liste`"
            @click="retirerLangue(i)"
          >
            ×
          </button>
        </li>
      </ol>
      <!-- Une liste vide n'est pas un vide : elle a un effet, qu'on énonce. -->
      <p v-else class="hint attention">
        Aucune langue : il n'y a rien à chercher, et la recherche ne peut pas être
        activée tant que cette liste est vide.
      </p>

      <p v-if="langues.length" class="hint">
        ▲ est éteint sur la première ligne et ▼ sur la dernière : il n'y a rien au-dessus
        ni en dessous. Le numéro à gauche donne le rang réel.
      </p>
      <p v-if="langues.length === 1 && subtitles.enabled" class="hint attention">
        Retirer cette dernière langue <strong>coupe aussi la recherche</strong> : sans
        langue demandée, il n'y a rien à chercher.
      </p>

      <div class="row ajout">
        <label class="visuellement-a-part" for="os-langue-ajout">Code de langue à ajouter</label>
        <input
          id="os-langue-ajout"
          v-model="langueBrouillon"
          type="text"
          spellcheck="false"
          autocomplete="off"
          placeholder="fr"
          @keyup.enter="ajouterLangue"
        />
        <button
          type="button"
          :disabled="!langueBrouillon.trim() || Boolean(blocageLangue)"
          @click="ajouterLangue"
        >
          Ajouter
        </button>
      </div>
      <p v-if="blocageLangue" class="hint refus">{{ blocageLangue }}</p>
      <p v-else-if="!langueBrouillon.trim()" class="hint">
        « Ajouter » attend deux lettres, éventuellement suivies d'une région de deux
        lettres : <code>fr</code>, <code>en</code>, <code>pt-BR</code>. Cet écran n'accepte
        que la forme courte (fr, pt-BR) : « eng » et « fre » s'y écrivent « en » et « fr ».
        Le serveur met la casse à plat et supprime les doublons —
        « FR » et « fr-FR » désignent la même langue que « fr ».
      </p>
    </div>

    <div class="field">
      <label class="switch">
        <input
          type="checkbox"
          :checked="subtitles.overwrite"
          @change="patch({ overwrite: $event.target.checked })"
        />
        Remplacer un sous-titre déjà présent
      </label>
      <p class="hint">
        Décoché à dessein : un sous-titre déjà là a souvent été
        <strong>corrigé ou resynchronisé à la main</strong>, et un téléchargement
        l'écraserait sans laisser de trace. Coché, chaque passage réécrit le fichier.
      </p>

      <label class="switch">
        <input
          type="checkbox"
          :checked="subtitles.audio_is_enough"
          @change="patch({ audio_is_enough: $event.target.checked })"
        />
        La piste audio suffit
      </label>
      <p class="hint">
        Ne rien chercher quand la vidéo porte déjà la langue <strong>en audio</strong>.
        Pour qui ne lit les sous-titres qu'à défaut de piste compréhensible — un film
        déjà en français ne déclenchera alors aucune recherche de sous-titres français.
      </p>
    </div>

    <div class="essai">
      <button type="button" :disabled="essai || !aUneCle" @click="tester">
        {{ essai ? 'Interrogation…' : 'Tester la connexion' }}
      </button>
      <!-- Trois états : rien encore demandé, en panne, données. -->
      <span v-if="!aUneCle" class="hint inline">
        Enregistre une clé avant de tester : sans elle, OpenSubtitles refuse toutes les
        requêtes et il n'y a rien à mesurer.
      </span>
      <span v-else-if="essai" class="hint inline">Interrogation d'OpenSubtitles…</span>
      <span v-else-if="resultat && !resultat.ok" class="resultat err">
        {{ resultat.texte }}
      </span>
      <span v-else-if="resultat" class="resultat ok">
        OpenSubtitles a répondu. Exemple trouvé : « {{ resultat.exemple }} ».
      </span>
      <span v-else class="hint inline">
        L'essai interroge réellement le service et rapporte son refus mot pour mot.
      </span>
    </div>

    <template v-if="resultat?.ok">
      <p class="hint">
        Langues rendues sur cet essai :
        <code v-for="l in resultat.rendues" :key="l">{{ l }}</code>
        <span v-if="!resultat.rendues.length">aucune.</span>
      </p>
      <p v-if="languesAbsentes.length" class="hint attention">
        Demandée<span v-if="languesAbsentes.length > 1">s</span> mais absente<span
          v-if="languesAbsentes.length > 1"
          >s</span
        >
        de cette réponse :
        <code v-for="l in languesAbsentes" :key="l">{{ l }}</code>
        — <strong>la clé fonctionne</strong>, c'est cette langue qui n'existe pas pour
        le titre d'essai. C'est le cas qu'on confond avec une clé refusée.
      </p>
    </template>

    <h3 class="sous-titre">Compléter la bibliothèque déjà rangée</h3>
    <p class="note">
      La recherche ci-dessus n'agit qu'<strong>au rangement</strong>. Ce passage reprend les
      vidéos déjà rangées et va chercher les sous-titres qui leur manquent, par lots de
      {{ LOT }} : une grande bibliothèque ne se parcourt pas d'un seul appel, et le quota
      quotidien d'OpenSubtitles non plus.
    </p>

    <ConfirmAction
      label="Compléter la bibliothèque"
      confirm-label="Confirmer — chercher sur toute la bibliothèque"
      :detail="detailLot"
      :busy="lotEnCours"
      :disabled="Boolean(raisonLot)"
      :disabled-reason="raisonLot"
      @confirm="completerBibliotheque(0)"
    />

    <!-- Trois états, et l'échec passe en premier : un lot raté ne doit pas
         laisser affiché le compte rendu du lot d'avant. -->
    <div v-if="panneLot" class="panne-inline" role="alert">
      <p>{{ panneLot }}</p>
      <template v-if="offsetEchoue !== null">
        <button
          type="button"
          :disabled="lotEnCours || Boolean(raisonLot)"
          @click="completerBibliotheque(offsetEchoue)"
        >
          Relancer ce lot
        </button>
        <p v-if="raisonLot" class="raison">{{ raisonLot }}</p>
      </template>
    </div>
    <p v-else-if="lotEnCours" class="indispo" role="status">
      Lot en cours : chaque vidéo à qui manque une langue demandée est cherchée chez
      OpenSubtitles. Le compte rendu s'affiche dès que le serveur le rend.
    </p>
    <template v-else-if="lot">
      <p v-if="lot.refused" class="hint attention" role="status">{{ lot.refused }}</p>
      <p v-if="lot.unavailable" class="hint attention" role="status">{{ lot.unavailable }}</p>
      <p v-if="lot.examined !== undefined" class="summary" role="status">
        <strong>{{ accord(lot.written ?? 0, 'sous-titre', 'déposé') }}</strong>
        sur {{ accord(lot.examined, 'vidéo', 'examinée') }} dans ce lot.
      </p>
      <p v-if="serie && serie.lots > 1" class="hint">
        Depuis le début de la série ({{ pluriel(serie.lots, 'lot') }}) :
        {{ accord(serie.written, 'sous-titre', 'déposé') }} sur
        {{ accord(serie.examined, 'vidéo', 'examinée') }}.
      </p>
      <p v-if="!lot.total" class="hint">
        Aucune vidéo rangée dans la bibliothèque : il n'y avait rien à compléter.
      </p>
      <p v-else-if="lot.next_offset === null" class="hint">
        Bibliothèque parcourue jusqu'au bout ({{ pluriel(lot.total, 'vidéo') }}) : il ne reste
        rien à examiner.
      </p>
      <div v-else class="suite">
        <button
          type="button"
          :disabled="lotEnCours || Boolean(raisonLot)"
          @click="completerBibliotheque(lot.next_offset)"
        >
          <template v-if="lot.refused || lot.unavailable">Réessayer à partir d'ici</template>
          <template v-else>
            Continuer ({{ pluriel(lot.remaining, 'vidéo') }} restante{{ lot.remaining > 1 ? 's' : '' }})
          </template>
        </button>
        <span v-if="raisonLot" class="raison">{{ raisonLot }}</span>
        <span v-else-if="lot.refused || lot.unavailable" class="hint inline">
          Lot arrêté après {{ lot.next_offset }} vidéos sur {{ lot.total }} : réessayer
          n'aboutira qu'une fois la cause ci-dessus levée.
        </span>
        <span v-else class="hint inline">
          {{ lot.next_offset }} vidéos parcourues sur {{ lot.total }}.
        </span>
      </div>
    </template>
    <p v-else class="hint">
      Aucun passage depuis l'ouverture de cet écran. Le compte rendu s'affichera ici : combien de
      vidéos examinées, combien de sous-titres déposés, et ce qui reste à parcourir.
    </p>
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
.sous-titre { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.note { margin: 0 0 14px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.note strong { color: var(--text-dim); }

.switch { display: flex; align-items: center; gap: 8px; font-size: var(--t-sm); cursor: pointer; }
.switch + .switch { margin-top: 12px; }
.switch input { width: auto; accent-color: var(--accent); }
/* Une case qu'on ne peut pas cocher doit se VOIR : sans cela, elle passe pour
   cassée plutôt que pour indisponible. */
.switch.bloque { color: var(--text-faint); cursor: not-allowed; }
.switch.bloque input { cursor: not-allowed; }

.field { margin-top: 16px; }
.field + .field { padding-top: 16px; border-top: 1px solid var(--border); }
.field > label, .titre-liste {
  display: block; font-size: var(--t-sm); color: var(--text-dim); margin-bottom: 5px;
}
.field > label.switch { display: flex; font-size: var(--t-sm); color: var(--text); margin-bottom: 0; }
.option { color: var(--text-faint); }

.row { display: flex; gap: 8px; align-items: center; }
.row input {
  flex: 1; min-width: 0; font-size: var(--t-sm); padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}
.row.ajout { margin-top: 10px; }
.row.ajout input { max-width: 200px; }
.clear { color: var(--text-faint); }
.clear:hover { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }

/* Un libellé retiré de l'œil mais pas du lecteur d'écran : le champ d'ajout est
   déjà nommé par le bouton d'à côté et par le titre de la liste, mais un
   <input> sans <label for> reste muet à la navigation vocale. */
.visuellement-a-part {
  position: absolute; width: 1px; height: 1px;
  padding: 0; margin: -1px; overflow: hidden;
  clip-path: inset(50%); white-space: nowrap;
}

.langues { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.langues li {
  display: flex; align-items: center; gap: 8px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 6px 4px 9px; max-width: 420px;
}
.rang {
  font-family: var(--mono); font-size: var(--t-xs); color: var(--text-faint);
  min-width: 14px; text-align: right;
}
.langues code {
  font-family: var(--mono); font-size: var(--t-sm); color: var(--text);
  background: none; padding: 0; min-width: 52px;
}
.premiere { font-size: var(--t-xs); color: var(--accent); }
.fleches { display: flex; gap: 3px; margin-left: auto; }
.fleche {
  font-size: var(--t-xs); line-height: 1; padding: 4px 7px;
  color: var(--text-dim); background: var(--surface); border: 1px solid var(--border);
}
.fleche:disabled { color: var(--text-faint); }
.retirer {
  font-size: var(--t-md); line-height: 1; padding: 1px 6px 3px;
  color: var(--text-faint); border: none; background: none;
}
.retirer:hover { color: var(--err); }

.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint strong { color: var(--text-dim); }
.hint.refus, .hint.attention, .hint.bloquant { color: var(--warn); opacity: .9; }
.hint.attention strong, .hint.bloquant strong { color: var(--warn); }
.hint.souligne { margin-top: 0; }
.hint.inline { margin: 0; }
.hint code { margin-right: 5px; }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}
a { color: var(--accent); }

.essai { margin-top: 18px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.resultat { font-size: var(--t-sm); line-height: 1.55; max-width: 520px; }
.resultat.ok { color: var(--ok); }
.resultat.err { color: var(--err); }

/* Compte rendu du rattrapage : la valeur qu'on vient chercher après le clic. */
.summary { margin: 12px 0 0; font-size: var(--t-md); color: var(--text-dim); }
.summary strong { color: var(--ok); }
.indispo { margin: 10px 0 0; font-size: var(--t-xs); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.suite { margin-top: 10px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
/* Pourquoi un bouton est indisponible : la seule information de la zone. */
.raison { margin: 0; font-size: var(--t-xs); color: var(--text-dim); line-height: 1.5; max-width: 46ch; }
.panne-inline {
  margin: 12px 0 0; padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-sm); line-height: 1.6; color: var(--err); max-width: 660px;
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.panne-inline > p { margin: 0; }
.panne-inline button { margin-top: 8px; font-size: var(--t-sm); }
.panne-inline .raison { margin-top: 6px; }

@media (max-width: 700px) {
  /* Le champ prend la ligne : à côté de deux boutons, il ne restait plus de
     place ni pour la saisie ni pour les boutons. */
  .row { flex-wrap: wrap; }
  .row input, .row.ajout input { flex: 1 0 100%; max-width: none; }

  /* La cible se gagne au rembourrage : grossir la police déplacerait toute la
     hiérarchie typographique de l'écran pour un problème de doigt. */
  .row button, .essai button { min-height: 32px; padding: 7px 12px; }
  .fleche { min-height: 32px; min-width: 32px; padding: 7px 9px; }
  .retirer { min-height: 32px; padding: 6px 10px; }
  .langues li { max-width: none; }
}
</style>
