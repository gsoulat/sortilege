# Chantiers

Liste de travail issue des audits du 10 septembre 2026 (ergonomie, concurrence,
réglages) et des demandes formulées en cours de route.

Convention : `[ ]` à faire, `[~]` en cours, `[x]` fait et **vérifié dans le code**,
pas seulement annoncé. Un chantier n'est coché que lorsqu'un contrôle indépendant
l'a confirmé — l'audit no 2 a montré qu'un système écrit mais non branché se coche
tout seul dans la tête de celui qui l'a écrit.

---

## 1. Finir ce qui a été commencé

La refonte avait posé les fondations sans câbler la moitié des pièces.

- [x] **`ConfirmAction.vue` câblé sur les huit sites de confirmation** — quatre
      dans `MaintenanceSettings`, quatre dans `WorkspaceView`. Les six mécanismes
      maison ont disparu : `grep` n'en trouve plus une seule occurrence.
- [x] **« Vider la corbeille » ne détruit plus au premier clic.** C'était
      l'action la plus destructrice du produit, et la seule sans confirmation.
- [ ] **Appliquer l'échelle typographique.** Deux fichiers migrés sur quinze.
      Le reste garde ses tailles en dur, dont beaucoup sous le plancher de 12 px
      — c'est là qu'est le vrai problème de lisibilité.
- [~] **Redresser la hiérarchie des titres.** Fait dans `MaintenanceSettings`
      (`--text-title`, plus clair que le corps) ; les autres écrans gardent des
      titres plus ternes que le texte qu'ils coiffent.

## 2. La règle « aucun état muet », tenue jusqu'aux bords

- [x] **L'écran Réglages ne reste plus blanc si l'API tombe.** Trois états
      explicites, et le code HTTP vérifié avant de lire le corps — une réponse
      502 arrive en HTML et faisait lever `json()` dans le vide.
- [x] **Trois pannes déguisées en chargement, éteintes.** Le journal et
      l'onglet Réencodage dans la vue principale, plus la corbeille que le
      contrôle a trouvée en plus. Un `catch` remettait la valeur à `null`, ce que
      l'affichage lisait comme « pas encore arrivé ».
- [~] **Les boutons désactivés qui ne disent pas pourquoi.** Une phrase
      collective sous la barre d'actions, cinq raisons dans l'écran d'entretien,
      une par filtre de type. Le contrôle en comptait 56 au total : il en reste,
      surtout hors de la vue principale.
- [x] **Contraste des boutons désactivés** : 3,23:1 → 5,21:1, mesuré, pas estimé.
- [x] **Les filtres ne disparaissent plus à zéro.** Types d'œuvre et natures
      d'opération restent visibles, grisés, avec leur raison — un compte à zéro
      dit qu'on a mesuré, un bouton absent ne dit rien.

## 3. Accessibilité et écrans étroits

- [x] **Accès clavier.** La jaquette sort du bouton parent (imbrication
      invalide), Entrée et Espace activent les éléments cliquables, Échap ferme
      le menu et rend le focus, `aria-expanded` et `aria-current` posés.
- [x] **Règles responsive** dans la vue principale et l'en-tête, qui n'en
      avaient aucune sur plus de deux mille lignes.
- [x] **Cibles tactiles** portées à 32 px par le rembourrage.
- [~] **Textes d'aide trop longs.** Le détail des diagnostics passe dans un
      `<details>` replié et la note de version est supprimée. Les autres
      paragraphes de plus de deux lignes restent.

## 4. Séparer les deux métiers

- [x] **« Ranger » et « Ma médiathèque » sont deux entrées de premier niveau.**
      Deux gestes qui n'ont rien à voir : l'un a une fin, l'autre jamais.
- [x] **Filtres « Livres » et « Animes »** ajoutés à la rangée de types.

## 5. La page de journal

- [x] **Page « Journal », au premier niveau.** Liste groupée par jour,
      annulation par entrée et par œuvre, chemins abrégés sous leur racine
      commune, filtres par nature avec compteurs, recherche par titre.
