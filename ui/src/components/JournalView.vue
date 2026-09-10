<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

/**
 * Le journal : ce qui a RÉELLEMENT bougé sur le disque, du plus récent au plus
 * ancien, annulable ligne à ligne.
 *
 * C'est le seul écran qui parle du passé. Tous les autres montrent une
 * intention — un plan calculé, une file à arbitrer — qui peut encore être
 * abandonnée sans conséquence. Ici, chaque ligne est un fichier qui n'est plus
 * là où il était, et le seul chemin de retour.
 *
 * Trois choix portent la page :
 *
 * 1. **Par moment, pas par numéro de ligne.** On se souvient de « hier soir »,
 *    pas de « page 3 ». Les entrées sont donc regroupées par jour, avec
 *    « Aujourd'hui » et « Hier » nommés plutôt que datés.
 * 2. **Les racines communes sortent des lignes.** Un chemin de rangement fait
 *    cent vingt caractères dont quatre-vingt-dix identiques d'une ligne à
 *    l'autre. Ils sont annoncés une fois en tête de liste ; les lignes ne
 *    portent que ce qui les distingue. Rien n'est perdu : racine + reste = le
 *    chemin complet.
 * 3. **L'annulation porte sur le PLAN, jamais sur la ligne seule.** Le serveur
 *    défait tout ce qui partage le `plan_id` — la vidéo et ses compagnons. Le
 *    détail de confirmation le dit, parce que cliquer « Annuler » sur un
 *    sous-titre ramène aussi son épisode, et découvrir ça après coup serait
 *    une mauvaise surprise sur une opération disque.
 */

const PAR_PAGE = 50

const data = ref(null)
/**
 * La panne de lecture, distincte des données. `data` à null signifie « pas
 * encore lu » et rien d'autre : il ne peut pas porter en plus le sens « lu, et
 * ça a échoué » — sans quoi un serveur muet et un journal vide se ressemblent,
 * et personne ne réessaie ce qu'il croit terminé.
 */
const panne = ref(null)
const chargement = ref(false)

const page = ref(1)
const operation = ref('')
const recherche = ref('')
/** La requête réellement envoyée. Distincte de la saisie : taper « Severance »
 *  déclencherait neuf relectures complètes du journal. */
const requete = ref('')

/** Clé de l'annulation en cours — `plan_id` ou `oeuvre:<clé>`. Une seule à la
 *  fois : deux annulations concurrentes réécrivent le même journal. */
const enCours = ref(null)
const message = ref(null)
const echecs = ref([])

const WORK_KINDS = { movie: 'Film', episode: 'Série', anime: 'Anime', book: 'Livre' }

async function charger() {
  chargement.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), per_page: String(PAR_PAGE) })
    if (operation.value) params.set('operation', operation.value)
    if (requete.value) params.set('q', requete.value)

    const res = await fetch(`/api/review/journal/entries?${params}`)
    // Une réponse 502 arrive en HTML : `res.json()` lèverait, la promesse
    // remonterait sans être attrapée, et l'écran resterait figé sans un mot.
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    const recu = await res.json()

    // Une annulation peut vider la dernière page ; y rester afficherait un
    // « aucune entrée » qui accuserait le filtre à tort.
    if (recu.page > recu.pages && recu.pages >= 1 && page.value !== recu.pages) {
      page.value = recu.pages
      chargement.value = false
      return charger()
    }

    data.value = recu
    panne.value = null
  } catch (e) {
    // `data` est CONSERVÉ : une liste qui date vaut mieux qu'un écran vide, à
    // condition de dire qu'elle date. Le bandeau s'en charge.
    panne.value = e.message ?? 'sans réponse'
  } finally {
    chargement.value = false
  }
}

let minuteurRecherche = null
watch(recherche, (valeur) => {
  clearTimeout(minuteurRecherche)
  minuteurRecherche = setTimeout(() => {
    requete.value = valeur.trim()
    page.value = 1
    charger()
  }, 300)
})
onBeforeUnmount(() => clearTimeout(minuteurRecherche))

