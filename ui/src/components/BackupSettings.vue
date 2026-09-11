<script setup>
import { computed, onMounted, ref, useId } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

/**
 * Sauvegarde et restauration de l'état.
 *
 * Les routes `/api/backup/*` existaient sans aucun écran : la seule façon de
 * sortir une copie des réglages et des arbitrages était un `curl`. Ce fichier
 * n'invente rien — il montre ce que le serveur décrit, envoie l'archive telle
 * quelle et reprend ses refus mot pour mot.
 *
 * L'ordre du geste est imposé : on choisit une archive, le serveur dit ce
 * qu'elle contient, et c'est ce contenu-là qu'on confirme. On ne confirme pas
 * « restaurer », on confirme « remplacer mes réglages par ceux du 3 août ».
 */

const emit = defineEmits(['restored'])

const idFichier = useId()
const idFichierAide = useId()

// --- Ce que contiendrait une archive aujourd'hui ---------------------------

const description = ref(null)
const chargement = ref(false)
const panneDescription = ref(null)

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

async function decrire() {
  chargement.value = true
  panneDescription.value = null
  try {
    const res = await fetch('/api/backup')
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      panneDescription.value =
        corps?.detail ?? `Le serveur n'a pas décrit la sauvegarde (réponse ${res.status}).`
      return
    }
    description.value = corps
  } catch {
    panneDescription.value = 'Serveur injoignable : impossible de dire ce que contiendrait une archive.'
  } finally {
    chargement.value = false
  }
}

onMounted(decrire)

const pieces = computed(() => description.value?.pieces ?? [])
const piecesPresentes = computed(() => pieces.value.filter((p) => p.present))
const secretsExclus = computed(() => description.value?.secrets_excluded ?? [])

function taille(octets) {
  const n = Number(octets) || 0
  if (n < 1024) return `${n} o`
  const nombre = (v) => v.toLocaleString('fr-FR', { maximumFractionDigits: 1 })
  if (n < 1024 * 1024) return `${nombre(n / 1024)} Ko`
  return `${nombre(n / (1024 * 1024))} Mo`
}

// --- Restauration ------------------------------------------------------------

const fichier = ref(null)
const inspection = ref(null)
const inspectionEnCours = ref(false)
const panneInspection = ref(null)

const restauration = ref(false)
const compteRendu = ref(null)
const panneRestauration = ref(null)

/** Le zip part dans le corps brut, sans formulaire multipart : c'est ce que
 *  la route attend (`curl --data-binary @archive.zip`), et un objet File
 *  s'envoie tel quel. */
function envoyer(url) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/zip' },
    body: fichier.value,
  })
}

async function choisir(event) {
  fichier.value = event.target.files?.[0] ?? null
  inspection.value = null
  panneInspection.value = null
  compteRendu.value = null
  panneRestauration.value = null
  if (!fichier.value) return

  inspectionEnCours.value = true
  try {
    const res = await envoyer('/api/backup/inspect')
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      panneInspection.value =
        corps?.detail ?? `Archive refusée à l'examen (réponse ${res.status}). Rien n'a été modifié.`
      return
    }
    inspection.value = corps
  } catch {
    panneInspection.value =
      "Serveur injoignable : l'archive n'a pas pu être examinée. Rien n'a été modifié."
  } finally {
    inspectionEnCours.value = false
  }
}

const contenuArchive = computed(() => inspection.value?.contents ?? [])
const dateArchive = computed(() => inspection.value?.manifest?.created_at ?? '')

const raisonRestauration = computed(() => {
  if (inspectionEnCours.value) return "L'archive est en cours d'examen."
  if (!fichier.value) return "Choisis d'abord une archive : elle est examinée avant toute restauration."
  if (!inspection.value) return "Cette archive n'a pas passé l'examen : il n'y a rien à restaurer."
  if (!contenuArchive.value.length) return "L'archive ne contient aucun fichier restaurable."
  return ''
})

const detailRestauration = computed(() => {
  const quoi = contenuArchive.value.map((c) => c.label).join(', ') || "le contenu de l'archive"
  const quand = dateArchive.value ? ` de l'archive du ${dateArchive.value}` : " de l'archive"
  return (
    `Remplace ${quoi} par ceux${quand}. L'état actuel est d'abord copié sous ` +
    "« avant-restauration.zip » dans le volume de données ; les clés et jetons en place sont gardés."
  )
})

