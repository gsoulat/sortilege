<script setup>
import { computed, ref, useId } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

/**
 * Fichiers annexes RECONNUS de la médiathèque : vignettes Jellyfin, affiches,
 * fiches, sous-titres et quelques déchets de release identifiés un par un.
 *
 * Le dépôt d'affiches pose un « poster.jpg » et un « fanart.jpg » à côté de
 * chaque œuvre rangée : peu de chose un par un, beaucoup sur plusieurs milliers
 * de dossiers. Cet écran dit combien ils pèsent, par famille, et les retire —
 * sans jamais proposer une vidéo, un livre, une piste audio, un fichier d'un
 * type inconnu, ni la corbeille elle-même : c'est le serveur qui trie, cet
 * écran ne fait que montrer son tri.
 *
 * Contrat : `GET /api/library/extras` pour le relevé —
 *   `root`, `categories` (5 entrées dans l'ordre `trickplay`, `images`,
 *   `fiches`, `sous_titres`, `autres` ; chacune `{key, label, count, bytes,
 *   samples}`), `protected` (`{videos, audio, books, unknown,
 *   unknown_samples}`), `skipped_dirs` (dossiers illisibles non parcourus),
 *   `total_bytes`, `redeposit` (`{artwork, nfo, opf}`), `trash_path`.
 * `POST /api/library/extras/prune` pour le geste — corps `{categories, mode,
 *   confirm: true}` (le booléen, rien d'autre : sinon 422), réponse `{mode,
 *   removed, bytes, failed, failed_count, trash_path, removed_dirs}`.
 *
 * Le serveur refait le parcours au moment du geste : les chiffres affichés ici
 * sont ceux de la dernière analyse, pas une promesse.
 */

/**
 * Déchets que « Autres restes reconnus » retient, et rien d'autre. Copie de
 * `DECHETS_EXTENSIONS` et `DECHETS_NOMS` (sortilege/core/companions.py) ; un
 * test côté serveur vérifie que chaque entrée figure ici.
 */
const DECHETS_EXTENSIONS = ['.sfv', '.md5', '.sha1', '.sha256', '.url', '.lnk', '.nzb', '.torrent', '.par2', '.srr', '.srs']
const DECHETS_NOMS = ['Thumbs.db', 'ehthumbs.db', 'desktop.ini', '.DS_Store']

const idTitre = useId()
const idBase = useId()

// --- Relevé ------------------------------------------------------------------

// Trois états qui ne se confondent pas : `analyse` à null veut dire « pas de
// relevé », et rien d'autre. La panne a sa propre variable, sans quoi un
// serveur mort afficherait « rien n'a encore été analysé » et personne ne
// saurait qu'il faut réessayer.
const analyse = ref(null)
const analysant = ref(false)
const panneAnalyse = ref(null)

/** Une erreur de validation FastAPI (`{loc, msg}`) en une ligne lisible. */
function erreurDeChamp(e) {
  if (!e || typeof e !== 'object') return ''
  const champ = Array.isArray(e.loc) ? e.loc.filter((x) => x !== 'body').join('.') : ''
  const msg = typeof e.msg === 'string' ? e.msg : ''
  if (champ && msg) return `champ « ${champ} » : ${msg}`
  return msg || (champ ? `champ « ${champ} »` : '')
}

/**
 * Le motif d'un refus, tel que le serveur l'a écrit. Un `detail` en liste — la
 * validation de FastAPI, réponse 422 — est aplati champ par champ plutôt
 * qu'affiché brut ; illisible, il laisse place au code, qui reste vrai.
 */
function motif(corps, statut, defaut) {
  const detail = corps?.detail
  if (typeof detail === 'string' && detail) return detail
  const lignes = Array.isArray(detail) ? detail.map(erreurDeChamp).filter(Boolean) : []
  if (statut === 422) {
    return lignes.length
      ? `Requête mal formée (réponse 422), rien n'a été touché : ${lignes.join(' ; ')}.`
      : "Requête mal formée (réponse 422) : le serveur l'a refusée sans rien toucher."
  }
  if (lignes.length) return `Refus du serveur (réponse ${statut}) : ${lignes.join(' ; ')}.`
  return defaut ?? `Refus du serveur (réponse ${statut}).`
}

