<script setup>
import { computed, ref, onMounted } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

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

/**
 * Pourquoi une action est indisponible, en toutes lettres et à l'écran.
 *
 * Ce fichier portait douze boutons désactivables et un seul disait pourquoi :
 * les onze autres se contentaient de griser, ce qui se lit « cassé » et non
 * « pas maintenant ». Un `title` n'aurait rien réglé — il n'existe ni au doigt
 * ni au clavier.
 *
 * Une phrase par CAUSE, pas par bouton : quand trois boutons attendent la même
 * réponse du serveur, la répéter trois fois n'apprend rien la deuxième fois.
 * Chaîne vide = rien à expliquer, l'action est disponible.
 */
const raisonEnregistrer = computed(() =>
  draftUrl.value.trim() || draftKey.value.trim()
    ? ''
    : 'Rien à enregistrer : saisis une adresse, une clé, ou les deux.',
)

const raisonTest = computed(() =>
  testing.value ? 'Appel en cours — le serveur a quelques secondes pour répondre.' : '',
)

// --- Corbeille -------------------------------------------------------------

const trash = ref(null)
// La panne de lecture, distincte de la corbeille elle-même. `trash` à null
// veut dire « pas encore lu », et rien d'autre : lui faire porter en plus
// « lu, et raté », c'est afficher « Lecture… » sur un serveur mort. Personne
// ne réessaie ce qu'il croit en cours.
const trashErreur = ref(null)
const days = ref(30)
const purging = ref(false)
const purgeMessage = ref(null)

async function loadTrash() {
  trashErreur.value = null
  try {
    const res = await fetch('/api/collection/trash')
    // Le code avant le corps. Une 500 renvoie du JSON qu'on affectait tel
    // quel, et `trash.batches.length` levait sur `undefined` au rendu suivant ;
    // une 502 arrive en HTML et fait lever `json()`.
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    trash.value = await res.json()
  } catch (e) {
    trash.value = null
    trashErreur.value = e.message ?? 'sans réponse'
  }
}

/**
 * `all` vide TOUT, sans condition d'âge. C'est le geste attendu quand on
 * cherche de la place, mais il ne doit pas partir d'un champ laissé à zéro :
 * le serveur exige une confirmation explicite, et l'interface la demande.
 *
 * La confirmation elle-même n'est plus ici : `ConfirmAction` la porte pour les
 * deux vidages. Le tri par âge s'exécutait au premier clic — l'action la plus
 * destructrice du produit était la seule à ne rien demander.
 */
async function purge({ all = false } = {}) {
  purging.value = true
  purgeMessage.value = null
  try {
    const res = await fetch('/api/collection/trash/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        all ? { older_than_days: 0, confirm_all: true } : { older_than_days: Number(days.value) },
      ),
    })
    // Un échec de passerelle répond en HTML : lire le corps sans filet ferait
    // lever ici, et le message d'échec n'arriverait jamais à l'écran.
    const body = await res.json().catch(() => ({}))
    if (res.ok) {
      trash.value = body
      trashErreur.value = null
      purgeMessage.value = {
        ok: true,
        texte: `${body.removed_files} fichier(s) supprimé(s) définitivement, ${gb(body.freed_bytes)} Go libérés.`,
      }
    } else {
      purgeMessage.value = { ok: false, texte: body.detail ?? `Échec du vidage (réponse ${res.status}).` }
    }
  } catch {
    purgeMessage.value = {
      ok: false,
      texte: 'Serveur injoignable. Ce qui a été supprimé, s\'il l\'a été, apparaîtra à la relecture.',
    }
  } finally {
    purging.value = false
  }
}

const gb = (bytes) => (bytes / 1024 ** 3).toFixed(1)
const pluriel = (n, mot) => `${n} ${mot}${n > 1 ? 's' : ''}`

