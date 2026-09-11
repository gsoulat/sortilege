<script setup>
import { computed, ref, useId } from 'vue'
import ConfirmAction from './ConfirmAction.vue'

/**
 * Métadonnées locales : ce que Sortilège dépose DANS la bibliothèque.
 *
 * Le réglage existait côté serveur, le pipeline le lisait, une route dédiée
 * l'attendait — et il n'apparaissait sur aucun écran. Personne ne pouvait donc
 * l'activer : la fonction la plus utile du produit, celle qui fait qu'un
 * arbitrage rendu à la main survit au rafraîchissement du serveur multimédia,
 * était inatteignable depuis l'interface. C'est cet écart-là que ce fichier
 * comble, et rien d'autre : aucune règle n'est réinventée ici, tout le
 * vocabulaire vient de `core/nfo` et de `core/opf`.
 */

const props = defineProps({
  /**
   * Le bloc `local_metadata` de `GET /api/settings/preferences` :
   * `{ nfo, artwork, opf, on_existing, on_existing_choices }`.
   */
  localMetadata: { type: Object, required: true },
})

const emit = defineEmits(['change'])

/**
 * Le patch part tel quel vers `PUT /api/settings/preferences`, sous
 * `local_metadata`. Chaque clé émise ici doit donc être déclarée dans le
 * schéma d'entrée côté serveur : un champ non déclaré est écarté en silence,
 * et la case correspondante retomberait à sa valeur d'avant au rechargement —
 * un réglage qui « ne tient pas » sans que rien ne le dise. `opf` est le
 * candidat à surveiller : il existe dans `LocalMetadataSettings` et dans le
 * pipeline, mais il n'est pas de la même famille que `nfo` et se déclare à
 * part.
 */
function patch(champs) {
  emit('change', champs)
}

// Un identifiant par contrôle, et un par phrase d'explication : `label for` et
// `aria-describedby` ne servent que si la cible existe vraiment, et deux
// instances du composant sur un même écran ne doivent pas se voler leurs ids.
const idNfo = useId()
const idNfoAide = useId()
const idArtwork = useId()
const idArtworkAide = useId()
const idOpf = useId()
const idOpfAide = useId()
const idExistant = useId()
const idExistantAide = useId()

/**
 * Ce que chaque conduite fait aux fichiers que l'utilisateur n'a PAS écrits.
 *
 * Les clés viennent du serveur (`on_existing_choices`, produit par
 * `OnExisting` dans `core/nfo`) : elles ne sont pas recopiées en dur ici, on
 * ne fait que les traduire. Une clé que le serveur enverrait sans que cet
 * écran la connaisse reste choisissable, mais elle le dit — proposer un
 * intitulé inventé serait pire que d'avouer l'ignorance, puisque le mot en
 * question décide du sort de fichiers qu'on n'a pas écrits.
 */
const CONDUITES = {
  skip: {
    label: 'Ne pas y toucher',
    resume:
      "Le fichier en place reste tel quel et rien n'est écrit à côté. C'est le défaut : " +
      'une fiche trouvée là a pu être posée à la main, ou écrite par tinyMediaManager avec ' +
      "bien plus de détails que nous n'en tenons. Une seule exception : au rangement, une " +
      "fiche qui décrit une autre œuvre que celle rangée est remplacée, et l'ancienne part " +
      'dans la corbeille.',
  },
  backup: {
    label: "Écrire, en gardant l'ancien",
    resume:
      "Notre fiche est écrite et l'ancienne part dans la corbeille de Sortilège, d'où elle " +
      'se récupère. Le compromis pour qui veut nos données sans jeter ce qui était là.',
  },
  overwrite: {
    label: 'Remplacer',
    resume:
      "Notre fiche remplace l'ancienne, qui part elle aussi dans la corbeille : rien n'est " +
      'détruit, et en pratique cette conduite fait la même chose que la précédente. À ne ' +
      "demander qu'en connaissance de cause : ce qui est remplacé n'a pas été écrit par Sortilège.",
  },
}