/**
 * Parcourt la médiathèque. Jamais au montage : sur un NAS, le parcours prend
 * plusieurs minutes, et l'ouverture d'un onglet ne doit pas occuper les disques
 * de quelqu'un qui venait regarder autre chose.
 *
 * Le relevé précédent est effacé AVANT l'appel : pas de liste périmée sous un
 * bouton qui supprime.
 */
async function analyser({ garderCompteRendu = false } = {}) {
  analysant.value = true
  panneAnalyse.value = null
  analyse.value = null
  if (!garderCompteRendu) {
    compteRendu.value = null
    refus.value = null
  }
  try {
    const res = await fetch('/api/library/extras')
    // Un échec de passerelle répond en HTML : lire le corps sans filet ferait
    // lever ici, et la panne arriverait à l'écran comme « injoignable ».
    const corps = await res.json().catch(() => null)
    if (!res.ok) {
      panneAnalyse.value = motif(corps, res.status, `L'analyse a échoué (réponse ${res.status}).`)
    } else if (!corps || !Array.isArray(corps.categories)) {
      panneAnalyse.value =
        "Réponse inattendue du serveur : le relevé ne contient aucune liste de catégories."
    } else {
      analyse.value = corps
    }
  } catch {
    panneAnalyse.value = "Serveur injoignable : l'analyse n'a pas pu être menée."
  } finally {
    analysant.value = false
  }
}

const categories = computed(() => analyse.value?.categories ?? [])
const proteges = computed(() => analyse.value?.protected ?? {})
const inconnus = computed(() => Number(proteges.value.unknown) || 0)
const exemplesInconnus = computed(() =>
  Array.isArray(proteges.value.unknown_samples) ? proteges.value.unknown_samples : [],
)
const dossiersSautes = computed(() => Number(analyse.value?.skipped_dirs) || 0)
const redepot = computed(() => analyse.value?.redeposit ?? {})
const rienNullePart = computed(() => categories.value.every((c) => !c.count))

// --- Sélection -----------------------------------------------------------------

/**
 * Les vignettes Jellyfin et les images, par défaut : ce sont elles qu'on vient
 * chercher, et elles se refont sans rien perdre — Jellyfin régénère les unes,
 * le dépôt repose les autres. Les fiches portent des arbitrages, les
 * sous-titres ont pu être gardés exprès : elles se cochent en connaissance de
 * cause, pas par défaut.
 *
 * Les cases survivent à une nouvelle analyse : relancer après un nettoyage ne
 * doit pas remettre à zéro ce qu'on venait de choisir.
 */
const coches = ref({ trickplay: true, images: true })

/** Une case cochée sur une catégorie vide ne retient rien : elle n'est ni
 *  envoyée, ni comptée, ni affichée cochée. */
const retenues = computed(() =>
  categories.value.filter((c) => coches.value[c.key] && c.count > 0),
)
const retenue = (cle) => retenues.value.some((c) => c.key === cle)

function basculer(cle, oui) {
  coches.value = { ...coches.value, [cle]: oui }
}

const totalFichiers = computed(() => retenues.value.reduce((s, c) => s + (c.count ?? 0), 0))
const totalOctets = computed(() => retenues.value.reduce((s, c) => s + (c.bytes ?? 0), 0))

const ouverts = ref({})
const inconnusOuverts = ref(false)
function basculerExemples(cle) {
  ouverts.value = { ...ouverts.value, [cle]: !ouverts.value[cle] }
}

/**
 * Ce que les prochains rangements redéposeront, pour chaque famille retenue.
 * Sans cet avertissement, la place gagnée se reperd au rangement suivant et
 * personne ne comprend pourquoi les affiches « reviennent ».
 *
 * Les intitulés sont ceux des cases de « Métadonnées locales », mot pour mot :
 * c'est là qu'il faudra les retrouver.
 */
const redepots = computed(() => {
  const r = redepot.value
  const avis = []
  if (retenue('images') && (r.artwork || r.opf)) {
    const cases = []
    if (r.artwork) cases.push("Déposer « poster.jpg » et « fanart.jpg » à côté du média")
    if (r.opf) cases.push("Écrire « metadata.opf » et la couverture à côté d'un livre")
    avis.push({
      cle: 'images',
      texte:
        (r.artwork && r.opf
          ? 'Les affiches et les couvertures de livres'
          : r.artwork
            ? 'Les affiches'
            : 'Les couvertures de livres') +
        " sont toujours déposées au rangement : les prochains rangements en remettront, et la place gagnée se reperdra à mesure.",
      cases,
    })
  }
  if (retenue('fiches') && (r.nfo || r.opf)) {
    const cases = []
    if (r.nfo) cases.push('Écrire les fiches .nfo à côté des vidéos')
    if (r.opf) cases.push("Écrire « metadata.opf » et la couverture à côté d'un livre")
    avis.push({
      cle: 'fiches',
      texte:
        "L'écriture des fiches est toujours active : les prochains rangements en redéposeront, " +
        "tout comme le bouton « Écrire les fiches de toute la bibliothèque ».",
      cases,
    })
  }
  return avis
})

