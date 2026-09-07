<script setup>
import { ref, onMounted } from 'vue'

const s = ref(null)

onMounted(async () => {
  s.value = await (await fetch('/api/settings')).json()
})
</script>

<template>
  <div v-if="s" class="settings">
    <p class="lead">
      Tout se configure par variables d'environnement, dans le <code>.env</code>.
      Cette page est donc en lecture seule : une modification depuis l'interface
      créerait un second état de vérité, invisible dans le <code>docker compose</code>.
    </p>

    <section>
      <h3>Diagnostic</h3>
      <ul class="checks">
        <li v-for="c in s.diagnostics" :key="c.name" :class="{ ko: !c.ok }">
          <span class="dot"></span>
          <span class="name">{{ c.name }}</span>
          <span class="detail">{{ c.detail }}</span>
        </li>
      </ul>
    </section>

    <div class="cols">
      <section>
        <h3>Chemins</h3>
        <dl>
          <dt>Sources</dt>
          <dd>
            <code v-for="p in s.paths.source_roots" :key="p">{{ p }}</code>
            <span v-if="!s.paths.source_roots.length" class="none">aucune</span>
          </dd>
          <dt>Bibliothèque</dt>
          <dd><code>{{ s.paths.library_root }}</code></dd>
        </dl>
      </section>

      <section>
        <h3>Décision</h3>
        <dl>
          <dt>Application automatique</dt>
          <dd>score ≥ <code>{{ s.behaviour.auto_apply_threshold }}</code></dd>
          <dt>Revue manuelle</dt>
          <dd>entre <code>{{ s.behaviour.reject_threshold }}</code> et <code>{{ s.behaviour.auto_apply_threshold }}</code></dd>
          <dt>Rejet</dt>
          <dd>score &lt; <code>{{ s.behaviour.reject_threshold }}</code></dd>
          <dt>Mode simulation</dt>
          <dd>
            <span :class="s.behaviour.dry_run ? 'on' : 'off'">
              {{ s.behaviour.dry_run ? 'actif — rien n\'est déplacé' : 'désactivé — les fichiers bougent' }}
            </span>
          </dd>
        </dl>
      </section>
    </div>

    <section>
      <h3>Fournisseurs de métadonnées</h3>
      <ul class="providers">
        <li v-for="p in s.providers" :key="p.name" :class="{ ko: p.required && !p.configured }">
          <span class="dot" :class="{ off: !p.configured }"></span>
          <div>
            <div class="name">
              {{ p.name }}
              <span v-if="p.required" class="req">requis</span>
            </div>
            <div class="role">{{ p.role }}</div>
          </div>
          <code class="hint">{{ p.hint }}</code>
        </li>
      </ul>
    </section>

    <section>
      <h3>Résolveur IA</h3>
      <dl>
        <dt>État</dt>
        <dd>
          <span :class="s.ai.enabled ? 'on' : 'off'">
            {{ s.ai.enabled ? 'activé' : 'désactivé' }}
          </span>
          <span v-if="s.ai.enabled && !s.ai.configured" class="warn"> — clé absente</span>
        </dd>
        <dt>Modèle</dt>
        <dd><code>{{ s.ai.model }}</code></dd>
        <dt>Taille de lot</dt>
        <dd>{{ s.ai.batch_size }} fichiers par appel</dd>
      </dl>
      <p class="note">
        Appelé uniquement sur les fichiers dont le score déterministe est ambigu.
        Il propose des métadonnées, jamais un chemin, et sa sortie repasse par le
        même scoring qu'une identification classique.
      </p>
    </section>
  </div>
</template>

<style scoped>
.settings { display: flex; flex-direction: column; gap: 22px; }

.lead {
  margin: 0; font-size: 13px; color: var(--text-dim); line-height: 1.65; max-width: 720px;
}

section {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px;
}
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 780px) { .cols { grid-template-columns: 1fr; } }

h3 {
  margin: 0 0 12px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
}

code {
  font-family: var(--mono); font-size: 11.5px;
  background: var(--surface-2); padding: 1.5px 6px; border-radius: 4px; color: var(--text-dim);
}

.checks { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 9px; }
.checks li { display: flex; align-items: baseline; gap: 9px; font-size: 13px; flex-wrap: wrap; }
.dot {
  width: 7px; height: 7px; border-radius: 50%; flex: none;
  background: var(--ok); transform: translateY(-1px);
}
.checks li.ko .dot, .dot.off { background: var(--warn); }
.checks .name { min-width: 150px; }
.checks .detail { color: var(--text-faint); font-size: 12px; }

dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 7px 16px; font-size: 13px; }
dt { color: var(--text-faint); }
dd { margin: 0; display: flex; gap: 5px; flex-wrap: wrap; }
dd .none { color: var(--text-faint); font-style: italic; }

.on { color: var(--ok); }
.off { color: var(--text-faint); }
.warn { color: var(--warn); }

.providers { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 11px; }
.providers li { display: flex; align-items: center; gap: 10px; }
.providers .name { font-size: 13px; display: flex; align-items: center; gap: 7px; }
.providers .role { font-size: 11.5px; color: var(--text-faint); }
.providers .hint { margin-left: auto; }
.req {
  font-size: 9.5px; padding: 1px 5px; border-radius: 3px; letter-spacing: .04em;
  color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
}

.note {
  margin: 12px 0 0; font-size: 12px; color: var(--text-faint);
  line-height: 1.6; max-width: 620px;
}
</style>