function filtrer(code) {
  operation.value = operation.value === code ? '' : code
  page.value = 1
  charger()
}

function allerA(cible) {
  page.value = cible
  charger()
}

function toutMontrer() {
  operation.value = ''
  recherche.value = ''
  requete.value = ''
  page.value = 1
  charger()
}

// --- Chemins ---------------------------------------------------------------

/**
 * Le plus long dossier commun à une colonne de chemins.
 *
 * Deux segments utiles au minimum : élider « /data » ne gagne rien et coûte une
 * ligne de légende à lire.
 */
function racineCommune(chemins) {
  const dossiers = chemins.filter(Boolean).map((p) => p.split('/').slice(0, -1))
  if (!dossiers.length) return ''
  let commun = dossiers[0]
  for (const d of dossiers.slice(1)) {
    let i = 0
    while (i < commun.length && i < d.length && commun[i] === d[i]) i += 1
    commun = commun.slice(0, i)
    if (!commun.length) return ''
  }
  return commun.filter(Boolean).length >= 2 ? commun.join('/') : ''
}

const racineDeparts = computed(() => racineCommune((data.value?.entries ?? []).map((e) => e.source)))
const racineArrivees = computed(() =>
  racineCommune((data.value?.entries ?? []).map((e) => e.destination)),
)

function relatif(chemin, racine) {
  if (!chemin) return '—'
  return racine && chemin.startsWith(`${racine}/`) ? chemin.slice(racine.length + 1) : chemin
}

const nomFichier = (chemin) => (chemin ? chemin.split('/').pop() : 'ce fichier')
const dossierDe = (chemin) => (chemin ? chemin.split('/').slice(0, -1).join('/') || '/' : 'son dossier')

// --- Regroupement par jour -------------------------------------------------

const JOUR_LONG = new Intl.DateTimeFormat('fr-FR', {
  weekday: 'long',
  day: 'numeric',
  month: 'long',
  year: 'numeric',
})
const HEURE = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit' })

