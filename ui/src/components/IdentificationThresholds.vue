<script setup>
import { computed, ref, useId, watch } from 'vue'

/**
 * Les deux seuils qui transforment un score de confiance en verdict :
 * prêt à ranger, à vérifier, écarté.
 *
 * Ils ne se réglaient que dans l'environnement, en fraction (0.92), et
 * n'apparaissaient qu'en lecture seule dans l'onglet Système. Ils se règlent
 * ici en pourcentage ; l'environnement reste lu en repli, seuil par seuil,
 * exactement comme la clé TheMovieDB.
 */

const props = defineProps({
  /**
   * Le bloc `identification` de `GET /api/settings/preferences` :
   * `{ auto_apply_percent, reject_percent, auto_apply_source, reject_source,
   *    environment: { auto_apply_percent, reject_percent, auto_apply_declared,
   *                   reject_declared },
   *    ignored_reason }`.
   */
  identification: { type: Object, required: true },
  /** Le bloc `automation` : `apply_auto` dit si le rangement part sans
   *  personne devant, `enabled` si la boucle tourne en ce moment. */
  automation: { type: Object, default: null },
  /** Le bloc `ai`, pour situer son seuil par rapport à ceux-ci. */
  ai: { type: Object, default: null },
})

const emit = defineEmits(['change'])

const VARIABLES = {
  auto_apply: 'SORTILEGE_AUTO_APPLY_THRESHOLD',
  reject: 'SORTILEGE_REJECT_THRESHOLD',
}

const env = computed(() => props.identification.environment ?? {})

/** Valeur en place. Le parent applique le patch à l'écran avant la réponse :
 *  un `null` (retour à l'environnement) y passe le temps d'un aller-retour, et
 *  afficherait un champ vide si on ne lisait pas l'environnement à sa place. */
function enPlace(champ) {
  const v = props.identification[`${champ}_percent`]
  return typeof v === 'number' ? v : env.value[`${champ}_percent`]
}

const pret = ref(enPlace('auto_apply'))
const ecarte = ref(enPlace('reject'))

function resynchroniser() {
  pret.value = enPlace('auto_apply')
  ecarte.value = enPlace('reject')
}
// Chaque enregistrement — ou relecture après un refus — remplace le bloc :
// c'est le signal que les valeurs affichées doivent redevenir celles en place.
watch(() => props.identification, resynchroniser, { deep: true })

const idPret = useId()
const idPretLibelle = useId()
const idPretSource = useId()
const idEcarte = useId()
const idEcarteLibelle = useId()
const idEcarteSource = useId()

function horsBornes(v) {
  return !Number.isInteger(v) || v < 0 || v > 100
}

/** Le même refus que le serveur, dit avant l'envoi. Rien ne part tant qu'il
 *  tient : envoyer pour se faire refuser relirait les réglages et effacerait
 *  la saisie qu'on est justement en train de corriger. */
const refusLocal = computed(() => {
  if (horsBornes(pret.value)) {
    return '« Prêt à ranger à partir de » attend un pourcentage entier entre 0 et 100.'
  }
  if (horsBornes(ecarte.value)) {
    return '« Écarté en dessous de » attend un pourcentage entier entre 0 et 100.'
  }
  if (ecarte.value >= pret.value) {
    return (
      `« Écarté en dessous de » (${ecarte.value} %) doit rester strictement sous « Prêt à ` +
      `ranger à partir de » (${pret.value} %) : sinon plus aucun plan n'arrive « à vérifier », ` +
      'tout est rangé sans relecture ou écarté.'
    )
  }
  return ''
})

const modifie = computed(
  () => pret.value !== enPlace('auto_apply') || ecarte.value !== enPlace('reject'),
)

/** N'envoie que ce qui a changé : un seuil laissé à l'environnement doit y
 *  rester, sinon toucher l'un figerait l'autre en réglage sans qu'on l'ait
 *  voulu. Les deux partent ensemble quand les deux ont bougé — c'est le cas
 *  d'une incohérence qu'on vient de corriger en déplaçant le second. */
function enregistrer() {
  if (refusLocal.value) return
  const patch = {}
  if (pret.value !== enPlace('auto_apply')) patch.auto_apply_percent = pret.value
  if (ecarte.value !== enPlace('reject')) patch.reject_percent = ecarte.value
  if (Object.keys(patch).length) emit('change', patch)
}

function revenirALEnvironnement() {
  emit('change', { auto_apply_percent: null, reject_percent: null })
}

