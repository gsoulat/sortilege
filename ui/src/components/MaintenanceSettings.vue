<script setup>
import { ref, onMounted } from 'vue'

const props = defineProps({
  mediaServer: { type: Object, required: true },
  // Les trois blocs d'entretien n'ont rien a faire ensemble du point de vue de
  // l'utilisateur : le serveur multimedia releve de l'automatisation, la
  // corbeille du systeme, la remise en conformite de la bibliotheque. Ils
  // partagent ce composant par commodite de code, pas par parente.
  section: { type: String, default: 'tout' },
})
const montre = (nom) => props.section === 'tout' || props.section === nom
const emit = defineEmits(['change'])

// --- Serveur multimédia ----------------------------------------------------

const draftUrl = ref('')
const draftKey = ref('')
const testing = ref(false)
const testResult = ref(null)

function patch(fields) {
  emit('change', fields)
}

function saveServer() {
  const url = draftUrl.value.trim()
  const key = draftKey.value.trim()
  if (!url && !key) return
  const fields = {}
  if (url) fields.base_url = url
  if (key) fields.api_key = key
  patch(fields)
  draftKey.value = ''
  testResult.value = null
}

async function testServer() {
  testing.value = true
  testResult.value = null
  try {
    const res = await fetch('/api/settings/media-server/test', { method: 'POST' })
    const body = await res.json()
    testResult.value = res.ok
      ? { ok: true, text: 'Le serveur a bien reçu la demande.' }
      : { ok: false, text: body.detail ?? 'Échec.' }
  } catch {
    testResult.value = { ok: false, text: 'Serveur injoignable.' }
  } finally {
    testing.value = false
  }
}

// --- Corbeille -------------------------------------------------------------

const trash = ref(null)
const days = ref(30)
const purging = ref(false)
const purgeMessage = ref(null)

async function loadTrash() {
  try {
    trash.value = await (await fetch('/api/collection/trash')).json()
  } catch {
    trash.value = null
  }
}

const confirmingAll = ref(false)

/**
 * `all` vide TOUT, sans condition d'âge. C'est le geste attendu quand on
 * cherche de la place, mais il ne doit pas partir d'un champ laissé à zéro :
 * le serveur exige une confirmation explicite, et l'interface la demande.
 */
async function purge({ all = false } = {}) {
  if (all && !confirmingAll.value) {
    confirmingAll.value = true
    return
  }
  purging.value = true
  purgeMessage.value = null
  confirmingAll.value = false
  try {
    const res = await fetch('/api/collection/trash/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        all ? { older_than_days: 0, confirm_all: true } : { older_than_days: Number(days.value) },
      ),
    })
    const body = await res.json()
    if (res.ok) {
      trash.value = body
      purgeMessage.value =
        `${body.removed_files} fichier(s) supprimé(s) définitivement, ${gb(body.freed_bytes)} Go libérés.`
    } else {
      purgeMessage.value = body.detail ?? 'Échec.'
    }
  } finally {
    purging.value = false
  }
}

const gb = (bytes) => (bytes / 1024 ** 3).toFixed(1)

// --- Dossiers vides restes des rangements anterieurs ---------------------
//
// En DEUX temps, et pas par prudence excessive : un balayage destructeur sur
// des centaines de dossiers ne doit pas partir du meme geste que celui qui
// sert a le regarder.
const vides = ref(null)
const cherchantVides = ref(false)
const nettoyant = ref(false)
const videMessage = ref(null)

async function chercherVides() {
  cherchantVides.value = true
  videMessage.value = null
  try {
    vides.value = await (await fetch('/api/library/empty-dirs')).json()
  } catch {
    videMessage.value = 'Serveur injoignable.'
  } finally {
    cherchantVides.value = false
  }
}