- [x] Endpoint paginé — branché, et vu fonctionner sur un journal de 788 entrées.
- [x] Format sur disque inchangé : le JSONL existant reste lisible.

C'est l'une des rares choses que Sortilège fait et qu'aucun concurrent ne fait :
Radarr et Sonarr n'ont **aucune** fonction d'annulation — zéro occurrence de
« undo » ou « revert » dans leur fichier de traduction complet. Une page qui la
met en avant est un argument, pas seulement un confort.

## 6. Réglages

- [x] **La clé TMDB se règle dans l'interface.** Écriture seule, bouton d'essai
      qui rapporte le motif exact du refus — le serveur sait dire « clé v3
      attendue, jeton v4 reçu ». `TMDB_API_KEY` reste lue en repli, aucune
      installation existante n'est cassée.
- [x] **Liste d'exclusion** : dossiers et motifs, en plus des valeurs livrées.
- [x] **Taille minimale d'un fichier vidéo**, réglable — elle écartait des
      fichiers sans trace.
- [x] **Langue des métadonnées**, réglable. `fr-FR` était codé en dur.
- [x] `ai.batch_size` retiré : réglage fantôme qu'aucun écran n'exposait.
- [x] Une seule source de vérité pour les deux seuils de décision.
- [ ] **Plage horaire de la surveillance automatique.** Le réencodage a la
      sienne, le scan n'a qu'un intervalle. Un scan coûte pourtant autant en E/S.
- [ ] **Comportement en cas de conflit** : ne rien écraser (actuel), suffixer, ou
      garder le meilleur selon la stratégie de qualité — qui sait déjà trancher.

## 7. Téléchargement

Demandé le 10 septembre. À construire dans cet ordre, chaque étape étant utile
seule.

- [ ] **Intégration d'un client de téléchargement.** qBittorrent et Transmission
      d'abord (API HTTP documentée, présents sur tous les NAS), puis SABnzbd. Le
      client télécharge, Sortilège range à la fin — c'est le partage des rôles que
      Radarr a établi et qui a fait ses preuves.
- [ ] **Téléchargement direct** (HTTP) avec reprise sur coupure.
- [ ] **Comptes de débrideurs** : 1fichier, Debrid-Link, AllDebrid, Real-Debrid.
      Stockage des clés en écriture seule, comme les autres. Un lien d'hébergeur
      est débridé puis téléchargé.
- [ ] **Liaison VPN.** Vérifier avant de télécharger que le trafic sort bien par
      l'interface attendue, et **refuser de démarrer sinon** — un interrupteur qui
      ne vérifie rien donne une fausse sécurité, ce qui est pire que pas
      d'interrupteur.
- [ ] **File d'attente et reprise** : un téléchargement interrompu reprend, et la
      file survit à un redémarrage.

Hors périmètre, décidé et assumé : **pas de moissonnage de liens sur des sites
d'indexation de copies non autorisées.** L'application accepte des liens et des
comptes que l'utilisateur lui donne ; elle ne va pas les chercher.

## 8. Interopérabilité — le plus gros manque face à la concurrence

- [ ] **Écrire les `.nfo`.** Sur Jellyfin, les métadonnées locales sont *toujours*
      lues et **ont priorité** sur TMDB — impossible à désactiver. Plex les lit
      nativement depuis la 1.43.1. Un `.nfo` écrit par Sortilège ferait donc
      autorité : chaque arbitrage rendu à la main cesserait d'être écrasé au
      rafraîchissement suivant.
- [ ] **Télécharger les affiches** à côté des médias. Radarr et Sonarr les
      téléchargent aussi, mais dans leur dossier applicatif — pas dans la
      bibliothèque, et leurs générateurs de métadonnées sont désactivés par défaut.
- [ ] **Un dossier par livre**, et un `metadata.opf` à côté. Jellyfin demande
      exactement ça pour les livres et ne lit pas de `.nfo` pour eux. C'est aussi
      le format de Calibre, donc l'interopérabilité est déjà écrite ailleurs.
