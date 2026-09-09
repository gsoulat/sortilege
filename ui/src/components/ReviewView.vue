<script setup>
import { ref, computed, onMounted } from 'vue'
import CandidatePicker from './CandidatePicker.vue'

const data = ref(null)
const planning = ref(false)
const applying = ref(false)
const message = ref(null)
const error = ref(null)
const results = ref(null)
const selected = ref(new Set())
const progress = ref(null)

// Seuil de lecture, pas de decision : il ne change rien au traitement, il
// choisit seulement ce qu'on regarde. 80 % correspond au seuil de sollicitation
// de l'IA, donc a la frontiere entre « l'outil a su » et « il a hesite ».
const SCORE_CUT = 0.8
const scoreFilter = ref('all')

function passesScore(p) {
  if (scoreFilter.value === 'high') return p.score >= SCORE_CUT
  if (scoreFilter.value === 'low') return p.score < SCORE_CUT
  return true
}

const scoreCounts = computed(() => {
  const all = [...(data.value?.auto ?? []), ...(data.value?.items ?? []), ...(data.value?.rejected ?? [])]
  return {
    all: all.length,
    high: all.filter((p) => p.score >= SCORE_CUT).length,
    low: all.filter((p) => p.score < SCORE_CUT).length,
  }
})

// Score decroissant partout : ce dont l'outil est le plus sur se traite en
// premier, et l'ordre du scan n'a aucune valeur pour l'arbitrage.
const byScore = (list) => [...list].sort((a, b) => b.score - a.score)

const autoPlans = computed(() => byScore((data.value?.auto ?? []).filter(passesScore)))
// Les identifiants explicites, et non « tout ce qui est automatique » : sous
// filtre, le bouton annoncait douze plans et le serveur en aurait applique
// quarante. Un bouton qui ment sur ce qu'il deplace est pire qu'un bouton
// absent.
const autoIds = computed(() => autoPlans.value.map((p) => p.id))
const rejectedPlans = computed(() => byScore((data.value?.rejected ?? []).filter(passesScore)))

// Ce qui demande un arbitrage est REPLIE tant qu'il reste des plans surs a
// appliquer : sans cela, une file de plusieurs centaines de lignes noie les
// quelques clics qui traitent l'essentiel.
//
// Mais seulement TANT QU'IL Y EN A. Un lot ou rien n'atteint le seuil
// automatique restait replie : aucune case atteignable, donc aucune selection,
// donc pas de bouton « Executer » — le lot etait inexploitable. C'est `load()`
// qui reevalue, a chaque fois que la file change.
const reviewCollapsed = ref(true)

const percent = computed(() => {
  const p = progress.value
  if (!p || !p.total) return 0
  return Math.min(100, Math.round((p.processed / p.total) * 100))
})

function humanDuration(seconds) {
  if (seconds == null) return null
  if (seconds < 60) return `${Math.round(seconds)} s`
  const m = Math.floor(seconds / 60)
  return `${m} min ${String(Math.round(seconds % 60)).padStart(2, '0')}`
}

const hasPlans = computed(() => (data.value?.counts?.auto ?? 0) + (data.value?.counts?.review ?? 0) > 0)