function decrire(cle) {
  return (
    CONDUITES[cle] ?? {
      label: cle,
      resume:
        'Conduite transmise par le serveur et inconnue de cet écran : rien ici ne dit ce ' +
        "qu'elle fait des fichiers déjà en place. Reporte-toi à la documentation de ta " +
        'version avant de la retenir.',
    }
  )
}

/**
 * Une valeur enregistrée hors de cette liste — fichier de préférences édité à
 * la main — afficherait un menu vide, et le premier choix l'écraserait sans
 * que personne ait voulu en changer. On l'ajoute donc plutôt que de la faire
 * disparaître.
 */
const conduites = computed(() => {
  const courante = props.localMetadata.on_existing
  const cles = [...(props.localMetadata.on_existing_choices ?? [])]
  if (courante && !cles.includes(courante)) cles.unshift(courante)
  return cles.map((cle) => ({ cle, ...decrire(cle) }))
})

const conduiteCourante = computed(() => decrire(props.localMetadata.on_existing))

/** Ce que devient un fichier déjà en place. « Rien n'est déplacé ni supprimé »
 *  n'est vrai qu'en « ne pas y toucher » : les deux autres conduites envoient
 *  ce qu'elles remplacent dans la corbeille. L'annoncer quelle que soit la
 *  conduite laissait croire qu'un second passage ne touchait à rien. */
const sortDesExistants = computed(() => {
  const c = props.localMetadata.on_existing
  if (c === 'skip') return "Rien n'est déplacé ni supprimé."
  if (c === 'backup' || c === 'overwrite')
    return (
      'Un fichier déjà présent que ce passage remplace part dans la corbeille de Sortilège, ' +
      "d'où il se récupère."
    )
  return "Cette conduite est inconnue de cet écran : rien ici ne dit ce qu'elle fait des fichiers déjà présents."
})

// --- Écriture sur toute la bibliothèque ------------------------------------

const enCours = ref(false)
const rapport = ref(null)
// La panne est distincte du compte rendu : `rapport` à null veut dire « rien
// n'a encore été lancé », et rien d'autre. Lui faire porter en plus « lancé, et
// raté » afficherait « en attente » sur un serveur mort, et personne ne
// relance ce qu'il croit encore à faire.
const panne = ref(null)

/**
 * (Ré)écrit les fiches de toute la bibliothèque.
 *
 * La route refuse (400) quand l'écriture des fiches est désactivée : le
 * réglage EST le consentement à écrire dans la bibliothèque, et une route qui
 * l'ignorerait en ferait une case décorative. Son refus est repris mot pour
 * mot — il nomme la cause, là où « échec » ne dirait rien.
 */
async function ecrireBibliotheque() {
  enCours.value = true
  rapport.value = null
  panne.value = null
  try {
    const res = await fetch('/api/review/nfo-library', { method: 'POST' })
    // Un échec de passerelle répond en HTML : lire le corps sans filet ferait
    // lever ici, et le message d'échec n'arriverait jamais à l'écran.
    const corps = await res.json().catch(() => ({}))
    if (res.ok) rapport.value = corps
    else if (res.status === 409)
      panne.value =
        corps.detail ?? "Une écriture est déjà en cours : attends son compte rendu avant de relancer."
    else panne.value = corps.detail ?? `Échec de l'écriture (réponse ${res.status}).`
  } catch {
    panne.value =
      "Serveur injoignable. C'est le compte rendu qui manque, pas forcément l'écriture : " +
      'ce qui avait été écrit avant la coupure est resté en place. Relancer est sans danger — ' +
      'une fiche déjà présente retombe sur la conduite choisie ci-dessus.'
  } finally {
    enCours.value = false
  }
}

/** Le serveur accepte le passage dès que l'une des deux familles est cochée :
 *  les fiches .nfo des vidéos OU les manifestes des livres. Ne regarder que
 *  `nfo` grisait le bouton chez qui n'a coché que les livres. */
const peutEcrire = computed(() => Boolean(props.localMetadata.nfo || props.localMetadata.opf))

