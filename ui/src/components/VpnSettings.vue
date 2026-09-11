<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  vpn: { type: Object, required: true },
})
const emit = defineEmits(['change'])

/** Quatre verdicts, et surtout DEUX qui ne doivent jamais se confondre :
 *  « indéterminé » veut dire qu'une adresse a bien été mesurée sans qu'on
 *  puisse dire par où elle sort ; « non aboutie » veut dire qu'il n'y a pas eu
 *  de mesure du tout. Les afficher pareil ferait passer une panne réseau pour
 *  une absence de VPN — c'est la seule erreur qui rendrait cet écran nuisible,
 *  puisqu'elle pousse à débrancher un tunnel qui marchait. */
const VERDICTS = {
  protected: {
    ton: 'ok',
    titre: 'Sortie protégée',
    precision: 'La sortie par le tunnel est constatée par la mesure, pas supposée.',
  },
  exposed: {
    ton: 'alerte',
    titre: 'Sortie en clair',
    precision:
      "L'adresse vue par le service d'écho est celle enregistrée comme étant celle de la maison.",
  },
  unknown: {
    ton: 'attention',
    titre: 'Sortie indéterminée',
    precision:
      "Une adresse a bien été mesurée : ce qui manque, c'est de quoi la comparer. Ce n'est ni un oui ni un non.",
  },
  unavailable: {
    ton: 'attention',
    titre: 'Vérification non aboutie',
    precision:
      "Aucune mesure n'a pu être faite. Ce n'est PAS une absence de VPN : rien n'est affirmé ici sur la sortie, ni dans un sens ni dans l'autre.",
  },
}

const enCours = ref(false)
const resultat = ref(null)
/** Distinct du verdict « non aboutie » : là, c'est Sortilège lui-même qui n'a
 *  pas répondu, donc pas même une mesure ratée à rapporter. */
const panne = ref(null)

// Le témoin, lui, revient du serveur en clair — ce n'est pas un secret, c'est
// une adresse. Le champ montre donc la valeur enregistrée au lieu de rester
// vide comme les champs de clé.
const brouillon = ref(props.vpn.reference_ip ?? '')
watch(
  () => props.vpn.reference_ip,
  (valeur) => {
    brouillon.value = valeur ?? ''
  },
)

const politiques = computed(() => props.vpn.policies ?? [])

/** Une politique posée hors de cette interface — fichier de préférences édité
 *  à la main — ne cocherait aucune case, et l'écran laisserait croire qu'aucun
 *  choix n'est fait alors qu'un choix s'applique. On le dit plutôt que de
 *  laisser un groupe muet. */
const politiqueInconnue = computed(
  () =>
    politiques.value.length > 0 &&
    !politiques.value.some((p) => p.key === props.vpn.policy),
)

const temoin = computed(() => (props.vpn.reference_ip ?? '').trim())
const saisie = computed(() => brouillon.value.trim())
const inchange = computed(() => saisie.value === temoin.value)

const verdict = computed(() => {
  if (!resultat.value) return null
  return (
    VERDICTS[resultat.value.verdict] ?? {
      ton: 'attention',
      titre: `Verdict « ${resultat.value.verdict} » inconnu de cette interface`,
      precision:
        "Le serveur a renvoyé un verdict que cette version de l'écran ne sait pas colorer. La phrase ci-dessous vient de lui et reste valable.",
    }
  )
})

/** Le détail sous la phrase : ce qui a été observé, champ par champ. Il ne
 *  remplace pas l'explication — il permet de vérifier ce sur quoi elle repose,
 *  et de voir tout de suite qu'une interface existe pendant que la sortie,
 *  elle, ne passe pas dedans. */
const details = computed(() => {
  const r = resultat.value
  if (!r) return []
  const lignes = []

  if (r.tunnel_present === null || r.tunnel_present === undefined) {
    lignes.push({
      label: 'Interfaces de tunnel',
      valeur: "illisibles depuis le conteneur — ce n'est pas « aucune »",
    })
  } else if (r.tunnel_interfaces?.length) {
    lignes.push({
      label: 'Interfaces de tunnel',
      valeur: r.tunnel_interfaces.join(', '),
      mono: true,
    })
  } else {
    lignes.push({ label: 'Interfaces de tunnel', valeur: 'aucune visible' })
  }

  if (r.public_ip) {
    lignes.push({ label: 'Adresse publique mesurée', valeur: r.public_ip, mono: true })
  }
  if (r.organization) lignes.push({ label: 'Organisation', valeur: r.organization })
  if (r.provider) lignes.push({ label: 'Fournisseur reconnu', valeur: r.provider })
  if (r.echo_service) {
    lignes.push({ label: "Service d'écho interrogé", valeur: r.echo_service, mono: true })
  }
  if (r.error) lignes.push({ label: 'Erreur remontée', valeur: r.error, alerte: true })

  return lignes
})

