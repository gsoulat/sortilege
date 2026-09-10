<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import LoginView from './components/LoginView.vue'
import WorkspaceView from './components/WorkspaceView.vue'
import SettingsView from './components/SettingsView.vue'
import JournalView from './components/JournalView.vue'
import TranscodeView from './components/TranscodeView.vue'

const authenticated = ref(null) // null = on ne sait pas encore
const health = ref(null)
const view = ref('source')

// « À ranger », « Ma collection » et « File de revue » montraient trois moitiés
// du même objet et obligeaient à des allers-retours pour répondre à « où en est
// cette série ? ». Elles sont réunies dans « Ma médiathèque ».
//
// La file de revue avait d'abord été gardée à part, au prétexte qu'une grille
// de jaquettes se parcourt mieux qu'une arborescence dépliée. Le prétexte ne
// tenait pas : elle affichait les mêmes plans sous une autre forme, et deux
// écrans qui montrent la même chose obligent à se demander lequel fait foi.
// « Gabarits » a quitté ce niveau : un gabarit n'est pas une destination
// concurrente de la médiathèque, c'est la forme du chemin de rangement. Il vit
// donc dans Réglages, à côté de la destination qu'il complète — et il
// s'enregistre, ce qu'il ne faisait pas.
//
// « Ranger » et « Ma médiathèque » remontent ici depuis les onglets internes de
// la médiathèque. Ce sont deux intentions, pas deux filtres : on vient dans
// l'une pour vider la source et on en repart quand elle est vide ; on vient
// dans l'autre pour inspecter ce qu'on possède, et elle n'est jamais vide.
// Enterrées un cran plus bas, elles se cherchaient — et la barre du haut
// n'offrait qu'une destination unique flanquée des réglages.
//
// Le journal a sa propre entrée plutôt qu'un panneau replié dans la barre
// d'actions. Ce n'est pas un détail de rangement : annuler un déplacement est
// la fonction que ni Radarr ni Sonarr ne savent faire — aucune trace de
// « undo » dans toute leur interface. Une capacité qu'on cache derrière un
// bouton passe pour un dépannage ; à sa place, elle se voit.
const VIEWS = [
  { id: 'source', label: 'Ranger' },
  { id: 'library', label: 'Ma médiathèque' },
  { id: 'transcode', label: 'Réencodage' },
  { id: 'journal', label: 'Journal' },
  { id: 'settings', label: 'Réglages' },
]

// Les deux premières entrées partagent un composant : c'est le même écran, le
// même chargement et les mêmes actions — seul l'ensemble d'œuvres affiché
// change. En faire deux composants dupliquerait deux mille lignes pour un
// filtre.
const ESPACES = ['source', 'library']

async function checkAuth() {
  try {
    const res = await fetch('/api/auth/me')
    authenticated.value = (await res.json()).authenticated
  } catch {
    authenticated.value = false
  }
}

async function loadHealth() {
  try {
    health.value = await (await fetch('/api/health')).json()
  } catch {
    health.value = { status: 'unreachable' }
  }
}

async function onAuthenticated() {
  authenticated.value = true
  await loadHealth()
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' })
  authenticated.value = false
}

// Une session qui expire en cours d'usage ramène à l'écran de connexion
// plutôt que de laisser chaque vue afficher son erreur.
function onUnauthenticated() {
  authenticated.value = false
}

onMounted(async () => {
  window.addEventListener('sortilege:unauthenticated', onUnauthenticated)
  await checkAuth()
  if (authenticated.value) await loadHealth()
})

onUnmounted(() => {
  window.removeEventListener('sortilege:unauthenticated', onUnauthenticated)
})
</script>