/**
 * Ce que « Vider » emporte VRAIMENT, au jour près. Annoncer le total de la
 * corbeille serait faux — le champ de jours en épargne une partie — et parler
 * de « fichiers » sans compte ne fait décider personne. Le serveur retient les
 * lots dont l'âge atteint le seuil : on applique le même test.
 */
const cibleAge = computed(() => {
  const seuil = Number(days.value) || 0
  const lots = (trash.value?.batches ?? []).filter((b) => b.age_days >= seuil)
  return {
    fichiers: lots.reduce((n, b) => n + b.files, 0),
    octets: lots.reduce((n, b) => n + b.bytes, 0),
  }
})

// Les deux vidages tombent ensemble et pour la même raison : une phrase pour
// la rangée, pas une par bouton. « Vider » garde en plus sa raison propre —
// celle-là dépend du champ de jours, elle n'est pas partagée.
const raisonPurge = computed(() =>
  purging.value ? 'Vidage en cours — les deux boutons reprennent dès que le serveur répond.' : '',
)

// --- Dossiers vides restes des rangements anterieurs ---------------------
//
// En DEUX temps, et pas par prudence excessive : un balayage destructeur sur
// des centaines de dossiers ne doit pas partir du meme geste que celui qui
// sert a le regarder.
const vides = ref(null)
const cherchantVides = ref(false)
const nettoyant = ref(false)
const videMessage = ref(null)
const videErreur = ref(null)

async function chercherVides() {
  cherchantVides.value = true
  videMessage.value = null
  videErreur.value = null
  try {
    const res = await fetch('/api/library/empty-dirs')
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    vides.value = await res.json()
  } catch (e) {
    // On jette la liste précédente au lieu de la garder à l'écran : elle
    // décrivait le disque d'avant, et c'est sur elle qu'on cliquerait
    // « Supprimer ». Un inventaire périmé est pire qu'aucun inventaire quand
    // le bouton d'à côté efface.
    vides.value = null
    videErreur.value = e.message ?? 'sans réponse'
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
    const body = await res.json().catch(() => ({}))
    if (res.ok) {
      vides.value = body
      videMessage.value = {
        ok: true,
        texte:
          `${body.removed} dossier(s) supprimé(s)` +
          (body.failed?.length ? `, ${body.failed.length} en échec.` : '.'),
      }
    } else {
      videMessage.value = { ok: false, texte: body.detail ?? `Échec (réponse ${res.status}).` }
    }
  } catch {
    videMessage.value = {
      ok: false,
      texte: 'Serveur injoignable. Relance la recherche pour savoir ce qui reste.',
    }
  } finally {
    nettoyant.value = false
  }
}

const raisonVides = computed(() => {
  if (cherchantVides.value) return 'Recherche en cours — le disque est parcouru dossier par dossier.'
  if (nettoyant.value) return 'Suppression en cours — les actions de ce bloc attendent la réponse du serveur.'
  return ''
})

// --- Coquilles : des dossiers qui ont des fichiers mais plus de video -----
//
// Ranger un film emporte la video et ses compagnons, mais le dossier de
// release garde ce qui n'accompagnait RIEN : jaquette au nom de la release,
// .nfo, .xml. Il n'est donc jamais vide au sens strict.
const coquilles = ref(null)
const cherchantCoquilles = ref(false)
const nettoyantCoquilles = ref(false)
const coquilleMessage = ref(null)
const coquilleErreur = ref(null)

async function chercherCoquilles() {
  cherchantCoquilles.value = true
  coquilleMessage.value = null
  coquilleErreur.value = null
  try {
    const res = await fetch('/api/library/orphan-dirs')
    if (!res.ok) throw new Error(`réponse ${res.status}`)
    coquilles.value = await res.json()
  } catch (e) {
    // Même règle que pour les dossiers vides : pas de liste périmée sous un
    // bouton qui supprime.
    coquilles.value = null
    coquilleErreur.value = e.message ?? 'sans réponse'
  } finally {
    cherchantCoquilles.value = false
  }
}