async function nettoyerVides() {
  nettoyant.value = true
  try {
    const res = await fetch('/api/library/empty-dirs/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    })
    const body = await res.json()
    if (res.ok) {
      vides.value = body
      videMessage.value =
        `${body.removed} dossier(s) supprimé(s)` +
        (body.failed?.length ? `, ${body.failed.length} en échec.` : '.')
    } else {
      videMessage.value = body.detail ?? 'Échec.'
    }
  } finally {
    nettoyant.value = false
  }
}

// --- Coquilles : des dossiers qui ont des fichiers mais plus de video -----
//
// Ranger un film emporte la video et ses compagnons, mais le dossier de
// release garde ce qui n'accompagnait RIEN : jaquette au nom de la release,
// .nfo, .xml. Il n'est donc jamais vide au sens strict.
const coquilles = ref(null)
const cherchantCoquilles = ref(false)
const nettoyantCoquilles = ref(false)
const coquilleMessage = ref(null)
const confirmSuppr = ref(false)

async function chercherCoquilles() {
  cherchantCoquilles.value = true
  coquilleMessage.value = null
  try {
    coquilles.value = await (await fetch('/api/library/orphan-dirs')).json()
  } catch {
    coquilleMessage.value = 'Serveur injoignable.'
  } finally {
    cherchantCoquilles.value = false
  }
}

async function nettoyerCoquilles(mode) {
  if (mode === 'delete' && !confirmSuppr.value) {
    confirmSuppr.value = true
    return
  }
  confirmSuppr.value = false
  nettoyantCoquilles.value = true
  try {
    const res = await fetch('/api/library/orphan-dirs/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, mode }),
    })
    const body = await res.json()
    if (res.ok) {
      coquilles.value = body
      coquilleMessage.value =
        `${body.removed_dirs} dossier(s) supprimé(s), ${body.handled_files} fichier(s) ` +
        (mode === 'delete' ? 'supprimé(s)' : 'mis en corbeille') +
        (body.failed?.length ? `, ${body.failed.length} en échec.` : '.')
    } else {
      coquilleMessage.value = body.detail ?? 'Échec.'
    }
  } finally {
    nettoyantCoquilles.value = false
  }
}

const purgingThumbs = ref(false)
const thumbMessage = ref(null)

async function purgeThumbs() {
  purgingThumbs.value = true
  thumbMessage.value = null
  try {
    const res = await fetch('/api/media/thumbs/purge', { method: 'POST' })
    const body = await res.json()
    thumbMessage.value = res.ok
      ? `${body.removed} aperçu(s) supprimé(s). Ils se reconstruiront à la demande.`
      : (body.detail ?? 'Échec.')
  } catch {
    thumbMessage.value = 'Serveur injoignable.'
  } finally {
    purgingThumbs.value = false
  }
}

// --- Remise en conformité --------------------------------------------------

const renaming = ref(false)
const renameResult = ref(null)

async function renameLibrary() {
  renaming.value = true
  renameResult.value = null
  try {
    const res = await fetch('/api/review/rename-library', { method: 'POST' })
    const body = await res.json()
    renameResult.value = res.ok
      ? body.proposed
        ? `${body.proposed} fichier(s) à renommer sur ${body.works} œuvre(s). ` +
          'Ils attendent ta validation dans « File de revue ».'
        : 'Tout est déjà conforme au gabarit courant.'
      : (body.detail ?? 'Échec.')
  } finally {
    renaming.value = false
  }
}

onMounted(loadTrash)
</script>