const raisonBouton = computed(() =>
  peutEcrire.value
    ? ''
    : "Coche d'abord les fiches .nfo ou « metadata.opf » ci-dessus : ces cases sont le " +
      "consentement à écrire dans la bibliothèque, et le serveur refuse sans l'une d'elles.",
)

const detailBibliotheque = computed(() => {
  const quoi = []
  if (props.localMetadata.nfo) quoi.push('une fiche .nfo à côté de chaque vidéo rangée')
  if (props.localMetadata.opf) quoi.push('un metadata.opf et la couverture à côté de chaque livre')
  return (
    `Écrit ${quoi.join(', et ')}, en une seule passe, selon la conduite ` +
    `« ${conduiteCourante.value.label} ». ${sortDesExistants.value}`
  )
})

const erreurs = computed(() => rapport.value?.errors ?? [])

/** Le serveur borne sa liste d'erreurs à vingt lignes — trois cents dossiers
 *  en lecture seule produiraient trois cents fois la même. Le compte, lui,
 *  n'est pas borné : afficher l'un sans l'autre laisserait croire que vingt
 *  échecs résument tout. */
const erreursNonDetaillees = computed(() =>
  Math.max(0, (rapport.value?.failed ?? 0) - erreurs.value.length),
)

/** Pourquoi rien n'a été écrit alors que des fichiers ont été examinés. Sans
 *  cette phrase, un compte rendu « 0 écrite sur 1 240 » se lit comme une
 *  panne, alors que c'est le plus souvent le résultat normal d'un second
 *  passage en « ne pas y toucher ». */
const explicationZero = computed(() => {
  const r = rapport.value
  if (!r || r.written) return ''
  if (!r.examined)
    return "Aucun fichier rangé à traiter pour les cases cochées : il n'y avait nulle part où déposer une fiche."
  if (r.skipped && props.localMetadata.on_existing === 'skip')
    return (
      "Rien n'a été écrit parce que chaque fiche était déjà là et que la conduite retenue est " +
      '« ne pas y toucher ». Change-la pour que ces fiches soient reprises.'
    )
  return ''
})

const pluriel = (n, mot) => `${n} ${mot}${n > 1 ? 's' : ''}`
/** « 1 198 fiches écrites », pas « 1198 fiche(s) écrite(s) » : un compte rendu
 *  se lit à voix haute, et les parenthèses d'accord obligent à faire le tri
 *  soi-même là où la machine connaît déjà le nombre. */
const accord = (n, nom, participe) =>
  `${pluriel(n, nom)} ${participe}${n > 1 ? 's' : ''}`
</script>