// La mise en corbeille se rattrape, la suppression non : seule la seconde
// passe par une confirmation, portée par `ConfirmAction` dans le gabarit.
async function nettoyerCoquilles(mode) {
  nettoyantCoquilles.value = true
  try {
    const res = await fetch('/api/library/orphan-dirs/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, mode }),
    })
    const body = await res.json().catch(() => ({}))
    if (res.ok) {
      coquilles.value = body
      coquilleMessage.value = {
        ok: true,
        texte:
          `${body.removed_dirs} dossier(s) supprimé(s), ${body.handled_files} fichier(s) ` +
          (mode === 'delete' ? 'supprimé(s)' : 'mis en corbeille') +
          (body.failed?.length ? `, ${body.failed.length} en échec.` : '.'),
      }
    } else {
      coquilleMessage.value = { ok: false, texte: body.detail ?? `Échec (réponse ${res.status}).` }
    }
  } catch {
    coquilleMessage.value = {
      ok: false,
      texte: 'Serveur injoignable. Relance la recherche pour savoir ce qui reste.',
    }
  } finally {
    nettoyantCoquilles.value = false
  }
}

const raisonCoquilles = computed(() => {
  if (cherchantCoquilles.value) return 'Recherche en cours — le disque est parcouru dossier par dossier.'
  if (nettoyantCoquilles.value)
    return 'Opération en cours — les deux actions de ce bloc attendent la réponse du serveur.'
  return ''
})

const purgingThumbs = ref(false)
const thumbMessage = ref(null)

async function purgeThumbs() {
  purgingThumbs.value = true
  thumbMessage.value = null
  try {
    const res = await fetch('/api/media/thumbs/purge', { method: 'POST' })
    const body = await res.json().catch(() => ({}))
    thumbMessage.value = res.ok
      ? { ok: true, texte: `${body.removed} aperçu(s) supprimé(s). Ils se reconstruiront à la demande.` }
      : { ok: false, texte: body.detail ?? `Échec (réponse ${res.status}).` }
  } catch {
    thumbMessage.value = { ok: false, texte: 'Serveur injoignable — le cache est intact.' }
  } finally {
    purgingThumbs.value = false
  }
}

const raisonThumbs = computed(() =>
  purgingThumbs.value ? 'Vidage du cache en cours — un aperçu à la fois.' : '',
)

// --- Remise en conformité --------------------------------------------------

const renaming = ref(false)
const renameResult = ref(null)

async function renameLibrary() {
  renaming.value = true
  renameResult.value = null
  try {
    const res = await fetch('/api/review/rename-library', { method: 'POST' })
    const body = await res.json().catch(() => ({}))
    renameResult.value = res.ok
      ? {
          ok: true,
          texte: body.proposed
            ? `${body.proposed} fichier(s) à renommer sur ${body.works} œuvre(s). ` +
              'Ils attendent ta validation dans « File de revue ».'
            : 'Tout est déjà conforme au gabarit courant.',
        }
      : { ok: false, texte: body.detail ?? `Échec (réponse ${res.status}).` }
  } catch {
    renameResult.value = {
      ok: false,
      texte: 'Serveur injoignable. Rien n\'a été proposé, rien n\'a été déplacé.',
    }
  } finally {
    renaming.value = false
  }
}

