# Chantiers

État au 11 septembre 2026. Dernière version publiée : **v0.50.0** ; le lot
décrit ici deviendra la **v0.51.0**.

Convention : `[ ]` à faire, `[~]` en cours, `[x]` fait **et vérifié dans le code**
— pas seulement annoncé. Un audit a montré qu'un chantier se coche tout seul
dans la tête de celui qui l'a écrit : une échelle typographique définie et jamais
appliquée, un composant de confirmation que personne n'importait. Depuis, une
case ne bouge qu'après un contrôle indépendant. Ce lot-ci a été relu par sept
audits, deux contrôleurs et un recontrôle des derniers correctifs.

---

## Fait, et vérifié

### Le cœur

- [x] Analyse, identification, score de confiance à trois verdicts
- [x] Journal d'annulation, avec **page dédiée** : chaque changement se défait
      seul, ou par œuvre entière dès qu'elle compte deux entrées sur la page
- [x] Les fiches et affiches déposées sont journalisées : annuler un rangement
      les envoie en corbeille avec le reste
- [x] « Valider définitivement » : referme le chantier et vide le journal
- [x] Corbeille par défaut ; les suppressions définitives (doublons, évacuation,
      coquilles en mode « supprimer », vidage) passent par une double
      confirmation. Le nettoyage des coquilles ne touche plus la corbeille
- [x] Doublons, stratégie de qualité par type, budget de poids par fichier
- [x] Réencodage nocturne différé, **sur sa propre page**, avec vérification de
      durée avant remplacement
- [x] Livres EPUB et PDF, lecteur intégré (HTML nettoyé, cadre bac à sable)
- [x] Lecteur vidéo avec remux MKV à la volée
- [x] Résolveur IA en second recours, sept fournisseurs, avec bouton d'essai réel
- [x] Notifications Discord : un cycle automatique ne parle que s'il a rangé,
      les échecs ont leur canal, et **aucune adresse IP ne sort** — masquage au
      point unique d'envoi, et rien ne part quand la sortie n'est pas confirmée
- [x] Rafraîchissement Jellyfin, après un rangement manuel comme après un cycle
      automatique

### Métadonnées locales et sous-titres

- [x] Fiches `.nfo` et affiches déposées au rangement ; `metadata.opf` et
      couverture pour les livres ; un dossier par livre dans le préréglage
      Jellyfin. Tout est désactivé par défaut
- [x] Rien n'est détruit : une fiche ou une affiche remplacée part en corbeille,
      une fiche identifiée n'est jamais remplacée par une fiche sans identifiant,
      et la fiche d'une autre œuvre est corrigée
- [x] Sous-titres OpenSubtitles, par empreinte puis par titre ; langues
      ordonnées ; « la piste audio suffit » ; au rangement manuel, au cycle
      automatique, et sur toute la bibliothèque par lots qui reprennent là où ils
      se sont arrêtés, y compris sur un quota épuisé en cours de route

### Sortie réseau

- [x] Contrôle de sortie VPN : libre, signaler, exiger. Sous « exiger », rien ne
      sort sans sortie confirmée — identification, recherche et choix d'un
      candidat, collections, affiches, sous-titres, Discord, boutons d'essai.
      Exceptions assumées : le serveur multimédia (réseau local) et la
      vérification elle-même. Le réglage ne monte aucun tunnel : il constate

### Intégration et sauvegarde

- [x] Clé d'API (`X-Api-Key`), écran **Réglages → Système → Intégration** : elle
      vaut le mot de passe, se régénère en deux temps, n'est jamais mise en cache
- [x] `download-complete` : un client de téléchargement déclenche un cycle
- [x] Sauvegarde et restauration, écran **Réglages → Système → Sauvegarde** :
      aucun secret dans l'archive, adresse de référence du VPN comprise ; filet
      « avant-restauration » obligatoire ; restauration tout ou rien ; un secret
      n'est dit manquant que si l'archive le portait
- [x] Démarrage robuste : un fichier de réglages illisible n'est jamais écrasé
      (copie `.illisible-*`), un fichier refusé ne bloque plus le démarrage

### L'interface

- [x] Quatre destinations : **Ranger · Ma médiathèque · Réencodage · Journal**
- [x] La barre d'actions n'appartient qu'à « Ranger »
- [x] L'identification s'enchaîne par lots de cent, avec un bouton d'arrêt
- [x] Le scan enchaîne sur l'identification
- [x] Ranger nettoie les dossiers de source qu'il vient de vider
- [x] Bandeau de diagnostic : clé absente, racine non montée, aucun scan, sortie
      non confirmée
- [x] Provenance de chaque identification affichée sur la ligne
- [x] `ConfirmAction` sur tous les sites de confirmation, aucun mécanisme maison
- [x] Un refus d'enregistrement se voit dans tous les onglets des réglages

### Les réglages

- [x] Clé TMDB dans l'interface, avec essai réel — plus besoin d'éditer le `.env`
- [x] Langue des métadonnées, taille minimale d'un fichier, listes d'exclusion
- [x] Gabarits enregistrables, jetons des livres inclus

---

## En cours

- [~] Trois états partout où l'écran peut être vide. Restent :
      `AutomationSettings.vue` (statut en panne sans message),
      `TemplateBuilder.vue` (`/api/tokens` sans filet), `SourcePicker.vue`
      (`try/finally` sans `catch`)