// --- Geste ---------------------------------------------------------------------

/** `false`, `'trash'` ou `'delete'` : le bouton occupé est celui qu'on a pressé. */
const nettoyant = ref(false)
const compteRendu = ref(null)
// Distinct du compte rendu, pour la même raison que la panne d'analyse : un
// refus n'est pas un nettoyage qui n'aurait rien trouvé.
const refus = ref(null)

async function nettoyer(mode) {
  const cles = retenues.value.map((c) => c.key)
  if (!cles.length || nettoyant.value) return
  nettoyant.value = mode
  compteRendu.value = null
  refus.value = null
  let relancer = false
  try {
    const res = await fetch('/api/library/extras/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories: cles, mode, confirm: true }),
    })
    const corps = await res.json().catch(() => ({}))
    if (res.ok) {
      compteRendu.value = { ...corps, mode: corps.mode ?? mode }
      relancer = true
    } else if (res.status === 409) {
      refus.value = motif(
        corps,
        409,
        'Un nettoyage est déjà en cours : attends son compte rendu avant de relancer.',
      )
      // Un autre nettoyage déplace des fichiers en ce moment : le relevé
      // affiché ne tient déjà plus.
      relancer = true
    } else if (res.status >= 500) {
      // Une erreur du serveur peut survenir au milieu de la passe : des
      // fichiers ont pu partir avant elle. Même lecture qu'une coupure réseau.
      refus.value =
        `Erreur du serveur (réponse ${res.status}). C'est le compte rendu qui manque : ` +
        "des fichiers ont pu être traités avant l'erreur. L'analyse est relancée pour montrer ce qui reste."
      relancer = true
    } else {
      // 400, 404, 422 : la demande elle-même est refusée, rien n'a été touché —
      // le relevé affiché reste juste, inutile de reparcourir le NAS.
      refus.value = motif(corps, res.status)
    }
  } catch {
    refus.value =
      "Serveur injoignable. C'est le compte rendu qui manque, pas forcément le nettoyage : " +
      "l'analyse est relancée pour montrer ce qui reste."
    relancer = true
  } finally {
    nettoyant.value = false
  }
  if (relancer) await analyser({ garderCompteRendu: true })
}

const failed = computed(() => compteRendu.value?.failed ?? [])
/** Le serveur peut borner sa liste d'échecs ; le compte, lui, est complet.
 *  Afficher l'un sans l'autre laisserait croire que la liste résume tout. */
const echecsNonDetailles = computed(() =>
  Math.max(0, (compteRendu.value?.failed_count ?? 0) - failed.value.length),
)

// --- Raisons et textes -------------------------------------------------------------

const raisonAnalyse = computed(() =>
  nettoyant.value
    ? "Un nettoyage est en cours : l'analyse sera relancée d'elle-même à la fin."
    : '',
)

/** Une phrase par cause, passée aux deux gestes comme `disabled-reason` :
 *  ConfirmAction l'affiche à côté de chaque bouton désactivé et l'y relie
 *  (`aria-describedby`), pour qui arrive au clavier sur l'un ou l'autre. */
const raisonGestes = computed(() => {
  if (nettoyant.value === 'delete')
    return 'Suppression en cours : chaque fichier est retiré un par un. Sur un NAS, cela peut prendre plusieurs minutes ; le compte rendu arrive ici.'
  if (nettoyant.value)
    return 'Mise en corbeille en cours : chaque fichier est déplacé un par un. Sur un NAS, cela peut prendre plusieurs minutes ; le compte rendu arrive ici.'
  if (rienNullePart.value)
    return "Aucun fichier annexe dans la médiathèque : il n'y a rien à retirer."
  if (retenues.value.length) return ''
  if (categories.value.some((c) => coches.value[c.key]))
    return "Les catégories cochées sont vides : rien à retirer. Coche une catégorie qui contient des fichiers."
  return "Rien n'est coché : choisis au moins une catégorie ci-dessus."
})