const raisonRenommage = computed(() =>
  renaming.value
    ? 'Analyse en cours — chaque fichier de la bibliothèque est comparé au gabarit.'
    : '',
)

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
      <p v-if="raisonEnregistrer" class="indispo" role="status">{{ raisonEnregistrer }}</p>
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
      <span v-if="raisonTest" class="indispo" role="status">{{ raisonTest }}</span>
    </div>
  </section>

  <section v-if="montre('corbeille')">
    <h3>Corbeille</h3>
    <p class="note">
      Sortilège ne supprime jamais : restes de release, doublons et copies déjà rangées
      partent ici. C'est la bonne règle — une erreur d'identification se rattrape, une
      suppression non — mais rien ne vide la corbeille, et elle grossit indéfiniment.
    </p>

    <!-- L'échec passe AVANT l'attente, sans quoi il s'y cache : les deux états
         valaient un même `trash` à null, et une lecture ratée annonçait
         « Lecture… » jusqu'à la fin des temps. -->
    <p v-if="trashErreur" class="panne-inline">
      La corbeille n'a pas pu être lue ({{ trashErreur }}). Rien n'est perdu ni
      supprimé pour autant : c'est l'inventaire qui manque, pas les fichiers.
      <!-- Toujours cliquable : quand ce panneau s'affiche, les boutons de
           vidage ne sont pas rendus, un `:disabled` ici ne coifferait aucun
           état atteignable — et se lirait quand même comme une impasse. -->
      <button class="small" @click="loadTrash">Réessayer</button>
    </p>
    <p v-else-if="!trash" class="empty">Lecture…</p>
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
        <ConfirmAction
          label="Vider"
          :confirm-label="`Confirmer — supprimer ${pluriel(cibleAge.fichiers, 'fichier')}`"
          :detail="`Supprime ${pluriel(cibleAge.fichiers, 'fichier')}, ${gb(cibleAge.octets)} Go, définitivement. Rien ne se restaure ensuite.`"
          :busy="purging"
          :disabled="purging || !cibleAge.fichiers"
          :disabled-reason="cibleAge.fichiers ? '' : `Aucun lot n'a ${days} jour${days > 1 ? 's' : ''} ou plus.`"
          @confirm="purge()"
        />
        <span class="sep"></span>
        <ConfirmAction
          label="Tout vider"
          :confirm-label="`Confirmer — supprimer les ${gb(trash.total_bytes)} Go`"
          :detail="`Supprime ${pluriel(trash.total_files, 'fichier')}, ${gb(trash.total_bytes)} Go, définitivement — y compris ce qui vient d'être évacué. Rien ne se restaure ensuite.`"
          :busy="purging"
          :disabled="purging"
          @confirm="purge({ all: true })"
        />
      </div>
      <p v-if="raisonPurge" class="indispo" role="status">{{ raisonPurge }}</p>
      <p class="hint">
        C'est le seul endroit de l'application qui supprime réellement, et le seul qui
        rende de la place. Un détour par la corbeille du NAS a été envisagé puis écarté :
        il ne libère rien non plus, il donne juste deux corbeilles à vider au lieu d'une.
      </p>
      <p v-if="purgeMessage" :class="purgeMessage.ok ? 'ok-text' : 'err-text'">
        {{ purgeMessage.texte }}
      </p>
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
    <p v-if="raisonThumbs" class="indispo" role="status">{{ raisonThumbs }}</p>
    <p v-if="thumbMessage" :class="thumbMessage.ok ? 'ok-text' : 'err-text'">
      {{ thumbMessage.texte }}
    </p>
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
      <ConfirmAction
        v-if="vides?.count"
        :label="`Supprimer ces ${vides.count} dossiers`"
        :confirm-label="`Confirmer — supprimer ${pluriel(vides.count, 'dossier')}`"
        :detail="`Supprime ${pluriel(vides.count, 'dossier')} vides, définitivement. Aucun fichier n'est concerné : ils ne contiennent rien.`"
        :busy="nettoyant"
        :disabled="nettoyant"
        @confirm="nettoyerVides"
      />
    </div>
    <p v-if="raisonVides" class="indispo" role="status">{{ raisonVides }}</p>

    <p v-if="videErreur" class="panne-inline">
      Les dossiers vides n'ont pas pu être listés ({{ videErreur }}). Aucun dossier
      n'a été touché : c'est le relevé qui manque.
      <button class="small" :disabled="cherchantVides" @click="chercherVides">Réessayer</button>
    </p>
    <p v-else-if="vides && !vides.count" class="empty">Aucun dossier vide.</p>
    <ul v-else-if="vides" class="vides">
      <li v-for="d in vides.dirs" :key="d"><code>{{ d }}</code></li>
      <li v-if="vides.count > vides.dirs.length" class="more">
        … et {{ vides.count - vides.dirs.length }} autres
      </li>
    </ul>
    <p v-if="videMessage" :class="videMessage.ok ? 'ok-text' : 'err-text'">
      {{ videMessage.texte }}
    </p>

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
        <ConfirmAction
          label="Supprimer"
          :confirm-label="`Confirmer — supprimer ces ${coquilles.count} dossiers`"
          :detail="`Supprime ${pluriel(coquilles.count, 'dossier')} et leur contenu, ${gb(coquilles.bytes)} Go, définitivement. La mise en corbeille, elle, se rattrape.`"
          :busy="nettoyantCoquilles"
          :disabled="nettoyantCoquilles"
          @confirm="nettoyerCoquilles('delete')"
        />
      </template>
    </div>
    <p v-if="raisonCoquilles" class="indispo" role="status">{{ raisonCoquilles }}</p>

    <p v-if="coquilleErreur" class="panne-inline">
      Les dossiers sans vidéo n'ont pas pu être listés ({{ coquilleErreur }}). Aucun
      dossier n'a été touché : c'est le relevé qui manque.
      <button class="small" :disabled="cherchantCoquilles" @click="chercherCoquilles">
        Réessayer
      </button>
    </p>
    <p v-else-if="coquilles && !coquilles.count" class="empty">Aucun dossier sans vidéo.</p>
    <ul v-else-if="coquilles" class="vides">
      <li v-for="d in coquilles.dirs" :key="d.path">
        <code>{{ d.path }}</code>
        <span class="detail">{{ d.file_count }} fichier(s) : {{ d.files.join(', ') }}</span>
      </li>
      <li v-if="coquilles.count > coquilles.dirs.length" class="more">
        … et {{ coquilles.count - coquilles.dirs.length }} autres
      </li>
    </ul>
    <p v-if="coquilleMessage" :class="coquilleMessage.ok ? 'ok-text' : 'err-text'">
      {{ coquilleMessage.texte }}
    </p>
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
    <p v-if="raisonRenommage" class="indispo" role="status">{{ raisonRenommage }}</p>
    <p v-if="renameResult" :class="renameResult.ok ? 'ok-text' : 'err-text'">
      {{ renameResult.texte }}
    </p>
  </section>