async function restaurer() {
  if (raisonRestauration.value) return
  restauration.value = true
  compteRendu.value = null
  panneRestauration.value = null
  try {
    const res = await envoyer('/api/backup/restore?confirm=true')
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      panneRestauration.value =
        corps?.detail ?? `Restauration refusée (réponse ${res.status}).`
      return
    }
    compteRendu.value = corps
    // Les réglages affichés datent d'avant : l'écran parent les relit.
    emit('restored')
    decrire()
  } catch {
    panneRestauration.value =
      "Serveur injoignable pendant la restauration : impossible de savoir si elle a eu lieu. " +
      "Recharge la page et regarde les réglages. Si elle a eu lieu, « avant-restauration.zip », " +
      "dans le volume de données, contient l'état d'avant."
  } finally {
    restauration.value = false
  }
}
</script>

<template>
  <section>
    <h3>Sauvegarde</h3>
    <p class="note">
      Les réglages, les gabarits, les identifications retenues et le journal tiennent dans le
      volume de données. Un conteneur recréé sans ce volume repart de zéro : l'archive ci-dessous
      en est la copie, téléchargeable sans accès au NAS.
    </p>
    <p class="note capital">
      <strong>Les secrets ne sont pas dans l'archive.</strong> Clés d'API et jetons restent sur
      cette installation : une archive se copie et se transmet, elle ne doit pas ouvrir les
      comptes de quelqu'un. À la restauration, les clés déjà en place sont gardées.
    </p>

    <!-- Trois états : en attente, en panne, décrit. -->
    <p v-if="chargement && !description" class="indispo" role="status">
      Lecture de ce que contiendrait l'archive…
    </p>
    <div v-else-if="panneDescription" class="panne-inline" role="alert">
      <p>{{ panneDescription }}</p>
      <button type="button" @click="decrire">Réessayer</button>
    </div>
    <template v-else-if="description">
      <h4 class="etiquette">Ce que l'archive contiendrait aujourd'hui</h4>
      <ul v-if="pieces.length" class="pieces">
        <li v-for="p in pieces" :key="p.name" :class="{ absente: !p.present }">
          <span class="label">{{ p.label }}</span>
          <code>{{ p.name }}</code>
          <span class="poids">{{ p.present ? taille(p.bytes) : 'rien encore enregistré' }}</span>
        </li>
      </ul>
      <p v-else class="hint">Le serveur ne décrit aucune pièce à sauvegarder.</p>

      <p v-if="secretsExclus.length" class="hint">
        Laissés hors de l'archive parce qu'ils sont des secrets :
        <code v-for="s in secretsExclus" :key="s">{{ s }}</code>
      </p>
      <p v-if="description.secrets_policy" class="hint">{{ description.secrets_policy }}</p>

      <div class="actions">
        <a v-if="piecesPresentes.length" class="bouton" href="/api/backup/archive">
          Télécharger l'archive
        </a>
        <span v-if="piecesPresentes.length" class="hint inline">
          <code>{{ description.filename }}</code>
        </span>
        <p v-else class="hint attention">
          Rien n'est encore enregistré sur cette installation : l'archive serait vide, il n'y a
          rien à télécharger.
        </p>
      </div>
    </template>

    <h3 class="sous-titre">Restaurer une archive</h3>
    <p class="note">
      La restauration <strong>écrase</strong> les réglages, les décisions mémorisées et le journal
      en place par ceux de l'archive. Juste avant, l'état actuel est copié sous
      <code>avant-restauration.zip</code> dans le volume de données : c'est le chemin du retour.
    </p>

    <div class="champ">
      <label :for="idFichier" class="etiquette">Archive à restaurer (.zip)</label>
      <input
        :id="idFichier"
        type="file"
        accept=".zip,application/zip"
        :aria-describedby="idFichierAide"
        :disabled="inspectionEnCours || restauration"
        @change="choisir"
      />
      <p :id="idFichierAide" class="hint">
        Choisir un fichier ne restaure rien : le serveur l'examine et dit ce qu'il contient. La
        restauration se confirme ensuite, sur ce contenu-là.
      </p>
    </div>

    <p v-if="inspectionEnCours" class="indispo" role="status">Examen de l'archive…</p>
    <p v-else-if="panneInspection" class="panne-inline" role="alert">{{ panneInspection }}</p>
    <div v-else-if="inspection" class="inspection">
      <p class="summary" role="status">
        Archive<template v-if="dateArchive"> du <strong>{{ dateArchive }}</strong></template>
        <template v-if="inspection.manifest?.app_version">
          , produite par Sortilège {{ inspection.manifest.app_version }}</template
        >.
      </p>
      <ul v-if="contenuArchive.length" class="pieces">
        <li v-for="c in contenuArchive" :key="c.name">
          <span class="label">{{ c.label }}</span>
          <code>{{ c.name }}</code>
          <span class="poids">{{ taille(c.bytes) }}</span>
        </li>
      </ul>
      <p v-else class="hint attention">Cette archive ne contient aucun fichier restaurable.</p>
      <p v-if="inspection.ignored?.length" class="hint">
        Ignorés, parce qu'ils ne font pas partie de l'état de Sortilège :
        <code v-for="n in inspection.ignored" :key="n">{{ n }}</code>
      </p>
      <p v-if="inspection.secrets_removed?.length" class="hint">
        Secrets retirés à l'archivage, donc absents :
        <code v-for="s in inspection.secrets_removed" :key="s">{{ s }}</code>
      </p>
    </div>

    <div class="actions">
      <ConfirmAction
        label="Restaurer cette archive"
        confirm-label="Confirmer — écraser l'état en place"
        :detail="detailRestauration"
        :busy="restauration"
        :disabled="Boolean(raisonRestauration)"
        :disabled-reason="raisonRestauration"
        @confirm="restaurer"
      />
    </div>

    <p v-if="panneRestauration" class="panne-inline" role="alert">{{ panneRestauration }}</p>
    <div v-else-if="compteRendu" class="inspection" role="status">
      <p class="summary">
        <strong>Restauration faite</strong> :
        {{ (compteRendu.restored ?? []).map((r) => r.label).join(', ') || 'aucun fichier remplacé' }}.
      </p>
      <p v-if="compteRendu.previous_saved_as" class="hint">
        L'état d'avant est conservé sous <code>{{ compteRendu.previous_saved_as }}</code>, dans le
        volume de données.
      </p>
      <p v-else class="hint attention">
        La copie de l'état d'avant <strong>n'a pas pu être écrite</strong> : cette restauration
        n'a pas de retour possible depuis Sortilège.
      </p>
      <p v-if="compteRendu.secrets_kept?.length" class="hint">
        Clés gardées telles qu'elles étaient :
        <code v-for="s in compteRendu.secrets_kept" :key="s">{{ s }}</code>
      </p>
      <p v-if="compteRendu.secrets_missing?.length" class="hint attention">
        À ressaisir, faute d'être en place ici comme dans l'archive :
        <code v-for="s in compteRendu.secrets_missing" :key="s">{{ s }}</code>
      </p>
    </div>
    <p v-else class="hint">
      Aucune restauration depuis l'ouverture de cet écran.
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