const nombre = (n) => (Number(n) || 0).toLocaleString('fr-FR')
const pluriel = (n, mot) => `${nombre(n)} ${mot}${n > 1 ? 's' : ''}`
const accord = (n, nom, participe) => `${pluriel(n, nom)} ${participe}${n > 1 ? 's' : ''}`

/** Octets vers Ko, Mo, Go, To — en base 1024, comme les « Go » du reste de
 *  l'application : deux écrans qui annoncent deux poids pour le même dossier
 *  se contredisent. */
function poids(octets) {
  const n = Number(octets) || 0
  if (n < 1024) return `${n} o`
  const unites = ['Ko', 'Mo', 'Go', 'To']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < unites.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toLocaleString('fr-FR', { maximumFractionDigits: v < 10 ? 1 : 0 })} ${unites[i]}`
}

const libelles = computed(() => retenues.value.map((c) => `« ${c.label} »`).join(', '))

const resumeSelection = computed(
  () => `${pluriel(totalFichiers.value, 'fichier')} (${poids(totalOctets.value)}) : ${libelles.value}.`,
)

const detailCorbeille = computed(
  () =>
    'Déplace dans la corbeille de Sortilège' +
    (analyse.value?.trash_path ? ` (${analyse.value.trash_path})` : '') +
    ` ${resumeSelection.value} La place n'est libérée qu'une fois la corbeille vidée, dans Réglages → Système → Corbeille. ` +
    "Le serveur refait le relevé au moment du geste : les chiffres peuvent bouger si la médiathèque a changé depuis l'analyse.",
)

const detailSuppression = computed(
  () =>
    `Supprime définitivement ${resumeSelection.value} Rien ne passe par la corbeille, rien ne se restaure ensuite. ` +
    "Le serveur refait le relevé au moment du geste : les chiffres peuvent bouger si la médiathèque a changé depuis l'analyse.",
)

const libelleAnalyse = computed(() => {
  if (analysant.value) return 'Analyse en cours…'
  return analyse.value ? "Relancer l'analyse" : 'Analyser la médiathèque'
})
</script>

<template>
  <section class="annexes" :aria-labelledby="idTitre">
    <h3 :id="idTitre">Fichiers annexes</h3>
    <p class="note">
      Ce sont les fichiers de la médiathèque que Sortilège <strong>sait reconnaître comme
      annexes</strong> : vignettes de Jellyfin (dossiers <code>.trickplay</code>), affiches
      déposées au rangement (<code>poster.jpg</code>, <code>fanart.jpg</code>,
      <code>cover.jpg</code>), fiches <code>.nfo</code> et <code>.opf</code>, sous-titres, et
      quelques restes de release identifiés un par un. Tout le reste reste en place.
    </p>

    <div class="analyse">
      <button type="button" :disabled="analysant || Boolean(nettoyant)" @click="analyser()">
        {{ libelleAnalyse }}
      </button>
      <p v-if="raisonAnalyse" class="indispo" role="status">{{ raisonAnalyse }}</p>
    </div>

    <!-- Trois états, et la panne passe en premier : sans cela elle se cacherait
         derrière « rien n'a encore été analysé », qui vaut le même relevé nul. -->
    <p v-if="panneAnalyse" class="panne-inline" role="alert">
      <span>{{ panneAnalyse }} Aucun fichier n'a été touché : c'est le relevé qui manque.</span>
      <button type="button" class="small" @click="analyser({ garderCompteRendu: true })">
        Réessayer
      </button>
    </p>
    <p v-else-if="analysant" class="indispo" role="status">
      Analyse en cours — la médiathèque est parcourue dossier par dossier. Sur un NAS, cela
      peut prendre plusieurs minutes ; le reste de l'écran fonctionne pendant ce temps, et le
      relevé s'affichera ici.
    </p>
    <p v-else-if="!analyse" class="hint">
      Rien n'a encore été analysé. Le parcours lit toute la médiathèque : il ne part qu'à la
      demande, jamais à l'ouverture de l'écran, pour ne pas occuper les disques pour rien. Les
      catégories à cocher et les gestes de nettoyage apparaissent avec le relevé.
    </p>

    <template v-else>
      <p class="summary">
        <template v-if="rienNullePart">
          <strong class="neutre">Aucun fichier annexe</strong> dans
          <code>{{ analyse.root }}</code>.
        </template>
        <template v-else>
          <strong>{{ poids(analyse.total_bytes) }}</strong> de fichiers annexes dans
          <code>{{ analyse.root }}</code>.
        </template>
      </p>
      <p v-if="dossiersSautes > 0" class="hint attention" role="status">
        <template v-if="dossiersSautes > 1">
          {{ nombre(dossiersSautes) }} dossiers illisibles n'ont pas été parcourus : le relevé
          est incomplet.
        </template>
        <template v-else>
          1 dossier illisible n'a pas été parcouru : le relevé est incomplet.
        </template>
        Leurs fichiers n'apparaissent dans aucune catégorie et ne seront pas touchés.
      </p>

      <ul class="categories">
        <li
          v-for="c in categories"
          :key="c.key"
          :class="{ vide: !c.count, retenue: retenue(c.key) }"
        >
          <div class="ligne-cat">
            <input
              :id="`${idBase}-${c.key}`"
              type="checkbox"
              :checked="retenue(c.key)"
              :disabled="!c.count || Boolean(nettoyant)"
              @change="basculer(c.key, $event.target.checked)"
            />
            <label :for="`${idBase}-${c.key}`">{{ c.label }}</label>
            <span v-if="c.count" class="chiffres">
              {{ pluriel(c.count, 'fichier') }} · {{ poids(c.bytes) }}
            </span>
            <span v-else class="chiffres">rien à retirer</span>
          </div>

          <template v-if="c.count">
            <template v-if="c.samples?.length">
              <button
                type="button"
                class="depliant"
                :aria-expanded="Boolean(ouverts[c.key])"
                :aria-controls="`${idBase}-${c.key}-exemples`"
                @click="basculerExemples(c.key)"
              >
                {{ ouverts[c.key] ? 'Masquer les exemples' : 'Voir des exemples' }}
              </button>
              <div v-show="ouverts[c.key]" :id="`${idBase}-${c.key}-exemples`" class="exemples">
                <p class="exemples-titre">
                  <template v-if="c.count > c.samples.length">
                    Premiers exemples : {{ nombre(c.samples.length) }} sur {{ nombre(c.count) }}
                  </template>
                  <template v-else>Liste complète</template>
                </p>
                <ul>
                  <li v-for="s in c.samples" :key="s"><code>{{ s }}</code></li>
                </ul>
              </div>
            </template>
            <p v-else class="indispo">
              Le serveur n'a transmis aucun exemple pour cette catégorie.
            </p>
          </template>

          <p v-if="c.key === 'fiches' && retenue('fiches')" class="avertissement" role="note">
            Sans fiche <code>.nfo</code>, Jellyfin réidentifie l'œuvre par son nom de fichier :
            un arbitrage rendu à la main — « ce Dark Matter est celui de 2015 » — peut être
            perdu au rafraîchissement suivant. Les <code>metadata.opf</code> des livres partent
            aussi.
          </p>
          <p v-if="c.key === 'sous_titres' && retenue('sous_titres')" class="avertissement" role="note">
            Les sous-titres déposés au rangement ou gardés avec la vidéo seront retirés : une
            vidéo en version originale peut se retrouver sans. Ceux d'OpenSubtitles se
            recherchent de nouveau dans Réglages → Automatisation → Sous-titres manquants.
          </p>
          <p
            v-if="c.key === 'trickplay' && c.count"
            class="avertissement"
            :class="{ discret: !retenue('trickplay') }"
            role="note"
          >
            Vignettes d'aperçu que Jellyfin génère pour la barre de lecture : rien n'est perdu, il
            sait les refaire. Mais il les <strong>recrée à sa prochaine tâche planifiée</strong> tant
            que ses bibliothèques enregistrent le trickplay à côté des médias : la place gagnée se
            reperdra. Pour l'éviter, désactive dans Jellyfin cet enregistrement à côté des médias,
            ou l'extraction trickplay elle-même.
          </p>
          <p v-if="c.key === 'autres' && c.count" class="avertissement discret" role="note">
            Uniquement des déchets reconnus, dont la liste figure plus bas : sommes de contrôle,
            raccourcis, fichiers de parité et d'index de release. Un fichier d'un type inconnu
            n'y entre jamais.
          </p>
        </li>
      </ul>

      <p class="jamais">
        <strong>Jamais proposés :</strong> les vidéos, les pistes audio et les livres (ici
        {{ pluriel(proteges.videos ?? 0, 'vidéo') }}<template v-if="proteges.audio">,
          {{ nombre(proteges.audio) }} {{ proteges.audio > 1 ? 'pistes audio' : 'piste audio' }}</template>
        et {{ pluriel(proteges.books ?? 0, 'livre') }}), les structures de disque
        (<code>VIDEO_TS</code>, <code>BDMV</code>, <code>HVDVD_TS</code>, fichiers
        <code>.ifo</code>, <code>.bup</code>, <code>.mpls</code>…), et les
        <strong>fichiers d'un type que Sortilège ne reconnaît pas</strong>
        (<template v-if="inconnus">ici {{ pluriel(inconnus, 'fichier') }}</template><template v-else>aucun ici</template>)
        : ils ne figurent dans aucune catégorie et restent en place quoi qu'on coche. La
        corbeille de Sortilège<template v-if="analyse.trash_path">
          (<code>{{ analyse.trash_path }}</code>)</template> n'est pas parcourue non plus.
      </p>
      <template v-if="inconnus">
        <button
          v-if="exemplesInconnus.length"
          type="button"
          class="depliant depliant-jamais"
          :aria-expanded="inconnusOuverts"
          :aria-controls="`${idBase}-inconnus`"
          @click="inconnusOuverts = !inconnusOuverts"
        >
          {{ inconnusOuverts ? 'Masquer les fichiers non reconnus' : 'Voir des fichiers non reconnus' }}
        </button>
        <p v-else class="indispo">Le serveur n'a transmis aucun exemple de fichier non reconnu.</p>
        <div v-show="inconnusOuverts" :id="`${idBase}-inconnus`" class="exemples exemples-jamais">
          <p class="exemples-titre">
            <template v-if="inconnus > exemplesInconnus.length">
              Premiers exemples : {{ nombre(exemplesInconnus.length) }} sur {{ nombre(inconnus) }},
              laissés en place
            </template>
            <template v-else>Liste complète, laissée en place</template>
          </p>
          <ul>
            <li v-for="s in exemplesInconnus" :key="s"><code>{{ s }}</code></li>
          </ul>
        </div>
      </template>
      <p class="jamais">
        « Autres restes reconnus » ne retient que ces déchets : les extensions
        <template v-for="(e, i) in DECHETS_EXTENSIONS" :key="e"><template v-if="i > 0">, </template><code>{{ e }}</code></template>,
        et les fichiers
        <template v-for="(n, i) in DECHETS_NOMS" :key="n"><template v-if="i > 0">, </template><code>{{ n }}</code></template>.
      </p>

      <div v-for="a in redepots" :key="a.cle" class="redepot" role="note">
        {{ a.texte }} Pour l'éviter, décoche
        <template v-for="(cas, i) in a.cases" :key="cas">
          <template v-if="i > 0"> et </template><em>{{ cas }}</em>
        </template>
        dans <strong>Réglages → Bibliothèque → Métadonnées locales</strong>.
      </div>

      <p class="selection" aria-live="polite">
        <template v-if="retenues.length">
          Sélection : <strong>{{ pluriel(totalFichiers, 'fichier') }}</strong>,
          <strong>{{ poids(totalOctets) }}</strong>.
        </template>
        <template v-else>Sélection vide.</template>
      </p>

      <div class="gestes">
        <div class="geste">
          <ConfirmAction
            label="Mettre en corbeille"
            :confirm-label="`Confirmer — mettre ${pluriel(totalFichiers, 'fichier')} en corbeille`"
            :detail="detailCorbeille"
            :busy="nettoyant === 'trash'"
            :disabled="Boolean(raisonGestes)"
            :disabled-reason="raisonGestes"
            @confirm="nettoyer('trash')"
          />
          <p class="hint">
            Se rattrape : les fichiers partent dans la corbeille de Sortilège.
            <strong>La place n'est libérée qu'une fois la corbeille vidée</strong>, dans
            Réglages → Système → Corbeille.
          </p>
        </div>
        <div class="geste secondaire">
          <ConfirmAction
            label="Supprimer définitivement"
            :confirm-label="`Confirmer — supprimer ${pluriel(totalFichiers, 'fichier')} (${poids(totalOctets)})`"
            :detail="detailSuppression"
            :busy="nettoyant === 'delete'"
            :disabled="Boolean(raisonGestes)"
            :disabled-reason="raisonGestes"
            @confirm="nettoyer('delete')"
          />
          <p class="hint alerte">
            <strong>Irréversible</strong> : rien ne passe par la corbeille, rien ne se restaure.
            La place est libérée tout de suite.
          </p>
        </div>
      </div>
    </template>

    <!-- Le compte rendu vit hors du relevé : l'analyse relancée après le geste
         efface la liste le temps du parcours, pas ce qui vient d'être fait. -->
    <p v-if="refus" class="panne-inline" role="alert">{{ refus }}</p>
    <div v-if="compteRendu" class="compte-rendu" role="status">
      <p class="summary">
        <template v-if="compteRendu.mode === 'delete'">
          <strong>{{ accord(compteRendu.removed ?? 0, 'fichier', 'supprimé') }}</strong>
          définitivement, {{ poids(compteRendu.bytes) }} libérés.
        </template>
        <template v-else>
          <strong>{{ pluriel(compteRendu.removed ?? 0, 'fichier') }} mis en corbeille</strong>,
          {{ poids(compteRendu.bytes) }}.
        </template>
      </p>
      <p v-if="(Array.isArray(compteRendu.removed_dirs) ? compteRendu.removed_dirs.length : (compteRendu.removed_dirs ?? 0)) > 0" class="hint">
        {{ (Array.isArray(compteRendu.removed_dirs) ? compteRendu.removed_dirs.length : (compteRendu.removed_dirs ?? 0)) }} {{ (Array.isArray(compteRendu.removed_dirs) ? compteRendu.removed_dirs.length : (compteRendu.removed_dirs ?? 0)) > 1 ? 'dossiers devenus vides retirés' : 'dossier devenu vide retiré' }}.
      </p>
      <p v-if="compteRendu.mode !== 'delete' && compteRendu.removed" class="hint">
        <template v-if="compteRendu.trash_path">
          Corbeille : <code>{{ compteRendu.trash_path }}</code>.
        </template>
        <strong>La place n'est pas encore libérée</strong> : elle le sera quand la corbeille
        sera vidée, dans Réglages → Système → Corbeille.
      </p>
      <p v-if="!compteRendu.removed && !compteRendu.failed_count" class="hint">
        Rien n'a été retiré : au moment du geste, le serveur n'a trouvé aucun fichier dans les
        catégories cochées.
      </p>
      <template v-if="compteRendu.failed_count">
        <p class="hint attention">
          {{ accord(compteRendu.failed_count, 'fichier', 'resté') }} en place, faute d'avoir pu
          être {{ compteRendu.failed_count > 1 ? 'retirés' : 'retiré' }} :
        </p>
        <ul class="echecs">
          <li v-for="(f, i) in failed" :key="i">{{ f }}</li>
          <li v-if="echecsNonDetailles" class="more">
            … et {{ accord(echecsNonDetailles, 'autre', `que le serveur n'a pas détaillé`) }}.
          </li>
        </ul>
      </template>
    </div>
  </section>