function patch(fields) {
  emit('change', fields)
}

function enregistrerTemoin() {
  if (!saisie.value || inchange.value) return
  patch({ reference_ip: saisie.value })
}

function effacerTemoin() {
  // « - » efface explicitement le témoin côté serveur, comme « - » efface une
  // clé ailleurs : envoyer une chaîne vide serait ambigu d'un bloc à l'autre.
  patch({ reference_ip: '-' })
}

// Il n'y a plus de bouton « adopter l'adresse mesurée comme témoin ». Un clic
// suffisait à enregistrer l'adresse du TUNNEL comme celle de la maison, et tous
// les verdicts suivants s'inversaient : sortie protégée lue « en clair », vraie
// fuite lue « protégée ». Le relevé se fait à la main, VPN coupé, et l'écran
// dit comment.

/** Mesure MAINTENANT : le serveur court-circuite son cache d'une minute, parce
 *  qu'on presse ce bouton justement après avoir changé quelque chose. */
async function verifier() {
  enCours.value = true
  panne.value = null
  resultat.value = null
  try {
    const res = await fetch('/api/settings/vpn/test', { method: 'POST' })
    // Une 502 arrive en HTML : `res.json()` lèverait et l'échec se lirait
    // « serveur injoignable », ce qui est faux — il a répondu, mal.
    const brut = await res.text()
    let corps = null
    try {
      corps = JSON.parse(brut)
    } catch {
      corps = null
    }
    if (!res.ok) {
      panne.value =
        corps?.detail ?? `Le serveur a refusé la vérification (réponse ${res.status}).`
      return
    }
    if (!corps) {
      panne.value = "Réponse illisible du serveur : la vérification n'a rien rapporté."
      return
    }
    resultat.value = corps
  } catch {
    panne.value =
      "Serveur injoignable : la vérification n'a même pas pu être demandée. Ce n'est pas un verdict sur la sortie."
  } finally {
    enCours.value = false
  }
}
</script>

