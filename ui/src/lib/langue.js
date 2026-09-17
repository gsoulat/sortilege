/**
 * Le français de l'interface, en un seul endroit.
 *
 * Quatre composants portaient leur propre copie du même accord, et elles
 * avaient déjà divergé : un écran affichait « 3 402 » là où l'autre affichait
 * « 3402 » pour le même compte. Un compte rendu se lit à voix haute — « 1 198
 * fiches écrites », pas « 1198 fiche(s) écrite(s) » : les parenthèses d'accord
 * demandent de faire le tri soi-même là où la machine connaît déjà le nombre.
 *
 * Les phrases de panne sont ici pour la même raison. « Serveur injoignable
 * (Failed to fetch) » laissait passer le message de `fetch`, en anglais, dans
 * une application française ; et un « Échec. » nu ne dit ni ce qui s'est passé
 * ni quoi faire. Trois cas se distinguent, et ils ne se règlent pas pareil :
 * le serveur n'a pas répondu, il a répondu une erreur, il a refusé une action.
 */

/** Ce que vaut vraiment un compte venu du serveur : jamais NaN, jamais undefined. */
function valeur(n) {
  return Number(n) || 0
}

/** Séparateur de milliers français : « 3 402 » et non « 3402 ». */
export function nombre(n) {
  return valeur(n).toLocaleString('fr-FR')
}

/**
 * « 3 dossiers », « 1 dossier ».
 *
 * Le pluriel se passe en troisième argument quand il ne s'obtient pas en
 * ajoutant un « s » — un nom composé, surtout : `pluriel(n, 'dossier vide',
 * 'dossiers vides')`.
 *
 * Sert aussi au seul mot qui s'accorde quand le nom est déjà dit juste avant :
 * `pluriel(n, 'refusé')` donne « 3 refusés » et « 1 refusé ».
 */
export function pluriel(n, mot, mots) {
  const v = valeur(n)
  return `${nombre(v)} ${v > 1 ? (mots ?? `${mot}s`) : mot}`
}

/**
 * « 3 fichiers rangés », « 1 fichier rangé » : le nom et son participe
 * s'accordent ensemble.
 *
 * Un participe qui finit déjà par « s » n'en prend pas un second — « mis »,
 * « pris », « clos » s'écrivent pareil au pluriel, et « 2 copies miss » ne
 * s'écrit nulle part.
 */
export function accord(n, nom, participe, noms) {
  const v = valeur(n)
  const marque = v > 1 && !participe.endsWith('s') ? 's' : ''
  return `${pluriel(v, nom, noms)} ${participe}${marque}`
}

/**
 * Le verbe qui suit un compte : `${pluriel(n, 'fichier')} ${verbe(n, 'reste',
 * 'restent')}`. Un « (s) » collé à un verbe ne se lit pas, et la phrase se
 * passe du ternaire que cela demanderait sur place.
 */
export function verbe(n, unSeul, plusieurs) {
  return valeur(n) > 1 ? plusieurs : unSeul
}

// --- Quand le serveur ne suit pas ------------------------------------------

/**
 * Ce qui s'est passé, sans le geste qui suit.
 *
 * Pour les écrans de panne, qui portent déjà « Vérifie que le conteneur
 * tourne » et « docker logs sortilege » sous le message : la phrase complète
 * s'y lirait deux fois de suite. Sans statut : le serveur n'a pas répondu.
 */
export function constatServeur(statut) {
  return statut
    ? `Sortilège a répondu une erreur (réponse ${statut}).`
    : 'Sortilège ne répond pas.'
}

/** Le contact est perdu : rien n'est arrivé jusqu'au serveur. */
export function serveurMuet() {
  return `${constatServeur()} Vérifie que le conteneur tourne (docker ps), puis réessaie.`
}

/** Le serveur a répondu, mais en erreur : c'est lui qui sait pourquoi. */
export function serveurEnErreur(statut) {
  return (
    `${constatServeur(statut)} Réessaie ; si ça persiste, ` +
    '« docker logs sortilege » dit pourquoi.'
  )
}

/**
 * Une action refusée, sans un mot du serveur. Le dernier recours : quand il
 * donne un motif, c'est le sien qui s'affiche, jamais celui-ci.
 */
export function refusSansMotif(statut) {
  const ou = statut ? ` (réponse ${statut})` : ''
  return `Le serveur a refusé l'action${ou} sans donner de motif. Rien n'a bougé. « docker logs sortilege » le dit.`
}

/** Une erreur de validation FastAPI (`{loc, msg}`) en une ligne lisible. */
function erreurDeChamp(e) {
  if (!e || typeof e !== 'object') return ''
  const champ = Array.isArray(e.loc) ? e.loc.filter((x) => x !== 'body').join('.') : ''
  const msg = typeof e.msg === 'string' ? e.msg : ''
  if (champ && msg) return `champ « ${champ} » : ${msg}`
  return msg || (champ ? `champ « ${champ} »` : '')
}

/**
 * Le motif d'un refus, tel que le serveur l'a écrit. Un `detail` en liste — la
 * validation de FastAPI, réponse 422 — est aplati champ par champ plutôt
 * qu'affiché brut ; illisible, il laisse place au code, qui reste vrai.
 */
export function motif(corps, statut, defaut) {
  const detail = corps?.detail
  if (typeof detail === 'string' && detail) return detail
  const lignes = Array.isArray(detail) ? detail.map(erreurDeChamp).filter(Boolean) : []
  if (statut === 422) {
    return lignes.length
      ? `Requête mal formée (réponse 422), rien n'a été touché : ${lignes.join(' ; ')}.`
      : "Requête mal formée (réponse 422) : le serveur l'a refusée sans rien toucher."
  }
  if (lignes.length) return `Refus du serveur (réponse ${statut}) : ${lignes.join(' ; ')}.`
  return defaut ?? refusSansMotif(statut)
}