const cleJour = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`

function libelleJour(d) {
  const aujourdhui = new Date()
  const hier = new Date()
  hier.setDate(hier.getDate() - 1)
  if (cleJour(d) === cleJour(aujourdhui)) return "Aujourd'hui"
  if (cleJour(d) === cleJour(hier)) return 'Hier'
  const texte = JOUR_LONG.format(d)
  return texte.charAt(0).toUpperCase() + texte.slice(1)
}

/**
 * Les entrées arrivent déjà triées du plus récent au plus ancien : le
 * regroupement se fait donc en un passage, sans retrier côté client — retrier
 * casserait l'ordre stable que le serveur garantit pour un lot appliqué dans
 * la même seconde.
 *
 * Chaque entrée est enrichie de deux choses que le rendu ne peut pas déduire
 * seul : combien de lignes de la page partagent son œuvre, et si elle est la
 * première d'entre elles. Le bouton « toute l'œuvre » ne s'affiche que sur
 * cette première — le répéter sur les vingt-quatre épisodes d'une saison
 * mettrait vingt-quatre boutons destructeurs identiques à la file.
 */
const jours = computed(() => {
  const entrees = data.value?.entries ?? []
  const parOeuvre = new Map()
  for (const e of entrees) parOeuvre.set(e.work_key, (parOeuvre.get(e.work_key) ?? 0) + 1)

  const vues = new Set()
  const groupes = []

  entrees.forEach((e, i) => {
    const d = new Date(e.timestamp)
    const valide = !Number.isNaN(d.getTime())
    const cle = valide ? cleJour(d) : 'date-illisible'

    let groupe = groupes[groupes.length - 1]
    if (!groupe || groupe.cle !== cle) {
      groupe = {
        cle,
        // Une entrée dont l'horodatage ne se lit pas reste annulable : elle est
        // rangée à part plutôt que jetée dans un jour arbitraire.
        libelle: valide ? libelleJour(d) : 'Date illisible',
        entrees: [],
      }
      groupes.push(groupe)
    }

    const total = parOeuvre.get(e.work_key) ?? 1
    const ancre = total > 1 && !vues.has(e.work_key)
    if (total > 1) vues.add(e.work_key)

    groupe.entrees.push({
      ...e,
      // `plan_id` est partagé par la vidéo et ses compagnons : seul l'indice
      // distingue deux lignes du même plan.
      cleRendu: `${e.plan_id}:${i}`,
      heure: valide ? HEURE.format(d) : '--:--',
      ancreOeuvre: ancre,
      lignesOeuvre: total,
    })
  })

  return groupes
})

// --- Ce qui va se passer, dit avant de cliquer -----------------------------

function detailEntree(e) {
  const fichier = nomFichier(e.destination)
  const retour = dossierDe(e.source)
  const depart =
    e.operation === 'trash'
      ? `« ${fichier} » ressort de la corbeille`
      : `« ${fichier} » quitte ${dossierDe(e.destination)}`
  return (
    `${depart} et revient dans ${retour}. ` +
    "L'annulation porte sur le plan entier : la vidéo et ses fichiers compagnons repartent ensemble. " +
    'Rien n\'est supprimé, et une origine déjà occupée fait échouer le retour plutôt que d\'écraser.'
  )
}

function detailOeuvre(e) {
  return (
    `Toutes les opérations enregistrées pour « ${e.title} » sont défaites, ` +
    'y compris celles qui ne sont pas affichées sur cette page. ' +
    "Chaque fichier retourne à son emplacement d'origine ; rien n'est supprimé."
  )
}

// --- Annulation ------------------------------------------------------------

async function annuler(cle, corps, reussite) {
  enCours.value = cle
  message.value = null
  echecs.value = []
  try {
    const res = await fetch('/api/review/undo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(corps),
    })
    if (!res.ok) {
      // Un 400 porte un motif utile dans son JSON ; un 502 arrive en HTML et
      // ferait lever `json()`. On tente le motif, on retombe sur le code.
      const detail = await res.json().catch(() => null)
      throw new Error(detail?.detail ?? `réponse ${res.status}`)
    }
    const out = await res.json()

    echecs.value = (out.results ?? []).filter((r) => !r.ok).slice(0, 5)
    message.value = reussite(out)
    await charger()
  } catch (e) {
    // Une annulation ratée ne se range pas avec une lecture ratée : la liste
    // affichée reste juste, c'est le geste qui n'a pas abouti.
    echecs.value = [{ message: `L'annulation n'a pas abouti (${e.message ?? 'sans réponse'}).` }]
  } finally {
    enCours.value = null
  }
}

function annulerEntree(e) {
  const revenu = dossierDe(e.source)
  return annuler(
    e.cleRendu,
    { plan_ids: [e.plan_id] },
    (out) =>
      out.undone
        ? `Défait : ${out.undone} fichier(s) de « ${e.title} » sont revenus dans ${revenu}.`
        : `Rien n'a pu être défait pour « ${e.title} ».`,
  )
}

function annulerOeuvre(e) {
  return annuler(
    `oeuvre:${e.work_key}`,
    { work: e.work_key },
    (out) => `« ${e.title} » : ${out.undone} opération(s) défaites, l'œuvre est revenue à sa source.`,
  )
}

// --- États de la liste -----------------------------------------------------

const filtreActif = computed(() => Boolean(operation.value || requete.value))

/**
 * Une liste vide dit POURQUOI elle est vide. Journal réellement vierge et
 * filtre trop étroit produisent le même écran blanc, et ce sont deux
 * situations opposées : l'une se règle en rangeant des fichiers, l'autre en
 * effaçant une recherche.
 */