const aUnReglage = computed(
  () =>
    props.identification.auto_apply_source === 'reglage' ||
    props.identification.reject_source === 'reglage' ||
    Boolean(props.identification.ignored_reason),
)

function provenance(champ) {
  if (props.identification[`${champ}_source`] === 'reglage') {
    return `Réglé ici. Valeur de l'environnement : ${env.value[`${champ}_percent`]} %.`
  }
  if (env.value[`${champ}_declared`]) {
    return `Vient de ${VARIABLES[champ]} (environnement).`
  }
  return `Valeur livrée avec Sortilège : ${VARIABLES[champ]} n'est pas posée.`
}

// --- La barre des trois zones ---------------------------------------------
// Dessinée avec la saisie en cours, pas avec les valeurs enregistrées : c'est
// en déplaçant le curseur qu'on veut voir ce que ça change.
const zones = computed(() => {
  const e = Math.min(100, Math.max(0, Number(ecarte.value) || 0))
  const p = Math.min(100, Math.max(0, Number(pret.value) || 0))
  const milieu = Math.max(0, p - e)
  return { ecarte: e, verifier: milieu, pret: Math.max(0, 100 - Math.max(p, e)) }
})

const resumeBarre = computed(
  () =>
    `Écarté sous ${ecarte.value} %, à vérifier de ${ecarte.value} % à moins de ` +
    `${pret.value} %, prêt à ranger à partir de ${pret.value} %.`,
)

// --- Le résolveur IA, situé par rapport aux seuils ------------------------
// Il n'est consulté que pour un plan qui N'EST PAS prêt à ranger et dont le
// score passe sous son propre seuil (core/pipeline.py, seconde passe). Un seuil
// IA au-dessus du seuil « prêt » ne coûte donc aucun appel sur un plan prêt.
const phraseIa = computed(() => {
  if (!props.ai?.enabled || typeof props.ai.threshold !== 'number' || refusLocal.value) return ''
  const ia = Math.round(props.ai.threshold * 100)
  if (ia > pret.value) {
    return (
      `Le seuil du résolveur IA (${ia} %) dépasse le seuil « prêt » (${pret.value} %). Aucun ` +
      "appel perdu pour autant : un plan prêt à ranger n'est jamais soumis à l'IA. Elle est " +
      'consultée sur les plans à vérifier ou écartés.'
    )
  }
  if (ia <= ecarte.value) {
    return (
      `Le seuil du résolveur IA (${ia} %) ne dépasse pas le seuil « écarté » ` +
      `(${ecarte.value} %) : l'IA n'est consultée que sur des plans déjà écartés, jamais sur ` +
      'ceux à vérifier.'
    )
  }
  if (ia === pret.value) {
    return `Le résolveur IA (seuil ${ia} %) est consulté sur les plans à vérifier ou écartés.`
  }
  return (
    `Le résolveur IA est consulté sous ${ia} %. Les plans à vérifier entre ${ia} % et ` +
    `${pret.value} % partent en revue sans lui.`
  )
})

// --- Reclassement de la file déjà calculée ---------------------------------
// Le verdict d'un plan était figé à son calcul : régler « prêt » à 60 % laissait
// « à vérifier » un plan à 67 %. La file est maintenant rejouée sous les seuils
// enregistrés — par le parent après chaque enregistrement RÉUSSI (`reclasser`
// est exposé), ou par le bouton. Seuls les verdicts venus des seuils bougent.

const reclassement = ref(null)
const reclassEnCours = ref(false)
const panneReclassement = ref(null)
// Deux enregistrements rapprochés : le second reclassement ne doit pas être
// perdu parce que le premier tournait encore, sinon l'écran montrerait le
// compte rendu des seuils d'avant.
let aRefaire = false

async function lireJson(res) {
  const brut = await res.text()
  try {
    return JSON.parse(brut)
  } catch {
    return null
  }
}

async function reclasser() {
  if (reclassEnCours.value) {
    aRefaire = true
    return
  }
  reclassEnCours.value = true
  panneReclassement.value = null
  try {
    const res = await fetch('/api/review/redecide', { method: 'POST' })
    const corps = await lireJson(res)
    if (!res.ok || !corps) {
      const motif = typeof corps?.detail === 'string' && corps.detail
      panneReclassement.value =
        (motif || `Reclassement refusé (réponse ${res.status}).`) +
        ' La file garde ses verdicts précédents.'
      return
    }
    reclassement.value = corps
  } catch {
    panneReclassement.value =
      "Serveur injoignable : la file n'a pas été reclassée, elle garde ses verdicts précédents."
  } finally {
    reclassEnCours.value = false
    if (aRefaire) {
      aRefaire = false
      reclasser()
    }
  }
}