.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }
.note.capital strong { color: var(--warn); }

.etiquette {
  display: block; margin: 14px 0 6px; font-size: var(--t-sm); font-weight: 400;
  color: var(--text-dim);
}

.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.hint strong { color: var(--text-dim); }
.hint.attention, .hint.attention strong { color: var(--warn); }
.hint.inline { margin: 0; }
.hint code { margin-right: 5px; }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.pieces { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; max-width: 680px; }
.pieces li { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; font-size: var(--t-sm); }
.pieces .label { color: var(--text); }
.pieces .poids { margin-left: auto; font-size: var(--t-xs); color: var(--text-faint); font-family: var(--mono); }
.pieces li.absente .label { color: var(--text-faint); }

.actions { margin-top: 14px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.bouton {
  display: inline-block; font-size: var(--t-sm); padding: 5px 12px; border-radius: 6px;
  color: var(--accent); text-decoration: none;
  border: 1px solid var(--accent-dim);
  background: color-mix(in srgb, var(--accent) 20%, transparent);
}
.bouton:hover { background: color-mix(in srgb, var(--accent) 28%, transparent); }

.champ input[type='file'] { font-size: var(--t-sm); color: var(--text-dim); }

.inspection { margin-top: 12px; max-width: 680px; }
.summary { margin: 12px 0 8px; font-size: var(--t-md); color: var(--text-dim); }
.summary strong { color: var(--text); }

.indispo {
  margin: 10px 0 0; font-size: var(--t-xs); color: var(--text-faint);
  line-height: 1.6; max-width: 680px;
}

.panne-inline {
  margin: 12px 0 0; padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-sm); line-height: 1.6; color: var(--err); max-width: 680px;
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.panne-inline p { margin: 0 0 8px; }
.panne-inline button { font-size: var(--t-sm); }

@media (max-width: 700px) {
  .pieces .poids { margin-left: 0; }
  .bouton { min-height: 32px; padding: 7px 12px; }
}
</style>
