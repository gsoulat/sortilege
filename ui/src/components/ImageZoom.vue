<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

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
const props = defineProps({
  src: { type: String, required: true },
  // Description fournie par l'appelant quand il en a une. Aucun appelant n'en
  // passe aujourd'hui : la prop existe pour que celui qui saura decrire son
  // image puisse le dire, sans que les autres aient a changer.
  alt: { type: String, default: '' },
  legende: { type: String, default: '' },
})
const emit = defineEmits(['close'])

/**
 * Nom accessible de la surimpression.
 *
 * `aria-modal="true"` sans nom s'annonce « dialogue » et rien d'autre : on
 * apprend qu'on est entre quelque part sans apprendre ou, ce qui est
 * exactement l'information qui manque quand on ne voit pas l'ecran.
 */
const nom = computed(() =>
  props.legende ? `Image agrandie — ${props.legende}` : 'Image agrandie',
)

/**
 * Texte de remplacement de l'image.
 *
 * Un `alt` vide declare une image DECORATIVE. C'etait faux ici : l'image est le
 * seul contenu de la surimpression, on l'ouvre precisement pour la regarder.
 * A defaut de description fournie, la legende nomme au moins l'oeuvre ; a
 * defaut des deux, on dit ce que c'est sans pretendre decrire. Un repli qui
 * repete la legende vaut mieux qu'un repli qui pretend qu'il n'y a rien.
 */
const texteAlt = computed(() => props.alt || props.legende || 'Image agrandie')

// Le focus doit entrer dans la surimpression : sinon il reste sur la vignette
// d'ou l'on vient, derriere le voile, et le Tab suivant parcourt une page qu'on
// ne voit plus.
const boutonFermer = ref(null)

function surTouche(e) {
  if (e.key === 'Escape') emit('close')
}

onMounted(() => {
  window.addEventListener('keydown', surTouche)
  // Le fond ne doit pas défiler derrière la surimpression : on croit agir sur
  // l'image et c'est la liste qui bouge.
  document.body.style.overflow = 'hidden'
  // « Fermer » plutot que l'image : c'est la sortie, et la lire annonce du meme
  // coup comment sortir. Pas de piege a focus complet en revanche — Echap et le
  // clic sur le fond ferment deja, le cout ne se justifie pas ici.
  boutonFermer.value?.focus()
})
onUnmounted(() => {
  window.removeEventListener('keydown', surTouche)
  document.body.style.overflow = ''
})
</script>

<template>
  <!-- Le clic sur le fond ferme ; celui sur l'image ne le traverse pas. -->
  <div class="fond" role="dialog" aria-modal="true" :aria-label="nom" @click="$emit('close')">
    <figure class="cadre" @click.stop>
      <img :src="src" :alt="texteAlt" />
      <figcaption v-if="legende">{{ legende }}</figcaption>
    </figure>
    <button ref="boutonFermer" class="fermer" title="Fermer (Échap)" @click.stop="$emit('close')">
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