</template>

<style scoped>
.annexes {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px;
}
h3 {
  margin: 0 0 10px; font-size: var(--t-xs); font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-title);
}

.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }
.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.hint strong { color: var(--text-dim); }
.hint.attention { color: var(--warn); }
.hint.alerte strong { color: var(--err); }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
  overflow-wrap: anywhere;
}

.analyse { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.analyse .indispo { margin: 0; }

/* Ton d'appoint : l'état de l'écran dit à voix haute, jamais une alerte. */
.indispo {
  margin: 10px 0 0; font-size: var(--t-xs); color: var(--text-faint);
  line-height: 1.6; max-width: 680px;
}

.summary { margin: 14px 0 0; font-size: var(--t-md); color: var(--text-dim); }
.summary strong { color: var(--text); }
.summary strong.neutre { color: var(--text-dim); }

.categories {
  list-style: none; margin: 12px 0 0; padding: 0;
  display: flex; flex-direction: column;
  border: 1px solid var(--border); border-radius: 8px;
}
.categories > li { padding: 10px 12px; }
.categories > li + li { border-top: 1px solid var(--border); }
.categories > li.retenue { background: color-mix(in srgb, var(--accent) 6%, transparent); }

.ligne-cat { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--t-sm); }
.ligne-cat input { accent-color: var(--accent); flex: none; margin: 0; }
.ligne-cat label { color: var(--text); cursor: pointer; }
.chiffres { margin-left: auto; font-family: var(--mono); font-size: var(--t-xs); color: var(--text-dim); }
/* Grisée sans être effacée : --text-faint garde 5:1 sur --surface, là où une
   opacité aurait emporté le texte sous le seuil de lecture. */