- [ ] **Sous-titres** : brancher `filehash.py` — l'empreinte OpenSubtitles est déjà
      écrite et testée dans le projet, sans un seul appelant — puis récupérer et
      convertir en SubRip UTF-8.
- [ ] **Une clé d'API** et une surface d'intégration. Rien ne peut déclencher
      Sortilège de l'extérieur aujourd'hui : ni un script, ni un client de
      téléchargement à la fin d'un transfert.
- [ ] **Sauvegarde et restauration.** Gabarits, décisions mémorisées, journal : un
      conteneur recréé sans son volume repart de zéro, y compris sur des centaines
      d'arbitrages déjà rendus.

## 9. Les cinq idées

Toutes reposent sur des données déjà collectées puis jetées.

- [ ] **Détecteur de fichiers cassés.** Comparer la durée mesurée à la durée
      officielle de l'œuvre. Manque le champ `runtime` de TMDB ; le reste existe.
      Aucun concurrent ne regarde à l'intérieur du fichier.
- [ ] **Bilan de bibliothèque.** Un rapport en une page : possédé, en double,
      cassé, manquant, mal nommé, récupérable. Les cinq calculs existent déjà,
      dispersés dans trois écrans.
- [ ] **Poser une question à sa médiathèque.** Le modèle traduit la question en
      *filtre*, jamais en réponse — un modèle qui n'écrit pas la réponse ne peut
      pas l'inventer.
- [ ] **Apprendre des arbitrages.** Repérer une régularité et la *proposer* comme
      règle. Proposer, jamais appliquer.
- [ ] **Aperçu en arbre avant de valider.** Une série qui partirait dans deux
      dossiers se voit d'un coup d'œil sur un arbre ; en liste de chemins, elle est
      invisible.

## 10. Dette technique

- [ ] `core/filehash.py` : écrit, testé, zéro import. Le brancher (sous-titres) ou
      l'assumer explicitement.
- [ ] `rename-library` ne couvre pas les livres.
- [ ] `api/review.py` fait plus de 1 100 lignes et sert de point d'entrée à
      `automation.py` via une fonction privée.
- [ ] Le dossier d'attente du réencodage n'est purgé que par une route que
      l'interface n'appelle jamais.

---

## Trouvé par le contrôle, pas encore corrigé

Un agent indépendant a vérifié le travail des trois autres. Il a confirmé huit
points, en a trouvé sept incomplets, et sept défauts que personne n'avait
signalés. Ceux-ci restent :

- [ ] Environ 48 boutons désactivés encore muets, surtout hors de la vue
      principale.
- [ ] Les paragraphes d'aide de plus de deux lignes, hors ceux déjà repliés.
- [ ] `--t-xs` et `--t-sm` ne servent que dans deux fichiers : la migration
      typographique reste à faire ailleurs.

Corrigés dans la foulée : le type « livre » absent de la table des libellés du
journal, et un échec réseau d'aperçu qui s'affichait en « fichier illisible » —
accuser le fichier d'un défaut venu du réseau envoie chercher la panne au
mauvais endroit.

## Ce qui est fait

Vérifié dans le code, pas seulement annoncé.

- [x] Rangement, identification, score de confiance à trois verdicts
- [x] Journal d'annulation avec annulation sélective par œuvre
- [x] Corbeille — aucune suppression sans filet, sauf le cas listé en §1
- [x] Doublons, stratégie de qualité par type, budget de poids
- [x] Réencodage nocturne différé, vérification de durée avant remplacement
- [x] Livres EPUB et PDF, lecteur intégré avec HTML nettoyé et bac à sable
- [x] Lecteur vidéo avec remux MKV à la volée
- [x] Notifications Discord — uniquement quand un fichier a été rangé
- [x] Résolveur IA en second recours, sept fournisseurs, avec son état affiché
- [x] Bandeau de diagnostic : clé absente, racine non montée, aucun scan
- [x] Provenance de chaque identification affichée sur la ligne
- [x] Gabarits enregistrables, avec les jetons des livres