async function load() {
  data.value = await (await fetch('/api/review')).json()
  reviewCollapsed.value = (data.value?.auto ?? []).length > 0
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

/**
 * Le calcul interroge TheMovieDB pour chaque œuvre : sur une vraie
 * bibliothèque cela dure des minutes. On suit son avancement au lieu de figer
 * le bouton — la requête reste ouverte, mais le serveur répond en parallèle.
 */
let poller = null

async function plan({ reset = false } = {}) {
  planning.value = true
  results.value = null
  progress.value = null

  // Le calcul tourne cote serveur ; le POST rend la main tout de suite. On
  // suit son etat, comme pour le scan — une requete ouverte plusieurs minutes
  // finirait par expirer et le resultat serait perdu.
  const started = await call(`/api/review/plan?limit=100&reset=${reset}`)
  if (!started) {
    clearInterval(poller)
    poller = null
    planning.value = false
    return
  }

  poller = setInterval(async () => {
    try {
      const status = await (await fetch('/api/review/plan/status')).json()
      progress.value = status

      if (status.error) {
        error.value = status.error
      }
      if (!status.running) {
        clearInterval(poller)
        poller = null
        planning.value = false
        progress.value = null
        await load()
      }
    } catch {
      clearInterval(poller)
      poller = null
      planning.value = false
      error.value = 'Contact perdu avec le serveur pendant le calcul.'
    }
  }, 700)
}

/**
 * `dryRun` vient du bouton cliqué, plus d'une variable d'environnement : on
 * simule puis on exécute la même sélection sans redémarrer le conteneur.
 * SORTILEGE_DRY_RUN reste un verrou côté serveur, qui ne peut que refuser.
 */
async function apply(ids, { includeReview = false, dryRun = true } = {}) {
  applying.value = true
  try {
    const out = await call('/api/review/apply', {
      plan_ids: ids,
      include_review: includeReview,
      dry_run: dryRun,
    })
    if (!out) return

    results.value = out
    if (out.dry_run) {
      message.value = `Simulation : ${out.applied} déplacement(s) possible(s), ${out.failed} bloqué(s).`
    } else {
      message.value = `${out.applied} fichier(s) rangé(s), ${out.failed} en échec.`
    }
    // La cause domine le compte : « 340 en échec » ne dit pas quoi faire,
    // « 340 en échec, tous parce que la destination existe déjà » si.
    if (out.failed) {
      const [first] = failureGroups.value
      if (first) {
        const tous = first.items.length === out.failed ? 'tous' : `dont ${first.items.length}`
        message.value += ` — ${tous} : ${first.label.toLowerCase()}.`
      }
    }
    // Une exécution vide la sélection ; une simulation la garde, pour qu'on
    // puisse enchaîner sur l'exécution des mêmes lignes.
    if (!out.dry_run) selected.value = new Set()
    await load()
  } finally {
    applying.value = false
  }
}

async function undo(count) {
  const out = await call('/api/review/undo', { count })
  if (out) {
    results.value = null
    message.value = `${out.undone} opération(s) annulée(s), ${out.failed} en échec.`
    await Promise.all([load(), loadJournal()])
  }
}

function toggle(id) {
  const next = new Set(selected.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selected.value = next
}

/**
 * Les épisodes sont regroupés par série. Sur une bibliothèque réelle la file
 * contient des centaines de lignes pour quelques dizaines de séries : cocher
 * une par une serait inutilisable, et surtout la décision est la même pour
 * toute la série — soit l'identification est bonne, soit elle ne l'est pas.
 */
const reviewGroups = computed(() => {
  const movies = []
  const shows = new Map()

  for (const p of (data.value?.items ?? []).filter(passesScore)) {
    if (p.kind === 'movie') {
      movies.push(p)
      continue
    }
    const key = p.title || '(titre non identifié)'
    if (!shows.has(key)) shows.set(key, [])
    shows.get(key).push(p)
  }

  return {
    movies: byScore(movies),
    shows: [...shows.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([title, plans]) => ({
        title,
        plans: byScore(plans),
        year: plans[0].year,
        poster: plans[0].poster_url,
        score: Math.max(...plans.map((p) => p.score)),
      }))
      .sort((a, b) => b.score - a.score),
  }
})

// Quel plan a son sélecteur de candidats ouvert.
const picking = ref(null)
const choosing = ref(false)

/**
 * Impose un candidat choisi à l'œil. Entre deux « Dark Matter » — 2015 et 2024 —
 * aucun signal automatique ne tranche : même titre, même type, deux œuvres
 * réelles. L'affiche, elle, tranche en une seconde.
 */
async function choose(planId, candidate) {
  choosing.value = true
  try {
    const out = await call(`/api/review/${planId}/choose`, {
      provider: candidate.provider,
      external_id: candidate.external_id,
    })
    if (out) {
      data.value = out.queue
      picking.value = null
      const n = out.corrected ?? 1
      message.value =
        `Identifié comme « ${candidate.title} »${candidate.year ? ` (${candidate.year})` : ''}` +
        (n > 1 ? ` — ${n} épisodes corrigés.` : '.')
    }
  } finally {
    choosing.value = false
  }
}

// Toutes les lignes d'arbitrage actuellement visibles, filtre compris.
const visibleReview = computed(() => [
  ...reviewGroups.value.shows.flatMap((s) => s.plans),
  ...reviewGroups.value.movies,
])

function groupState(plans) {
  const picked = plans.filter((p) => selected.value.has(p.id)).length
  if (picked === 0) return 'none'
  return picked === plans.length ? 'all' : 'some'
}

function toggleGroup(plans) {
  const next = new Set(selected.value)
  // Tout décocher seulement si TOUT était coché : une sélection partielle
  // signifie qu'on était en train de composer, on la complète.
  const complete = groupState(plans) === 'all'
  for (const p of plans) {
    complete ? next.delete(p.id) : next.add(p.id)
  }
  selected.value = next
}

const shortPath = (p) => (p ? p.split('/').slice(-3).join('/') : '—')

/**
 * Ce qu'il faut dire quand une application échoue, et ce qu'on peut y faire.
 * Un compte d'échecs sans cause n'est pas exploitable : trois cents lignes
 * disant chacune « déplacement impossible : /un/chemin/différent » se lisent
 * exactement comme une seule.
 */
const REASONS = {
  destination_exists: {
    label: 'La destination existe déjà',
    fix: "Ces fichiers sont déjà rangés : seule une copie traîne encore dans les téléchargements. Rien n'a été écrasé — c'est volontaire. Tu peux évacuer ces copies vers la corbeille ; celles dont la taille diffère du fichier rangé seront refusées, car ce n'est alors pas le même fichier.",
    action: 'evacuate',
  },
  source_missing: {
    label: 'Fichier source introuvable',
    fix: 'Le fichier a bougé depuis le calcul du plan. Relance un scan, puis « Recommencer » pour repartir sur des plans à jour.',
  },
  permission_denied: {
    label: 'Permission refusée',
    fix: "Le conteneur n'a pas le droit d'écrire dans la bibliothèque. Vérifie PUID / PGID et le propriétaire du dossier de destination sur le NAS.",
  },
  move_failed: {
    label: 'Déplacement impossible',
    fix: 'Erreur système au moment du déplacement — disque plein, volume en lecture seule, ou chemin trop long.',
  },
  no_destination: {
    label: 'Aucune destination calculée',
    fix: "L'identification n'a rien donné pour ces fichiers ; ils ne peuvent pas être rangés automatiquement.",
  },
}

/** Les échecs, groupés par cause, du plus fréquent au moins fréquent. */
const failureGroups = computed(() => {
  const failed = (results.value?.results ?? []).filter((r) => !r.ok)
  const byReason = new Map()
  for (const r of failed) {
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
      // Le message complet du premier : il porte le détail système (errno,
      // chemin) que le libellé générique ne peut pas donner.
      sample: items[0].message,
    }))
    .sort((a, b) => b.items.length - a.items.length)
})