.vide .ligne-cat label, .vide .chiffres { color: var(--text-faint); }
.vide .ligne-cat label { cursor: default; }
.vide .chiffres { font-family: inherit; font-style: italic; }

.depliant {
  margin: 6px 0 0 21px; padding: 2px 6px; font-size: var(--t-xs);
  color: var(--accent); background: transparent; border-color: transparent;
}
.depliant:hover:not(:disabled) { border-color: var(--accent-dim); }
.depliant-jamais { margin-left: 0; }
.exemples-jamais { margin-left: 0; }
.categories .indispo { margin: 6px 0 0 21px; }

.exemples { margin: 6px 0 0 21px; }
.exemples-titre { margin: 0 0 4px; font-size: var(--t-xs); color: var(--text-faint); }
.exemples ul {
  list-style: none; margin: 0; padding: 0;
  display: flex; flex-direction: column; gap: 3px;
}
.exemples li { font-size: var(--t-xs); line-height: 1.5; }

.avertissement {
  margin: 8px 0 0 21px; font-size: var(--t-xs); line-height: 1.6; color: var(--warn);
  max-width: 64ch; border-left: 2px solid color-mix(in srgb, var(--warn) 55%, transparent);
  padding-left: 9px;
}
.avertissement strong { color: inherit; }
.avertissement.discret { color: var(--text-dim); border-left-color: var(--border); }