<template>
  <section v-if="montre('serveur')">
    <h3>Serveur multimédia</h3>
    <p class="note">
      Après chaque rangement, Sortilège demande à Jellyfin de relire sa bibliothèque.
      Sans cela, un film rangé n'apparaît qu'au prochain scan planifié — souvent
      plusieurs heures. L'adresse doit être celle du réseau local : c'est le serveur
      qui l'appelle, pas ton navigateur.
    </p>

    <label class="switch">
      <input
        type="checkbox"
        :checked="mediaServer.enabled"
        :disabled="!mediaServer.base_url || !mediaServer.api_key_set"
        @change="patch({ enabled: $event.target.checked })"
      />
      Prévenir après chaque rangement
      <span v-if="!mediaServer.base_url || !mediaServer.api_key_set" class="hint">
        — renseigne d'abord l'adresse et la clé
      </span>
    </label>

    <div class="field">
      <label>Adresse du serveur</label>
      <input
        type="text"
        :placeholder="mediaServer.base_url || 'http://192.168.10.10:8096'"
        v-model="draftUrl"
      />
    </div>
    <div class="field">
      <label>Clé d'API</label>
      <div class="row">
        <input
          type="password"
          autocomplete="off"
          :placeholder="mediaServer.api_key_set ? '•••••• enregistrée' : 'Tableau de bord → API'"
          v-model="draftKey"
        />
        <button :disabled="!draftUrl.trim() && !draftKey.trim()" @click="saveServer">
          Enregistrer
        </button>
      </div>
      <p class="hint">
        Dans Jellyfin : <em>Tableau de bord → Avancé → Clés d'API → +</em>. Elle donne
        accès au serveur, donc elle est stockée comme une clé : saisie une fois, jamais
        réaffichée.
      </p>
    </div>

    <div v-if="mediaServer.base_url && mediaServer.api_key_set" class="test">
      <button :disabled="testing" @click="testServer">
        {{ testing ? 'Appel…' : 'Tester la connexion' }}
      </button>
      <span v-if="testResult" :class="['result', testResult.ok ? 'ok' : 'err']">
        {{ testResult.text }}
      </span>
    </div>
  </section>

  <section v-if="montre('corbeille')">
    <h3>Corbeille</h3>
    <p class="note">
      Sortilège ne supprime jamais : restes de release, doublons et copies déjà rangées
      partent ici. C'est la bonne règle — une erreur d'identification se rattrape, une
      suppression non — mais rien ne vide la corbeille, et elle grossit indéfiniment.
    </p>

    <p v-if="!trash" class="empty">Lecture…</p>
    <p v-else-if="!trash.batches.length" class="empty">La corbeille est vide.</p>

    <template v-else>
      <p class="summary">
        <strong>{{ gb(trash.total_bytes) }} Go</strong> sur {{ trash.total_files }} fichier(s),
        en {{ trash.batches.length }} lot(s).
      </p>
      <ul class="batches">
        <li v-for="b in trash.batches" :key="b.day">
          <span class="day">{{ b.day }}</span>
          <span class="age">il y a {{ b.age_days }} jour{{ b.age_days > 1 ? 's' : '' }}</span>
          <span class="files">{{ b.files }} fichier{{ b.files > 1 ? 's' : '' }}</span>
          <span class="size">{{ gb(b.bytes) }} Go</span>
        </li>
      </ul>

      <div class="purge">
        <label>
          Supprimer définitivement ce qui a plus de
          <input type="number" min="0" max="365" v-model="days" />
          jours
        </label>
        <button class="danger" :disabled="purging" @click="purge()">
          {{ purging ? 'Suppression…' : 'Vider' }}
        </button>
        <span class="sep"></span>
        <button class="danger strong" :disabled="purging" @click="purge({ all: true })">
          {{ confirmingAll ? `Confirmer : supprimer les ${gb(trash.total_bytes)} Go` : 'Tout vider' }}
        </button>
      </div>
      <p class="hint">
        C'est le seul endroit de l'application qui supprime réellement, et le seul qui
        rende de la place. Un détour par la corbeille du NAS a été envisagé puis écarté :
        il ne libère rien non plus, il donne juste deux corbeilles à vider au lieu d'une.
        <template v-if="confirmingAll">
          <strong class="warn-strong">
            Un second clic supprime tout, y compris ce qui vient d'être évacué. Irréversible.
          </strong>
        </template>
      </p>
      <p v-if="purgeMessage" class="ok-text">{{ purgeMessage }}</p>
    </template>

    <div class="vignettes">
      <span class="warn-text">
        Les aperçus extraits des vidéos sont gardés sur disque pour ne pas relancer
        ffmpeg à chaque ouverture. Ils se vident à chaque scan.
      </span>
      <button :disabled="purgingThumbs" @click="purgeThumbs">
        {{ purgingThumbs ? 'Vidage…' : 'Vider le cache des aperçus' }}
      </button>
    </div>
    <p v-if="thumbMessage" class="ok-text">{{ thumbMessage }}</p>
  </section>

  <section v-if="montre('renommage')">
    <h3>Dossiers vides</h3>
    <p class="note">
      Ranger un fichier laisse derrière lui le dossier de la release. Le nettoyage
      automatique ne rattrape que ce qu'il vient de vider : les carcasses des
      rangements antérieurs restent, et le client de téléchargement en crée de son
      côté — liens abandonnés, extractions ratées.
    </p>
    <p class="note">
      <strong>Aucun fichier n'est touché.</strong> Un dossier n'est proposé que s'il ne
      contient rien, ou seulement des dossiers eux-mêmes vides. Les sources elles-mêmes
      ne sont jamais supprimées.
    </p>

    <div class="vides-actions">
      <button :disabled="cherchantVides" @click="chercherVides">
        {{ cherchantVides ? 'Recherche…' : 'Chercher les dossiers vides' }}
      </button>
      <button
        v-if="vides?.count"
        class="danger"
        :disabled="nettoyant"
        @click="nettoyerVides"
      >
        {{ nettoyant ? 'Suppression…' : `Supprimer ces ${vides.count} dossiers` }}
      </button>
    </div>

    <p v-if="vides && !vides.count" class="empty">Aucun dossier vide.</p>
    <ul v-else-if="vides" class="vides">
      <li v-for="d in vides.dirs" :key="d"><code>{{ d }}</code></li>
      <li v-if="vides.count > vides.dirs.length" class="more">
        … et {{ vides.count - vides.dirs.length }} autres
      </li>
    </ul>
    <p v-if="videMessage" class="ok-text">{{ videMessage }}</p>

    <h3 class="sous-titre">Dossiers sans vidéo</h3>
    <p class="note">
      Une fois le film rangé, le dossier de release garde souvent ce qui
      n'accompagnait rien : une jaquette au nom de la release, un <code>.nfo</code>, un
      <code>.xml</code> de métadonnées. Il n'est donc jamais vide au sens strict, et le
      balayage ci-dessus ne le voit pas — ce n'est plus qu'une coquille.
    </p>
    <p class="note">
      Le critère est strict : <strong>aucune vidéo, et rien d'autre que des accessoires
      connus</strong>. Un dossier contenant une archive ou un fichier d'un type inattendu
      n'est pas proposé — mieux vaut laisser un résidu que supprimer ce qu'on n'a pas su
      reconnaître.
    </p>

    <div class="vides-actions">
      <button :disabled="cherchantCoquilles" @click="chercherCoquilles">
        {{ cherchantCoquilles ? 'Recherche…' : 'Chercher les dossiers sans vidéo' }}
      </button>
      <template v-if="coquilles?.count">
        <button :disabled="nettoyantCoquilles" @click="nettoyerCoquilles('trash')">
          Mettre en corbeille ({{ gb(coquilles.bytes) }} Go)
        </button>
        <button class="danger" :disabled="nettoyantCoquilles" @click="nettoyerCoquilles('delete')">
          {{ confirmSuppr ? `Confirmer : supprimer ces ${coquilles.count} dossiers` : 'Supprimer' }}
        </button>
      </template>
    </div>

    <p v-if="coquilles && !coquilles.count" class="empty">Aucun dossier sans vidéo.</p>
    <ul v-else-if="coquilles" class="vides">
      <li v-for="d in coquilles.dirs" :key="d.path">
        <code>{{ d.path }}</code>
        <span class="detail">{{ d.file_count }} fichier(s) : {{ d.files.join(', ') }}</span>
      </li>
      <li v-if="coquilles.count > coquilles.dirs.length" class="more">
        … et {{ coquilles.count - coquilles.dirs.length }} autres
      </li>
    </ul>
    <p v-if="coquilleMessage" class="ok-text">{{ coquilleMessage }}</p>
  </section>

  <section v-if="montre('renommage')">
    <h3>Remettre la bibliothèque en conformité</h3>
    <p class="note">
      Quand un gabarit change, ce qui est déjà rangé garde des noms produits par une
      règle qui n'a plus cours. Ce passage compare chaque fichier de la bibliothèque au
      gabarit courant et propose ceux qui différeraient.
    </p>
    <p class="note">
      <strong>Rien n'est déplacé ici.</strong> Les propositions rejoignent la file de
      revue, où elles se valident comme les autres — un renommage de masse sur une
      bibliothèque constituée ne doit pas partir d'un seul clic. Aucune identification
      n'est refaite : on repart de ce que le fichier dit déjà de lui-même.
    </p>
    <button :disabled="renaming" @click="renameLibrary">
      {{ renaming ? 'Analyse…' : 'Analyser la bibliothèque' }}
    </button>
    <p v-if="renameResult" class="ok-text">{{ renameResult }}</p>
  </section>