const openReason = ref(null)
const evacuating = ref(false)
const evacProgress = ref(null)

const evacPercent = computed(() => {
  const p = evacProgress.value
  if (!p || !p.total) return 0
  return Math.min(100, Math.round((p.processed / p.total) * 100))
})

/**
 * Met en corbeille les copies dont le fichier est déjà rangé. Jamais une
 * suppression : l'opération est journalisée, donc annulable, et une source de
 * taille différente est refusée plutôt que confondue avec un doublon.
 *
 * Le travail tourne côté serveur et on en suit l'avancement. Trois cents
 * déplacements durent assez longtemps pour qu'un bouton figé ne dise plus rien,
 * et une requête synchrone aussi longue finirait par expirer alors que le
 * serveur, lui, a fini.
 */
let evacPoller = null

async function evacuate(group) {
  evacuating.value = true
  evacProgress.value = null

  const started = await call('/api/review/evacuate', {
    plan_ids: group.items.map((r) => r.plan_id),
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
        message.value =
          `${status.evacuated} copie(s) mise(s) en corbeille` +
          (status.failed
            ? `, ${status.failed} refusée(s) — taille différente ou fichier absent.`
            : '.')
        results.value = status.failed ? { results: status.results } : null
        await Promise.all([load(), loadJournal()])
      }
    } catch {
      clearInterval(evacPoller)
      evacPoller = null
      evacuating.value = false
      error.value = 'Contact perdu avec le serveur pendant l\'évacuation.'
    }
  }, 700)
}

// --- Annulation ciblée ---------------------------------------------------
//
// « Tout annuler (703) » est un aveu : il suppose qu'on veuille défaire une
// session entière, alors qu'en pratique on veut défaire UNE série mal
// identifiée au milieu de sept cents déplacements corrects.
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

async function undoWork(work) {
  undoing.value = work.key
  try {
    const out = await call('/api/review/undo', { work: work.key })
    if (out) {
      message.value = `« ${work.title} » : ${out.undone} déplacement(s) annulé(s)` +
        (out.failed ? `, ${out.failed} en échec.` : '.')
      results.value = null
      await Promise.all([load(), loadJournal()])
    }
  } finally {
    undoing.value = null
  }
}

async function toggleUndo() {
  showUndo.value = !showUndo.value
  if (showUndo.value) await loadJournal()
}

onMounted(load)
</script>

