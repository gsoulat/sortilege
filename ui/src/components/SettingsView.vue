<script setup>
import { ref, onMounted } from 'vue'
import SourcePicker from './SourcePicker.vue'
import FolderBrowser from './FolderBrowser.vue'
import AiSettings from './AiSettings.vue'
import AutomationSettings from './AutomationSettings.vue'

const KIND_LABELS = { movie: 'Films', episode: 'Séries TV', anime: 'Animes' }

const s = ref(null)
const prefs = ref(null)
const saving = ref(false)
const saveError = ref(null)
const saved = ref(false)
const dirty = ref(false)

/**
 * Ajouter ou retirer une source est enregistré immédiatement : c'est une action
 * discrète, pas une saisie qu'on affine. Les destinations, elles, se tapent
 * caractère par caractère et attendent un clic explicite.
 */
function onSourceChange(patch) {
  Object.assign(prefs.value, patch)
  save()
}

// Les réglages IA s'enregistrent au changement : ce sont des choix discrets
// (un fournisseur, un seuil), pas une saisie qu'on affine caractère par
// caractère comme un chemin.
function onAiChange(patch) {
  Object.assign(prefs.value.ai, patch)
  save({ ai: patch })
}

function onAutomationChange(patch) {
  Object.assign(prefs.value.automation, patch)
  save({ automation: patch })
}

function onOversizeChange(patch) {
  Object.assign(prefs.value.oversize, patch)
  save({ oversize: patch })
}

function setOversizeDestination(kind, value) {
  const destinations = { ...prefs.value.oversize.destinations, [kind]: value }
  onOversizeChange({ destinations })
}

function setDestination(kind, value) {
  prefs.value.destinations[kind] = value
  dirty.value = true
}

// Quel type de média est en cours de sélection dans l'explorateur.
const browsingKind = ref(null)

/**
 * L'explorateur rend un chemin absolu ; la destination se stocke RELATIVE à la
 * racine de bibliothèque. C'est cette racine qui garantit le confinement : une
 * destination absolue pourrait désigner n'importe où sur le NAS.
 */
function onPick(kind, absolute) {
  const root = prefs.value.library_root.replace(/\/+$/, '')
  let relative = absolute

  if (absolute === root) {
    relative = '.'
  } else if (absolute.startsWith(root + '/')) {
    relative = absolute.slice(root.length + 1)
  } else {
    saveError.value =
      `« ${absolute} » est hors de la racine de bibliothèque (${root}). ` +
      'Choisis un dossier situé dessous.'
    return
  }

  if (relative === '.') {
    saveError.value = 'La racine elle-même ne peut pas servir de destination : ' +
      'choisis un sous-dossier, sinon les trois types se mélangeraient.'
    return
  }

  saveError.value = null
  setDestination(kind, relative)
  browsingKind.value = null
}

async function load() {
  s.value = await (await fetch('/api/settings')).json()
  prefs.value = await (await fetch('/api/settings/preferences')).json()
}

