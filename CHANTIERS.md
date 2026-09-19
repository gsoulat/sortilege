# Chantiers

État au 19 septembre 2026. Dernière version publiée : **v0.53.0** ; le lot
en cours deviendra la **v0.54.0**.

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
- [x] Copie interrompue reconnue : quand le plus petit des deux fichiers est le
      début exact du plus gros (cinq fenêtres d'1 Mio) ou vide, le refus le dit,
      et ni « Garder le plus petit » ni une stratégie ne gardent le tronqué.
      L'écart exact s'affiche : l'arrondi montrait « 3,31 Go » des deux côtés
- [x] Conseil de permission : PUID/PGID n'est proposé que si l'autre bout du
      déplacement reste inscriptible sous cette identité ; sinon, réattribuer le
      dossier bloqué et régler le client de téléchargement
- [x] `PUID=auto` : l'entrypoint lit les dossiers montés et choisit l'identité —
      celle demandée si elle écrit partout, sinon la seule qui convienne, sinon
      root, avec le motif dans `docker logs`. C'est la seule réponse quand
      téléchargements (999), vignettes Jellyfin (0) et bibliothèque (1000)
      appartiennent à trois comptes sans écriture au groupe
- [x] En root, tout fichier ou dossier déposé prend l'identité du dossier qui
      l'accueille : la bibliothèque ne devient pas propriété de root, et reste
      administrable depuis un partage réseau
- [x] Fiches, affiches et sous-titres déposés en 0644 et non plus en 0600 :
      `NamedTemporaryFile` les rendait illisibles par un serveur multimédia
      tournant sous un autre compte
- [x] Les refus du nettoyage des annexes sont regroupés par cause : un dossier
      root bloquant 3 887 fichiers affichait 3 887 fois le même conseil
- [x] Ma médiathèque : « Supprimer les doublons selon la stratégie » (et la
      mise en corbeille) depuis « Ce que je possède », avec les stratégies
      nommées ; l'index oublie ce qui vient d'être retiré, y compris quand une
      lecture de bibliothèque tournait au même moment
- [x] Ce qu'on possède d'une saison est recalculé après un retrait : vider un
      emplacement laissait la saison se dire complète
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

### Récupérer de la place

- [x] Nettoyage des fichiers annexes de la médiathèque, par catégorie : vignettes
      Jellyfin (`.trickplay`), images, fiches, sous-titres, restes reconnus.
      Jamais proposés, même en suppression définitive : vidéos, audio, livres,
      fichiers de disque, corbeille, dossiers système, liens, et **toute extension
      non reconnue** — un contrôle a montré qu'une catégorie ouverte détruisait des
      vidéos et des livres rares. Corbeille par défaut
- [x] Le nettoyage des coquilles ne prend plus un téléchargement en cours
      (`.part`, `.!ut`) pour un dossier abandonné

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
- [x] Ranger et Ma médiathèque sont séparés par l'API, avant la pagination :
      Ranger n'affiche et ne compte que ce qui reste à ranger
- [x] Un rangement refusé ne laisse plus de copie dans la bibliothèque, et son
      motif survit au redémarrage

### Les réglages

- [x] Clé TMDB dans l'interface, avec essai réel — plus besoin d'éditer le `.env`
- [x] Seuils d'identification en pourcentage (« prêt à ranger », « écarté »),
      avec la provenance de chaque valeur ; le `.env` reste lu en repli
- [x] Enregistrer un seuil reclasse la file déjà calculée, sans réinterroger les
      fournisseurs ; les verdicts humains, mémorisés ou imposés ne bougent pas
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

### Copie vers un disque externe (19 septembre)

- [x] Page « Copie » : la médiathèque à gauche (recherche à la frappe dans films
      et séries, sélection qui survit à la recherche), les disques à droite ;
      ne copie que ce qui manque, en reconnaissant ce qui est déjà sur le disque
      même sous un autre nom, et en réutilisant ses dossiers (« Saison 1 »)
- [x] Un fichier à la fois, une barre et un débit en Mo/s par fichier, plafond
      de débit optionnel, pause, arrêt reprenable, reprise au dernier point de
      contrôle après vérification (identité de la source, dernier segment relu
      en entier, échantillons ailleurs)
- [x] Sûreté du NAS : seuls les disques réellement montés À L'INTÉRIEUR d'un
      dossier partagé sont proposés (un disque débranché ne laisse plus un
      dossier vide où écrire sur la partition système) ; tout s'écrit par
      descripteur depuis la racine du disque ouverte une fois, sans suivre de
      lien ; le disque est reconnu par un fichier d'identité qu'il porte
- [x] Vérifié sur un vrai volume exFAT monté : 8/8 fichiers identiques par
      SHA-256 après copie, arrêt et reprise depuis l'interface

### Lecteur et réencodage (19 septembre)

- [x] Lecteur HLS : lecture directe, réemballage (image intacte) ou aperçu
      converti selon ce que le navigateur déclare décoder (HEVC, VP9, AV1) ;
      position réelle affichée après un saut ; « le fichier s'arrête vers
      1:12 » quand il ment sur sa durée
- [x] Un échec de lecture fait décoder le fichier par le serveur en 5 points :
      « aucune erreur aux 5 points testés », ou l'endroit exact de l'erreur —
      plus de faux « fichier corrompu » du navigateur
- [x] Fiche ORIGINAL | RÉENCODÉ avant « Remplacer » : profil, profondeur, HDR,
      définition, durée, pistes ; avertissement grave pour un H.264 10 bits,
      un HDR perdu, une piste perdue
- [x] Réencodage en HEVC 10 bits pour tout, HDR10/HLG conservés avec leurs
      métadonnées, Dolby Vision 5 refusé, débit plafonné pour le wifi, audio
      copié (option E-AC3), désentrelacement, couverture et sous-titres MP4
      conservés, films larges non déformés (2,39:1 → 1920×800)
- [x] Pause et reprise du réencodage : ffmpeg suspendu sur place (SIGSTOP),
      la pause survit au redémarrage
- [x] Un encodage ne peut plus se bloquer à vie sur une source abîmée
- [x] Test de fumée ffmpeg exécuté DANS l'image en CI

### Passe UX/UI du 17 septembre (cinq analyses, puis correctifs)

- [x] **Une action était impossible** : le tiroir Entretien se refermait au clic
      d'armement et démontait son propre bouton, donc « Tout effacer » ne
      pouvait jamais être confirmé
- [x] Tout échec d'appel parle : `call()` n'attrapait ni la perte de contact ni
      une réponse illisible, et le serveur continuait pendant que l'écran se
      taisait. Le message renvoie au Journal, qui sait ce qui a bougé
- [x] Ranger ne prétend plus « la source est vide, c'est l'état recherché »
      quand rien n'a été mesuré, et dit ce que le dernier scan a sauté
- [x] Les deux renvois qui nommaient des écrans inexistants (« Réglages →
      Métadonnées », « Médiathèque → Analyser les sources ») sont justes, et un
      test refuse la prochaine adresse fausse
- [x] `button.primary` existait dans deux styles scopés mais nulle part pour
      l'écran principal : « Ranger N prêts » était rendu comme un bouton
      ordinaire
- [x] Plus aucun débordement horizontal sur 375 px, sur les cinq écrans et les
      quatre onglets de réglages : navigation qui se replie, chemins qui se
      coupent, sélecteur CSS cassé par une virgule rétabli
- [x] Contrastes : filtres à zéro (2,31:1 → 4,75:1), cartes candidates
      (2,12:1 → 4,75:1), et le rouge au repos rendu aux boutons qui détruisent
- [x] La bande cliquable d'une ligne faisait 21 px au centre d'un survol de
      61 px
- [x] Les pastilles de la médiathèque remontées au plancher de 12 px : le
      chiffre des gigaoctets était le plus petit texte de l'application
- [x] La recherche et le filtre de type ne survivent plus au changement
      d'onglet ni de sous-vue — l'écran pouvait rester vide sans un mot
- [x] État des filtres annoncé (`aria-pressed`), retours d'action annoncés
      (`role="alert"` / `role="status"`), loupe des candidats atteignable au
      clavier, lecteur de livre et image agrandie nommés
- [x] Un mot par concept : « médiathèque » en façade, « ranger » pour le geste,
      « refusé » pour un fichier non rangé ; pluriels justes (« 1 entrée
      validée » et non « 1 entrée(s) validées »), erreurs qui disent quoi faire
- [x] Fiches de réglages : sept titres sur dix-huit étaient plus ternes que le
      texte qu'ils coiffent ; trois blocs CSS dupliqués sortis en commun

### À faire, repéré le 17 septembre

- [ ] Le nettoyage des annexes annonce « 3 887 fichiers, 2,7 Go » sans vérifier
      les droits au préalable, alors que le rangement les teste pendant l'essai
      à blanc. L'inventaire devrait dire combien de fichiers sont dans des
      dossiers où l'écriture est refusée
- [ ] `send_to_trash` entre deux volumes copie puis supprime : en root, la copie
      posée en corbeille appartient à root
- [ ] `Journal.append` crée le dossier de données sans adoption d'identité —
      sans conséquence connue, l'entrypoint le chown déjà
- [ ] « Ranger » est l'opération la plus longue et la seule sans avancement :
      l'évacuation a barre, compteur et fichier courant, le rangement n'a rien
- [ ] La liste se réordonne toutes les deux secondes pendant qu'on arbitre, et
      les réponses de `load()` peuvent se chevaucher
- [ ] Le rapport « Ce qui a été refusé » et ses boutons vivent dans la page :
      changer d'écran les perd, alors que `plan.apply_failure` est côté serveur
- [ ] « Récupérer de la place » promet des gigaoctets dont la moitié ne se
      récupère que par l'écran Réencodage, que la médiathèque ne nomme jamais
- [ ] `ExtrasCleanup` pèse 562 mots au-dessus des filtres : à replier dans un
      `<details>` titré avec son total

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
- [ ] Un nettoyage des annexes peut croiser un rangement : une affiche redéposée à
      l'instant, ou un sous-titre retiré pendant une copie entre deux volumes.
- [ ] Un `.txt` compte comme livre : une note posée dans la médiathèque gonfle le
      compteur des livres protégés.
- [ ] Le parser retire de vrais mots de titre pris pour des étiquettes :
      « The French Connection » devient « The Connection », « Charlotte's Web »
      devient « Charlotte's ».
- [ ] La provenance d'une identification (`identified_by`) n'est pas écrite dans
      l'instantané : elle se perd au redémarrage.
- [ ] Les réglages `trust_ai` et `trust_external_ids` ne sont exposés nulle part.
- [ ] L'index de la médiathèque ne se met pas à jour après un rangement : l'écran
      demande de « Relire la bibliothèque ».
- [ ] « Identifier (N) » compte des fichiers, « Pas encore identifiés (N) » des
      œuvres : les deux nombres diffèrent.
- [ ] Les copies orphelines laissées AVANT ce correctif ne sont pas détectées.
