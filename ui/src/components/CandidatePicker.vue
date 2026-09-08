<script setup>
defineProps({
  candidates: { type: Array, required: true },
  busy: { type: Boolean, default: false },
})
defineEmits(['choose', 'close'])
</script>

<template>
  <div class="picker">
    <div class="head">
      <span class="hint">
        Choisis la bonne œuvre. Aucun signal automatique ne distingue deux titres
        identiques — l'affiche, si.
      </span>
      <button class="close" @click="$emit('close')">Fermer</button>
    </div>

    <ul class="grid">
      <li v-for="c in candidates" :key="`${c.provider}-${c.external_id}`">
        <button class="card" :disabled="busy" @click="$emit('choose', c)">
          <div class="poster">
            <img v-if="c.poster_url" :src="c.poster_url" :alt="c.title" loading="lazy" />
            <span v-else class="noposter">sans affiche</span>
          </div>
          <div class="label">
            <span class="title">{{ c.title }}</span>
            <span class="meta">
              <span v-if="c.year">{{ c.year }}</span>
              <span class="provider">{{ c.provider }}</span>
            </span>
          </div>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.picker {
  border: 1px solid var(--accent-dim); border-radius: 8px;
  background: var(--bg); padding: 12px 14px; margin: 8px 0 4px;
}

.head { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 11px; }
.hint { font-size: 12px; color: var(--text-faint); line-height: 1.5; flex: 1; }
.close { font-size: 11.5px; padding: 3px 9px; flex: none; }

.grid {
  list-style: none; margin: 0; padding: 0;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 10px;
}

.card {
  width: 100%; padding: 0; border: 1px solid var(--border);
  border-radius: 7px; overflow: hidden; background: var(--surface);
  display: flex; flex-direction: column; text-align: left;
}
.card:hover:not(:disabled) { border-color: var(--accent); }
.card:disabled { opacity: .5; }

.poster {
  aspect-ratio: 2 / 3; background: var(--surface-2);
  display: grid; place-items: center; overflow: hidden;
}
.poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
.noposter { font-size: 10px; color: var(--text-faint); }

.label { padding: 6px 8px 8px; display: flex; flex-direction: column; gap: 3px; }
.title {
  font-size: 12px; line-height: 1.3; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.meta { display: flex; gap: 6px; font-size: 10px; color: var(--text-faint); }
.provider { margin-left: auto; }
</style>