<template>
  <div v-if="data" class="review">
    <div class="toolbar">
      <button class="primary" :disabled="planning" @click="plan()">
        {{
          planning
            ? 'Identification en cours…'
            : data?.planned
              ? `Traiter les ${Math.min(100, data.remaining)} suivants`
              : 'Calculer les plans'
        }}
      </button>
      <span v-if="data?.remaining" class="remaining">
        {{ data.remaining }} fichier{{ data.remaining > 1 ? 's' : '' }} en attente
      </span>
      <span v-else-if="data?.planned" class="remaining done">Tout est planifié</span>
      <button
        v-if="data?.planned"
        class="reset"
        :disabled="planning"
        title="Vide la file et repart du premier fichier"
        @click="plan({ reset: true })"
      >Recommencer</button>
      <button
        v-if="data.journal_size"
        class="undo"
        :class="{ open: showUndo }"
        @click="toggleUndo"
      >Annuler… ({{ data.journal_size }})</button>
    </div>

    <!-- Ce qui a été rangé, par œuvre, avec une annulation par ligne -->
    <section v-if="showUndo" class="undo-panel">
      <div class="head">
        <h3>Annuler un rangement</h3>
        <button
          v-if="data.journal_size"
          class="danger"
          @click="undo(data.journal_size)"
        >Tout annuler ({{ data.journal_size }})</button>
      </div>
      <p class="note">
        Les fichiers retournent à leur emplacement d'origine. Rien n'est supprimé, et
        une origine déjà occupée fait échouer le retour plutôt que d'écraser.
      </p>

      <p v-if="!journal" class="empty">Lecture du journal…</p>
      <p v-else-if="!journal.works.length" class="empty">Aucun déplacement à annuler.</p>

      <ul v-else class="works">
        <li v-for="w in journal.works" :key="w.key">
          <div class="body">
            <div class="title">
              {{ w.title }}
              <span v-if="w.work_kind" class="kind">{{ WORK_KINDS[w.work_kind] ?? w.work_kind }}</span>
            </div>
            <div class="meta">
              {{ w.files }} fichier{{ w.files > 1 ? 's' : '' }}
              <span v-if="w.companions">+ {{ w.companions }} associé{{ w.companions > 1 ? 's' : '' }}</span>
              <code>{{ shortPath(w.sample) }}</code>
            </div>
          </div>
          <button
            class="undo-one"
            :disabled="undoing === w.key"
            @click="undoWork(w)"
          >{{ undoing === w.key ? 'Annulation…' : 'Annuler' }}</button>
        </li>
      </ul>
    </section>

    <div v-if="planning && progress?.total" class="progress">
      <div class="bar"><div class="fill" :style="{ width: percent + '%' }"></div></div>
      <div class="stats">
        <span class="phase">{{ progress.processed }} / {{ progress.total }}</span>
        <span class="pct">{{ percent }} %</span>
        <span v-if="progress.eta != null" class="eta">
          environ {{ humanDuration(progress.eta) }} restantes
        </span>
        <span class="elapsed">{{ humanDuration(progress.elapsed) }} écoulées</span>
      </div>
      <div v-if="progress.current" class="current">{{ progress.current }}</div>
    </div>

    <div v-if="hasPlans" class="score-filters">
      <button :class="{ active: scoreFilter === 'all' }" @click="scoreFilter = 'all'">
        Tout ({{ scoreCounts.all }})
      </button>
      <button class="ok" :class="{ active: scoreFilter === 'high' }" @click="scoreFilter = 'high'">
        ≥ 80 % ({{ scoreCounts.high }})
      </button>
      <button class="warn" :class="{ active: scoreFilter === 'low' }" @click="scoreFilter = 'low'">
        &lt; 80 % ({{ scoreCounts.low }})
      </button>
    </div>

    <p v-if="error" class="err-msg">{{ error }}</p>
    <p v-if="message" class="ok-msg">{{ message }}</p>

    <!-- Pourquoi ça a échoué. En haut, pas en bas : chercher la cause sous
         trois cents lignes de plans revient à ne pas la donner. -->
    <section v-if="failureGroups.length" class="failures">
      <h3>Ce qui a bloqué</h3>
      <ul>
        <li v-for="g in failureGroups" :key="g.key">
          <button class="head" @click="openReason = openReason === g.key ? null : g.key">
            <span class="chev" :class="{ closed: openReason !== g.key }">▾</span>
            <span class="count">{{ g.items.length }}</span>
            <span class="label">{{ g.label }}</span>
          </button>
          <p class="fix">{{ g.fix }}</p>
          <button
            v-if="g.action === 'evacuate'"
            class="act"
            :disabled="evacuating"
            @click="evacuate(g)"
          >{{ evacuating ? 'Évacuation…' : `Mettre ces ${g.items.length} copies en corbeille` }}</button>
          <div v-if="g.action === 'evacuate' && evacProgress" class="evac">
            <div class="bar"><div class="fill" :style="{ width: evacPercent + '%' }"></div></div>
            <div class="stats">
              <span>{{ evacProgress.processed }} / {{ evacProgress.total }}</span>
              <span class="ok-count">{{ evacProgress.evacuated }} en corbeille</span>
              <span v-if="evacProgress.failed" class="ko-count">
                {{ evacProgress.failed }} refusée(s)
              </span>
              <span v-if="evacProgress.current" class="current">{{ evacProgress.current }}</span>
            </div>
          </div>
          <code class="sample">{{ g.sample }}</code>
          <ul v-if="openReason === g.key" class="files">
            <li v-for="(r, i) in g.items.slice(0, 50)" :key="i">
              <code>{{ shortPath(r.source) }}</code>
            </li>
            <li v-if="g.items.length > 50" class="more">
              … et {{ g.items.length - 50 }} autres
            </li>
          </ul>
        </li>
      </ul>
    </section>

    <!-- Ce qui manque, quand rien n'est calculable -->
    <ol v-if="data.blockers.length && !hasPlans" class="blockers">
      <li v-for="(b, i) in data.blockers" :key="i">
        <div class="head"><span class="num">{{ i + 1 }}</span><span class="title">{{ b.title }}</span></div>
        <p class="detail">{{ b.detail }}</p>
        <code class="where">{{ b.where }}</code>
      </li>
    </ol>

    <div v-else-if="!hasPlans" class="empty">
      <h3>Aucun plan</h3>
      <p>Lance un scan puis « Calculer les plans ».</p>
    </div>

    <template v-else>
      <!-- Application automatique -->
      <section v-if="autoPlans.length" class="group auto">
        <div class="group-head">
          <h3>Assez sûr pour être appliqué seul</h3>
          <span class="count">{{ autoPlans.length }}</span>
          <button :disabled="applying" @click="apply(autoIds, { dryRun: true })">
            Simuler
          </button>
          <button class="primary" :disabled="applying" @click="apply(autoIds, { dryRun: false })">
            Exécuter ces {{ autoPlans.length }}
          </button>
        </div>
        <ul class="plans">
          <li v-for="p in autoPlans" :key="p.id">
            <div class="line">
              <img v-if="p.poster_url" class="thumb" :src="p.poster_url" :alt="p.title" loading="lazy" />
              <span v-else class="thumb empty"></span>
              <span class="score ok">{{ (p.score * 100).toFixed(0) }}</span>
              <span class="title">{{ p.title }}<span v-if="p.year" class="year"> ({{ p.year }})</span></span>
            </div>
            <div class="move">
              <code class="from">{{ shortPath(p.source) }}</code>
              <span class="arrow">→</span>
              <code class="to">{{ shortPath(p.destination) }}</code>
            </div>
          </li>
        </ul>
      </section>

      <!-- Arbitrage humain -->
      <section v-if="data.items.length" class="group review-group">
        <div class="group-head">
          <button class="collapse" @click="reviewCollapsed = !reviewCollapsed">
            <span class="chev" :class="{ closed: reviewCollapsed }">▾</span>
            <h3>En attente de ton arbitrage</h3>
          </button>
          <span class="count">{{ data.items.length }}</span>
          <button
            v-if="visibleReview.length"
            class="select-all"
            @click="toggleGroup(visibleReview)"
          >
            {{ groupState(visibleReview) === 'all' ? 'Tout décocher' : `Tout cocher (${visibleReview.length})` }}
          </button>
          <template v-if="selected.size">
            <button :disabled="applying" @click="apply([...selected], { dryRun: true })">
              Simuler ({{ selected.size }})
            </button>
            <button class="primary" :disabled="applying" @click="apply([...selected], { dryRun: false })">
              Exécuter ({{ selected.size }})
            </button>
          </template>
        </div>
        <!-- Séries : une case coche toute la série -->
        <template v-if="!reviewCollapsed">
        <div v-for="show in reviewGroups.shows" :key="show.title" class="show">
          <label class="show-head">
            <input
              type="checkbox"
              :checked="groupState(show.plans) === 'all'"
              :indeterminate.prop="groupState(show.plans) === 'some'"
              @change="toggleGroup(show.plans)"
            />
            <img v-if="show.poster" class="thumb" :src="show.poster" :alt="show.title" loading="lazy" />
            <span v-else class="thumb empty"></span>
            <span class="show-title">{{ show.title }}</span>
            <span v-if="show.year" class="year">({{ show.year }})</span>
            <span class="count">{{ show.plans.length }} épisode{{ show.plans.length > 1 ? 's' : '' }}</span>
            <button
              v-if="show.plans[0].alternatives.length"
              class="fix"
              title="Ce n'est pas la bonne œuvre"
              @click.prevent="picking = picking === show.plans[0].id ? null : show.plans[0].id"
            >Ce n'est pas ça</button>
          </label>

          <CandidatePicker
            v-if="picking === show.plans[0].id"
            :candidates="show.plans[0].alternatives"
            :busy="choosing"
            @choose="choose(show.plans[0].id, $event)"
            @close="picking = null"
          />

          <ul class="plans nested">
            <li v-for="p in show.plans" :key="p.id" :class="{ picked: selected.has(p.id) }">
              <label class="line">
                <input type="checkbox" :checked="selected.has(p.id)" @change="toggle(p.id)" />
                <span class="score mid">{{ (p.score * 100).toFixed(0) }}</span>
                <code class="to">{{ shortPath(p.destination) }}</code>
              </label>
              <ul class="reasons">
                <li v-for="(r, i) in p.reasons" :key="i">{{ r }}</li>
              </ul>
            </li>
          </ul>
        </div>

        <!-- Films : à plat, chaque décision est indépendante -->
        <ul v-if="reviewGroups.movies.length" class="plans">
          <li v-for="p in reviewGroups.movies" :key="p.id" :class="{ picked: selected.has(p.id) }">
            <label class="line">
              <input type="checkbox" :checked="selected.has(p.id)" @change="toggle(p.id)" />
              <img v-if="p.poster_url" class="thumb" :src="p.poster_url" :alt="p.title" loading="lazy" />
              <span v-else class="thumb empty"></span>
              <span class="score mid">{{ (p.score * 100).toFixed(0) }}</span>
              <span class="title">{{ p.title }}<span v-if="p.year" class="year"> ({{ p.year }})</span></span>
              <span class="provider">{{ p.provider }}</span>
            </label>
            <div class="move">
              <code class="from">{{ shortPath(p.source) }}</code>
              <span class="arrow">→</span>
              <code class="to">{{ shortPath(p.destination) }}</code>
              <button
                v-if="p.alternatives.length"
                class="fix"
                @click="picking = picking === p.id ? null : p.id"
              >Ce n'est pas ça</button>
            </div>
            <ul class="reasons">
              <li v-for="(r, i) in p.reasons" :key="i">{{ r }}</li>
            </ul>
            <CandidatePicker
              v-if="picking === p.id"
              :candidates="p.alternatives"
              :busy="choosing"
              @choose="choose(p.id, $event)"
              @close="picking = null"
            />
          </li>
        </ul>
        </template>
      </section>

      <!-- Écartés -->
      <details v-if="rejectedPlans.length" class="group rejected">
        <summary>{{ rejectedPlans.length }} fichier(s) écarté(s)</summary>
        <ul class="plans">
          <li v-for="p in rejectedPlans" :key="p.id">
            <div class="line">
              <span class="score low">{{ (p.score * 100).toFixed(0) }}</span>
              <span class="title">{{ p.title || p.filename }}</span>
            </div>
            <div class="move"><code class="from">{{ shortPath(p.source) }}</code></div>
            <ul class="reasons">
              <li v-for="(r, i) in p.reasons" :key="i">{{ r }}</li>
            </ul>
          </li>
        </ul>
      </details>
    </template>

    <!-- Détail ligne à ligne. Les échecs sont déjà résumés en haut ; ici on
         garde la trace de ce qui est réellement parti. -->
    <section v-if="results" class="group results">
      <h3>Détail</h3>
      <ul class="plans">
        <li v-for="(r, i) in results.results" :key="i" :class="{ failed: !r.ok }">
          <div class="line">
            <span class="dot" :class="{ ko: !r.ok }"></span>
            <span class="msg">{{ r.message }}</span>
          </div>
          <div class="move"><code class="from">{{ shortPath(r.source) }}</code></div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.review { display: flex; flex-direction: column; gap: 16px; }

