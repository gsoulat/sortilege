<script setup>
import { onMounted, onUnmounted } from 'vue'

/**
 * Agrandissement d'une image, en surimpression.
 *
 * Une jaquette de trente pixels de large ne permet pas de distinguer deux
 * saisons d'une même série, ni de lire un titre imprimé sur l'affiche. C'est
 * pourtant sur elle qu'on tranche.
 *
 * Trois façons d'en sortir, et ce n'est pas du zèle : une surimpression sans
 * issue évidente est un piège. Échap est le réflexe, le clic à côté est le
 * geste naturel, et le bouton reste pour qui ne connaît ni l'un ni l'autre.
 */
defineProps({
  src: { type: String, required: true },
  alt: { type: String, default: '' },
  legende: { type: String, default: '' },
})
const emit = defineEmits(['close'])

function surTouche(e) {
  if (e.key === 'Escape') emit('close')
}

onMounted(() => {
  window.addEventListener('keydown', surTouche)
  // Le fond ne doit pas défiler derrière la surimpression : on croit agir sur
  // l'image et c'est la liste qui bouge.
  document.body.style.overflow = 'hidden'
})
onUnmounted(() => {
  window.removeEventListener('keydown', surTouche)
  document.body.style.overflow = ''
})
</script>

<template>
  <!-- Le clic sur le fond ferme ; celui sur l'image ne le traverse pas. -->
  <div class="fond" role="dialog" aria-modal="true" @click="$emit('close')">
    <figure class="cadre" @click.stop>
      <img :src="src" :alt="alt" />
      <figcaption v-if="legende">{{ legende }}</figcaption>
    </figure>
    <button class="fermer" title="Fermer (Échap)" @click.stop="$emit('close')">
      Fermer ✕
    </button>
  </div>
</template>

<style scoped>
.fond {
  position: fixed; inset: 0; z-index: 50;
  background: color-mix(in srgb, #000 82%, transparent);
  display: grid; place-items: center; padding: 32px;
  backdrop-filter: blur(2px);
}

.cadre { margin: 0; display: flex; flex-direction: column; gap: 10px; align-items: center; max-height: 100%; }
.cadre img {
  max-width: min(90vw, 900px); max-height: 80vh;
  border-radius: 8px; display: block;
  box-shadow: 0 18px 60px rgba(0, 0, 0, .6);
}
figcaption { font-size: 12.5px; color: #ddd; text-align: center; max-width: 640px; line-height: 1.5; }

.fermer {
  position: fixed; top: 18px; right: 20px;
  font-size: 12.5px; padding: 5px 13px;
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; cursor: pointer;
}
.fermer:hover { border-color: var(--accent); }
</style>