<template>
  <!-- Rien tant qu'on ignore l'état : afficher la connexion puis l'application
       ferait clignoter l'écran à chaque chargement. -->
  <div v-if="authenticated === null" />

  <LoginView v-else-if="!authenticated" @authenticated="onAuthenticated" />

  <div v-else class="shell">
    <header>
      <div class="brand">
        <span class="mark">✦</span>
        <h1>Sortilège</h1>
        <span v-if="health?.version" class="version">v{{ health.version }}</span>
      </div>

      <nav>
        <!-- `aria-current` plutôt qu'une classe seule : la couleur dit l'onglet
             courant à qui regarde, elle ne le dit à personne d'autre. -->
        <button
          v-for="v in VIEWS"
          :key="v.id"
          :class="{ active: view === v.id }"
          :aria-current="view === v.id ? 'page' : undefined"
          @click="view = v.id"
        >{{ v.label }}</button>
      </nav>

      <div class="status">
        <template v-if="health && health.status !== 'unreachable'">
          <span v-if="health.ffprobe === false" class="badge warn" title="Durée et tags non lisibles">
            sans ffprobe
          </span>
          <span class="badge" :class="health.ai_enabled ? 'on' : 'off'">
            IA {{ health.ai_enabled ? 'activée' : 'désactivée' }}
          </span>
        </template>
        <span v-else-if="health" class="badge err">hors ligne</span>
        <button class="logout" title="Se déconnecter" @click="logout">Quitter</button>
      </div>
    </header>

    <main>
      <WorkspaceView v-if="ESPACES.includes(view)" :espace="view" />
      <TranscodeView v-else-if="view === 'transcode'" />
      <JournalView v-else-if="view === 'journal'" />
      <SettingsView v-else-if="view === 'settings'" />
    </main>
  </div>
</template>

<style scoped>
.shell { max-width: 1180px; margin: 0 auto; padding: 0 24px 64px; }

header {
  display: flex; align-items: center; gap: 28px;
  padding: 20px 0 22px; margin-bottom: 28px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}

.brand { display: flex; align-items: baseline; gap: 9px; }
.mark { color: var(--accent); font-size: 17px; }
h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -.01em; }
.version { font-family: var(--mono); font-size: 10.5px; color: var(--text-faint); }

nav { display: flex; gap: 2px; margin-right: auto; }
nav button {
  border: none; background: none; color: var(--text-dim);
  padding: 6px 11px; border-radius: 6px; font-size: 13px;
}
nav button:hover { color: var(--text); background: var(--surface); }
nav button.active { color: var(--text); background: var(--surface-2); }

.status { display: flex; align-items: center; gap: 6px; }
.badge {
  font-size: 11px; padding: 3px 9px; border-radius: 20px;
  border: 1px solid var(--border); color: var(--text-dim); letter-spacing: .02em;
}
.badge.warn {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}
.badge.live {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok) 35%, transparent);
  background: color-mix(in srgb, var(--ok) 10%, transparent);
}
.badge.err { color: var(--err); border-color: color-mix(in srgb, var(--err) 35%, transparent); }
.badge.on { color: var(--accent); border-color: var(--accent-dim); }

.logout {
  font-size: 11px; padding: 3px 10px; border-radius: 20px;
  color: var(--text-faint); background: none;
}
.logout:hover { color: var(--text); }

/* --- Écrans étroits ------------------------------------------------------ *
 * L'en-tête tient sur une ligne tant qu'il y a de la place ; à trois entrées
 * de navigation il n'en a plus sur un téléphone. Le `flex-wrap` existant le
 * replierait n'importe comment — la marque partirait seule sur sa ligne et les
 * badges d'état se retrouveraient au-dessus de la navigation. On fixe donc
 * l'ordre : identité et état ensemble en haut, navigation pleine largeur
 * dessous, là où le pouce l'atteint.
 *
 * 700 px et non 768 : c'est la largeur à laquelle CETTE barre déborde, pas
 * celle d'une tablette générique. */
@media (max-width: 700px) {
  .shell { padding: 0 14px 48px; }

  header { gap: 10px 14px; padding: 14px 0 16px; margin-bottom: 20px; }

  /* La navigation passe en dernier et prend toute la ligne. `margin-right`
     poussait l'état à droite quand tout tenait sur un rang ; ce rôle revient
     à l'état lui-même une fois la navigation descendue. */
  nav { order: 3; flex: 1 0 100%; margin-right: 0; gap: 4px; }
  .status { margin-left: auto; }

  /* 32 px de haut : une cible qu'on atteint au doigt du premier coup. Par le
     rembourrage, jamais par la taille du texte — grossir la police déplacerait
     la hiérarchie typographique de tout l'écran pour un problème de doigt. */
  nav button { padding: 8px 13px; min-height: 32px; }
  .logout { padding: 7px 12px; min-height: 32px; }
}
</style>