- [~] Contraste des boutons désactivés : 4,75:1 sur le fond du bouton. Restent
      les filtres à zéro (2,30:1) et les cartes candidates (2,19:1), encore
      désactivés par opacité
- [~] Accès clavier en place ; les cibles de 32 px n'existent que sous 700 px de
      large, sur cinq écrans
- [~] Filtres visibles à zéro : Doublons, Surpoids et Hors stratégie gardent leur
      raison dans un `title`, invisible au doigt et au clavier

---

## À faire

### Téléchargement

Sortilège émet déjà du trafic — les requêtes de métadonnées — et le contrôle de
sortie le couvre. Il s'appliquera tel quel aux téléchargements le jour où ils
existeront.

- [ ] **Client de téléchargement** : qBittorrent et Transmission d'abord (API
      HTTP documentée, présents sur tous les NAS), puis SABnzbd. Le client
      télécharge, Sortilège range à la fin — le partage des rôles que Radarr a
      établi et qui a fait ses preuves.
- [ ] **Téléchargement direct** (HTTP), avec reprise sur coupure.
- [ ] **Comptes de débrideurs** : 1fichier, Debrid-Link, AllDebrid,
      Real-Debrid. Clés en écriture seule, comme les autres.
- [ ] **File d'attente** qui survit à un redémarrage.

Hors périmètre, décidé et assumé : **pas de moissonnage de liens sur des sites
d'indexation de copies non autorisées, ni de recherche sur des indexeurs.**
L'application accepte les liens et les comptes qu'on lui donne ; elle ne va pas
les chercher. C'est la limite entre un rangeur et un outil d'acquisition
illicite.

### Interopérabilité

- [ ] **`rename-library` ne couvre pas les livres.**
- [ ] **Les sous-titres déposés ne sont pas journalisés** : une annulation les
      laisse à côté du dossier d'origine.

### Ce qu'on ne copiera pas

Le téléchargement automatique depuis des indexeurs, les listes de suivi, les
profils de qualité à l'acquisition. C'est le cœur de Radarr, c'est excellent, et
l'imiter ferait de Sortilège une mauvaise version de Radarr — qui reste installé
à côté. Sa place est en aval : ce que Radarr n'a pas su nommer, ce qui existait
avant lui, ce qui pèse trop lourd.

### Les cinq idées

Toutes reposent sur des données déjà collectées puis jetées. Aucune ne demande
une dépendance nouvelle.

- [ ] **Détecteur de fichiers cassés.** Comparer la durée mesurée à la durée
      officielle de l'œuvre. Manque le champ `runtime` de TMDB ; le reste
      existe. Aucun concurrent ne regarde à l'intérieur du fichier.
- [ ] **Bilan de bibliothèque.** Un rapport en une page : possédé, en double,
      cassé, manquant, mal nommé, récupérable. Les cinq calculs existent déjà,
      dispersés sur trois écrans.
- [ ] **Poser une question à sa médiathèque.** Le modèle traduit la question en
      *filtre*, jamais en réponse — un modèle qui n'écrit pas la réponse ne peut
      pas l'inventer.
- [ ] **Apprendre des arbitrages.** Repérer une régularité et la *proposer*
      comme règle. Proposer, jamais appliquer.
- [ ] **Aperçu en arbre avant de valider.** Une série qui partirait dans deux
      dossiers se voit d'un coup d'œil sur un arbre ; en liste de chemins, elle
      est invisible.

### Reliquats (chiffres mesurés le 11 septembre)

- [ ] Contrôles désactivés : 85 au total, 32 sans explication visible selon le
      script de l'audit **avant tri manuel**, dont 13 dans `WorkspaceView`. Le tri
      précédent en écartait une dizaine qui s'expliquent d'eux-mêmes (bornes de
      pagination, flèches d'ordre, libellés « en cours »).
- [ ] Migration typographique : 9 fichiers `.vue` migrés sur 27, 3 partiels
      (`AutomationSettings`, `SettingsView`, `WorkspaceView`), 15 intacts ;
      108 tailles sous 12 px, toutes dans `WorkspaceView` et les fichiers intacts.
- [ ] Les paragraphes d'aide de plus de deux lignes.
- [ ] Plage horaire de la surveillance automatique — le réencodage a la sienne.
- [ ] Comportement en cas de conflit : ne rien écraser (actuel), suffixer, ou
      garder le meilleur selon la stratégie de qualité, qui sait déjà trancher.
- [ ] `api/review.py` fait 1 850 lignes, et `automation.py` y appelle quatre
      fonctions privées.
- [ ] Les conduites « garder l'ancien » et « remplacer » font désormais la même
      chose (tout passe par la corbeille) : n'en garder qu'une.
- [ ] La table des alias de langues est recopiée dans l'interface au lieu d'être
      servie par le serveur.
- [ ] Le lien de téléchargement de l'archive quitte l'application si la
      construction échoue, et affiche du JSON brut.
- [ ] Un volume de données **vierge** en lecture seule fait encore tomber le démarrage ;
      un volume déjà initialisé démarre, et n'échoue qu'à la première écriture.
- [ ] Tant que le fichier de réglages est illisible, la clé d'API vit en mémoire
      et change à chaque redémarrage.
- [ ] La construction des collections ne revérifie pas la sortie réseau pendant
      un long enrichissement.
