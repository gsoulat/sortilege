<script setup>
import { ref, nextTick, onMounted } from 'vue'

const emit = defineEmits(['authenticated'])

const password = ref('')
const error = ref(null)
const busy = ref(false)
const field = ref(null)

async function submit() {
  if (!password.value || busy.value) return
  busy.value = true
  error.value = null
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password.value }),
    })
    const body = await res.json()
    if (!res.ok) {
      error.value = body.detail ?? 'Connexion refusée.'
      password.value = ''
      await nextTick()
      field.value?.focus()
      return
    }
    emit('authenticated')
  } catch {
    error.value = 'Serveur injoignable.'
  } finally {
    busy.value = false
  }
}

onMounted(() => field.value?.focus())
</script>

<template>
  <div class="gate">
    <form class="card" @submit.prevent="submit">
      <div class="brand">
        <span class="mark">✦</span>
        <h1>Sortilège</h1>
      </div>

      <label for="pw">Mot de passe</label>
      <input
        id="pw"
        ref="field"
        v-model="password"
        type="password"
        autocomplete="current-password"
        :disabled="busy"
      />

      <button class="primary" type="submit" :disabled="busy || !password">
        {{ busy ? 'Vérification…' : 'Entrer' }}
      </button>

      <p v-if="error" class="err">{{ error }}</p>
      <p class="hint">
        Défini par <code>SORTILEGE_ADMIN_PASSWORD</code> dans le <code>.env</code>.
      </p>
    </form>
  </div>
</template>

<style scoped>
.gate { min-height: 100vh; display: grid; place-items: center; padding: 24px; }

.card {
  width: 100%; max-width: 330px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 26px 24px;
  display: flex; flex-direction: column; gap: 10px;
}

.brand { display: flex; align-items: baseline; gap: 9px; margin-bottom: 12px; }
.mark { color: var(--accent); font-size: 17px; }
h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -.01em; }

label { font-size: 12px; color: var(--text-dim); }
input { width: 100%; }

button.primary {
  margin-top: 6px;
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  border-color: var(--accent-dim); color: var(--accent);
  padding: 8px 12px;
}

.err {
  margin: 4px 0 0; font-size: 12.5px; color: var(--err);
  background: color-mix(in srgb, var(--err) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 30%, transparent);
  border-radius: 6px; padding: 7px 10px;
}

.hint { margin: 10px 0 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.5; }
.hint code {
  font-family: var(--mono); font-size: 10.5px;
  background: var(--surface-2); padding: 1px 5px; border-radius: 3px;
}
</style>