.jamais { margin: 12px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.jamais strong { color: var(--ok); }

.redepot {
  margin: 12px 0 0; padding: 9px 12px; border-radius: 8px; max-width: 680px;
  font-size: var(--t-xs); line-height: 1.6; color: var(--warn);
  background: color-mix(in srgb, var(--warn) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--warn) 28%, transparent);
}
.redepot em { color: var(--text); font-style: normal; }
.redepot strong { color: var(--text); }

.selection { margin: 14px 0 0; font-size: var(--t-sm); color: var(--text-dim); }
.selection strong { color: var(--text); font-family: var(--mono); font-size: var(--t-sm); }

.gestes { display: flex; gap: 14px 24px; flex-wrap: wrap; margin-top: 10px; align-items: flex-start; }
.geste { flex: 1 1 260px; min-width: 0; }
.geste .hint { margin-top: 6px; font-size: var(--t-xs); }
.geste.secondaire { padding-left: 24px; border-left: 1px solid var(--border); }

/* Une panne ou un refus local : cadré là où il s'est produit. */
.panne-inline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 12px 0 0; padding: 9px 12px; border-radius: 8px; max-width: 680px;
  font-size: var(--t-xs); line-height: 1.6; color: var(--err);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.panne-inline button { color: var(--text); flex: none; }