defineExpose({ reclasser })

function pluriel(n, un, plusieurs) {
  return n > 1 ? plusieurs : un
}

/** Le compte rendu, phrase par phrase. Chaque plan de la file est compté une
 *  fois : ce qui n'a pas bougé est dit, avec sa raison. */
const phrasesReclassement = computed(() => {
  const r = reclassement.value
  if (!r) return []
  const pourcent = (v) => Math.round(v * 100)
  const phrases = []
  const sous =
    `sous « prêt à ranger » ${pourcent(r.policy.auto_apply_threshold)} % et « écarté » ` +
    `${pourcent(r.policy.reject_threshold)} %`

  if (!r.examined) {
    phrases.push(`File reclassée ${sous} : elle est vide, rien n'a bougé.`)
    return phrases
  }

  const vers = r.changed_to ?? {}
  const morceaux = [
    [vers.auto, 'prêt à ranger', 'prêts à ranger'],
    [vers.review, 'à vérifier', 'à vérifier'],
    [vers.reject, 'écarté', 'écartés'],
  ]
    .filter(([n]) => n > 0)
    .map(([n, un, plusieurs]) => `${n} « ${pluriel(n, un, plusieurs)} »`)
  if (r.changed) {
    phrases.push(
      `File reclassée ${sous} : ${r.changed} ${pluriel(r.changed, 'plan change', 'plans changent')} ` +
        `de verdict — ${morceaux.join(', ')}.`,
    )
  } else {
    phrases.push(`File reclassée ${sous} : aucun plan ne change de verdict.`)
  }
  if (r.unchanged) {
    phrases.push(
      `${r.unchanged} ${pluriel(r.unchanged, 'plan garde son', 'plans gardent leur')} verdict ` +
        'sous ces seuils.',
    )
  }
  if (r.imposed) {
    phrases.push(
      `${r.imposed} ${pluriel(r.imposed, "plan n'a pas bougé : son verdict est imposé", "plans n'ont pas bougé : leur verdict est imposé")} ` +
        '(confirmation ou choix à la main, identification retenue, identifiant déclaré, livre, ' +
        "série sans numéro d'épisode, renommage, ou échec d'identification). C'est voulu.",
    )
  }
  return phrases
})

const phraseAnciens = computed(() => {
  const n = reclassement.value?.legacy
  if (!n) return ''
  return (
    `${n} ${pluriel(n, 'plan a été calculé', 'plans ont été calculés')} par une version ` +
    `précédente et ${pluriel(n, 'ne peut pas être reclassé', 'ne peuvent pas être reclassés')} :`
  )
})

/** Le cycle automatique ne range que ce qu'il vient lui-même d'identifier
 *  (api/automation.py : il n'applique que les plans de son propre lot, et ne
 *  replanifie jamais un fichier déjà dans la file). Des plans devenus prêts
 *  par reclassement n'en font pas partie. */
const phraseRangementAuto = computed(() => {
  const n = reclassement.value?.changed_to?.auto
  if (!n || !props.automation?.apply_auto) return ''
  return (
    `Le rangement automatique ne déplacera pas ${pluriel(n, 'ce plan', 'ces ' + n + ' plans')} : ` +
    "le cycle ne range que les fichiers qu'il vient lui-même d'identifier. " +
    `${pluriel(n, 'Il attend', 'Ils attendent')} le bouton « Ranger … prêts » de l'onglet Ranger.`
  )
})

const raisonBoutonReclasser = computed(() => {
  if (modifie.value) {
    return "Les valeurs affichées ne sont pas enregistrées : la file serait reclassée sous les seuils en place."
  }
  return ''
})
</script>