</template>

<style scoped>
section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
}
/* Ce fichier est le premier migré vers l'échelle typographique de `style.css`.
   Il comptait treize tailles en dur entre 10,5 et 13 px, dont dix sous les
   12 px que l'échelle pose comme plancher : sous ce seuil on ne lit plus, on
   devine. Chaque taille dit maintenant à quoi elle sert — annotation, appoint,
   lecture — au lieu de dire à quelle heure la règle a été écrite. */

/* Un titre doit être PLUS clair que ce qu'il coiffe. En --text-dim, ces
   intitulés étaient plus ternes que la prose qu'ils annonçaient : la hiérarchie
   s'inversait, l'œil tombait du titre vers le corps. --text-title est le seul
   ton du jeu qui dépasse --text, il est fait pour ça.
   La forme reste celle de la maison — capitales, interlettrage, 600 — parce
   que six sections de réglages la partagent sur le même écran ; seuls la
   couleur et le plancher de taille changent. */
h3 {
  margin: 0 0 10px; font-size: var(--t-xs); font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-title);
}
.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }
.empty { margin: 0; font-size: var(--t-sm); color: var(--text-faint); font-style: italic; }

.switch { display: flex; align-items: center; gap: 8px; font-size: var(--t-sm); margin-bottom: 10px; }
.switch input { accent-color: var(--accent); }
.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.switch .hint { margin: 0; }