button.small { font-size: var(--t-xs); padding: 3px 10px; }

.compte-rendu { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }
.compte-rendu .summary { margin-top: 0; }
.compte-rendu .summary strong { color: var(--ok); }
.echecs {
  list-style: none; margin: 8px 0 0; padding: 0;
  display: flex; flex-direction: column; gap: 3px;
  max-height: 220px; overflow-y: auto;
}
.echecs li { font-size: var(--t-xs); line-height: 1.55; color: var(--err); font-family: var(--mono); overflow-wrap: anywhere; }
.echecs .more { color: var(--text-faint); font-family: inherit; font-style: italic; }

@media (max-width: 700px) {
  .annexes { padding: 14px 12px; }
  /* 32 px de cible au doigt, gagnés au rembourrage et non à la police. */
  .ligne-cat { min-height: 32px; }
  .ligne-cat label { padding: 6px 0; }
  .chiffres { margin-left: 21px; flex: 1 0 100%; }
  .depliant, .categories .indispo, .exemples, .avertissement { margin-left: 0; }
  .depliant { min-height: 32px; padding: 6px 8px; }
  button.small { min-height: 32px; padding: 7px 12px; }
  .geste.secondaire { padding-left: 0; border-left: 0; padding-top: 14px; border-top: 1px solid var(--border); }
}
</style>
