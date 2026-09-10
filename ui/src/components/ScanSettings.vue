<script setup>
import { ref } from 'vue'
import StringListEditor from './StringListEditor.vue'

const props = defineProps({
  scan: { type: Object, required: true },
})
const emit = defineEmits(['change'])

// Les valeurs livrées sont rappelées ici, et pas seulement mentionnées : sans
// elles sous les yeux, on ré-ajoute « sample » ou « @eaDir » en croyant combler
// un manque, et on finit par douter que la liste serve à quelque chose.
const DOSSIERS_LIVRES = [
  '@eaDir', '.@__thumb', '#recycle', '.Trash-1000',
  'lost+found', 'extras', 'featurettes', 'sample', 'samples',
]
const MOTIFS_LIVRES = ['sample', 'trailer', 'bande-annonce', 'extrait']

const MAX_MO = 100000
const refusSeuil = ref(null)

function patch(fields) {
  emit('change', fields)
}

/** Le serveur refuse un seuil hors bornes par une erreur d'enregistrement qui
 *  s'affiche ailleurs dans la page. On tranche ici, à côté du champ : un seuil
 *  ramené en silence dans le cadran ferait croire que la valeur tapée est
 *  celle qui s'applique. */
function setSeuil(brut) {
  const valeur = Number(brut)
  if (!Number.isFinite(valeur) || !Number.isInteger(valeur)) {
    refusSeuil.value = 'Indique un nombre entier de mégaoctets.'
    return
  }
  if (valeur < 0) {
    refusSeuil.value = 'Un plancher négatif n\'a pas de sens. Mets 0 pour ne rien écarter.'
    return
  }
  if (valeur > MAX_MO) {
    refusSeuil.value =
      `Au-delà de ${MAX_MO} Mo — cent gigaoctets — le scan n'aurait plus rien à trouver. ` +
      'Ce n\'est pas un réglage prudent, c\'est un scan vide.'
    return
  }
  refusSeuil.value = null
  patch({ min_size_mb: valeur })
}
</script>

<template>
  <section>
    <h3>Ce que le scan écarte</h3>
    <p class="note">
      Ces trois règles s'appliquent <strong>avant toute analyse</strong> : un fichier écarté
      ici n'apparaît nulle part, ni dans la file de revue, ni dans les rejets. C'est rapide,
      et c'est justement pourquoi il faut savoir ce qu'on écarte.
    </p>

    <div class="field">
      <label for="min-size">Taille minimale d'un fichier vidéo</label>
      <div class="row">
        <input
          id="min-size"
          type="number"
          min="0"
          step="10"
          :value="scan.min_size_mb"
          @change="setSeuil($event.target.value)"
        />
        <span class="unit">Mo</span>
      </div>
      <p v-if="refusSeuil" class="hint refus">{{ refusSeuil }}</p>
      <p class="hint">
        En dessous de ce seuil, un fichier vidéo est presque toujours un échantillon ou un
        téléchargement avorté : le retenir remplirait la file de revue de choses qu'on ne
        veut pas ranger. <strong>Mais « presque » a un coût</strong> — un court-métrage, un
        bonus, un épisode en 480p passe sous le plancher et disparaît sans laisser de trace.
        Descends-le si ta bibliothèque en contient.
      </p>
      <p v-if="scan.min_size_mb === 0" class="hint attention">
        À 0, plus aucun plancher : tout fichier vidéo est retenu, échantillons et fragments
        compris.
      </p>
    </div>

    <div class="field">
      <StringListEditor
        label="Dossiers à ignorer"
        placeholder="Perso"
        interdit-chemin
        vide="Aucun dossier supplémentaire : seules les valeurs livrées s'appliquent."
        :items="scan.extra_skip_dirs"
        @update="patch({ extra_skip_dirs: $event })"
      />
      <p class="hint">
        Un <strong>nom</strong> de dossier, pas un chemin : l'exclusion vaut partout où ce
        nom apparaît sous les sources. C'est ce qui permet de sortir un dossier personnel du
        périmètre sans le déplacer.
      </p>
      <p class="hint livres">
        Toujours ignorés, sans avoir à les saisir :
        <code v-for="d in DOSSIERS_LIVRES" :key="d">{{ d }}</code>
        et tout dossier commençant par un point. Ce que tu ajoutes ici <strong>s'ajoute</strong>
        à cette liste, ne la remplace pas.
      </p>
    </div>

    <div class="field">
      <StringListEditor
        label="Motifs de nom à ignorer"
        placeholder="proper"
        vide="Aucun motif supplémentaire : seules les valeurs livrées s'appliquent."
        :items="scan.extra_skip_hints"
        @update="patch({ extra_skip_hints: $event })"
      />
      <p class="hint">
        Un fragment de nom de fichier, comparé en minuscules <strong>n'importe où</strong>
        dans le nom. Un motif trop court écarte large : « ep » ferait disparaître tout ce qui
        contient « episode ».
      </p>
      <p class="hint livres">
        Toujours ignorés :
        <code v-for="m in MOTIFS_LIVRES" :key="m">{{ m }}</code>
        Ce que tu ajoutes ici s'y ajoute.
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
  margin: 0 0 10px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}
.note { margin: 0 0 12px; font-size: 12px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.note strong { color: var(--text-dim); }

.field { margin-top: 16px; }
.field + .field { padding-top: 16px; border-top: 1px solid var(--border); }
.field > label { display: block; font-size: 11.5px; color: var(--text-dim); margin-bottom: 5px; }
.row { display: flex; gap: 9px; align-items: center; }
.row input {
  width: 92px; font-size: 12.5px; padding: 6px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text);
}
.unit { font-size: 12px; color: var(--text-faint); }

.hint { margin: 8px 0 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint strong { color: var(--text-dim); }
.hint.refus, .hint.attention { color: var(--warn); opacity: .9; }
.hint.livres code { margin-right: 5px; }
code {
  font-family: var(--mono); font-size: 11px;
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}
</style>