<template>
  <section>
    <h3>Sortie réseau et VPN</h3>
    <p class="note">
      <strong>Sortilège émet déjà, à chaque identification.</strong> Il interroge
      TheMovieDB, AniList, OpenSubtitles, Discord et le fournisseur d'IA choisi ; chacun de
      ces appels montre l'adresse publique de la maison, et la suite des requêtes dessine,
      chez qui les reçoit, le catalogue de ce qu'on regarde et l'heure à laquelle on range.
      Ce n'est pas un réglage de téléchargement — il n'y a pas encore de téléchargement.
    </p>
    <p class="note">
      Ce réglage <strong>ne monte aucun tunnel</strong> : il vérifie par où le trafic sort.
      Le tunnel se pose ailleurs — sur le routeur, sur l'hôte, ou dans un conteneur dédié.
      Ce qui manque presque partout, et qui est fait ici, c'est de le constater plutôt que
      de l'afficher.
    </p>

    <fieldset class="politiques">
      <legend>Que faire du trafic sortant</legend>

      <p v-if="!politiques.length" class="hint attention">
        Le serveur n'a renvoyé aucune politique : impossible de choisir ici. La politique
        active reste
        <code>{{ vpn.policy || 'inconnue' }}</code
        >.
      </p>

      <div
        v-for="p in politiques"
        :key="p.key"
        class="option"
        :class="{ actif: vpn.policy === p.key }"
      >
        <input
          :id="`vpn-politique-${p.key}`"
          type="radio"
          name="vpn-politique"
          :value="p.key"
          :checked="vpn.policy === p.key"
          @change="patch({ policy: p.key })"
        />
        <div class="corps">
          <label :for="`vpn-politique-${p.key}`">{{ p.label }}</label>
          <!-- La phrase vient du serveur et n'est pas reformulée : c'est elle
               qui dit ce que fait vraiment chaque mode. Un menu déroulant la
               cacherait derrière un mot de trois syllabes. -->
          <p class="summary">{{ p.summary }}</p>

          <div v-if="p.key === 'require'" class="consequence">
            <p>Tant que la sortie n'est pas <strong>confirmée</strong>, sont arrêtés :</p>
            <ul class="arrets">
              <li>l'identification ;</li>
              <li>la recherche et le choix d'un candidat ;</li>
              <li>les collections ;</li>
              <li>le téléchargement des affiches ;</li>
              <li>les sous-titres ;</li>
              <li>les notifications Discord ;</li>
              <li>
                les boutons d'essai de TheMovieDB, du résolveur IA, d'OpenSubtitles et de
                Discord.
              </li>
            </ul>
            <p>
              Ne sont pas arrêtés : le rafraîchissement du serveur multimédia (réseau local) et
              la vérification de sortie elle-même. Une panne du service d'écho, un conteneur
              sans accès sortant ou un tunnel qui vient de tomber suffisent à tout arrêter. À
              lire avant de cocher, pas après.
            </p>
          </div>
          <p v-if="p.key === 'require' && !temoin" class="consequence attention">
            Aucune adresse de référence n'est enregistrée. Si le fournisseur n'est pas
            reconnu à son organisation, le verdict restera « indéterminé » — donc refus
            d'émettre dans ce mode. Renseigne le témoin ci-dessous d'abord.
          </p>
        </div>
      </div>

      <p v-if="politiqueInconnue" class="hint attention">
        La politique enregistrée
        <code>{{ vpn.policy }}</code>
        ne figure pas parmi celles que le serveur propose : aucune case n'est cochée
        ci-dessus, alors qu'un réglage s'applique bel et bien.
      </p>
    </fieldset>

    <div class="field">
      <label for="vpn-reference">Adresse de référence (relevée VPN coupé)</label>
      <div class="row">
        <input
          id="vpn-reference"
          v-model="brouillon"
          autocomplete="off"
          spellcheck="false"
          placeholder="par exemple 88.120.4.17"
          @keyup.enter="enregistrerTemoin"
        />
        <button type="button" :disabled="!saisie || inchange" @click="enregistrerTemoin">
          Enregistrer
        </button>
        <button v-if="temoin" type="button" class="clear" @click="effacerTemoin">Effacer</button>
      </div>

      <p class="hint">
        C'est l'adresse publique de la maison, relevée <strong>tunnel coupé</strong>. Sans
        elle, une sortie protégée par un fournisseur que Sortilège ne reconnaît pas à son
        nom est indistinguable d'une sortie en clair : deux adresses inconnues se
        ressemblent.
      </p>

      <!-- Un bouton grisé sans motif affiché passe pour cassé. -->
      <p v-if="!saisie" class="hint">
        « Enregistrer » attend une adresse. Vider le champ n'efface pas le témoin : c'est
        « Effacer » qui le fait.
      </p>
      <p v-else-if="inchange" class="hint">
        L'adresse affichée est déjà celle qui est enregistrée : rien à enregistrer.
      </p>

      <p v-if="temoin" class="hint">
        Témoin actuel : <code>{{ temoin }}</code
        >. Une adresse domestique attribuée en DHCP change parfois toute seule — ce jour-là,
        une référence périmée ferait conclure à tort à une sortie protégée. « Effacer »
        vaut mieux qu'un témoin douteux.
      </p>
      <p v-else class="hint attention">
        Aucun témoin enregistré. Pour le relever : <strong>coupe le VPN</strong>, clique
        « Vérifier maintenant » ci-dessous, puis recopie ici l'adresse mesurée et enregistre-la.
        Rebranche ensuite le tunnel et vérifie de nouveau : le verdict doit changer.
      </p>
    </div>

    <div class="essai">
      <button type="button" :disabled="enCours" @click="verifier">
        {{ enCours ? 'Mesure en cours…' : 'Vérifier maintenant' }}
      </button>
      <span v-if="enCours" class="etat attente">
        <span class="dot"></span>
        Une requête part vers un service d'écho, par le chemin même qu'on vérifie.
      </span>
      <span v-else-if="!resultat && !panne" class="indispo">
        Aucune vérification depuis l'ouverture de cet écran.
      </span>
    </div>

    <!-- Trois états : rien encore / la demande a échoué / un verdict. -->
    <p v-if="!resultat && !panne && !enCours" class="hint">
      La mesure est faite au moment du clic, sans resservir la précédente : on presse ce
      bouton justement parce qu'on vient de brancher, de redémarrer ou de couper quelque
      chose.
    </p>

    <p v-else-if="panne" class="panne">{{ panne }}</p>

    <div v-else-if="resultat" class="verdict" :class="verdict.ton">
      <div class="bandeau">
        <span class="pastille"></span>
        <strong>{{ verdict.titre }}</strong>
      </div>
      <p class="precision">{{ verdict.precision }}</p>
      <!-- Rédigée côté serveur pour être lue telle quelle. -->
      <p class="explication">{{ resultat.explanation }}</p>

      <dl v-if="details.length" class="details">
        <template v-for="d in details" :key="d.label">
          <dt>{{ d.label }}</dt>
          <dd :class="{ mono: d.mono, alerte: d.alerte }">{{ d.valeur }}</dd>
        </template>
      </dl>

      <p v-if="resultat.tunnel_interfaces?.length" class="hint">
        Une interface de tunnel présente ne prouve pas que le trafic sort par elle : la
        route par défaut peut parfaitement l'ignorer. Seule l'adresse mesurée porte sur ce
        qui est réellement émis.
      </p>

      <!-- Pas de bouton « adopter comme témoin » : voir le commentaire du script. -->
      <p v-if="resultat.public_ip && resultat.public_ip !== temoin" class="hint consigne">
        Pour faire de cette adresse le témoin : <strong>seulement si le VPN est coupé en ce
        moment</strong>, recopie <code>{{ resultat.public_ip }}</code> dans « Adresse de
        référence » ci-dessus, puis enregistre. Tunnel ouvert, n'en fais rien : enregistrer
        l'adresse du tunnel inverserait les verdicts — toute sortie protégée passerait pour
        exposée, et une vraie fuite pour une sortie protégée.
      </p>
    </div>
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
.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.note strong { color: var(--text-dim); }

