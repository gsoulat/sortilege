<script setup>
defineProps({
  transcode: { type: Object, required: true },
})
const emit = defineEmits(['change'])

function patch(fields) {
  emit('change', fields)
}

const HEURES = Array.from({ length: 24 }, (_, h) => h)
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

    <div class="row">
      <label>
        Codec
        <select :value="transcode.codec" @change="patch({ codec: $event.target.value })">
          <option value="libx264">H.264 — lu partout</option>
          <option value="libx265">HEVC — plus petit, moins compatible</option>
        </select>
      </label>
      <label>
        Qualité (CRF)
        <input
          type="number"
          min="14"
          max="30"
          :value="transcode.crf"
          @change="patch({ crf: Number($event.target.value) })"
        />
      </label>
      <label>
        Vitesse
        <select :value="transcode.preset" @change="patch({ preset: $event.target.value })">
          <option value="veryfast">Très rapide</option>
          <option value="fast">Rapide</option>
          <option value="medium">Moyenne</option>
          <option value="slow">Lente — un peu plus compact</option>
        </select>
      </label>
    </div>
    <p class="hint">
      H.264 gagne moins de place que le HEVC mais se lit partout, y compris sur les téléviseurs
      et boîtiers anciens — une bibliothèque qu'on ne peut plus lire n'a pas gagné de place,
      elle a perdu des films. CRF plus bas = meilleure image et fichier plus gros ; 21 est le
      compromis courant pour un réencodage qu'on ne veut pas voir.
    </p>
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
.note.attention { color: var(--warn); opacity: .85; }
.switch { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 12px; }
.switch input { accent-color: var(--accent); }
.row { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.row label { font-size: 12px; color: var(--text-dim); display: flex; gap: 6px; align-items: center; }
select, input[type='number'] {
  font-size: 12px; padding: 4px 8px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text);
}
input[type='number'] { width: 64px; }
.hint { margin: 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.6; max-width: 660px; }
</style>
