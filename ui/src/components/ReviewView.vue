<script setup>
import { ref, onMounted } from 'vue'

const data = ref(null)

onMounted(async () => {
  data.value = await (await fetch('/api/review')).json()
})
</script>

<template>
  <div v-if="data" class="review">
    <div v-if="data.ready && !data.items.length" class="empty">
      <h3>File vide</h3>
      <p>Aucun fichier n'attend d'arbitrage. Tout ce qui a été identifié l'a été avec assez de certitude.</p>
    </div>

    <template v-else-if="!data.ready">
      <div class="notice">
        <h3>La file de revue n'est pas encore alimentée</h3>
        <p>
          Elle recevra les fichiers dont le score de confiance tombe entre
          <code>{{ data.policy.reject_threshold }}</code> et
          <code>{{ data.policy.auto_apply_threshold }}</code> —
          trop incertains pour être appliqués seuls, trop plausibles pour être jetés.
        </p>
        <p class="sub">Il manque ceci pour qu'elle se remplisse :</p>
      </div>

      <ol class="blockers">
        <li v-for="(b, i) in data.blockers" :key="i">
          <div class="head">
            <span class="num">{{ i + 1 }}</span>
            <span class="title">{{ b.title }}</span>
          </div>
          <p class="detail">{{ b.detail }}</p>
          <code class="where">{{ b.where }}</code>
        </li>
      </ol>
    </template>

    <ul v-else class="items">
      <li v-for="(item, i) in data.items" :key="i">{{ item }}</li>
    </ul>
  </div>
</template>

<style scoped>
.review { display: flex; flex-direction: column; gap: 16px; }

.empty, .notice {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 24px 26px;
}
.empty { border-style: dashed; text-align: center; }

h3 { margin: 0 0 10px; font-size: 14px; color: var(--text); text-transform: none; letter-spacing: 0; font-weight: 600; }
.notice p { margin: 0 0 8px; font-size: 13px; color: var(--text-dim); line-height: 1.65; max-width: 640px; }
.notice p.sub { margin-top: 14px; margin-bottom: 0; color: var(--text-faint); }
.empty p { margin: 0; font-size: 13px; color: var(--text-dim); }

code {
  font-family: var(--mono); font-size: 11.5px;
  background: var(--surface-2); padding: 1px 6px; border-radius: 4px; color: var(--accent);
}

.blockers { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; counter-reset: b; }
.blockers li {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 2px solid var(--warn);
  border-radius: 8px; padding: 13px 16px;
}
.head { display: flex; align-items: center; gap: 9px; }
.num {
  width: 18px; height: 18px; flex: none; border-radius: 50%;
  display: grid; place-items: center; font-size: 10px;
  background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn);
}
.head .title { font-weight: 500; font-size: 13.5px; }
.detail { margin: 7px 0 9px 27px; font-size: 12.5px; color: var(--text-dim); line-height: 1.6; max-width: 620px; }
.where { margin-left: 27px; display: inline-block; }

.items { list-style: none; margin: 0; padding: 0; }
</style>