.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
button.primary {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  border-color: var(--accent-dim); color: var(--accent);
}
.collapse { border: none; background: none; padding: 0; display: flex; align-items: center; gap: 7px; }
.collapse:hover h3 { color: var(--text); }
.chev { color: var(--text-faint); font-size: 10px; transition: transform .15s; }
.chev.closed { transform: rotate(-90deg); }

.score-filters { display: flex; gap: 4px; flex-wrap: wrap; }
.score-filters button { font-size: 12px; padding: 4px 10px; }
.score-filters button.ok { color: var(--ok); }
.score-filters button.warn { color: var(--warn); }
.score-filters button.active { background: var(--surface-2); border-color: var(--accent-dim); }

.remaining { font-size: 12.5px; color: var(--text-dim); }
.remaining.done { color: var(--ok); }
.reset { font-size: 11.5px; padding: 3px 9px; color: var(--text-faint); }

.undo { margin-left: auto; font-size: 12px; color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }

.badge {
  font-size: 11px; padding: 3px 9px; border-radius: 20px; border: 1px solid var(--border);
}
.badge.warn {
  color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}

.err-msg, .ok-msg { margin: 0; font-size: 13px; border-radius: 7px; padding: 9px 12px; }
.err-msg { color: var(--err); background: color-mix(in srgb, var(--err) 8%, transparent); border: 1px solid color-mix(in srgb, var(--err) 30%, transparent); }
.ok-msg { color: var(--ok); background: color-mix(in srgb, var(--ok) 8%, transparent); border: 1px solid color-mix(in srgb, var(--ok) 25%, transparent); }