const motifDuVide = computed(() => {
  if (!data.value || data.value.entries.length) return null
  if (!data.value.journal_size) {
    return "Le journal est vide : aucun fichier n'a encore été déplacé par Sortilège. Il se remplira au premier rangement, et chaque ligne y restera annulable."
  }
  return `Aucune entrée ne correspond à ce filtre. Le journal en compte ${data.value.journal_size} au total.`
})

/**
 * Pourquoi les annulations sont inertes, en une phrase et non en cinquante.
 *
 * La cause est commune à toute la liste : la répéter sous chacun des cinquante
 * boutons demanderait de la chercher au lieu de la lire. Les `ConfirmAction`
 * de la page laissent donc leur `disabled-reason` vide et cette phrase
 * s'affiche une fois, au-dessus.
 */
const raisonIndispo = computed(() => {
  if (enCours.value) {
    return 'Une annulation est en cours : les autres attendent la fin. Le journal est réécrit à chaque retour arrière, deux annulations simultanées se marcheraient dessus.'
  }
  if (chargement.value) return 'La page est en cours de relecture : les annulations reprennent dès qu\'elle est à jour.'
  return null
})

const positionPager = computed(() => {
  if (!data.value) return ''
  if (chargement.value) return 'Relecture en cours : la pagination attend la fin.'
  if (data.value.pages <= 1) return 'Tout le journal filtré tient sur cette page.'
  if (data.value.page <= 1) return 'Début du journal : rien de plus récent.'
  if (data.value.page >= data.value.pages) return 'Fin du journal : rien de plus ancien.'
  return null
})

onMounted(charger)
</script>

