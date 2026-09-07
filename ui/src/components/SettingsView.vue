<script setup>
import { ref, computed, onMounted } from 'vue'

const KIND_LABELS = { movie: 'Films', episode: 'Séries TV', anime: 'Animes' }

const s = ref(null)
const prefs = ref(null)
const saving = ref(false)
const saveError = ref(null)
const saved = ref(false)

const dirty = ref(false)

const selectedSources = computed({
  get: () => prefs.value?.enabled_sources ?? [],
  set: (v) => {
    prefs.value.enabled_sources = v
    dirty.value = true
  },
})

function toggleSource(path) {
  const current = new Set(prefs.value.enabled_sources)
  // Liste vide = toutes les sources. Décocher la dernière n'aurait aucun sens,
  // on matérialise donc « tout » par la liste complète avant de retirer.
  if (current.size === 0) {
    for (const a of s.value.paths.source_roots) current.add(a)
  }
  current.has(path) ? current.delete(path) : current.add(path)
  selectedSources.value = [...current]
}

function isSelected(path) {
  const list = prefs.value?.enabled_sources ?? []
  return list.length === 0 || list.includes(path)
}

function setDestination(kind, value) {
  prefs.value.destinations[kind] = value
  dirty.value = true
}

async function load() {
  s.value = await (await fetch('/api/settings')).json()
  prefs.value = await (await fetch('/api/settings/preferences')).json()
}

async function save() {
  saving.value = true
  saveError.value = null
  saved.value = false
  try {
    const res = await fetch('/api/settings/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled_sources: prefs.value.enabled_sources,
        destinations: prefs.value.destinations,
        templates: {},
      }),
    })
    const body = await res.json()
    if (!res.ok) {
      saveError.value = body.detail ?? 'Enregistrement refusé.'
      return
    }
    prefs.value = body
    dirty.value = false
    saved.value = true
    setTimeout(() => (saved.value = false), 2500)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-if="s && prefs" class="settings">
    <!-- ========== Modifiable : les choix d'usage ========== -->

    <section>
      <h3>Sources à scanner</h3>
      <p class="note top">
        Les racines viennent de <code>SORTILEGE_SOURCE_ROOTS</code> et doivent correspondre
        aux volumes montés. Tu choisis ici lesquelles parcourir — utile pour exclure
        un montage réseau lent.
      </p>
      <ul class="sources">
        <li v-for="src in prefs.available_sources" :key="src.path">
          <label>
            <input type="checkbox" :checked="isSelected(src.path)" @change="toggleSource(src.path)" />
            <code>{{ src.path }}</code>
          </label>
          <span v-if="!src.exists" class="warn">introuvable</span>
        </li>
      </ul>
    </section>

    <section>
      <h3>Destination par type</h3>
      <p class="note top">
        Chemin relatif à la racine de bibliothèque
        (<code>{{ prefs.library_root }}</code>). Une destination doit rester sous cette
        racine : c'est ce qui empêche l'interface d'écrire ailleurs sur le NAS.
      </p>
      <div class="dest">
        <div v-for="(label, kind) in KIND_LABELS" :key="kind" class="dest-row">
          <label :for="`d-${kind}`">{{ label }}</label>
          <input
            :id="`d-${kind}`"
            :value="prefs.destinations[kind]"
            spellcheck="false"
            @input="setDestination(kind, $event.target.value)"
          />
          <code class="resolved">{{ prefs.resolved_destinations[kind] }}</code>
        </div>
      </div>

      <div class="actions">
        <button class="primary" :disabled="!dirty || saving" @click="save">
          {{ saving ? 'Enregistrement…' : 'Enregistrer' }}
        </button>
        <span v-if="saved" class="ok-msg">Enregistré</span>
        <span v-if="saveError" class="err-msg">{{ saveError }}</span>
      </div>
    </section>

    <!-- ========== Lecture seule : le déploiement ========== -->

    <p class="lead">
      Ce qui suit décrit le <strong>déploiement</strong> et vient de l'environnement.
      Ces valeurs doivent correspondre aux volumes du conteneur, les modifier depuis
      l'interface produirait une configuration qui ne survit pas à un redémarrage.
    </p>

    <section>
      <h3>Diagnostic</h3>
      <ul class="checks">
        <li v-for="c in s.diagnostics" :key="c.name" :class="{ ko: !c.ok }">
          <span class="dot"></span>
          <span class="name">{{ c.name }}</span>
          <span class="detail">{{ c.detail }}</span>
        </li>
      </ul>
    </section>

    <div class="cols">
      <section>
        <h3>Décision</h3>
        <dl>
          <dt>Application automatique</dt>
          <dd>score ≥ <code>{{ s.behaviour.auto_apply_threshold }}</code></dd>
          <dt>Revue manuelle</dt>
          <dd>entre <code>{{ s.behaviour.reject_threshold }}</code> et <code>{{ s.behaviour.auto_apply_threshold }}</code></dd>
          <dt>Rejet</dt>
          <dd>score &lt; <code>{{ s.behaviour.reject_threshold }}</code></dd>
          <dt>Mode simulation</dt>
          <dd>
            <span :class="s.behaviour.dry_run ? 'on' : 'off'">
              {{ s.behaviour.dry_run ? 'actif — rien n\'est déplacé' : 'désactivé — les fichiers bougent' }}
            </span>
          </dd>
        </dl>
      </section>

      <section>
        <h3>Résolveur IA</h3>
        <dl>
          <dt>État</dt>
          <dd>
            <span :class="s.ai.enabled ? 'on' : 'off'">{{ s.ai.enabled ? 'activé' : 'désactivé' }}</span>
            <span v-if="s.ai.enabled && !s.ai.configured" class="warn"> — clé absente</span>
          </dd>
          <dt>Modèle</dt>
          <dd><code>{{ s.ai.model }}</code></dd>
          <dt>Taille de lot</dt>
          <dd>{{ s.ai.batch_size }} fichiers par appel</dd>
        </dl>
      </section>
    </div>

    <section>
      <h3>Fournisseurs de métadonnées</h3>
      <ul class="providers">
        <li v-for="p in s.providers" :key="p.name" :class="{ ko: p.required && !p.configured }">
          <span class="dot" :class="{ off: !p.configured }"></span>
          <div>
            <div class="name">
              {{ p.name }}
              <span v-if="p.required" class="req">requis</span>
            </div>
            <div class="role">{{ p.role }}</div>
          </div>
          <code class="hint">{{ p.hint }}</code>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.settings { display: flex; flex-direction: column; gap: 18px; }