/* --- Progression du calcul --- */
.progress { background: var(--surface); border: 1px solid var(--accent-dim); border-radius: 9px; padding: 12px 14px; }
.bar { height: 4px; border-radius: 2px; overflow: hidden; background: var(--surface-2); margin-bottom: 9px; }
.fill { height: 100%; background: var(--accent); transition: width .4s ease; }
.stats { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; font-size: 12.5px; }
.stats .phase { font-family: var(--mono); color: var(--text); }
.stats .pct { font-family: var(--mono); color: var(--accent); }
.stats .eta, .stats .elapsed { color: var(--text-faint); font-size: 11.5px; }
.current { margin-top: 6px; font-family: var(--mono); font-size: 11px; color: var(--text-faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.group {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 15px 17px;
}
.group.auto { border-left: 2px solid var(--ok); }
.group.review-group { border-left: 2px solid var(--warn); }

.group-head { display: flex; align-items: center; gap: 11px; margin-bottom: 12px; }
h3 { margin: 0; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--text-dim); }
.count { font-size: 11px; color: var(--text-faint); }
.select-all { font-size: 11.5px; padding: 3px 10px; color: var(--text-dim); }
.group-head button { font-size: 12px; }
.group-head button:nth-of-type(1) { margin-left: auto; }
.group-head template + button { margin-left: 0; }

/* --- Séries groupées dans la file --- */
.show { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; overflow: hidden; }
.show-head {
  display: flex; align-items: center; gap: 9px;
  padding: 8px 12px; background: var(--surface-2); cursor: pointer;
}
.show-head input { width: auto; }
.show-title { font-weight: 500; font-size: 13.5px; }
.show-head .count { margin-left: auto; font-size: 11px; color: var(--text-faint); }
.plans.nested { padding: 6px 12px 9px 32px; }
.plans.nested > li { border-top: none; padding: 3px 0; }

.thumb {
  width: 30px; height: 45px; flex: none; border-radius: 3px;
  object-fit: cover; background: var(--surface-2);
}
.thumb.empty { display: inline-block; border: 1px dashed var(--border); }

.plans { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 9px; }
.plans > li { padding: 8px 0; border-top: 1px solid color-mix(in srgb, var(--border) 55%, transparent); }
.plans > li:first-child { border-top: none; padding-top: 0; }
.plans > li.picked { background: color-mix(in srgb, var(--accent) 5%, transparent); }

.line { display: flex; align-items: center; gap: 9px; cursor: default; }
label.line { cursor: pointer; }
.line input { width: auto; }

.score {
  font-family: var(--mono); font-size: 11px; min-width: 26px;
  padding: 1px 5px; border-radius: 4px; text-align: center; flex: none;
}
.score.ok { color: var(--ok); border: 1px solid color-mix(in srgb, var(--ok) 30%, transparent); }
.score.mid { color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent); }
.score.low { color: var(--text-faint); border: 1px solid var(--border); }