<template>
  <div class="journal">
    <header class="entete">
      <h2>Journal des changements</h2>
      <p class="lead">
        Ce qui a <strong>réellement</strong> bougé sur le disque, du plus récent au plus ancien.
        Chaque ligne se défait seule : le fichier retourne d'où il vient, et rien n'est jamais
        supprimé au passage.
      </p>
    </header>

    <!-- Trois états, et non « données ou rien ». Un écran vide ne dit pas s'il
         attend, s'il a échoué ou s'il n'a rien à montrer. -->
    <div v-if="!data && chargement" class="attente">
      <span class="pulsation"></span>
      Lecture du journal…
    </div>

    <div v-else-if="!data" class="panne">
      <h3>Le journal n'a pas pu être lu</h3>
      <p>{{ panne ?? 'Aucune réponse de Sortilège.' }}</p>
      <p class="quoi-faire">
        Rien n'est perdu : le journal vit sur le disque, pas dans cette page, et tout y reste
        annulable dès que le serveur répond. Vérifie que le conteneur tourne
        (<code>docker ps</code>), puis réessaie.
      </p>
      <button class="primary" :disabled="chargement" @click="charger">Réessayer</button>
    </div>

    <template v-else>
      <!-- Rechargement raté alors qu'une liste est déjà affichée : elle reste,
           mais on dit qu'elle date. Une liste périmée passée pour fraîche ferait
           cliquer « Annuler » sur une entrée qui n'existe plus. -->
      <p v-if="panne" class="panne-inline">
        Cette liste date de la dernière lecture réussie : le rechargement a échoué
        ({{ panne }}).
        <button class="petit" :disabled="chargement" @click="charger">Réessayer</button>
      </p>

      <div class="outils">
        <div class="natures" role="group" aria-label="Filtrer par nature d'opération">
          <button :class="{ actif: !operation }" @click="filtrer('')">
            Tout <span class="compte">{{ data.journal_size }}</span>
          </button>
          <button
            v-for="op in data.operations"
            :key="op.code"
            :class="{ actif: operation === op.code }"
            :aria-pressed="operation === op.code"
            @click="filtrer(op.code)"
          >
            {{ op.label }} <span class="compte">{{ op.count }}</span>
          </button>
        </div>

        <label class="champ">
          <span>Titre</span>
          <input
            v-model="recherche"
            type="search"
            placeholder="Severance, Dune…"
            autocomplete="off"
          />
        </label>

        <button class="petit" :disabled="chargement" @click="charger">
          {{ chargement ? 'Relecture…' : 'Actualiser' }}
        </button>
      </div>

      <p v-if="requete" class="note-compteurs">
        Les compteurs de nature portent sur tout le journal, pas sur la recherche en cours.
      </p>

      <!-- Les racines communes, annoncées une fois. Racine + reste de ligne
           reconstitue le chemin complet : rien n'est caché, seule la répétition
           disparaît. -->
      <p v-if="racineDeparts || racineArrivees" class="racines">
        Chemins abrégés<span v-if="racineDeparts">
          — départs sous <code>{{ racineDeparts }}</code></span
        ><span v-if="racineArrivees">
          — arrivées sous <code>{{ racineArrivees }}</code></span
        >.
      </p>

      <p v-if="raisonIndispo" class="indispo" role="status">{{ raisonIndispo }}</p>

      <div class="retours" aria-live="polite">
        <p v-if="message" class="ok-msg">{{ message }}</p>
        <ul v-if="echecs.length" class="echecs">
          <li v-for="(r, i) in echecs" :key="i">
            {{ r.message }}<span v-if="r.from"> — <code>{{ r.from }}</code></span>
          </li>
        </ul>
      </div>

      <p v-if="motifDuVide" class="vide">
        {{ motifDuVide }}
        <button v-if="filtreActif" class="petit" @click="toutMontrer">Tout montrer</button>
      </p>

      <template v-else>
        <section v-for="jour in jours" :key="jour.cle" class="jour">
          <h3>
            {{ jour.libelle }}
            <span class="combien">{{ jour.entrees.length }} entrée(s)</span>
          </h3>

          <ul class="entrees">
            <li v-for="e in jour.entrees" :key="e.cleRendu" class="entree">
              <div class="corps">
                <div class="ligne-titre">
                  <time class="heure">{{ e.heure }}</time>
                  <span class="titre">{{ e.title }}</span>
                  <span class="nature" :class="e.operation">{{ e.operation_label }}</span>
                  <span v-if="e.work_kind" class="kind">
                    {{ WORK_KINDS[e.work_kind] ?? e.work_kind }}
                  </span>
                  <span v-if="e.method === 'copy'" class="kind">recopié entre volumes</span>
                </div>

                <div class="chemins">
                  <div class="chemin">
                    <span class="sens">de</span>
                    <code>{{ relatif(e.source, racineDeparts) }}</code>
                  </div>
                  <div class="chemin">
                    <span class="sens vers">vers</span>
                    <code>{{ relatif(e.destination, racineArrivees) }}</code>
                  </div>
                </div>
              </div>

              <div class="actions">
                <ConfirmAction
                  label="Annuler"
                  confirm-label="Confirmer le retour"
                  :detail="detailEntree(e)"
                  :busy="enCours === e.cleRendu"
                  :disabled="Boolean(raisonIndispo) && enCours !== e.cleRendu"
                  @confirm="annulerEntree(e)"
                />

                <!-- Une seule fois par œuvre : une saison rangée produit une
                     entrée par épisode, et vingt-quatre boutons « toute
                     l'œuvre » côte à côte ne proposeraient qu'un seul geste. -->
                <template v-if="e.ancreOeuvre">
                  <ConfirmAction
                    label="Annuler toute l'œuvre"
                    :confirm-label="`Confirmer — tout « ${e.title} »`"
                    :detail="detailOeuvre(e)"
                    :busy="enCours === `oeuvre:${e.work_key}`"
                    :disabled="Boolean(raisonIndispo) && enCours !== `oeuvre:${e.work_key}`"
                    @confirm="annulerOeuvre(e)"
                  />
                  <span class="portee">{{ e.lignesOeuvre }} entrées de cette œuvre sur cette page</span>
                </template>
              </div>
            </li>
          </ul>
        </section>

        <nav class="pagination" aria-label="Pagination du journal">
          <button :disabled="data.page <= 1 || chargement" @click="allerA(data.page - 1)">
            ← Plus récent
          </button>
          <span class="position">
            Page {{ data.page }} sur {{ data.pages }} — {{ data.total }} entrée(s)
            <template v-if="filtreActif"> filtrée(s) sur {{ data.journal_size }}</template>
          </span>
          <button
            :disabled="data.page >= data.pages || chargement"
            @click="allerA(data.page + 1)"
          >
            Plus ancien →
          </button>
        </nav>
        <!-- Pourquoi une flèche est inerte. À l'écran et non dans un `title` :
             ni le doigt ni le clavier ne font apparaître un `title`. -->
        <p v-if="positionPager" class="bord">{{ positionPager }}</p>
      </template>
    </template>
  </div>
