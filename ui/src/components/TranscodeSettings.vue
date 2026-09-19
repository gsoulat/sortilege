<script setup>
import { computed } from 'vue'

const props = defineProps({
  transcode: { type: Object, required: true },
})
const emit = defineEmits(['change'])

function patch(fields) {
  emit('change', fields)
}

const HEURES = Array.from({ length: 24 }, (_, h) => h)

// Bornes et cran 4K viennent du serveur, qui les applique de toute façon :
// les valeurs de repli ne servent que le temps du premier chargement.
const crfMin = computed(() => props.transcode.crf_min ?? 16)
const crfMax = computed(() => props.transcode.crf_max ?? 28)
const crfUhd = computed(() => props.transcode.crf_uhd ?? props.transcode.crf + 1)
const format = computed(() => props.transcode.format || 'HEVC 10 bits')

function changerCrf(event) {
  const saisie = event.target.value
  const n = Math.round(Number(saisie))
  // Un champ vidé ne doit rien enregistrer : Number('') vaut 0, que le serveur
  // ramenait à 16 — le réglage changeait sans qu'on l'ait choisi. On remet la
  // valeur en place.
  if (saisie.trim() === '' || !Number.isFinite(n)) {
    event.target.value = props.transcode.crf
    return
  }
  patch({ crf: n })
}
</script>

<template>
  <section>
    <h3>Réencodage différé</h3>
    <p class="note">
      Les fichiers dont la résolution dépasse ce que ta stratégie demande peuvent être
      réencodés <strong>la nuit</strong>, un à la fois, <strong>à côté</strong> de l'original.
      Rien n'est remplacé sans ton accord : le lendemain, tu regardes et tu décides.
    </p>
    <p class="note attention">
      C'est la seule fonction de Sortilège qui <strong>dégrade volontairement</strong> de la
      qualité. L'original part en corbeille et reste récupérable, mais les détails perdus à
      l'encodage, eux, ne reviennent pas. D'où le réglage désactivé par défaut.
    </p>

    <label class="switch">
      <input
        type="checkbox"
        :checked="transcode.enabled"
        @change="patch({ enabled: $event.target.checked })"
      />
      Autoriser le réencodage nocturne
    </label>

    <div class="row">
      <label>
        De
        <select
          :value="transcode.start_hour"
          @change="patch({ start_hour: Number($event.target.value) })"
        >
          <option v-for="h in HEURES" :key="h" :value="h">{{ h }} h</option>
        </select>
      </label>
      <label>
        à
        <select
          :value="transcode.end_hour"
          @change="patch({ end_hour: Number($event.target.value) })"
        >
          <option v-for="h in HEURES" :key="h" :value="h">{{ h }} h</option>
        </select>
      </label>
      <span class="hint">
        Un encodage déjà commencé va à son terme même si la plage se ferme : l'interrompre à
        six heures du matin jetterait une nuit de calcul.
      </span>
    </div>

    <div class="format">
      <p class="ligne">
        <span class="puce">{{ format }}</span>
        <span class="details">Main 10 · MKV · sous-titres et polices copiés</span>
      </p>
      <p class="note">
        La télévision (Jellyfin sur Android TV), l'iPad, l'iPhone et Firefox à partir de la
        version 136 décodent ce format par le matériel : Jellyfin le leur envoie tel quel, sans
        le convertir à la volée. Le HDR de la source est conservé — HDR10, HLG, et la base
        HDR10 des Dolby Vision 7 et 8.1 — au lieu d'être aplati. Le débit est plafonné à
        20 Mbit/s jusqu'au 1080p et 40 Mbit/s au-delà, pour que la lecture tienne en wifi sans
        s'arrêter pour charger.
      </p>
      <p class="hint">
        Un Dolby Vision profil 5 n'est jamais réencodé : il n'a pas de couche HDR10, et le
        résultat aurait des couleurs violettes et vertes. Il est signalé une fois, puis n'est
        plus proposé.
      </p>
    </div>

    <div class="row">
      <label>
        Qualité (CRF)
        <input
          type="number"
          :min="crfMin"
          :max="crfMax"
          :value="transcode.crf"
          @change="changerCrf"
        />
      </label>
      <label>
        Vitesse
        <select :value="transcode.preset" @change="patch({ preset: $event.target.value })">
          <option value="veryfast">Très rapide — fichiers plus gros</option>
          <option value="fast">Rapide</option>
          <option value="medium">Moyenne — recommandée</option>
          <option value="slow">Lente — un peu plus compact</option>
        </select>
      </label>
    </div>
    <p class="hint">
      CRF {{ transcode.crf }} jusqu'au 1080p, {{ crfUhd }} au-delà : le 4K masque davantage ce
      cran, et il vaut plusieurs gigaoctets par film. Plus bas, l'image est plus fidèle et le
      fichier plus gros ; à 21, on ne distingue pas le résultat de la source à distance normale
      d'un téléviseur.
    </p>
    <p class="hint">
      « Lente » gagne encore quelques pour cent de place, pour environ deux fois plus de temps.
      Sur un Celeron à 4 cœurs, un film de deux heures peut déjà occuper une nuit entière en
      « Moyenne » : en « Lente », compte le double.
    </p>

    <label class="switch audio">
      <input
        type="checkbox"
        :checked="transcode.compress_audio"
        @change="patch({ compress_audio: $event.target.checked })"
      />
      Compresser l'audio sans perte audible
    </label>
    <p class="hint">
      Les pistes TrueHD, DTS-HD MA, DTS, PCM et FLAC multicanal deviennent de l'E-AC-3 à
      640 kbit/s (5.1 au plus) ; les autres pistes sont copiées. Gain typique : 3 à 4 Go par
      film. Le prix, franchement : <strong>l'Atmos et le DTS:X sont perdus</strong>, et un 7.1
      devient un 5.1. Désactivé, l'audio est copié à l'identique.
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
.note { margin: 0 0 12px; font-size: var(--t-sm); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.note strong { color: var(--text-dim); }
.note.attention { color: var(--warn); opacity: .85; }
.switch { display: flex; align-items: center; gap: 8px; font-size: var(--t-sm); margin-bottom: 12px; }
.switch input { accent-color: var(--accent); flex: none; }
.switch.audio { margin: 16px 0 6px; }
.row { display: flex; gap: 10px 14px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.row label {
  font-size: var(--t-sm); color: var(--text-dim);
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-width: 0;
}
select, input[type='number'] {
  font-size: var(--t-sm); padding: 4px 8px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text);
  max-width: 100%;
}
input[type='number'] { width: 68px; }
.hint { margin: 0 0 8px; font-size: var(--t-xs); color: var(--text-faint); line-height: 1.6; max-width: 660px; }
.hint strong { color: var(--text-dim); }

/* Le format n'est plus un choix : il se lit, avec sa raison juste dessous. */
.format {
  margin: 14px 0; padding: 12px 14px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--surface-2);
}
.format .note { margin-bottom: 8px; }
.format .hint { margin: 0; }
.ligne { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin: 0 0 8px; }
.puce {
  font-size: var(--t-xs); font-weight: 600; padding: 2px 8px; border-radius: 999px;
  color: var(--accent); background: color-mix(in srgb, var(--accent) 16%, transparent);
  border: 1px solid var(--accent-dim);
}
.details { font-size: var(--t-xs); color: var(--text-faint); }
</style>