<template>
  <section>
    <h3>Métadonnées locales</h3>
    <p class="note">
      « Local metadata » est le vocabulaire de Jellyfin, et il dit exactement de quoi il
      s'agit : des données qui vivent <strong>dans</strong> la bibliothèque, à côté des
      fichiers, et non dans la base de Sortilège.
    </p>
    <p class="note capital">
      <strong>Tout est désactivé par défaut, et ce n'est pas décoratif.</strong>
      Écrire des fichiers dans la bibliothèque de quelqu'un est un geste qui se demande :
      qui met à jour son image ne s'attend pas à trouver deux cents fichiers nouveaux le
      lendemain. Rien de ce qui suit ne s'active tout seul.
    </p>

    <div class="reglage">
      <div class="switch">
        <input
          :id="idNfo"
          type="checkbox"
          :checked="localMetadata.nfo"
          :aria-describedby="idNfoAide"
          @change="patch({ nfo: $event.target.checked })"
        />
        <label :for="idNfo">Écrire les fiches <code>.nfo</code> à côté des vidéos</label>
      </div>
      <p :id="idNfoAide" class="hint">
        <strong>C'est le plus gros gain de la fonction.</strong> Jellyfin récupère toujours
        les métadonnées locales et leur donne la <strong>priorité</strong> sur les
        fournisseurs distants — ce comportement ne se désactive pas ; Plex lit nativement
        ce format depuis sa version 1.43.1. Une fiche écrite par Sortilège ne suggère donc
        rien au serveur : <strong>elle fait autorité</strong>. Un arbitrage rendu à la main
        — « non, ce Dark Matter est celui de 2015 » — cesse d'être écrasé au rafraîchissement
        suivant, au lieu de mourir dans la file de revue.
      </p>
      <p class="hint">
        La fiche n'inscrit que ce qu'on sait <em>mieux</em> que le serveur : l'identité et la
        numérotation, qui sortent d'un arbitrage humain. Le synopsis, les genres, le studio
        sont laissés vides — le serveur les télécharge complets, là où nous n'en tenons qu'un
        extrait gardé pour départager deux homonymes à l'écran.
      </p>
    </div>

    <div class="reglage">
      <div class="switch">
        <input
          :id="idArtwork"
          type="checkbox"
          :checked="localMetadata.artwork"
          :aria-describedby="idArtworkAide"
          @change="patch({ artwork: $event.target.checked })"
        />
        <label :for="idArtwork">
          Déposer « poster.jpg » et « fanart.jpg » à côté du média
        </label>
      </div>
      <p :id="idArtworkAide" class="hint">
        Ces deux noms sont lus par Jellyfin <em>comme</em> par Plex : un seul dépôt sert les
        deux serveurs, au lieu de deux jeux de fichiers. Les images viennent de
        l'identification, donc elles se déposent <strong>au moment du rangement</strong> —
        l'écriture sur toute la bibliothèque, plus bas, n'en télécharge aucune : un fichier
        déjà rangé ne porte plus d'adresse d'affiche.
      </p>
    </div>

    <div class="reglage">
      <div class="switch">
        <input
          :id="idOpf"
          type="checkbox"
          :checked="localMetadata.opf"
          :aria-describedby="idOpfAide"
          @change="patch({ opf: $event.target.checked })"
        />
        <label :for="idOpf">
          Écrire « metadata.opf » et la couverture à côté d'un <strong>livre</strong>
        </label>
      </div>
      <p :id="idOpfAide" class="hint">
        Réglage distinct des fiches <code>.nfo</code>, et pas un alias :
        <strong>Jellyfin ne lit aucun <code>.nfo</code> pour les livres</strong>, il attend le
        format de Calibre. Les confondre ferait croire qu'activer les fiches suffit — et rien
        n'apparaîtrait sur les livres. Le format retenu est celui de Calibre justement parce
        qu'il est déjà établi : une bibliothèque rangée par Sortilège s'importe dans Calibre
        sans conversion, et l'inverse.
      </p>
      <p class="hint">
        <code>metadata.opf</code> et la couverture portent des noms fixes : deux livres dans un
        même dossier ne peuvent pas avoir chacun leur manifeste — le second écraserait le
        premier. Ce réglage suppose donc <strong>un dossier par livre</strong>, ce que le
        gabarit des livres décide.
      </p>
    </div>

    <div class="reglage">
      <label :for="idExistant" class="etiquette">Face à un fichier déjà présent</label>
      <select
        v-if="conduites.length"
        :id="idExistant"
        :value="localMetadata.on_existing"
        :aria-describedby="idExistantAide"
        @change="patch({ on_existing: $event.target.value })"
      >
        <option v-for="c in conduites" :key="c.cle" :value="c.cle">{{ c.label }}</option>
      </select>
      <!-- Trois états jusque dans un menu : une liste de conduites absente de
           la réponse laisserait un menu vide sous une étiquette, ce qui se lit
           « cassé ». On le dit, et on ne réinvente pas la liste ici. -->
      <p v-else class="indispo" role="status">
        Le serveur n'a transmis aucune conduite possible. La valeur enregistrée
        (<code>{{ localMetadata.on_existing || 'aucune' }}</code>) reste appliquée ; elle ne
        peut pas être changée depuis cet écran tant que la liste manque.
      </p>
      <p :id="idExistantAide" class="hint">{{ conduiteCourante.resume }}</p>
      <p class="hint">
        Cette conduite vaut pour les fiches <code>.nfo</code> comme pour le
        <code>metadata.opf</code> des livres. Aucune valeur n'est corrigée en silence : une
        conduite mal orthographiée dans le fichier de préférences fait refuser
        l'enregistrement. Ramenée sans le dire à « Remplacer », elle enverrait dans la
        corbeille des fiches qu'on voulait garder : rien ne serait détruit, mais personne ne
        saurait qu'il faut aller les y chercher.
      </p>
    </div>

    <h3 class="sous-titre">Écrire les fiches de toute la bibliothèque</h3>
    <p class="note">
      Les réglages ci-dessus n'agissent qu'<strong>au rangement suivant</strong>. Ce passage
      est ce qui fait apparaître les fiches sur ce qui était déjà rangé : la bibliothèque est
      relue, et une fiche est écrite à côté de chaque vidéo.
    </p>
    <p class="note">
      <strong>Aucune identification n'est refaite.</strong> On repart de ce que chaque fichier
      dit déjà de lui-même : la fiche produite est pauvre — un titre, une année, une
      numérotation, les identifiants qu'une fiche antérieure portait — et c'est précisément ce
      qu'il faut. Elle épingle l'identité et laisse le serveur compléter. Réinterroger un
      fournisseur ferait courir le risque qu'une mauvaise réponse écrase,
      <strong>avec autorité</strong>, une bibliothèque correcte.
    </p>
    <p class="note">
      Aucune affiche n'est téléchargée.
      <template v-if="localMetadata.opf">
        Les livres reçoivent leur <code>metadata.opf</code> et leur couverture, jamais de
        <code>.nfo</code> — Jellyfin n'en lit pas pour eux.
      </template>
      <template v-else>
        <strong>Les livres en sont écartés</strong> tant que « metadata.opf » est décoché —
        Jellyfin n'y lit pas de <code>.nfo</code>.
      </template>
      <template v-if="!localMetadata.nfo && localMetadata.opf">
        Les fiches <code>.nfo</code> étant décochées, <strong>seuls les livres</strong> sont
        traités.
      </template>
      <template v-if="localMetadata.on_existing === 'skip'">
        Rien n'est déplacé ni supprimé : une fiche se pose à côté du média sans y toucher.
      </template>
      <template v-else>
        Le média n'est pas touché : une fiche se pose à côté de lui. {{ sortDesExistants }}
      </template>
    </p>

    <ConfirmAction
      label="Écrire les fiches de toute la bibliothèque"
      confirm-label="Confirmer — écrire dans toute la bibliothèque"
      :detail="detailBibliotheque"
      :busy="enCours"
      :disabled="enCours || !peutEcrire"
      :disabled-reason="raisonBouton"
      @confirm="ecrireBibliotheque"
    />

    <!-- Trois états, et l'échec passe en premier : les deux autres valaient un
         même `rapport` à null, et une écriture ratée aurait annoncé « en
         attente » jusqu'à la fin des temps. -->
    <p v-if="panne" class="panne-inline" role="alert">{{ panne }}</p>
    <p v-else-if="enCours" class="indispo" role="status">
      Écriture en cours — la bibliothèque est parcourue fichier par fichier, puis chaque fiche
      est écrite. Sur une grande bibliothèque, cela prend plusieurs minutes ; le bouton reprend
      dès que le serveur rend son compte.
    </p>
    <template v-else-if="rapport">
      <p class="summary" role="status">
        <strong>{{ accord(rapport.written ?? 0, 'fiche', 'écrite') }}</strong>
        sur {{ accord(rapport.examined ?? 0, 'fichier', 'examiné') }}.
      </p>
      <p class="rapport-detail">
        {{ accord(rapport.skipped ?? 0, 'fiche', 'laissée') }} en place,
        {{ pluriel(rapport.failed ?? 0, 'écriture') }} en échec.
        <template v-if="rapport.books !== undefined && (rapport.books || localMetadata.opf)">
          Dont {{ pluriel(rapport.books, 'livre') }} parmi les fichiers examinés.
        </template>
      </p>
      <p v-if="rapport.without_sheet" class="hint attention">
        {{ pluriel(rapport.without_sheet, 'fichier') }} sans fiche : numérotation inconnue ou
        dossier d'œuvre introuvable.
      </p>
      <p v-if="explicationZero" class="hint">{{ explicationZero }}</p>
      <ul v-if="erreurs.length" class="erreurs">
        <li v-for="(e, i) in erreurs" :key="i">{{ e }}</li>
        <li v-if="erreursNonDetaillees" class="more">
          … et {{ accord(erreursNonDetaillees, 'autre', `que le serveur n'a pas détaillée`) }}.
        </li>
      </ul>
    </template>
    <p v-else class="hint">
      Le compte rendu s'affichera ici : combien de fichiers examinés, combien de fiches
      écrites, combien laissées en place selon la conduite retenue, et les écritures qui ont
      échoué — un dossier en lecture seule, par exemple.
    </p>
  </section>
