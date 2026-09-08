# CHANGELOG


## v0.13.0 (2026-09-08)

### Features

- **revue**: Choisir la bonne oeuvre parmi les candidats, avec les jaquettes
  ([`ce284a8`](https://github.com/gsoulat/sortilege/commit/ce284a8bfc327fed9369dd91fa31149f2615e421))

Le cas qui a motive ce travail : « Dark Matter » 2015 et « Dark Matter » 2024 sont deux series
  reelles, meme titre, meme type. AUCUN signal automatique ne les separe — ni l'annee (absente du
  nom), ni la notoriete, ni l'accord des fournisseurs. Le score avait raison de ne pas trancher ; ce
  qui manquait, c'etait le moyen de trancher a la main.

Les candidats etaient calcules puis JETES : seul le gagnant survivait. C'est exactement a l'inverse
  de ce dont on a besoin, puisqu'un score bas signifie « plusieurs possibilites » et que ces
  possibilites etaient perdues au moment ou elles devenaient utiles.

Le plan garde donc jusqu'a huit alternatives, avec leur jaquette. Au-dela, une grille d'affiches
  cesse d'aider a decider et redevient une liste a lire.

Les jaquettes ne sont pas decoratives : entre deux oeuvres homonymes, une affiche tranche en une
  seconde la ou une date demande de reflechir. C'est le seul endroit de l'application ou une image a
  une valeur fonctionnelle.

POST /api/review/{id}/choose impose un candidat et recalcule la destination. Le score n'est PAS
  recalcule : un choix humain explicite n'est pas une hypothese a evaluer, et sur un cas d'homonymie
  le calcul avait deja montre qu'il ne savait pas. Le plan est marque `manual` et passe en
  application automatique.

L'ancien candidat rejoint les alternatives : changer d'avis ne doit pas imposer de relancer un scan.

Cote interface, un bouton « Ce n'est pas ca » ouvre la grille. Sur une serie il porte sur l'ensemble
  : l'identification est la meme pour tous les episodes, la corriger episode par episode reviendrait
  a repondre douze fois a la meme question.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.12.0 (2026-09-08)

### Features

- **revue**: Boutons Simuler/Executer, selection par serie, zone explorable elargie
  ([`efe3564`](https://github.com/gsoulat/sortilege/commit/efe35645ea69e420ed6dfc5a076c07f225f607be))

Simuler et executer deviennent deux boutons ------------------------------------------- Le mode
  venait de SORTILEGE_DRY_RUN, donc changer d'avis imposait de modifier la stack et de redemarrer le
  conteneur. Les deux boutons sont maintenant cote a cote : on simule une selection, on lit le
  resultat, on execute la meme selection.

La variable devient un VERROU qui ne sait que RESTREINDRE : elle peut forcer la simulation, jamais
  l'autoriser a l'inverse. Sans cette regle, un verrou pose volontairement sur une instance serait
  contourne par un clic. Quand elle refuse, l'interface le dit au lieu d'annoncer une simulation
  sans motif.

Le defaut de la requete est la simulation : une requete qui omettrait le champ ne deplace rien. Le
  defaut d'une operation irreversible doit etre l'inaction.

Une simulation conserve la selection, une execution la vide — pour pouvoir enchainer les deux sur
  les memes lignes.

Selection par serie ------------------- Cocher episode par episode etait inutilisable : une
  bibliotheque reelle produit des centaines de lignes pour quelques dizaines de series. La file de
  revue est donc regroupee par serie, et une case coche toute la serie.

C'est aussi plus juste que pratique : la decision porte sur l'IDENTIFICATION, qui est la meme pour
  tous les episodes d'une serie. Soit elle est bonne, soit elle ne l'est pas. Les films restent a
  plat, chacun etant une decision independante.

Une selection partielle affiche un etat indetermine et se COMPLETE au clic plutot que de se vider :
  une case a moitie cochee signifie qu'on etait en train de composer.

Zone explorable --------------- Nouvelle variable SORTILEGE_ALLOWED_ROOTS. Un NAS monte souvent un
  volume entier (« /volume1:/storage ») dont seuls quelques dossiers servent de source ; les autres
  etaient montes mais invisibles dans l'explorateur et impossibles a ajouter. La zone parcourable
  est desormais declarable independamment des sources par defaut.

Corrige aussi ApplyRequest, qui avait perdu son champ dry_run dans un patch interrompu a mi-chemin :
  l'endpoint aurait leve une AttributeError des le premier appel.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.11.0 (2026-09-08)

### Features

- **tmdb**: Accepter la cle d' API comme le jeton d' acces en lecture
  ([`fb9b942`](https://github.com/gsoulat/sortilege/commit/fb9b9420de5b39350fb4150bd645039a0eb93dfe))

TMDB propose les deux cote a cote dans les parametres du compte : une « Cle d API » (32 caracteres
  hexa, authentification v3, en parametre d URL) et un « Jeton d acces en lecture » (JWT v4, en
  en-tete Authorization). Ils ne sont pas interchangeables et se tromper donne un 401 sans la
  moindre explication.

Le format est desormais reconnu a la forme du secret et achemine correctement.

Un appel avait ete oublie au passage — la relance de recherche sans annee, qui ne portait pas les
  en-tetes. Avec un jeton v4 elle aurait echoue en 401 precisement dans le cas ou le premier essai n
  a rien donne, donc sur les fichiers les plus difficiles. Un test couvre chaque route.

Le jeton ne part jamais dans l URL : les URL finissent dans les journaux d accces et les en-tetes de
  referent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.10.0 (2026-09-08)

### Features

- Compagnons, nettoyage en corbeille, et correction d'une ruee sur le cache
  ([`2016c9b`](https://github.com/gsoulat/sortilege/commit/2016c9b1969ba3732faff93cdc1dc323dc97f2b0))

Compagnons ---------- Seul le fichier video etait deplace : les sous-titres restaient en arriere.
  Perte de donnees silencieuse — Jellyfin affiche un film sans VOSTFR et personne ne s'en apercoit
  avant de lancer la lecture.

Les sous-titres et les jaquettes suivent desormais la video en etant renommes comme elle. Le suffixe
  distinctif est preserve (« .fr.srt », « .eng.forced.srt ») sans quoi deux pistes se recouvriraient
  sous le meme nom. Une jaquette au nom canonique (poster.jpg) n'est emportee que si la video est
  seule dans son dossier : sinon elle n'appartient a personne en particulier.

Nettoyage --------- Les restes de release vont dans une corbeille datee sous la racine de
  bibliotheque. RIEN N'EST JAMAIS SUPPRIME, hormis les dossiers devenus vides — un dossier vide ne
  contient rien a recuperer.

La liste des dechets reconnus est volontairement CONSERVATRICE : tout ce qui n'y figure pas reste en
  place. Oublier un dechet coute un peu de desordre ; evacuer un fichier qui comptait coute bien
  plus.

L'annulation ramene tout : video, compagnons et restes. Chaque deplacement est journalise
  separement, une annulation partielle serait pire que pas d'annulation du tout.

Ruee sur le cache ----------------- Trouve en MESURANT le volume de requetes plutot qu'en le
  supposant : 96 recherches pour 6 series. Le cache existait mais n'etait rempli QU'APRES la reponse
  ; les 96 fichiers etant traites en parallele, tous consultaient le cache avant la premiere
  reponse, tous manquaient, et tous partaient. Le cache ne servait qu'aux scans suivants.

SingleFlight deduplique les requetes identiques en vol : le premier appelant lance, les suivants
  attendent la meme tache. 6 requetes au lieu de 96, verifie au niveau du transport HTTP sur le vrai
  fournisseur — une imitation contournerait justement les caches qu'on veut tester.

Deux autres reductions du meme ordre : une saison entiere est recuperee en une requete au lieu d'une
  par episode (400 requetes -> 60 sur une bibliotheque reelle), et les sagas de films sont mises en
  cache.

Ajout d'un limiteur de debit a 20 requetes/seconde. Le semaphore bornait les appels SIMULTANES, pas
  le debit : six requetes concurrentes de 50 ms enchainees font 120 requetes par seconde.

Titre des series ---------------- « Avatar The Last Airbender 2024 » gardait l'annee dans le titre —
  vu sur une vraie bibliotheque. Pour un film le titre est coupe a l'annee, mais pour un episode il
  est coupe au motif SxxExx, donc une annee placee avant restait collee et faisait echouer la
  recherche. Elle est retiree, sauf si elle EST le titre (« 2012 »).

Interface --------- Explorateur de dossiers pour choisir les destinations par type, extrait en
  composant reutilisable partage avec le selecteur de sources. Le chemin choisi est converti en
  relatif a la racine de bibliotheque, et un dossier hors de cette racine est refuse avec le motif —
  c'est elle qui garantit le confinement.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.9.1 (2026-09-08)

### Bug Fixes

- **scan**: Passer le scan en tache de fond avec progression
  ([`ddd0cbd`](https://github.com/gsoulat/sortilege/commit/ddd0cbd33cc15f1048a77eeb02c3ed699f0589d9))

Le scan etait synchrone : la requete HTTP ne repondait qu'une fois toute la bibliotheque analysee.
  Sur un NAS reel avec ffprobe actif, cela represente plusieurs minutes pendant lesquelles
  l'utilisateur ne voyait qu'un bouton fige, sans savoir si l'outil travaillait ou etait bloque.
  Pire : un delai d'attente du navigateur ou d'un proxy faisait perdre le resultat d'un scan qui
  avait pourtant abouti.

Le POST demarre desormais un thread et rend la main immediatement (12 ms mesurees au lieu de la
  duree complete). GET /api/library/scan/status expose phase, avance, fichier en cours, temps ecoule
  et estimation du restant.

Le recensement est separe de l'analyse : une premiere passe rapide parcourt l'arborescence sans
  ffprobe pour connaitre le TOTAL. Sans elle, une barre de progression n'aurait pas de denominateur
  et on ne pourrait afficher qu'un compteur qui monte sans fin visible.

L'estimation du restant est une extrapolation lineaire, donc imprecise — les fichiers ne coutent pas
  tous pareil a ffprobe. Elle repond neanmoins a la seule question qui compte pendant l'attente :
  des secondes ou des minutes.

Un second clic sur « Lancer un scan » renvoie l'etat du scan en cours au lieu d'une erreur « un scan
  est deja en cours ». Voir la progression est utile ; lire un refus ne l'est pas.

L'interface reprend le suivi d'un scan deja lance si on recharge la page ou qu'on revient depuis un
  autre onglet, plutot que de laisser croire qu'il ne se passe rien.

Une exception dans le thread est capturee et exposee dans le statut : sans cela le thread mourrait
  en silence et l'interface attendrait indefiniment un scan qui n'existe plus.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.9.0 (2026-09-08)

### Features

- **ai**: Brancher le resolveur en seconde passe sur les cas ambigus
  ([`79fe337`](https://github.com/gsoulat/sortilege/commit/79fe3374ec9820db1717d55a024227c294649e67))

Le module existait, teste, mais aucun code ne l'appelait. Il est desormais integre au pipeline, avec
  une place volontairement etroite.

Quand il est appele ------------------- JAMAIS sur le tout-venant. Une premiere passe deterministe
  traite la bibliotheque a cout nul ; seuls les fichiers qui n'ont PAS obtenu une application
  automatique sont soumis au modele. On ne paie donc un appel que pour ce qui allait de toute facon
  demander une intervention humaine. Les appels sont groupes par lot : un appel par fichier
  couterait dix fois le prix.

Ce qu'on fait de sa reponse --------------------------- Le modele ne fournit pas la reponse, il
  fournit une meilleure REQUETE. Son titre relance une recherche chez les fournisseurs, et les
  candidats continuent de venir d'eux. Sa confiance entre dans le scoring comme un PLAFOND, pas
  comme un terme additif : elle dit « je crois reconnaitre cette oeuvre », pas « les donnees
  concordent ».

Ce qu'on lui refuse ------------------- - Une proposition ne remplace le resultat deterministe que
  si elle score MIEUX. Sans cette regle, un modele qui se trompe ferait perdre une identification
  deja correcte. - Un titre vide ne declenche rien : le modele qui avoue ne pas savoir est respecte,
  pas contourne. - Une panne du resolveur laisse le lot exactement dans l'etat ou la premiere passe
  l'a mis. C'est un bonus, jamais une dependance.

Le SDK Anthropic etant synchrone, l'appel passe par asyncio.to_thread : l'invoquer directement
  figerait la boucle d'evenements et donc tout le scan.

L'import d'`anthropic` reste tardif et son absence est rattrapee proprement — une installation sans
  l'extra « ai » doit fonctionner normalement plutot que de planter au chargement du module.

Les utilitaires de test partages sont sortis dans tests/helpers.py : importer un fichier de test
  depuis un autre creait un couplage ou renommer l'un cassait l'autre, et `tests/` n'etant pas un
  paquet l'import relatif echouait de toute facon.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.8.0 (2026-09-07)

### Features

- **auth**: Fermer l'API derriere le mot de passe
  ([`25dc6e8`](https://github.com/gsoulat/sortilege/commit/25dc6e8bbd42baf1ea07476fb6ea61271de10fdb))

SORTILEGE_ADMIN_PASSWORD etait EXIGE au demarrage mais n'etait utilise nulle part. L'application
  refusait de demarrer sans mot de passe tout en etant entierement ouverte : pire qu'une absence
  d'authentification, puisqu'elle en donnait l'illusion. Sur un LAN, pour un outil qui deplace des
  fichiers.

Toute l'API est desormais fermee par defaut, via une LISTE BLANCHE de routes publiques et non une
  liste noire : oublier d'ouvrir une route rend une page inaccessible, ce qui se voit tout de suite
  ; oublier d'en fermer une ne se voit jamais. Seuls /api/health (necessaire au HEALTHCHECK du
  conteneur, qui n'a pas de session) et /api/auth/* restent ouverts.

L'interface elle-meme reste servie sans session — c'est elle qui affiche l'ecran de connexion.
  Seules les donnees et les actions sont protegees.

Le mot de passe n'est pas stocke : il EST la variable d'environnement. Le hacher ne servirait a rien
  puisque la source de verite est deja en clair dans le .env. La comparaison est en revanche faite
  en temps constant, sans quoi la duree des reponses laisserait deduire le prefixe correct. Ce qui
  part vers le navigateur ne contient jamais le mot de passe : seulement un jeton date, signe avec
  SECRET_KEY, en cookie HttpOnly + SameSite=Lax. Regenerer SECRET_KEY deconnecte donc tout le monde,
  ce qui est le comportement attendu.

Verrouillage apres 8 echecs pendant 5 minutes : sans cela un mot de passe faible tombe en quelques
  minutes sur un LAN. La reponse passe alors en 429 et non 401 — le probleme n'est plus le mot de
  passe.

Cote interface, fetch est enveloppe une fois pour toutes : un 401 ramene a la connexion au lieu de
  laisser chaque vue afficher son erreur cryptique. Traiter le cas dans chaque appel aurait garanti
  un oubli quelque part.

Les tests parcourent les 13 routes une par une pour verifier qu'aucune n'est accessible sans session
  : une seule oubliee rendrait la protection decorative. Le fixture de test_api.py se connecte
  desormais, ces tests portant sur le comportement des endpoints et non sur leur protection.

Retire au passage argon2-cffi et sqlmodel des dependances : declares mais importes nulle part, ils
  alourdissaient l'image en laissant croire a un usage. sqlmodel reviendra avec la persistance.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.7.0 (2026-09-07)

### Features

- Cabler le pipeline de bout en bout et la file de revue
  ([`488680d`](https://github.com/gsoulat/sortilege/commit/488680d9a22045f3eeb61504f81fee731e4f6d2f))

Le coeur existait piece par piece ; il tourne maintenant en entier : scan -> fournisseurs ->
  correspondance -> score -> plan -> deplacement -> annulation.

pipeline -------- Chef d'orchestre, sans logique metier — sa brievete est le signe que la separation
  tient. Deux contraintes gouvernent sa forme :

- **Concurrence bornee** (semaphore a 6). Une bibliotheque de 2 000 fichiers lancerait autant de
  requetes simultanees, epuiserait le quota TMDB et ferait bannir l'adresse. Un scan lent n'est
  jamais un probleme, un bannissement si. - **Un fichier qui echoue n'arrete pas le lot.** Chaque
  fichier produit un plan, fut-il un rejet motive ; une exception qui remonterait ferait perdre le
  travail deja fait sur les precedents.

L'enrichissement (saga du film, titre de l'episode) n'est demande que pour le candidat GAGNANT : le
  faire sur tous multiplierait le cout par dix pour une information dont on ne se sert que sur un
  seul. L'absence de reponse sur un episode devient le signal episode_match=False — la saison
  n'existe pas chez ce candidat, donc l'identification est douteuse.

API et interface ---------------- POST /api/review/plan calcule, /apply applique, /undo annule,
  /journal liste ce qui a bouge. La file separe trois groupes : assez sur pour etre applique seul,
  en attente d'arbitrage (avec cases a cocher et les raisons du score), et ecarte. Appliquer les
  plans en attente demande un clic explicite sur une selection : c'est precisement ce que l'outil
  s'interdit de faire seul.

Tests d'integration ------------------- Fournisseurs simules — pas pour eviter le reseau, mais parce
  qu'un test dependant de TMDB echouerait un jour pour une raison etrangere au code. Le parcours
  complet est verifie : un film est identifie, range dans son dossier de saga, puis ramene a sa
  place d'origine par l'annulation.

Un de ces tests a d'abord echoue pour une bonne raison : le faux TMDB ne renvoyait que le titre
  francais, et « Dune Part Two » ne ressemble pas assez a « Dune, deuxieme partie » pour passer en
  automatique. C'etait la simulation qui etait fausse — le vrai TMDB renvoie aussi original_title.
  Le cas a ete conserve comme test a part entiere : sans titre original, l'outil doit demander un
  regard humain plutot que de bluffer.

asyncio_mode = "auto" est explicite dans pyproject : sans lui les tests asynchrones seraient
  collectes puis silencieusement ignores, soit le pire des echecs — celui qui se presente comme un
  succes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.6.0 (2026-09-07)

### Features

- **core**: Calcul de confiance, plan de rangement et journal d'annulation
  ([`44ff50a`](https://github.com/gsoulat/sortilege/commit/44ff50a0f8126d7804ebc7db964fbf0f79f1832f))

Les trois pieces qui manquaient pour que le pipeline tourne de bout en bout.

compute_score ------------- Ecrit comme une BASE A AJUSTER : les poids sont des constantes nommees
  et documentees, pas des valeurs enfouies. La bonne facon de les regler est de lancer un scan,
  regarder ce qui atterrit en revue, et deplacer le curseur du signal qui a mal juge.

Trois etages : une base de preuves ponderees, des modificateurs signes, puis des plafonds.

Deux principes gouvernent les poids :

- **Les contradictions pesent plus lourd que les confirmations.** Trouver l'annee juste est
  ordinaire (+0.20), en trouver une fausse est alarmant (-0.35). Une annee absente ne fait rien : «
  je ne sais pas » n'est pas « c'est faux ». - **L'accord entre fournisseurs progresse de facon non
  lineaire** (0 / +0.12 / +0.18). L'essentiel de l'information est dans le fait qu'une SECONDE
  source confirme ; la troisieme ajoute peu.

Les plafonds traitent ce qu'aucun bonus ne doit effacer : l'homonymie non tranchee borne a 0.75,
  donc sous le seuil d'application automatique quels que soient les autres signaux. Et la confiance
  de l'IA est un plafond, pas un terme additif — elle dit « je crois reconnaitre cette oeuvre », pas
  « les donnees concordent ».

Un credit de depart a ete ajoute apres coup : sans lui, un fichier au titre correct mais sans annee
  ni second fournisseur tombait sous le seuil de rejet, donc ecarte en silence alors qu'il etait
  seulement ambigu. Or matching a deja elimine les candidats sous 0.35 de similarite — tout ce qui
  arrive au scoring est deja une piste serieuse et merite au minimum un regard.

Les tests portent sur des PROPRIETES (ordres, asymetries, plafonds), pas sur des nombres : regler
  les poids ne doit pas casser la suite, changer la logique si.

planner ------- Un plan est un OBJET, pas une action — le choix qui separe Sortilege de Radarr. Un
  echec (aucun candidat, gabarit invalide, destination hors racine) ne leve pas : il produit un plan
  REJECT porteur du motif, sinon un seul fichier interromprait le traitement de toute la
  bibliotheque.

Les valeurs du gabarit suivent un ordre de preference constant : ce que dit le FOURNISSEUR, puis le
  FICHIER, puis le NOM.

journal ------- Le seul module qui ecrit sur le disque. Trois regles absolues :

- **Rien n'ecrase jamais rien.** Deux encodages du meme episode produisent la meme destination ;
  c'est a l'utilisateur de trancher, pas a l'outil. Perdre un fichier serait le pire defaut possible
  pour un rangeur. - **Chaque operation est fsync sur le disque avant d'etre oubliee.** Sans cela le
  journal peut rester en cache pendant qu'un fichier est deja deplace : on perdrait la trace de ce
  qu'on vient de faire. - **La simulation ne touche a rien** et suit exactement le meme chemin de
  verification.

Journal en JSON Lines et non en JSON : on ajoute sans relire le fichier entier, et un journal
  tronque par une coupure reste exploitable jusqu'a sa derniere ligne complete.

L'annulation se fait dans l'ordre inverse — deux operations peuvent avoir touche des chemins
  imbriques, et defaire chronologiquement recreerait des collisions. Une annulation qui echoue
  laisse son entree au journal : elle reste a faire.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.5.0 (2026-09-07)

### Features

- **sources**: Ajouter plusieurs sources depuis l'interface, bibliotheque comprise
  ([`1276a80`](https://github.com/gsoulat/sortilege/commit/1276a80bf6074784fec993dec32b36a011671d9c))

On ne pouvait que CHOISIR parmi les racines declarees dans l'environnement. On peut desormais en
  ajouter, avec un explorateur de dossiers plutot qu'une saisie de chemin a l'aveugle.

Perimetre autorise : les racines sources ET la racine de bibliotheque. Cette derniere n'est pas un
  oubli — scanner une bibliotheque deja rangee est un usage a part entiere : la normaliser, corriger
  d'anciens noms, rattraper ce qui a ete classe a la main. C'est exactement le cas pour lequel on
  sort FileBot d'habitude.

Une source ajoutee doit vivre sous une de ces zones. Le conteneur ne voit que ses volumes, et
  accepter un chemin libre offrirait un parcours de tout ce qui est monte. Le message de refus
  indique les zones autorisees et comment en ouvrir une autre, plutot que de se contenter d'un «
  interdit ».

L'explorateur (GET /api/settings/browse) applique le meme confinement : on ne descend que sous une
  zone autorisee et le bouton « Remonter » disparait des qu'on atteint sa racine, plutot que de
  proposer une navigation qui echouera.

Les fichiers deja dans la bibliotheque sont marques in_library. Sans ce marqueur, scanner ses
  propres Films proposerait de les deplacer alors qu'ils sont au bon endroit : du bruit dans la file
  de revue, et une operation nulle dans le journal d'annulation si elle etait appliquee.

Une racine venue du compose ne peut pas etre retiree depuis l'interface — elle n'appartient pas aux
  preferences.

Correction : l'interface envoyait un patch partiel apres un Object.assign, ce qui laissait une
  fenetre ou deux enregistrements rapproches pouvaient s'entrecroiser et perdre un champ. Elle
  envoie desormais l'etat complet, ce qui est idempotent et supprime la course.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.4.0 (2026-09-07)

### Features

- Sous-dossiers, regroupement par saga, destinations par type
  ([`f44edde`](https://github.com/gsoulat/sortilege/commit/f44edde61b4690991dd1d8ef530c44b3545c3ec8))

Sous-dossiers ------------- Le parseur ne lisait que le dossier parent immediat. Sur « Dune
  (2024)/CD1/film.mkv » il lisait « CD1 », sur « Severance/Season 02/ep07.mkv » il lisait « Season
  02 » : dans les deux cas le titre et l'annee sont un cran plus haut. Il recoit desormais toute la
  chaine de dossiers entre la racine et le fichier.

Trois consequences : - les dossiers sans valeur de titre sont ecartes du contexte (CD1, VIDEO_TS,
  Season 02, Films, Subs...) — les inclure ferait deriver le titre et pourrait fournir une fausse
  annee ; - un nom de fichier vide de sens (film.mkv, VTS_01_1.mkv, 00001.m2ts) fait remonter au
  dossier le plus proche qui en porte un ; - « Season 02 » fournit la saison, ce qui rend
  exploitable un numero d'episode nu (« 07.mkv »). Hors dossier de saison ce motif reste ignore,
  sinon « 2012.mkv » deviendrait l'episode 2012.

Un titre repris d'un dossier est ampute de son annee : « Severance (2022) » donne « Severance », pas
  « Severance 2022 », sinon la similarite chute face au libelle du fournisseur.

Regroupement ------------ Jeton {collection}. Pour les films il vient de belongs_to_collection chez
  TMDB (« Hunger Games - Saga ») ; un appel de detail est necessaire, la recherche ne le renvoie
  pas, d'ou le cache.

Pour les series, TMDB n'expose AUCUN champ de franchise — « Star Trek: Discovery » et « Star Trek:
  Picard » y sont deux series sans lien. On deduit donc la franchise du prefixe avant le sous-titre.
  franchise_for() peut corroborer la deduction avec le reste de la bibliotheque et n'accepte le
  regroupement que si une autre serie le partage, ce qui evite le dossier a un seul element.

Aucun conditionnel n'est necessaire dans les gabarits : un jeton vide produit un segment vide, et un
  segment vide disparait du chemin. Le meme gabarit sert donc au film isole comme au film de saga.

Destinations et sources choisies -------------------------------- Nouveau module preferences : ce
  qui releve de l'USAGE (quelles sources scanner, ou ranger chaque type, avec quel gabarit) devient
  modifiable depuis l'interface, tandis que config.py garde ce qui releve du DEPLOIEMENT (montages,
  cles, secrets) — ces valeurs doivent correspondre aux volumes du conteneur, les changer a chaud
  produirait une configuration qui ne survit pas a un redemarrage.

Garde-fou : une destination est resolue sous la racine de bibliotheque. Et comme resolve_within
  NEUTRALISE une remontee au lieu de la refuser — bon comportement pour un titre venu d'une API,
  mauvais pour une saisie humaine — les « .. » sont refuses explicitement en amont. L'utilisateur
  lit un refus au lieu de decouvrir plus tard que ses fichiers sont partis dans « media/etc ».

Interface --------- Bibliotheque : series et animes regroupes par titre puis par saison, sections
  repliables. Une liste plate de 300 episodes est illisible, et c'est justement l'arborescence qui
  sera produite sur le disque. Les animes en numerotation absolue vont sous une cle distincte plutot
  que de se voir inventer une saison.

Reglages : selection des sources et destination par type, editables.

core/matching.py : le chainon entre le scan et le scoring. Il ne decide rien, il mesure — similarite
  de titre sur TOUS les libelles connus (« Very Bad Trip » doit matcher « The Hangover »),
  concordance d'annee a un an pres, accord entre fournisseurs, homonymie, identifiant declare.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.3.0 (2026-09-07)

### Features

- **ui**: Rendre Bibliotheque, File de revue et Reglages fonctionnelles
  ([`90f3383`](https://github.com/gsoulat/sortilege/commit/90f33836f478ea223c8624de6e22794e1d6b0b20))

Les trois onglets etaient desactives. Ils repondent maintenant a de vraies donnees, et l'API est
  decoupee en modules — main.py devenait un fourre-tout.

Bibliotheque : scan des racines sources, avec ce que le parseur et la sonde comprennent de chaque
  fichier. Aucune cle d'API n'est requise a cette etape, ce qui permet de juger la reconnaissance
  avant meme de configurer quoi que ce soit. La resolution mesuree prime sur celle annoncee dans le
  nom. Filtres par type et par « lecture douteuse » (qualite < 0.6) pour aller droit aux cas
  problematiques.

Le scanner ecarte les echantillons et bandes-annonces, les fichiers sous 50 Mo et les dossiers
  systeme des NAS (@eaDir, .@__thumb, #recycle). Un verrou empeche deux scans concurrents : ils
  doubleraient la charge disque pour un resultat identique.

File de revue : plutot que des donnees factices, elle enonce precisement ce qui manque pour qu'elle
  se remplisse — compute_score, une cle TMDB, core/matching. La detection appelle reellement
  compute_score et intercepte NotImplementedError, donc l'ecran se debloquera tout seul le jour ou
  la fonction sera ecrite.

Reglages : diagnostic (ffprobe, racines, cle TMDB, mode simulation), chemins, seuils, fournisseurs,
  resolveur IA. En LECTURE SEULE et a dessein : toute la configuration vient de l'environnement, une
  ecriture depuis l'UI creerait un second etat de verite invisible dans le compose. Aucune valeur
  secrete n'est renvoyee, seulement des booleens « configure ou non » — cette reponse finit dans la
  console du navigateur.

Chaque diagnostic enonce la consequence concrete, pas seulement l'absence : un « ffprobe manquant »
  sans effet enonce n'aide personne a decider s'il faut agir.

Le stockage du dernier scan est en memoire, assume et temporaire jusqu'aux modeles SQLModel. Rien
  n'est encore applique au disque, donc rien de precieux n'est perdu au redemarrage.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.2.0 (2026-09-07)

### Features

- **probe**: Reconnaitre les fichiers par leur contenu, pas que par leur nom
  ([`d37e66b`](https://github.com/gsoulat/sortilege/commit/d37e66b1b33427fb1540f680322305c21df88699))

C'est l'approche de Plex et la raison pour laquelle il se trompe moins. Trois sources s'ajoutent au
  parseur, et leur valeur ne vient pas de leur nombre mais du fait qu'elles sont DECORRELEES de lui
  : un nom de release et une duree ne se trompent pas de la meme facon, donc leur accord vaut
  confirmation.

- Fichier .nfo (convention Kodi/Emby, ecrite par Radarr et Sonarr) : un tmdbid ou imdbid declare
  n'est pas une ressemblance a evaluer, c'est une reponse. Gere le XML Kodi, les champs plats,
  <uniqueid type="...">, et retombe sur une recherche d'identifiant en texte brut pour les .nfo de
  release en ASCII art. - Tags du conteneur MKV/MP4 : titre reel, serie, numero d'episode. - Duree :
  separe film et episode sans rien lire du nom. - Resolution reelle : un fichier etiquete 1080p
  encode en 1280x720 est frequent ; on range d'apres la mesure, pas d'apres l'etiquette.

Trois consequences dans la politique de decision : - un identifiant concordant court-circuite le
  score et applique — le faire passer par le calcul reviendrait a douter d'une certitude ; - un
  identifiant designant une AUTRE oeuvre rejette quel que soit le score ; - une duree incompatible
  bloque l'application automatique et renvoie en revue.

Signals gagne external_id_match, runtime_plausible et container_title_similarity ; Policy gagne
  trust_external_ids pour les sources dont on ne fait pas confiance aux .nfo.

ffmpeg est ajoute a l'image pour ffprobe. Le code degrade proprement sans lui, mais on ne veut pas
  de cette degradation par defaut. Aucun test ne depend du binaire : le parsing .nfo, la fusion et
  les bornes de duree sont purs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **providers**: Tmdb et AniList, tolerants aux pannes et mis en cache
  ([`16b4c0f`](https://github.com/gsoulat/sortilege/commit/16b4c0f7408a06527339b4b818a2ebdd6ba565f9))

TMDB couvre films et series — c'est aussi la source vers laquelle FileBot a bascule apres les
  restrictions d'API de TheTVDB. AniList couvre les animes et n'est pas substituable : les releases
  de fansub portent un titre romaji et AniList expose romaji, anglais, natif et synonymes, ce dont
  la comparaison de titres a besoin.

Candidate.all_titles() renvoie tous les libelles connus. Sans cela « Very Bad Trip » ne trouve
  jamais « The Hangover » et la moitie d'une bibliotheque francaise echoue.

Deux principes tenus par le socle commun :

- Un fournisseur en panne renvoie une liste vide, jamais une exception. Quota depasse ou reseau
  coupe font baisser provider_agreement, donc le score, donc le fichier part en revue manuelle. On
  degrade vers l'humain, jamais vers une decision hasardeuse. - Un fournisseur ne decide rien : il
  propose des candidats bruts, la ponderation appartient au scoring.

Cache TTL en memoire : une bibliotheque contient des dizaines d'episodes de la meme serie, sans
  cache on interroge une fois par fichier et le quota saute. La popularite est normalisee 0->1 par
  chaque fournisseur, les echelles brutes (votes TMDB, popularite AniList) n'etant pas comparables.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.1.0 (2026-09-07)

### Bug Fixes

- **parser**: Ne pas confondre un nombre du titre avec l'annee de sortie
  ([`1de61ed`](https://github.com/gsoulat/sortilege/commit/1de61ed93ac10f3751f42ee22def4777b215c296))

« Blade Runner 2049 (2017) » etait lu comme sorti en 2049, avec « Blade Runner » pour titre. Meme
  probleme sur « 2012 (2009) ». La lecture naive prenait la premiere occurrence ressemblant a une
  annee.

Deux regles suffisent sur les cas reels : une annee entre parentheses ou crochets gagne toujours ;
  sinon on prend la derniere, puisqu'un titre precede son annee.

Corrige aussi les prereglages de gabarit, qui codaient « ({year}) » en dur et produisaient «
  Films/Sans Titre Connu ()/... » sur un film sans annee. L'annee passe desormais par le
  conditionnel. Le defaut a ete rendu visible par l'apercu live, ce qui est exactement sa raison
  d'etre.

Les prereglages anime utilisent {absolute_episode} : c'est ce que portent les releases de fansub,
  {episode} restant absent tant que la correspondance saison/episode n'est pas resolue chez le
  provider.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Continuous Integration

- Tests, lint, versionnage semantique et publication de l'image
  ([`783294d`](https://github.com/gsoulat/sortilege/commit/783294d9250bb73f53a8f55f7d4007d33327f0c0))

80 tests couvrant le parseur (noms de release reels), le moteur de gabarit, le confinement des
  chemins et la politique de decision. Les deux tests du contrat de compute_score sont marques xfail
  : ils basculeront tout seuls quand la fonction sera ecrite, sans qu'on ait a y penser.

conftest.py force l'environnement de test : sans cela un .env local ferait diverger la CI du poste
  du developpeur.

CI (ci.yml) : ruff check + ruff format + pytest sur Python 3.11 et 3.12, compilation de l'UI, et
  construction de l'image avec demarrage effectif du conteneur — la CI verifie que l'interface est
  bien servie par le meme port que l'API, pour que la contrainte « une seule application » soit
  tenue par un test et pas seulement par une intention.

Release (release.yml) : python-semantic-release derive la version des commits conventionnels, cree
  le tag et la release, puis publie l'image multi-arch sur ghcr.io. arm64 inclus, les NAS Asustor et
  Synology recents en ont besoin.

Passe Decision en StrEnum et ajoute un .dockerignore qui exclut tout build local de l'UI, pour qu'il
  ne masque jamais les sources reelles dans l'image.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- Socle Sortilege — pipeline, gabarits surs, confinement, resolveur IA
  ([`9c0ed2c`](https://github.com/gsoulat/sortilege/commit/9c0ed2c8787d244fa436deaa2abb8c7ec584f49d))

Application unique (un processus, un port) : FastAPI sert l'UI Vue compilee, sur le modele de
  Radarr. Pas de CORS, pas de second port.

Coeur deterministe : - parser.py : lecture des noms de release (film / episode / anime absolu), avec
  auto-evaluation de sa propre fiabilite - template.py : moteur de gabarit SANS eval — substitution
  de jetons et un conditionnel declaratif. FileBot execute du Groovy, donc du code arbitraire sur le
  NAS ; on refuse cette surface. - safety.py : confinement des chemins, chaque segment assaini
  separement. Une metadonnee de provider ou de LLM ne doit jamais pouvoir ecrire hors bibliotheque.
  - scoring.py : signaux et politique de decision AUTO / REVIEW / REJECT. compute_score reste a
  implementer.

Resolveur IA (optionnel, off par defaut) : appele par lots sur les seuls cas ambigus. Il propose des
  metadonnees, jamais un chemin, et sa sortie repasse par le scoring. Les noms de fichiers sont
  traites comme des donnees non fiables.

Packaging : image non-root, systeme de fichiers en lecture seule, mode simulation actif par defaut.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **ui**: Constructeur de gabarit en glisser-deposer avec apercu live
  ([`6ca0756`](https://github.com/gsoulat/sortilege/commit/6ca0756a9d2c2a47ad09daf5765a6ba48c11b26f))

Interface Vue 3 servie en statique par FastAPI : un processus, un port. Pas de CORS, pas de second
  service. Vite n'intervient qu'au developpement et a la construction de l'image.

La palette de jetons est construite depuis GET /api/tokens, donc depuis core/template.py : la liste
  n'existe qu'a un seul endroit et le frontend ne la duplique pas.

Ajoute POST /api/templates/preview. L'apercu appelle le vrai moteur de rendu plutot qu'une
  reimplementation JavaScript, sinon les deux divergeraient au premier changement du moteur — et
  l'apercu mentirait precisement quand il compte le plus.

Remplace on_event("startup") par un gestionnaire lifespan (deprecie par FastAPI).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