async function save(extra = {}) {
  saving.value = true
  saveError.value = null
  saved.value = false
  try {
    // On envoie TOUJOURS l'etat complet, jamais un patch partiel : deux
    // enregistrements rapproches (ajout d'une source puis bascule d'une case)
    // pourraient sinon s'entrecroiser et faire perdre un champ. C'est
    // idempotent, donc rejouable sans consequence.
    const payload = {
      custom_sources: prefs.value.custom_sources,
      enabled_sources: prefs.value.enabled_sources,
      destinations: prefs.value.destinations,
      templates: {},
      // Le bloc IA n'est envoyé QUE lorsqu'il change, et sous forme de patch :
      // la clé n'étant jamais renvoyée par le serveur, un envoi systématique
      // de l'état complet l'écraserait par une chaîne vide.
      ...extra,
    }
    const res = await fetch('/api/settings/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
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
        Ajoute autant de sources que tu veux, à condition qu'elles soient
        <strong>sous une racine montée</strong> — le conteneur ne voit que ses volumes.
        Pour ouvrir une zone supplémentaire, ajoute un volume au
        <code>docker compose</code> et déclare-la dans <code>SORTILEGE_SOURCE_ROOTS</code>.
      </p>
      <SourcePicker :prefs="prefs" @change="onSourceChange" />
    </section>

    <section>
      <h3>Destination par type</h3>
      <p class="note top">
        Chemin relatif à la racine de bibliothèque
        (<code>{{ prefs.library_root }}</code>). Une destination doit rester sous cette
        racine : c'est ce qui empêche l'interface d'écrire ailleurs sur le NAS.
      </p>
      <div class="dest">
        <div v-for="(label, kind) in KIND_LABELS" :key="kind" class="dest-block">
          <div class="dest-row">
            <label :for="`d-${kind}`">{{ label }}</label>
            <input
              :id="`d-${kind}`"
              :value="prefs.destinations[kind]"
              spellcheck="false"
              @input="setDestination(kind, $event.target.value)"
            />
            <button
              class="browse"
              @click="browsingKind = browsingKind === kind ? null : kind"
            >{{ browsingKind === kind ? 'Fermer' : 'Parcourir…' }}</button>
          </div>
          <code class="resolved">{{ prefs.resolved_destinations[kind] }}</code>
          <FolderBrowser
            v-if="browsingKind === kind"
            :start="prefs.library_root"
            :pick-label="`Ranger les ${label.toLowerCase()} ici`"
            @pick="onPick(kind, $event)"
            @close="browsingKind = null"
          />
        </div>
      </div>

      <label class="switch oversize-toggle">
        <input
          type="checkbox"
          :checked="prefs.oversize.enabled"
          @change="onOversizeChange({ enabled: $event.target.checked })"
        />
        Dossier séparé pour les fichiers volumineux
      </label>

      <div v-if="prefs.oversize.enabled" class="oversize">
        <p class="note">
          Un remux 4K de 60 Go et un épisode de 800 Mo n'ont pas les mêmes contraintes.
          Au-delà du seuil, le fichier part dans la destination indiquée ici — souvent
          sur un autre volume, ou simplement isolé pour être repéré.
        </p>

        <div class="dest-row">
          <label for="over-threshold">Seuil</label>
          <div class="inline">
            <input
              id="over-threshold"
              type="number"
              min="1"
              step="1"
              :value="prefs.oversize.threshold_gb"
              @change="onOversizeChange({ threshold_gb: Number($event.target.value) })"
            />
            <span class="unit">Go et plus</span>
          </div>
        </div>

        <div v-for="(label, kind) in KIND_LABELS" :key="`o-${kind}`" class="dest-row">
          <label :for="`o-${kind}`">{{ label }}</label>
          <input
            :id="`o-${kind}`"
            :value="prefs.oversize.destinations[kind]"
            spellcheck="false"
            @change="setOversizeDestination(kind, $event.target.value)"
          />
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

    <AutomationSettings :automation="prefs.automation" @change="onAutomationChange" />

    <AiSettings :ai="prefs.ai" :providers="prefs.ai_providers" @change="onAiChange" />

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
.dest-block .resolved { margin-left: 101px; }
.dest-row label { font-size: 13px; color: var(--text-dim); }
.dest-row input { font-family: var(--mono); font-size: 12.5px; padding: 6px 10px; }
.resolved { font-size: 10.5px; color: var(--text-faint); }
@media (max-width: 700px) {
  .dest-row { grid-template-columns: 1fr; gap: 4px; }
}

.dest-block { display: flex; flex-direction: column; gap: 5px; }
.dest-block + .dest-block { margin-top: 4px; }
.browse { font-size: 11.5px; padding: 4px 10px; white-space: nowrap; }

.oversize-toggle { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); }
.oversize { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.oversize .note { margin: 0 0 6px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 620px; }
.oversize .inline { display: flex; align-items: center; gap: 9px; }
.oversize .inline input { width: 90px; }
.oversize .unit { font-size: 12px; color: var(--text-faint); }
.switch { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
.switch input { width: auto; }

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