</template>

<style scoped>
section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
}
h3 {
  margin: 0 0 10px; font-size: var(--t-xs); font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-title);
}
.sous-titre { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }

.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.note strong { color: var(--text-dim); }
/* Le seul avertissement de l'écran : ce qui suit écrit chez l'utilisateur. */
.note.capital strong { color: var(--warn); }

.reglage { margin-top: 16px; }
.switch { display: flex; align-items: center; gap: 8px; font-size: var(--t-sm); }
.switch input { accent-color: var(--accent); flex: none; }
.switch label { color: var(--text); }
.etiquette { display: block; font-size: var(--t-sm); color: var(--text-dim); margin-bottom: 5px; }

select {
  font-size: var(--t-sm); padding: 5px 9px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text);
}

.hint { margin: 7px 0 0; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 680px; }
.hint strong { color: var(--text-dim); }
.hint.attention { color: var(--warn); }
code {
  font-family: var(--mono); font-size: var(--t-xs);
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

/* Pourquoi une action est indisponible, ou ce qui se passe en ce moment. Ton
   d'appoint, jamais celui d'une alerte : ce n'est pas une panne, c'est l'état
   de l'écran dit à voix haute. L'écrire en rouge apprendrait à ignorer le
   rouge. */
.indispo {
  margin: 10px 0 0; font-size: var(--t-xs); color: var(--text-faint);
  line-height: 1.6; max-width: 680px;
}

/* Le compte rendu : c'est la valeur qu'on vient chercher après avoir cliqué,
   elle se lit au lieu de s'annoter. */
.summary { margin: 12px 0 0; font-size: var(--t-md); color: var(--text-dim); }
.summary strong { color: var(--ok); }
.rapport-detail { margin: 4px 0 0; font-size: var(--t-sm); color: var(--text-faint); }

.erreurs {
  list-style: none; margin: 10px 0 0; padding: 0;
  display: flex; flex-direction: column; gap: 3px;
  max-height: 220px; overflow-y: auto;
}
.erreurs li { font-size: var(--t-xs); line-height: 1.55; color: var(--err); font-family: var(--mono); }
.erreurs .more { color: var(--text-faint); font-family: inherit; font-style: italic; }

/* Une panne locale : le reste de l'écran fonctionne, seule cette écriture a
   échoué. D'où le cadre là où elle s'est produite, plutôt qu'un bandeau en
   haut de page. */
.panne-inline {
  margin: 12px 0 0; padding: 9px 12px; border-radius: 8px;
  font-size: var(--t-xs); line-height: 1.6; color: var(--err); max-width: 680px;
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 28%, transparent);
}

@media (max-width: 700px) {
  /* Un libellé de trois lignes ne doit pas centrer sa case sur sa hauteur :
     elle irait se poser au milieu du texte, loin de son premier mot. */
  .switch { align-items: flex-start; }
  .switch input { margin-top: 3px; }
  /* 32 px de cible, gagnés au rembourrage. Grossir la police à la place
     déplacerait toute la hiérarchie typographique de l'écran pour résoudre un
     problème de doigt. */
  select { min-height: 32px; padding: 7px 9px; }
}
</style>