</template>

<style scoped>
.journal { display: flex; flex-direction: column; gap: 14px; }

.entete h2 { margin: 0 0 6px; font-size: var(--t-lg); font-weight: 600; color: var(--text-title); }
.lead { margin: 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.65; max-width: 720px; }
.lead strong { color: var(--text-dim); }

/* --- États globaux -------------------------------------------------------- */

.attente, .panne {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 12px; min-height: 34vh; text-align: center; padding: 36px 20px;
}
.attente { color: var(--text-dim); font-size: var(--t-sm); }
.pulsation {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2px solid var(--border); border-top-color: var(--accent);
  animation: tourne 1s linear infinite;
}
@keyframes tourne { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .pulsation { animation: none; } }

.panne h3 { margin: 0; font-size: var(--t-md); color: var(--text-title); }
.panne p { margin: 0; font-size: var(--t-sm); color: var(--text-dim); max-width: 52ch; line-height: 1.6; }
.panne .quoi-faire { color: var(--text-faint); font-size: var(--t-xs); }
.panne code, .racines code { font-family: var(--mono); font-size: var(--t-xs); }
.panne .primary { border-color: var(--accent-dim); color: var(--text); }

.panne-inline {
  margin: 0; padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-xs); line-height: 1.6; color: var(--warn);
  border: 1px solid color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}

/* --- Barre d'outils ------------------------------------------------------- */