.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint.attention { color: var(--warn); opacity: .9; }
.hint.consigne { color: var(--text-dim); }
.hint strong { color: inherit; font-weight: 600; }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

/* Trois choix visibles d'un coup, chacun avec la phrase qui dit ce qu'il fait :
   c'est tout l'intérêt des radios contre un menu déroulant ici. */
.politiques { border: 0; margin: 16px 0 0; padding: 0; }
.politiques legend {
  padding: 0; font-size: var(--t-sm); color: var(--text-dim); margin-bottom: 7px;
}
.option {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface-2); margin-bottom: 7px;
}
.option.actif { border-color: var(--accent-dim); }
.option input { margin: 2px 0 0; accent-color: var(--accent); flex: none; }
.corps { min-width: 0; }
.corps > label { display: block; font-size: var(--t-sm); color: var(--text); cursor: pointer; }
.summary { margin: 3px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 620px; }
.consequence { margin: 5px 0 0; font-size: var(--t-sm); color: var(--text-dim); line-height: 1.6; max-width: 620px; }
.consequence p { margin: 0; }
.arrets { margin: 3px 0 5px; padding-left: 18px; }
.arrets li { margin: 1px 0; }
.consequence.attention { color: var(--warn); opacity: .9; }

.field { margin-top: 18px; }
.field > label { display: block; font-size: var(--t-sm); color: var(--text-dim); margin-bottom: 5px; }
.row { display: flex; gap: 8px; align-items: center; }
.row input {
  flex: 1; min-width: 0; font-size: var(--t-sm); padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--mono);
}
.clear { color: var(--text-faint); }
.clear:hover { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }

.essai { margin-top: 18px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.essai .indispo { font-size: var(--t-xs); color: var(--text-faint); }
.etat { display: flex; align-items: center; gap: 8px; font-size: var(--t-xs); }
.etat .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }
.etat.attente { color: var(--text-faint); }

/* La demande elle-même n'a pas abouti : ce n'est pas un verdict, donc pas une
   carte de verdict. */
.panne {
  margin: 10px 0 0; font-size: var(--t-sm); color: var(--err); line-height: 1.6; max-width: 660px;
}

.verdict {
  margin-top: 12px; padding: 11px 13px; border-radius: 8px;
  background: var(--surface-2); border: 1px solid var(--border);
  max-width: 660px;
}
.bandeau { display: flex; align-items: center; gap: 8px; font-size: var(--t-sm); }
.pastille { width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }
.verdict.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, var(--border)); }
.verdict.alerte { color: var(--err); border-color: color-mix(in srgb, var(--err) 45%, var(--border)); }
/* « Indéterminé » et « non aboutie » partagent la couleur — ni bon ni mauvais —
   mais jamais le libellé : la pastille creuse dit qu'il n'y a pas eu de mesure. */
.verdict.attention { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, var(--border)); }
.precision { margin: 5px 0 0; font-size: var(--t-sm); color: currentColor; opacity: .85; line-height: 1.6; }
.explication { margin: 8px 0 0; font-size: var(--t-sm); color: var(--text); line-height: 1.6; }

.details {
  margin: 10px 0 0; display: grid; grid-template-columns: 190px 1fr;
  gap: 3px 12px; font-size: var(--t-xs);
}
.details dt { color: var(--text-faint); }
.details dd { margin: 0; color: var(--text-dim); word-break: break-word; }
.details dd.mono { font-family: var(--mono); }
.details dd.alerte { color: var(--warn); }

@media (max-width: 700px) {
  .row { flex-wrap: wrap; }
  .row input { flex: 1 0 100%; }
  .details { grid-template-columns: 1fr; gap: 1px; }
  .details dd { margin-bottom: 5px; }
}
</style>
