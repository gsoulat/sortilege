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

async function plan() {
  planning.value = true
  results.value = null
  progress.value = null

  poller = setInterval(async () => {
    try {
      const status = await (await fetch('/api/review/plan/status')).json()
      progress.value = status
      if (!status.running && status.total) clearInterval(poller)
    } catch {
      clearInterval(poller)
    }
  }, 700)

  try {
    const out = await call('/api/review/plan')
    if (out) data.value = out
  } finally {
    clearInterval(poller)
    poller = null
    planning.value = false
    progress.value = null
  }
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
    await load()
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

  for (const p of data.value?.items ?? []) {
    if (p.kind === 'movie') {
      movies.push(p)
      continue
    }
    const key = p.title || '(titre non identifié)'
    if (!shows.has(key)) shows.set(key, [])
    shows.get(key).push(p)
  }

  return {
    movies,
    shows: [...shows.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([title, plans]) => ({ title, plans, year: plans[0].year })),
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
      message.value = `Identifié comme « ${candidate.title} »${candidate.year ? ` (${candidate.year})` : ''}.`
    }
  } finally {
    choosing.value = false
  }
}

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

onMounted(load)
</script>

<template>
  <div v-if="data" class="review">
    <div class="toolbar">
      <button class="primary" :disabled="planning" @click="plan">
        {{ planning ? 'Identification en cours…' : 'Calculer les plans' }}
      </button>
      <button
        v-if="data.journal_size"
        class="undo"
        @click="undo(data.journal_size)"
      >Tout annuler ({{ data.journal_size }})</button>
    </div>

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

    <p v-if="error" class="err-msg">{{ error }}</p>
    <p v-if="message" class="ok-msg">{{ message }}</p>

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
      <section v-if="data.auto.length" class="group auto">
        <div class="group-head">
          <h3>Assez sûr pour être appliqué seul</h3>
          <span class="count">{{ data.auto.length }}</span>
          <button :disabled="applying" @click="apply(null, { dryRun: true })">
            Simuler
          </button>
          <button class="primary" :disabled="applying" @click="apply(null, { dryRun: false })">
            Exécuter ces {{ data.auto.length }}
          </button>
        </div>
        <ul class="plans">
          <li v-for="p in data.auto" :key="p.id">
            <div class="line">
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
          <h3>En attente de ton arbitrage</h3>
          <span class="count">{{ data.items.length }}</span>
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
        <div v-for="show in reviewGroups.shows" :key="show.title" class="show">
          <label class="show-head">
            <input
              type="checkbox"
              :checked="groupState(show.plans) === 'all'"
              :indeterminate.prop="groupState(show.plans) === 'some'"
              @change="toggleGroup(show.plans)"
            />
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
      </section>

      <!-- Écartés -->
      <details v-if="data.rejected.length" class="group rejected">
        <summary>{{ data.rejected.length }} fichier(s) écarté(s)</summary>
        <ul class="plans">
          <li v-for="p in data.rejected" :key="p.id">
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

    <!-- Résultat d'une application -->
    <section v-if="results" class="group results">
      <h3>Résultat</h3>
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