.line .title { font-size: 13.5px; font-weight: 500; }
.year { color: var(--text-faint); font-weight: 400; }
.provider { margin-left: auto; font-size: 10px; color: var(--text-faint); }

.move { display: flex; align-items: center; gap: 7px; margin: 4px 0 0 35px; flex-wrap: wrap; }
code {
  font-family: var(--mono); font-size: 11px; padding: 1px 5px; border-radius: 3px;
  background: var(--surface-2); word-break: break-all;
}
.from { color: var(--text-faint); }
.to { color: var(--ok); }
.arrow { color: var(--text-faint); font-size: 11px; }

.reasons { list-style: none; margin: 5px 0 0 35px; padding: 0; }
.reasons li { font-size: 11px; color: var(--text-faint); line-height: 1.5; }

.rejected summary { font-size: 12.5px; color: var(--text-dim); cursor: pointer; }
.rejected[open] summary { margin-bottom: 11px; }

.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); flex: none; }
.dot.ko { background: var(--err); }
.msg { font-size: 12.5px; }
.results li.failed .msg { color: var(--err); }

.undo-panel {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}
.undo-panel .head { display: flex; align-items: center; gap: 12px; }
.undo-panel > .head h3 {
  margin: 0; flex: 1; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}
.undo-panel .danger { font-size: 11.5px; padding: 3px 10px; color: var(--text-faint); }
.undo-panel .danger:hover { color: var(--err); border-color: color-mix(in srgb, var(--err) 30%, transparent); }
.undo-panel .note { margin: 9px 0 12px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.undo-panel .empty { margin: 0; font-size: 12.5px; color: var(--text-faint); font-style: italic; }

.works { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
.works li { display: flex; align-items: center; gap: 12px; }
.works .body { flex: 1; min-width: 0; }
.works .title { font-size: 13px; display: flex; align-items: baseline; gap: 8px; }
.works .kind {
  font-size: 10px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--text-faint); border: 1px solid var(--border);
  border-radius: 3px; padding: 0 5px;
}
.works .meta { display: flex; gap: 9px; align-items: baseline; margin-top: 2px; font-size: 11px; color: var(--text-faint); flex-wrap: wrap; }
.works .meta code { font-family: var(--mono); font-size: 10.5px; }
.undo-one { font-size: 11.5px; padding: 3px 12px; flex: none; }
.undo-one:hover:not(:disabled) { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }

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
  border: none; background: none; padding: 0; display: flex; align-items: center;
  gap: 9px; font-size: 13.5px; color: var(--text); cursor: pointer;
}
.failures .chev { font-size: 10px; color: var(--text-faint); transition: transform .15s; }
.failures .chev.closed { transform: rotate(-90deg); }
.failures .count {
  font-family: var(--mono); font-size: 11.5px; padding: 1px 7px; border-radius: 4px;
  background: color-mix(in srgb, var(--err) 18%, transparent); color: var(--err);
}
.failures .fix { margin: 6px 0 0 28px; font-size: 12px; color: var(--text-dim); line-height: 1.6; max-width: 680px; }
.evac { margin: 10px 0 0 28px; max-width: 680px; }
.evac .bar { height: 3px; background: var(--surface-2); border-radius: 2px; overflow: hidden; }
.evac .fill { height: 100%; background: var(--warn); transition: width .3s; }
.evac .stats { display: flex; gap: 12px; align-items: baseline; margin-top: 6px; font-size: 11.5px; color: var(--text-faint); flex-wrap: wrap; }
.evac .ok-count { color: var(--ok); }
.evac .ko-count { color: var(--err); }
.evac .current { font-family: var(--mono); font-size: 10.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.failures .act { margin: 9px 0 0 28px; font-size: 12px; padding: 4px 12px; }
.failures .act:hover:not(:disabled) { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, transparent); }
.failures .sample {
  display: block; margin: 6px 0 0 28px; font-family: var(--mono);
  font-size: 11px; color: var(--text-faint);
}
.failures .files { list-style: none; margin: 9px 0 0 28px; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.failures .files code { font-family: var(--mono); font-size: 11px; color: var(--text-faint); }
.failures .files .more { font-size: 11.5px; color: var(--text-faint); font-style: italic; }

.fix {
  font-size: 10.5px; padding: 2px 8px; margin-left: 8px;
  color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent);
  background: none;
}
.fix:hover { background: color-mix(in srgb, var(--warn) 10%, transparent); }

.blockers { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.blockers li {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 2px solid var(--warn); border-radius: 8px; padding: 13px 16px;
}
.head { display: flex; align-items: center; gap: 9px; }
.num {
  width: 18px; height: 18px; flex: none; border-radius: 50%; display: grid; place-items: center;
  font-size: 10px; background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn);
}
.head .title { font-weight: 500; font-size: 13.5px; }
.detail { margin: 7px 0 9px 27px; font-size: 12.5px; color: var(--text-dim); line-height: 1.6; max-width: 620px; }
.where { margin-left: 27px; display: inline-block; }

.empty {
  background: var(--surface); border: 1px dashed var(--border);
  border-radius: 10px; padding: 30px; text-align: center;
}
.empty h3 { text-transform: none; letter-spacing: 0; font-size: 14px; color: var(--text); margin-bottom: 7px; }
.empty p { margin: 0; font-size: 13px; color: var(--text-dim); }
</style>