.lead { margin: 8px 0 0; font-size: 12.5px; color: var(--text-faint); line-height: 1.65; max-width: 720px; }

section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 780px) { .cols { grid-template-columns: 1fr; } }

h3 {
  margin: 0 0 10px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}

code {
  font-family: var(--mono); font-size: 11.5px;
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.note { margin: 0 0 12px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 640px; }

/* --- Sources --- */
.sources { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.sources li { display: flex; align-items: center; gap: 10px; }
.sources label { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.sources input { width: auto; }

/* --- Destinations --- */
.dest { display: flex; flex-direction: column; gap: 9px; }
.dest-row { display: grid; grid-template-columns: 90px 1fr auto; gap: 11px; align-items: center; }
.dest-row label { font-size: 13px; color: var(--text-dim); }
.dest-row input { font-family: var(--mono); font-size: 12.5px; padding: 6px 10px; }
.resolved { font-size: 10.5px; color: var(--text-faint); }
@media (max-width: 700px) {
  .dest-row { grid-template-columns: 1fr; gap: 4px; }
}

.actions { display: flex; align-items: center; gap: 12px; margin-top: 14px; }
button.primary {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  border-color: var(--accent-dim); color: var(--accent);
}
.ok-msg { font-size: 12px; color: var(--ok); }
.err-msg { font-size: 12px; color: var(--err); }

/* --- Diagnostics --- */
.checks { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 9px; }
.checks li { display: flex; align-items: baseline; gap: 9px; font-size: 13px; flex-wrap: wrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--ok); transform: translateY(-1px); }
.checks li.ko .dot, .dot.off { background: var(--warn); }
.checks .name { min-width: 150px; }
.checks .detail { color: var(--text-faint); font-size: 12px; }

dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 7px 16px; font-size: 13px; }
dt { color: var(--text-faint); }
dd { margin: 0; display: flex; gap: 5px; flex-wrap: wrap; }

.on { color: var(--ok); }
.off { color: var(--text-faint); }
.warn { color: var(--warn); font-size: 11.5px; }

.providers { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 11px; }
.providers li { display: flex; align-items: center; gap: 10px; }
.providers .name { font-size: 13px; display: flex; align-items: center; gap: 7px; }
.providers .role { font-size: 11.5px; color: var(--text-faint); }
.providers .hint { margin-left: auto; }
.req {
  font-size: 9.5px; padding: 1px 5px; border-radius: 3px; letter-spacing: .04em;
  color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
}
</style>