<template>
  <section>
    <h3>Seuils de décision</h3>
    <p class="note">
      Chaque plan reçoit un score de confiance entre 0 et 100 %. Deux seuils le
      transforment en verdict : <strong>prêt à ranger</strong>, <strong>à vérifier</strong>
      ou <strong>écarté</strong>.
    </p>

    <!-- Les trois zones, avec la saisie en cours. -->
    <div class="barre" role="img" :aria-label="resumeBarre">
      <span class="zone z-ecarte" :style="{ flexBasis: `${zones.ecarte}%` }"></span>
      <span class="zone z-verifier" :style="{ flexBasis: `${zones.verifier}%` }"></span>
      <span class="zone z-pret" :style="{ flexBasis: `${zones.pret}%` }"></span>
    </div>
    <ul class="legende">
      <li><span class="pastille z-ecarte"></span>Écarté : score sous {{ ecarte }} %</li>
      <li>
        <span class="pastille z-verifier"></span>À vérifier : de {{ ecarte }} % à moins de
        {{ pret }} %
      </li>
      <li><span class="pastille z-pret"></span>Prêt à ranger : {{ pret }} % et plus</li>
    </ul>

    <div class="reglage">
      <label :id="idPretLibelle" :for="idPret">Prêt à ranger à partir de</label>
      <div class="saisie">
        <input
          :id="idPret"
          v-model.number="pret"
          class="nombre"
          type="number"
          min="0"
          max="100"
          step="1"
          :aria-describedby="idPretSource"
          @change="enregistrer"
        />
        <span class="unite">%</span>
        <input
          v-model.number="pret"
          class="curseur"
          type="range"
          min="0"
          max="100"
          step="1"
          :aria-labelledby="idPretLibelle"
          :aria-describedby="idPretSource"
          @change="enregistrer"
        />
      </div>
      <p :id="idPretSource" class="source" :class="{ ici: identification.auto_apply_source === 'reglage' }">
        {{ provenance('auto_apply') }}
      </p>
    </div>

    <div class="reglage">
      <label :id="idEcarteLibelle" :for="idEcarte">Écarté en dessous de</label>
      <div class="saisie">
        <input
          :id="idEcarte"
          v-model.number="ecarte"
          class="nombre"
          type="number"
          min="0"
          max="100"
          step="1"
          :aria-describedby="idEcarteSource"
          @change="enregistrer"
        />
        <span class="unite">%</span>
        <input
          v-model.number="ecarte"
          class="curseur"
          type="range"
          min="0"
          max="100"
          step="1"
          :aria-labelledby="idEcarteLibelle"
          :aria-describedby="idEcarteSource"
          @change="enregistrer"
        />
      </div>
      <p :id="idEcarteSource" class="source" :class="{ ici: identification.reject_source === 'reglage' }">
        {{ provenance('reject') }}
      </p>
    </div>

    <div v-if="refusLocal" class="refus" role="alert">
      <p>{{ refusLocal }} Rien n'est enregistré tant que c'est le cas.</p>
      <button type="button" @click="resynchroniser">Rétablir les valeurs en place</button>
    </div>
    <p v-else-if="modifie" class="hint">
      Enregistré en relâchant le curseur, ou en quittant le champ.
    </p>

    <p v-if="identification.ignored_reason" class="hint attention" role="status">
      {{ identification.ignored_reason }}
    </p>

    <div v-if="aUnReglage" class="retour">
      <button type="button" @click="revenirALEnvironnement">Revenir aux valeurs du .env</button>
      <span class="hint en-ligne">
        Efface ce qui est réglé ici : « prêt » revient à {{ env.auto_apply_percent }} % et
        « écarté » à {{ env.reject_percent }} %, les valeurs de l'environnement.
      </span>
    </div>
    <p v-else class="hint">
      Les deux seuils viennent de l'environnement. Les modifier ici les remplace sans toucher
      au fichier <code>.env</code>, qui reste lu en repli.
    </p>

    <p v-if="automation?.apply_auto" class="hint attention">
      <strong>Le rangement automatique est actif</strong> (Réglages → Automatisation) : chaque
      fichier que le cycle identifie et qui atteint le seuil « prêt » est déplacé sans relecture
      humaine. Baisser ce seuil laisse partir davantage de fichiers sans que personne ne les voie.
      <template v-if="!automation.enabled">
        La boucle est arrêtée pour l'instant : cela vaudra dès qu'elle sera relancée.
      </template>
    </p>
    <p v-else class="hint">
      Le rangement automatique est désactivé : « prêt » veut dire proposé au bouton
      <strong>Ranger</strong>, rien ne se déplace tout seul.
    </p>

    <p v-if="phraseIa" class="hint">{{ phraseIa }}</p>

    <div class="reclasse">
      <p class="hint">
        <strong>Enregistrer un seuil reclasse aussi la file déjà calculée.</strong> Les plans dont
        le verdict vient des seuils suivent les nouvelles valeurs, sans rien redemander aux
        fournisseurs. Un verdict imposé ne bouge pas : confirmation ou choix à la main,
        identification retenue, et les cas ci-dessous qui passent outre les seuils.
      </p>
      <div class="reclasse-action">
        <button
          type="button"
          :disabled="reclassEnCours || Boolean(raisonBoutonReclasser)"
          @click="reclasser"
        >
          {{ reclassEnCours ? 'Reclassement…' : 'Reclasser la file maintenant' }}
        </button>
        <span v-if="raisonBoutonReclasser" class="hint en-ligne">{{ raisonBoutonReclasser }}</span>
      </div>

      <div aria-live="polite">
        <p v-if="panneReclassement" class="hint erreur" role="alert">{{ panneReclassement }}</p>
        <template v-else-if="reclassement">
          <p v-for="phrase in phrasesReclassement" :key="phrase" class="hint">{{ phrase }}</p>
          <p v-if="phraseAnciens" class="hint attention">
            {{ phraseAnciens }} <strong>Ranger → Entretien → « Recommencer l'identification »</strong>,
            qui vide la file et repasse chaque fichier chez les fournisseurs.
          </p>
          <p v-if="phraseRangementAuto" class="hint attention">{{ phraseRangementAuto }}</p>
          <p v-if="reclassement.planning_running" class="hint attention">
            Un calcul de plans (lot ou cycle automatique) est en cours : les plans qu'il publie
            encore suivent les seuils de son lancement. Reclasse la file une fois le calcul terminé.
          </p>
        </template>
      </div>
    </div>
    <p class="hint">
      Deux cas passent outre les seuils : un identifiant déclaré (<code>.nfo</code> ou tags du
      fichier) qui concorde rend le plan prêt, et un identifiant qui désigne une autre œuvre
      l'écarte. Une durée incompatible avec le type retenu ramène un plan prêt « à vérifier ».
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
.note { margin: 0 0 14px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }

/* --- Barre des trois zones --- */
.barre {
  display: flex; height: 12px; max-width: 680px; border-radius: 6px; overflow: hidden;
  border: 1px solid var(--border); background: var(--surface-2);
}
.zone { flex-grow: 0; flex-shrink: 0; transition: flex-basis .12s; }
.z-ecarte { background: color-mix(in srgb, var(--err) 55%, var(--surface-2)); }
.z-verifier { background: color-mix(in srgb, var(--warn) 55%, var(--surface-2)); }
.z-pret { background: color-mix(in srgb, var(--ok) 55%, var(--surface-2)); }
@media (prefers-reduced-motion: reduce) { .zone { transition: none; } }

.legende {
  list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 18px;
  font-size: var(--t-xs); color: var(--text-dim);
}
.legende li { display: flex; align-items: center; gap: 6px; }
.pastille { width: 10px; height: 10px; border-radius: 3px; flex: none; }

/* --- Réglages --- */
.reglage { margin-top: 16px; max-width: 680px; }
.reglage > label { display: block; font-size: var(--t-sm); color: var(--text-dim); margin-bottom: 5px; }
.saisie { display: flex; align-items: center; gap: 8px; }
.nombre {
  width: 72px; font-family: var(--mono); font-size: var(--t-sm); padding: 5px 8px;
  background: var(--surface-2);
}
.unite { font-size: var(--t-sm); color: var(--text-faint); }
.curseur { flex: 1; min-width: 0; padding: 0; margin-left: 6px; accent-color: var(--accent); }
.source { margin: 5px 0 0; font-size: var(--t-xs); color: var(--text-faint); }
.source.ici { color: var(--accent); }

.hint { margin: 10px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.hint strong { color: var(--text-dim); }
.hint.attention { color: var(--warn); }
.hint.attention strong { color: var(--warn); }
.hint.en-ligne { margin: 0; }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.refus {
  display: flex; align-items: flex-start; gap: 12px; margin-top: 12px; max-width: 680px;
  padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-sm); line-height: 1.6; color: var(--err);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}
.refus p { margin: 0; flex: 1; }
.refus button { flex: none; font-size: var(--t-xs); padding: 3px 10px; }

.retour { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 14px; }

/* --- Reclassement de la file --- */
.reclasse {
  margin-top: 16px; padding-top: 12px; max-width: 680px;
  border-top: 1px solid var(--border);
}
.reclasse > .hint:first-child { margin-top: 0; }
.reclasse-action { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.reclasse-action button { font-size: var(--t-sm); }
.hint.erreur { color: var(--err); }
.retour button { font-size: var(--t-sm); }

@media (max-width: 700px) {
  .saisie { flex-wrap: wrap; }
  .curseur { flex-basis: 100%; margin-left: 0; }
  .refus { flex-direction: column; }
}
</style>