.outils { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.natures { display: flex; gap: 6px; flex-wrap: wrap; }
.natures button {
  font-size: var(--t-xs); padding: 5px 12px; color: var(--text-dim);
  display: inline-flex; align-items: center; gap: 7px;
}
.natures button.actif { border-color: var(--accent); color: var(--text); background: var(--surface); }
.compte { font-family: var(--mono); font-size: 11px; color: var(--text-faint); }
.natures button.actif .compte { color: var(--accent); }

.champ { display: flex; align-items: center; gap: 7px; font-size: var(--t-xs); color: var(--text-dim); }
.champ input {
  font-size: var(--t-sm); padding: 5px 10px; min-width: 190px;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px;
}
button.petit { font-size: var(--t-xs); padding: 5px 11px; color: var(--text-dim); }

.note-compteurs, .racines {
  margin: 0; font-size: var(--t-xs); color: var(--text-faint); line-height: 1.6;
  overflow-wrap: anywhere;
}

/* --- Messages ------------------------------------------------------------- */

.indispo {
  margin: 0; font-size: var(--t-xs); line-height: 1.6; color: var(--text-dim);
  border-left: 2px solid var(--accent-dim); padding-left: 10px; max-width: 76ch;
}
.retours:empty { display: none; }
.ok-msg {
  margin: 0 0 8px; font-size: var(--t-sm); color: var(--ok); line-height: 1.6;
}
.echecs { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; }
.echecs li { font-size: var(--t-xs); color: var(--err); line-height: 1.55; overflow-wrap: anywhere; }
.echecs code { font-family: var(--mono); color: var(--text-faint); }

.vide {
  margin: 0; padding: 26px 18px; border-radius: 10px; text-align: center;
  background: var(--surface); border: 1px solid var(--border);
  font-size: var(--t-sm); color: var(--text-dim); line-height: 1.65;
  display: flex; flex-direction: column; align-items: center; gap: 12px;
}

/* --- Liste ---------------------------------------------------------------- */

.jour { display: flex; flex-direction: column; gap: 8px; }
.jour h3 {
  margin: 0; font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .07em; color: var(--text-dim);
  display: flex; align-items: baseline; gap: 10px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
.combien { font-family: var(--mono); font-size: 11px; color: var(--text-faint); text-transform: none; letter-spacing: 0; }

.entrees { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.entree {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 9px; padding: 11px 14px;
}
.corps { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 6px; }

.ligne-titre { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.heure { font-family: var(--mono); font-size: var(--t-xs); color: var(--text-faint); }
.titre { font-size: var(--t-sm); color: var(--text); overflow-wrap: anywhere; }

/* La nature se lit à la couleur avant de se lire au mot : « Corbeille » au
   milieu de quarante « Rangement » doit sauter aux yeux, c'est la seule ligne
   dont l'annulation change autre chose qu'un classement. */
.nature {
  font-size: 11px; padding: 2px 8px; border-radius: 20px;
  border: 1px solid var(--border); color: var(--text-dim);
}
.nature.video { color: var(--accent); border-color: var(--accent-dim); }
.nature.trash {
  color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 9%, transparent);
}
.kind { font-size: 11px; color: var(--text-faint); }

.chemins { display: flex; flex-direction: column; gap: 3px; }
.chemin { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.sens {
  font-size: 11px; color: var(--text-faint); flex: 0 0 30px; text-align: right;
}
.sens.vers { color: var(--text-dim); }
.chemin code {
  font-family: var(--mono); font-size: var(--t-xs); color: var(--text-dim);
  /* Un chemin de rangement dépasse cent caractères : sans césure il pousse la
     page en largeur au lieu de se replier. */
  overflow-wrap: anywhere; min-width: 0;
}

.actions { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex-shrink: 0; }
.portee { font-size: 11px; color: var(--text-faint); text-align: right; }

/* --- Pagination ----------------------------------------------------------- */

.pagination { display: flex; align-items: center; justify-content: center; gap: 14px; flex-wrap: wrap; padding-top: 4px; }
.pagination button { font-size: var(--t-xs); padding: 6px 13px; min-height: 32px; }
.position { font-size: var(--t-xs); color: var(--text-dim); }
.bord { margin: 0; text-align: center; font-size: 11px; color: var(--text-faint); }

/* --- Écrans étroits ------------------------------------------------------- *
 * 700 px : la largeur à laquelle une ligne d'entrée déborde, pas celle d'une
 * tablette générique. */
@media (max-width: 700px) {
  /* Les actions passent sous le contenu : à droite d'un chemin de cent
     caractères, elles ne laissaient plus de place ni à l'un ni à l'autre. */
  .entree { flex-direction: column; align-items: stretch; gap: 10px; }
  .actions { align-items: flex-start; }
  .portee { text-align: left; }

  /* La cible se gagne au rembourrage : grossir la police déplacerait toute la
     hiérarchie typographique de l'écran pour un problème de doigt. */
  .natures button, button.petit { min-height: 32px; padding: 7px 12px; }
  .champ { flex: 1 0 100%; }
  .champ input { flex: 1; min-width: 0; min-height: 32px; }

  /* Le sens quitte sa colonne fixe : trente pixels perdus à gauche sur un
     téléphone, c'est une ligne de chemin de plus à faire tenir. */
  .chemin { flex-direction: column; gap: 0; }
  .sens { text-align: left; flex: none; }
  .chemin code { overflow-wrap: anywhere; }
}
</style>