.field { margin-top: 12px; }
.field > label { display: block; font-size: var(--t-sm); color: var(--text-dim); margin-bottom: 5px; }
.row { display: flex; gap: 8px; align-items: center; }
.field input[type="text"], .field input[type="password"] {
  flex: 1; width: 100%; min-width: 0; font-size: var(--t-sm); padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}

.test { margin-top: 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.result { font-size: var(--t-xs); }
.result.ok, .ok-text { color: var(--ok); }
.result.err, .err-text { color: var(--err); }
.ok-text, .err-text { margin: 10px 0 0; font-size: var(--t-xs); line-height: 1.6; max-width: 680px; }

/* La ligne qui porte les chiffres de la corbeille — combien de Go, combien de
   fichiers. C'est la valeur qu'on vient chercher dans cette section : elle se
   lit, elle ne s'annote pas. Seul emploi du pas de lecture ici. */
.summary { margin: 0 0 10px; font-size: var(--t-md); color: var(--text-dim); }
.batches { list-style: none; margin: 0 0 14px; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow-y: auto; }
.batches li { display: flex; gap: 12px; align-items: baseline; font-size: var(--t-xs); }
.batches .day { font-family: var(--mono); font-size: var(--t-xs); min-width: 90px; }
.batches .age, .batches .files { color: var(--text-faint); }
.batches .size { margin-left: auto; font-family: var(--mono); font-size: var(--t-xs); }

.purge { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.purge label { font-size: var(--t-sm); color: var(--text-dim); display: flex; align-items: center; gap: 7px; }
.purge input[type="number"] {
  width: 68px; font-size: var(--t-sm); padding: 4px 7px; text-align: center;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 5px; color: var(--text); font-family: var(--mono);
}
.purge .sep { flex: 1; }
.vides-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.vides { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 260px; overflow-y: auto; }
.vides code { font-family: var(--mono); font-size: var(--t-xs); color: var(--text-faint); }
.vides .detail { display: block; font-size: var(--t-xs); color: var(--text-faint); opacity: .75; margin-left: 10px; }
.sous-titre { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--border); }
.note code { font-family: var(--mono); font-size: var(--t-xs); }
.vides .more { font-size: var(--t-xs); color: var(--text-faint); font-style: italic; margin-top: 4px; }

.vignettes { display: flex; align-items: center; gap: 12px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); flex-wrap: wrap; }
.vignettes .warn-text { flex: 1; min-width: 240px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.55; }

/* Pourquoi une action est indisponible. Ton d'appoint, jamais celui d'une
   alerte : ce n'est pas une panne, c'est l'état normal de l'écran dit à voix
   haute. L'écrire en rouge apprendrait à ignorer le rouge. */
.indispo {
  margin: 8px 0 0; font-size: var(--t-xs); color: var(--text-faint);
  line-height: 1.6; max-width: 680px;
}
.test .indispo { margin: 0; }

/* Une panne locale : le reste de l'écran fonctionne, seul ce relevé est
   aveugle. D'où le cadre plutôt qu'un bandeau en haut de page — l'erreur est
   là où elle s'est produite, avec le bouton qui la relance. */
.panne-inline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 10px 0 0; padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-xs); line-height: 1.6; color: var(--err);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.panne-inline button { color: var(--text); flex: none; }
.panne-inline button:disabled { color: var(--text-faint); }
button.small { font-size: var(--t-xs); padding: 3px 10px; }
/* `.danger`, `.danger.strong` et `.warn-strong` sont partis avec les boutons
   qu'ils habillaient : ConfirmAction porte désormais le rouge au repos, l'état
   armé et la phrase d'avertissement. Les garder ici, c'était garder une
   seconde définition de « ce bouton efface » qui divergerait à la première
   retouche. */
</style>