</template>

<style scoped>
section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
}
h3 {
  margin: 0 0 10px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}
.note { margin: 0 0 12px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }
.empty { margin: 0; font-size: 12.5px; color: var(--text-faint); font-style: italic; }

.switch { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 10px; }
.switch input { accent-color: var(--accent); }
.hint { margin: 7px 0 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.switch .hint { margin: 0; }

.field { margin-top: 12px; }
.field > label { display: block; font-size: 11.5px; color: var(--text-dim); margin-bottom: 5px; }
.row { display: flex; gap: 8px; align-items: center; }
.field input[type="text"], .field input[type="password"] {
  flex: 1; width: 100%; min-width: 0; font-size: 12.5px; padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}

.test { margin-top: 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.result { font-size: 12px; }
.result.ok, .ok-text { color: var(--ok); }
.result.err { color: var(--err); }
.ok-text { margin: 10px 0 0; font-size: 12.5px; }

.summary { margin: 0 0 10px; font-size: 12.5px; color: var(--text-dim); }
.batches { list-style: none; margin: 0 0 14px; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow-y: auto; }
.batches li { display: flex; gap: 12px; align-items: baseline; font-size: 12px; }
.batches .day { font-family: var(--mono); font-size: 11px; min-width: 82px; }
.batches .age, .batches .files { color: var(--text-faint); }
.batches .size { margin-left: auto; font-family: var(--mono); font-size: 11px; }

.purge { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.purge label { font-size: 12.5px; color: var(--text-dim); display: flex; align-items: center; gap: 7px; }
.purge input[type="number"] {
  width: 64px; font-size: 12.5px; padding: 4px 7px; text-align: center;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 5px; color: var(--text); font-family: var(--mono);
}
.purge .sep { flex: 1; }
.vides-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.vides { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 260px; overflow-y: auto; }
.vides code { font-family: var(--mono); font-size: 10.5px; color: var(--text-faint); }
.vides .detail { display: block; font-size: 10.5px; color: var(--text-faint); opacity: .75; margin-left: 10px; }
.sous-titre { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--border); }
.note code { font-family: var(--mono); font-size: 11px; }
.vides .more { font-size: 11.5px; color: var(--text-faint); font-style: italic; margin-top: 4px; }

.vignettes { display: flex; align-items: center; gap: 12px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); flex-wrap: wrap; }
.vignettes .warn-text { flex: 1; min-width: 240px; font-size: 11.5px; color: var(--text-faint); line-height: 1.55; }
.danger { color: var(--text-faint); }
.danger.strong { border-color: color-mix(in srgb, var(--err) 30%, transparent); color: var(--err); }
.warn-strong { display: block; margin-top: 7px; color: var(--err); }
.danger:hover:not(:disabled) { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }
</style>
