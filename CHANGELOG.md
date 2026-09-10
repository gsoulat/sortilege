# CHANGELOG


## v0.39.0 (2026-09-10)

### Features

- **lecture**: Lire un MKV en le reemballant, sans reencoder l image
  ([`e5d9479`](https://github.com/gsoulat/sortilege/commit/e5d9479617583b5322ab0eec7ed9d1c3d1ce571e))

J'avais conclu trop vite. En ecartant le transcodage a la volee — a juste titre, il ferait chauffer
  le NAS pour verifier trois secondes de film — j'ai laisse croire qu'un MKV etait par nature
  illisible dans un navigateur. C'est faux, et la confusion est la mienne : le CONTENEUR n'est pas
  le contenu.

Une release H.264 dans un MKV est parfaitement lisible par un navigateur. Il suffit de changer
  d'emballage, ce qui ne touche pas une seule image et ne coute presque rien en processeur. Seule la
  piste audio est reencodee quand il le faut — AC-3 et DTS sont courants dans les MKV et ne passent
  nulle part — et c'est sans commune mesure avec une video.

Verifie dans le conteneur, sur un vrai fichier :

MKV h264 + ac3 -> MP4 h264 + aac

L'image est copiee telle quelle. Le HEVC, lui, reste refuse : le reemballer ne suffirait pas, il
  faudrait le reencoder — et la, les vignettes repondent deja a la question sans faire tourner le
  NAS. L'interface le dit plutot que de laisser un lecteur noir.

Deux details qui evitent des ennuis. Le flux est FRAGMENTE, donc il commence a arriver immediatement
  au lieu d'attendre le reemballage complet ; en contrepartie la barre ne permet pas de sauter, d'ou
  un parametre de position. Et le processus est TUE des que le client se detache : sans cela, fermer
  un lecteur laisserait ffmpeg lire le fichier jusqu'au bout, et quelques ouvertures suffiraient a
  saturer le NAS avec du travail que plus personne n'attend.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.38.1 (2026-09-10)

### Bug Fixes

- **evacuation**: Diagnostiquer partout, et cesser d appeler « echec » un refus
  ([`a17fd29`](https://github.com/gsoulat/sortilege/commit/a17fd292e5ca429383adf1430f8e6e4c8fbb98e4))

Deux corrections tirees d'un cas reel.

**Le diagnostic manquait sur un chemin.** Quand la corbeille contient deja le fichier, l'evacuation
  supprime l'exemplaire surnumeraire — et ce chemin-la n'ajoutait pas le message qui nomme le
  proprietaire du dossier. C'est precisement celui qu'on emprunte apres une premiere tentative
  interrompue, donc le plus frequent en pratique : le seul ou le diagnostic manquait etait le seul
  dont on avait besoin.

**Vingt-sept refus etaient etiquetes « Echec ».** Ils n'en sont pas. Le fichier range et la copie
  n'ont pas la meme taille — 1 439 234 650 contre 1 442 789 811 octets dans le cas observe — donc ce
  sont deux ENCODAGES distincts de la meme oeuvre, pas deux exemplaires du meme fichier. Le refus
  est le comportement voulu : supprimer l'un des deux ferait perdre une version.

Mais l'appeler « echec » induit en erreur, et surtout cache ce qu'il faut faire. C'est une decision
  qu'aucun algorithme ne peut prendre a la place de l'utilisateur : comparer les deux, garder celui
  qu'il prefere. Le libelle le dit maintenant, et renvoie a l'apercu video pour comparer.

Deux autres motifs n'avaient pas d'explication et tombaient dans le meme fourre-tout : fichier pas
  encore range, et corbeille inaccessible.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.38.0 (2026-09-09)

### Features

- **mediatheque**: Bouton « Tout effacer » pour repartir d un scan neuf
  ([`37e968c`](https://github.com/gsoulat/sortilege/commit/37e968c94b65d01b5b7ad72502ab070344e6c591))

La liste se remplissait de residus qu'aucune action ne retirait : titres illisibles tires de noms de
  fichiers abimes, plans devenus sans objet, entrees accumulees au fil des essais. « Recommencer »
  ne vidait que la file de plans, pas l'instantane du scan qui la nourrit.

Ce qui est efface est RECONSTRUCTIBLE, et un scan le refait : l'instantane, les plans calcules,
  l'avancement des lots, les apercus en cache.

Ce qui reste ne se refait pas, et cette distinction est tout l'interet de la fonction :

- **le journal d'annulation** — plus de six mille entrees dans le cas qui m'a ete montre. C'est le
  seul chemin de retour pour tout ce qui a deja ete deplace ; l'emporter condamnerait ces fichiers a
  rester ou ils sont, sans recours ; - **les identifications retenues**, tranchees une a une a la
  main. Les perdre reposerait toutes les questions au scan suivant ; - **les preferences** :
  sources, destinations, gabarits.

Une purge qui emporterait le journal serait une catastrophe silencieuse — on ne s'en apercevrait
  qu'en cherchant a revenir en arriere, c'est-a-dire trop tard. Les deux tests ecrits en premier
  verifient donc ce qui SURVIT, pas ce qui part.

Deux clics, avec l'avertissement qui dit exactement ce qui est conserve. Aucun fichier n'est deplace
  ni supprime.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.37.0 (2026-09-09)

### Features

- **entretien**: Balayer les dossiers vides restes des rangements anterieurs
  ([`c025992`](https://github.com/gsoulat/sortilege/commit/c0259924475f1ac3c142030e0f283d78a2858cbf))

Le nettoyage a la volee ne rattrape que ce qu'il vient lui-meme de vider. Une bibliotheque
  constituee garde donc les carcasses de tout ce qui a ete range avant qu'il existe — deux cent
  soixante-sept dossiers dans le cas qui m'a ete montre — et le client de telechargement en cree de
  son cote : liens abandonnes, extractions ratees.

Le balayage se fait en DEUX temps, et ce n'est pas de la prudence de facade : un passage destructeur
  sur des centaines de dossiers ne doit pas partir du meme geste que celui qui sert a le regarder.
  On liste, on lit, puis on supprime.

Le parcours est REMONTANT, les feuilles avant les branches. Un dossier qui ne contient que des
  dossiers vides est vide lui aussi, et ne serait jamais vu autrement : « Serie/Serie S01E01/ »
  compte pour deux, et ne traiter que le niveau profond laisserait une carcasse par serie. L'ordre
  du resultat suit la profondeur, qui est celui dans lequel il faut supprimer.

Deux exclusions : les fichiers systeme du NAS ne rendent pas un dossier occupe — sinon aucun ne
  serait jamais considere vide — et une racine source n'est jamais proposee, la supprimer ferait
  echouer le scan suivant sur un dossier absent.

Aucun fichier n'est touche, a aucun moment.

7 tests, dont le parent qui devient vide par ses enfants et la racine epargnee.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.36.0 (2026-09-09)

### Features

- **interface**: Agrandir une jaquette au clic, avec trois facons d en sortir
  ([`02510e8`](https://github.com/gsoulat/sortilege/commit/02510e865e47555424352a468e69fc0ed9124b9e))

Une jaquette de trente pixels de large ne permet ni de distinguer deux saisons d'une meme serie, ni
  de lire un titre imprime sur l'affiche. C'est pourtant sur elle qu'on tranche entre deux oeuvres
  homonymes — c'est meme la raison pour laquelle elle est affichee.

Le clic agrandit donc : la jaquette d'une oeuvre, et les images extraites du fichier, qui gagnent le
  plus a l'agrandissement puisqu'on y cherche un detail de scene ou une image noire de fin de
  telechargement.

**Le zoom est SEPARE du choix dans le selecteur**, et ce n'est pas un detail de mise en page. Y
  cliquer retient l'oeuvre ; agrandir au meme endroit rendrait le geste imprevisible, et on
  validerait une identification en croyant seulement regarder l'affiche. Une loupe distincte
  apparait donc au survol.

Le dezoom compte autant que le zoom : une surimpression sans issue evidente est un piege. Echap est
  le reflexe, le clic a cote le geste naturel, et le bouton reste pour qui ne connait ni l'un ni
  l'autre. Le fond cesse de defiler pendant l'affichage — sinon on croit agir sur l'image et c'est
  la liste qui bouge.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.35.1 (2026-09-09)

### Bug Fixes

- **scan**: Recaler la file sur ce que le scan trouve, et vider le cache
  ([`c40783c`](https://github.com/gsoulat/sortilege/commit/c40783cc6d57b984ee0b1473a91560f9cb588b4d))

Un rescan ne remettait rien d'aplomb. Deux etats survivaient a leur objet.

**Les plans dont le fichier a disparu restaient dans la file.** Range, evacue, ou supprime a la main
  : le plan continuait de s'afficher, proposait un deplacement impossible, et echouait a
  l'application. La file ne se vidait donc jamais tout a fait, et les compteurs annoncaient un
  travail qui n'existait plus.

**Les chemins marques « deja planifies » ne l'etaient jamais moins.** Un telechargement refait apres
  un echec, revenu sous le meme nom, etait donc tenu pour deja traite et ne pouvait plus etre
  identifie — sans que rien ne le signale.

Les deux sont maintenant restreints a ce que le scan a REELLEMENT trouve. Ce qui est toujours la ne
  bouge pas, et c'est la propriete qui compte le plus : un arbitrage en attente sur un fichier
  present est du travail humain, et le perdre serait bien pire que de garder un plan perime.

**Le cache des vignettes se vide au scan.** Les cles incluent la taille et la date du fichier, donc
  une image perimee n'est jamais SERVIE — mais elle n'est jamais liberee non plus, et le cache ne
  faisait que croitre. Un scan est le moment ou l'on sait que la bibliotheque a bouge : c'est la que
  le menage a le plus de sens, et il ne coute rien puisque les images se reconstruisent a la
  demande. Un bouton permet aussi de le vider sans relancer un scan complet.

7 tests, dont ceux qui verifient ce qui ne doit PAS bouger.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.35.0 (2026-09-09)

### Features

- **revue**: Chercher une oeuvre par son titre quand rien ne convient
  ([`b509325`](https://github.com/gsoulat/sortilege/commit/b5093258a82b52350ba66616aa9ef19602b80588))

Les candidats proposes viennent du titre LU dans le nom de fichier. Quand ce nom est trop abime — un
  titre traduit, une abreviation, une faute du groupe de release — aucune proposition ne peut etre
  bonne. L'utilisateur voyait alors que c'etait faux sans aucun moyen de le corriger : le selecteur
  montrait huit mauvaises jaquettes et s'arretait la.

Un champ de recherche complete donc le selecteur, avec les memes jaquettes : c'est l'image qui
  tranche entre deux titres proches, pas la date.

Le point qui fait tenir l'ensemble est ailleurs que dans l'interface. Les resultats REJOIGNENT les
  alternatives du plan cote serveur, ce qui permet a « choisir » de rester inchange et surtout de
  conserver son invariant : on ne peut retenir qu'un candidat que le SERVEUR a lui-meme rapporte,
  jamais un identifiant fabrique par le navigateur. Sans cette jonction, on aurait ajoute une belle
  grille sur laquelle cliquer n'aurait rien fait.

Ils s'ajoutent sans remplacer, et sans doublon : une recherche decevante se quitte en revenant aux
  propositions d'origine.

Sans cle TheMovieDB, la recherche REFUSE au lieu de renvoyer une liste vide. Une liste vide laisse
  croire que la requete etait mauvaise, alors que la recherche n'a jamais eu lieu — on cherche alors
  l'erreur du mauvais cote.

9 tests, dont celui qui verifie qu'un identifiant invente reste refuse.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.34.0 (2026-09-09)

### Features

- Nettoyer les dossiers vides, mettre en cache les vignettes, paginer
  ([`56a75cc`](https://github.com/gsoulat/sortilege/commit/56a75cc6c00d3e36a972cf9805dd71f92de0fde1))

**Les dossiers de release restaient derriere.** Le nettoyage existait, mais enferme dans
  l'evacuation des restes — laquelle rend la main aussitot quand un plan n'en a aucun, ce qui est le
  cas courant : une release ne contient souvent que son .mkv. Le dossier vide survivait donc a
  chaque fichier range.

Il est sorti de la, et il remonte desormais plusieurs niveaux. Une release occupe souvent deux
  etages — « Serie/Serie S01E01 GROUPE/ » — et ne defaire que le second laissait une carcasse par
  episode.

Un garde-fou est ajoute au passage, qui manquait : la remontee s'arrete AVANT toute racine declaree.
  L'ancien code supprimait le dossier parent sans borne ; un fichier pose directement dans une
  source aurait donc fait disparaitre la source elle-meme, et le scan suivant aurait echoue sur un
  dossier absent.

**« Simuler » ne verifiait pas les droits.** Il annoncait « deplacement possible » sur des fichiers
  que l'execution refusait ensuite un par un — trois cents echecs decouverts apres coup, la ou un
  controle prealable les nommait d'avance. C'est pourtant exactement ce a quoi sert une simulation.

Les deux droits necessaires sont verifies, et aucun ne porte sur le fichier : ecrire dans le dossier
  qui le contient pour l'en retirer, et dans celui qui l'accueillera pour l'y poser. Plutot qu'un
  nouveau bouton, le controle rejoint celui qui existait deja pour cela.

**Les vignettes relancaient ffmpeg a chaque ouverture.** Elles sont mises en cache sur disque,
  indexees sur la taille ET la date du fichier : un encodage remplace sous le meme nom produit une
  cle differente, et l'ancienne image n'est jamais servie a sa place. Plafond a deux mille, les plus
  anciennes evincees — le volume de donnees contient aussi le journal d'annulation, qui lui est
  precieux.

Pas de cache navigateur en revanche : l'URL ne change pas quand le fichier change, il servirait donc
  une image perimee sans moyen de le savoir.

**La mediatheque se charge par paliers de deux cents.** Non pour le nombre d'oeuvres, mais pour ce
  que chacune traine — plans, saisons, doublons — envoye toutes les deux secondes y compris pour des
  lignes hors de l'ecran. Par paliers et non par pages : on parcourt une mediatheque en deroulant,
  et perdre les lignes precedentes obligerait a revenir en arriere pour comparer.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.33.2 (2026-09-09)

### Bug Fixes

- Cinq passes d analyse — perte de donnees, gel de l interface, file effacee
  ([`6889dc6`](https://github.com/gsoulat/sortilege/commit/6889dc61da3c1989b82e9a46d3a7de257a22c24e))

**Perte de donnees silencieuse.** `_move` promettait « sans jamais ecraser » et ecrasait. Son
  commentaire affirmait meme l'inverse de la verite : sous POSIX, os.rename REMPLACE la destination
  sans rien dire, et shutil.move aussi. La protection reposait donc entierement sur la vigilance de
  chaque appelant — et l'un l'avait oubliee : la mise en corbeille des doublons deplacait sans
  verifier. Deux mises en corbeille du meme chemin le meme jour effacaient le premier exemplaire,
  dans la fonction meme qui sert de filet.

Le refus est desormais APPLIQUE dans la primitive. Un garde-fou la protege aussi les appelants a
  venir, ce qu'une convention ne fait jamais.

**Interface gelee pendant les rangements.** `apply` deplacait des centaines de fichiers — parfois
  des gigaoctets d'un volume a l'autre — directement sur la boucle d'evenements. Toute l'application
  se figeait le temps du lot, suivi de progression compris. C'est une regression que j'ai introduite
  en rendant cet endpoint async pour prevenir le serveur multimedia : auparavant, FastAPI le placait
  lui-meme dans un thread. Idem pour l'analyse de conformite, qui parcourt toute la bibliotheque.

**File d'arbitrage effacee toutes les quinze minutes.** Le cycle automatique REMPLACAIT la file a
  chaque tour. Tout ce qui attendait une decision humaine disparaissait donc au tour suivant, sans
  un mot. Il ajoute maintenant, et marque les fichiers deja passes pour ne pas les repayer au
  fournisseur.

**Cycle automatique non borne.** Il planifiait tous les fichiers d'un seul tenant : sur une
  bibliotheque constituee, des heures d'appels pendant lesquelles rien n'est applicable. Il traite
  desormais un lot par tour, comme le manuel, et annonce ce qu'il laisse au suivant.

**Quarante megaoctets par minute pour rien.** La vue unifiee, interrogee toutes les deux secondes,
  joignait a chaque plan jusqu'a huit candidats alternatifs avec leur resume complet — des donnees
  qui ne servent qu'a l'ouverture du selecteur. Elles ne sont plus envoyees que pour ce qui demande
  un arbitrage.

Les deux autres passes n'ont rien donne, et c'est une information : le confinement des chemins tient
  (assainissement puis revalidation par resolve, ce qui couvre les liens symboliques), aucun secret
  ne redescend au navigateur, et chaque preference declaree est effectivement lue.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **reglages**: Decouper la page, et faire descendre le compteur « a traiter »
  ([`4e038d9`](https://github.com/gsoulat/sortilege/commit/4e038d9e26c12baf9d25accbc9a3a129a9fa6b3a))

Deux problemes rapportes ensemble.

**Le compteur ne descendait jamais.** « À traiter » restait fige a plusieurs milliers quoi qu'on
  range. Les fichiers deja planifies etaient reconnus a partir des plans VIVANTS ; or un plan
  applique quitte la file. Son fichier retombait donc dans « pas encore planifie », et le total ne
  bougeait pas — pire, il aurait ete repropose au calcul suivant alors qu'il venait d'etre range.

Le suivi passe donc par les chemins deja PASSES par le calcul, qui eux ne disparaissent pas avec le
  plan. Un fichier qui n'existe plus sur le disque est egalement ecarte : le scan est un instantane,
  et annoncer « en attente » un fichier deja parti promet un travail qui n'aura pas lieu.

**Les reglages tenaient sur une seule page.** Plus de trois cents lignes, ou la liste des
  identifications retenues — souvent plusieurs dizaines d'entrees — s'installait au milieu et
  enterrait tout ce qui suivait : notifications, corbeille, resolveur IA devenaient invisibles sans
  defiler longuement.

Quatre onglets, decoupes selon ce qu'on vient FAIRE et non selon la structure du code : ou vont mes
  fichiers, que fait l'outil tout seul, comment il identifie, et l'etat du deploiement. Un reglage
  se cherche par son intention.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.33.1 (2026-09-09)

### Bug Fixes

- **diagnostic**: Regarder le DOSSIER parent, pas le fichier
  ([`0ca9bbd`](https://github.com/gsoulat/sortilege/commit/0ca9bbd8e4a0e4c9c6470c69beb8b9f254c7e7de))

Le diagnostic ajoute juste avant designait le mauvais objet. Sous Unix, supprimer ou deplacer un
  fichier exige le droit d'ECRITURE SUR LE REPERTOIRE qui le contient — jamais sur le fichier
  lui-meme. Un fichier parfaitement accessible dans un dossier verrouille produit exactement «
  Permission denied », et regarder le fichier envoie chercher la ou il n'y a rien.

Le cas reel le montrait : JDownloader ecrit en USER_ID=1000, ce qui est deja l'identite par defaut
  de Sortilege. Conseiller d'aligner PUID aurait fait perdre du temps sans rien changer.

Le message donne donc maintenant le proprietaire ET le mode du dossier, puis adapte le conseil selon
  ce qu'il constate :

- identites differentes -> la ligne PUID/PGID a recopier ; - proprietaire sans droit d'ecriture ->
  corriger le mode du dossier ; - identites concordantes -> chercher ailleurs, et ou : ACL du
  partage, montage en lecture seule, volume different de celui qu'on croit.

Ce dernier cas est le plus utile. Un diagnostic qui repete « aligne PUID » alors que PUID est deja
  aligne ne fait pas qu'echouer a aider : il envoie dans la mauvaise direction avec assurance.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.33.0 (2026-09-09)

### Bug Fixes

- **docker**: Appliquer PUID/PGID au demarrage, comme Radarr le fait
  ([`b20ebb6`](https://github.com/gsoulat/sortilege/commit/b20ebb6784f7af003f25f8a2a9b8327b16834ab3))

« Pourquoi Radarr y arrive » etait la bonne question, et la reponse est un defaut de cette image.

L'identite etait un ARG, donc figee A LA CONSTRUCTION. L'image publiee sur GHCR est construite par
  la CI avec la valeur par defaut : quiconque la tire tourne en 1000:1000, quoi qu'il mette dans son
  .env. Si le client de telechargement ecrit sous une autre identite — et c'est le cas courant —
  Sortilege ne peut ni deplacer ni supprimer ses fichiers, et echoue en « [Errno 13] Permission
  denied ».

Le README promettait « UID/GID parametrables ». C'etait faux pour tout le monde sauf ceux qui
  reconstruisent l'image eux-memes. Encore un reglage qui ment, et celui-ci bloquait l'usage
  principal.

Radarr, Sonarr et leurs semblables ajustent l'identite AU DEMARRAGE. C'est ce que fait desormais
  l'entrypoint : le conteneur demarre root le temps de deux commandes, puis abandonne ces droits par
  gosu. Le compromis est assume — sans lui, PUID/PGID sont decoratifs.

Deux details qui evitent des ennuis : l'identite est donnee en NUMERIQUE, donc aucun compte n'a
  besoin d'exister et /etc/passwd n'est pas touche, ce qui permet de garder read_only ; et gosu
  plutot que su, parce qu'il transmet les signaux et qu'un conteneur doit pouvoir s'arreter
  proprement.

Verifie par construction reelle : PUID=1027 PGID=100 donne bien « Uid: 1027 Gid: 100 » sur le
  processus uvicorn, read_only compris, et l'application repond.

Le docker-compose passe aussi a l'image publiee plutot qu'a une construction locale : c'est ce que
  fait « docker compose pull », et les arguments de build n'y avaient de toute facon plus d'effet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Continuous Integration

- **release**: Retirer le rebase de rattrapage, qui aggravait les echecs
  ([`7854476`](https://github.com/gsoulat/sortilege/commit/7854476d184bebf9d3b6394b1e579a8232fd65a9))

Quatrieme cause distincte pour le meme symptome, et la derniere de la serie : le rebase de
  rattrapage entrait en conflit sur le CHANGELOG. C'est un fichier GENERE ; rejouer un commit genere
  par-dessus un autre commit genere ne peut pas bien se terminer.

Le rattrapage automatique est donc retire. Ce n'etait pas une bonne idee au depart, et les trois
  pannes precedentes en decoulaient toutes :

- il reecrivait le commit de version, ce qui orphelinait le tag pose dessus. La branche partait
  alors seule, --atomic n'y voyant qu'une seule ref a envoyer, et le job se declarait satisfait ; -
  il echouait faute d'identite git sur le runner ; - il echouait faute de droits sur .git apres le
  build Docker ; - il conflitait sur le CHANGELOG.

A chaque fois, il transformait un echec franc — rattrapable en une commande — en echec obscur, quand
  ce n'etait pas en faux succes.

Les tentatives ne servent plus qu'aux erreurs vraiment transitoires du serveur. Une branche qui a
  bouge sous le runner est un cas pour un humain : le message d'erreur donne desormais les trois
  commandes exactes, et nomme la cause la plus frequente — une poussee manuelle pendant la release.

Ce qui reste, et qui a fait ses preuves : l'image construite AVANT la poussee, donc jamais perdue ;
  les deux refs nommees explicitement et poussees atomiquement ; un echec de poussee qui fait
  echouer le job au lieu de le laisser vert.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- **diagnostic**: Dire quel UID possede le fichier quand l acces est refuse
  ([`99d0f66`](https://github.com/gsoulat/sortilege/commit/99d0f6632cac168d4d4bb908b2fd4d07184a408c))

Un « [Errno 13] Permission denied » nu oblige a partir en chasse : activer SSH, trouver la bonne
  commande, la lancer au bon endroit — pour recuperer deux nombres. Or ces deux nombres sont connus
  ici meme, a l'instant de l'echec.

Le message devient donc :

le fichier appartient a UID 1027:100, Sortilege tourne en UID 1000:1000. Mets PUID=1027 et PGID=100
  dans ton docker-compose.

Une enigme devient une ligne a recopier. C'est le pendant du correctif precedent : rendre PUID/PGID
  reellement effectifs ne sert a rien si l'on ne sait pas quelles valeurs y mettre.

Les trois points ou un refus peut survenir sont couverts : le rangement lui-meme, l'evacuation vers
  la corbeille, et la suppression directe.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **evacuation**: Suppression directe, et nommer la vraie cause des refus
  ([`9366c04`](https://github.com/gsoulat/sortilege/commit/9366c04e151bc23965171e1694a1eb32e5459db0))

Deux choses, dont la seconde compte bien plus que la premiere.

**La suppression directe, demandee.** Quand le fichier est verifie present a destination ET de meme
  taille, la source n'est pas un fichier : c'est un doublon strict de quelque chose qu'on vient de
  constater. Y ajouter un deplacement vers la corbeille puis une seconde corvee de vidage n'apporte
  rien.

Les deux garde-fous restent, et ce sont eux qui rendent l'operation defendable : rien n'est supprime
  si le fichier n'est pas reellement a destination, ni si les tailles different. Rien n'est
  journalise non plus, parce que rien ne serait annulable — l'ecrire serait moins honnete que de le
  dire. La corbeille reste le defaut, et le bouton de suppression demande un second clic.

**La vraie cause des trois cent trente-neuf refus.** Elle vient d'apparaitre dans les journaux et ce
  n'etait ni la corbeille ni la copie :

[Errno 13] Permission denied: '/storage/Download/JDownloader2/...'

Sortilege n'a pas le droit d'ecrire dans le dossier de telechargement. Les fichiers appartiennent au
  client qui les a crees, et aucun reglage de cette application ne peut le contourner : c'est une
  permission du NAS. La suppression directe echouerait d'ailleurs exactement pareil.

Le message d'aide le disait mal — il parlait du dossier de DESTINATION, alors que le cas frequent
  est la SOURCE. Il nomme desormais le bon dossier, explique pourquoi l'application ne peut rien y
  faire, et donne la commande de diagnostic.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.32.1 (2026-09-09)

### Bug Fixes

- **evacuation**: Deplacer au lieu de copier, et resoudre les exemplaires en trop
  ([`cb2f2a4`](https://github.com/gsoulat/sortilege/commit/cb2f2a4ec9619f02b35193cc03c1ae6cd34cfb70))

Trois cent soixante-deux fichiers de deux gigaoctets etaient RECOPIES au lieu d'etre renommes. La
  corbeille vivait sous la racine de bibliotheque alors que les telechargements sont un autre
  partage : os.rename echouait sur EXDEV et l'on retombait sur copie puis suppression. Plusieurs
  centaines de gigaoctets transferes pour ne rien produire, et une attente sans commune mesure avec
  l'operation demandee.

La corbeille est desormais choisie PAR FICHIER, sur le volume du fichier lui-meme. Ce n'est pas un
  detail de rangement : un deplacement a l'interieur d'un volume est un renommage, instantane quelle
  que soit la taille. La bibliotheque ne sert plus que de dernier recours — mieux vaut une copie
  lente qu'un refus.

Second point, lie : une source deja presente en corbeille etait refusee, donc elle revenait a chaque
  scan sans jamais se resoudre. C'est pourtant le cas le plus clair qui soit — un exemplaire en
  bibliotheque, un en corbeille, celui-ci en trop. Il est maintenant supprime.

C'est le seul endroit ou l'evacuation supprime, et il est doublement encadre : le fichier doit etre
  a destination avec la meme taille, ET la copie en corbeille doit elle aussi avoir cette taille. Un
  homonyme de taille differente n'est pas le meme fichier et reste refuse.

Les motifs de refus s'affichent enfin PENDANT l'operation. Sur trois cents fichiers, decouvrir a la
  fin que tout a ete refuse pour une seule raison fait perdre l'attente entiere.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Continuous Integration

- **release**: Rendre .git au runner apres le build Docker
  ([`0015c72`](https://github.com/gsoulat/sortilege/commit/0015c72a59f89cbe800c4db568d3f5b3bef23b9d))

Le rattrapage de poussee echouait sur :

error: insufficient permission for adding an object to repository database .git/objects

C'est une consequence directe d'un choix fait plus tot, et je le maintiens : le build Docker passe
  AVANT la poussee des refs, pour qu'un incident git ne coute pas l'image — le seul artefact
  reellement deployable. Mais ce build tourne en root et laisse derriere lui des objets lui
  appartenant dans .git, que le rebase suivant ne peut plus ecrire.

La propriete du repertoire est donc rendue au runner juste avant la poussee, plutot que de revenir a
  l'ordre precedent qui, lui, coutait l'image entiere a chaque alea.

Troisieme cause distincte pour un meme symptome apparent, ce qui vaut d'etre note : d'abord un tag
  pousse sans sa branche, puis une identite git absente, maintenant des droits perdus. Aucune
  n'etait l'erreur serveur intermittente soupconnee au depart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Refactoring

- Supprimer la file de revue et le code qu elle laissait derriere
  ([`be30005`](https://github.com/gsoulat/sortilege/commit/be30005d2306fbfee4c23aa3bda329021964eb34))

L'onglet « File de revue » n'avait plus de raison d'etre. Je l'avais garde apres la fusion en
  invoquant qu'une grille de jaquettes se parcourt mieux qu'une arborescence depliee ; le pretexte
  ne tenait pas. Il affichait les memes plans sous une autre forme, et deux ecrans qui montrent la
  meme chose obligent a se demander lequel fait foi.

Trois fonctions n'existaient que la et ont ete portees dans la vue unifiee avant sa suppression : le
  panneau d'annulation par oeuvre, le bouton « Simuler », et « Recommencer » qui repart du premier
  fichier.

La passe de code mort qui a suivi a trouve ce que ces suppressions avaient laisse derriere, et un
  peu plus :

- Huit routes exposees que plus rien n'appelait. Quatre etaient orphelines des anciennes vues ; les
  quatre autres — les etats de scan, d'indexation et de calcul, et la file elle-meme — restent des
  FONCTIONS, appelees dans le processus par /api/workspace qui les agrege. Seule leur exposition
  HTTP disparait. - `schemas.py` en entier, 114 lignes devenues sans lecteur. - `plan_one`, laissee
  derriere en scindant le rendu du plan pour permettre la seconde passe de franchise. - `_work_out`,
  `Provider`, `invalidate`, `has_declared_id`, `drop_blob` : helpers sans appelant. `Provider` etait
  une interface que rien ne verifiait, et qui decrivait deja mal un TMDBProvider devenu bien plus
  large. - `database_url`, declare et jamais lu — le meme defaut que TheTVDB, retire plus tot pour
  la meme raison : un reglage qui ment coute plus qu'une fonction absente.

Deux tests s'appuyaient sur des helpers retires. Ils n'ont pas ete supprimes mais reecrits, et ils
  testent mieux : la relecture des preferences passe desormais par un magasin NEUF, ce que fait un
  redemarrage, au lieu de vider un cache — c'est l'ecriture sur disque qui est ainsi prouvee, pas le
  cache.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.32.0 (2026-09-09)

### Features

- **revue**: Confirmer toute une serie d un seul bouton
  ([`55ccace`](https://github.com/gsoulat/sortilege/commit/55ccace5d264252c75234a4285f3b743df62ca81))

Quinze episodes de la meme serie demandaient quinze clics. Le bouton de ligne confirmait en realite
  deja toute la serie — mais rien ne le disait, et un bouton dont on ignore la portee est aussi
  inutilisable qu'un bouton absent : on ne sait pas ce qu'on vient de valider.

Les deux niveaux sont desormais explicites et l'ecrivent :

- « C'est bon pour les 15 », en tete du bloc d'arbitrage, pour toute l'oeuvre. - « C'est bon », sur
  une ligne, pour ce fichier seul.

La confirmation groupee passe par les IDENTIFIANTS des plans affiches, et non par une deduction a
  partir du titre. La deduction echoue des qu'une meme oeuvre a ete lue sous deux orthographes — cas
  frequent entre le titre d'une release et celui d'un fournisseur — alors que l'interface, elle,
  sait precisement ce qu'elle montre.

Le defaut cote serveur passe donc a « ce fichier seul ». Elargir la portee en silence est exactement
  le defaut qu'on cherche a eviter ; qui veut toute une serie le demande. « Ce n'est pas ca » garde
  son comportement inverse, et c'est volontaire : corriger une identification fausse doit valoir
  pour l'oeuvre entiere, sinon on repond douze fois a la meme question.

10 tests, portant sur la portee et sur ce qui ne doit PAS bouger : l'autre serie, la destination
  deja calculee, et les films homonymes qui restent deux oeuvres distinctes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.31.0 (2026-09-09)

### Features

- **apercu**: Des vignettes la ou le navigateur ne sait pas lire
  ([`1035bce`](https://github.com/gsoulat/sortilege/commit/1035bce29fee12855695f8121d4022330f0d85bb))

Le lecteur video livre juste avant ne repond pas a la question posee sur la plupart des fichiers :
  aucun navigateur courant ne lit le MKV, qui est le format majoritaire d'une bibliotheque
  constituee. Un lecteur noir sur neuf fichiers sur dix n'aide pas a decider si le fichier est le
  bon.

Le serveur annonce donc d'abord ce qu'il peut faire, avant que l'interface ne tente quoi que ce soit
  : lecture directe quand le conteneur s'y prete, sinon des images extraites par ffmpeg — qui marche
  sur tout ce qu'il sait ouvrir. Repondre avant d'extraire evite d'afficher cinq images cassees
  quand ffmpeg manque ou que le fichier est illisible.

Cinq images reparties sur toute la duree, la derniere a 90 %. Ce n'est pas un detail de mise en page
  : c'est la FIN qui trahit un telechargement interrompu, et une lecture du debut ne le montrerait
  jamais. Un episode qui s'arrete a la moitie se repere ici en une seconde.

Le positionnement se fait avec « -ss » AVANT « -i », donc sur les images cles sans decoder ce qui
  precede — sur un fichier de trois gigaoctets la difference est d'un ordre de grandeur. Un delai
  borne empeche un fichier pathologique de retenir un worker, et les chemins de ffmpeg et ffprobe
  sont resolus plutot que laisses au PATH du processus, qui n'est pas le notre.

Un transcodage a la volee aurait aussi marche. Il coute un ordre de grandeur de plus en processeur
  sur un NAS, pour repondre moins bien : cinq images montrent l'oeuvre ET son integrite d'un coup
  d'oeil, la ou une lecture demande de chercher.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.30.0 (2026-09-09)

### Chores

- Recuperer le commit de version v0.29.0 laisse par la CI
  ([`1b077d4`](https://github.com/gsoulat/sortilege/commit/1b077d4ab7441cb985c906899702b8ad44ee11fc))

La poussee du runner a envoye le TAG sans la branche : GitHub a accepte refs/tags/v0.29.0 puis
  refuse refs/heads/main. Le depot s'est retrouve avec un tag pointant hors de toute branche, et
  semantic-release, voyant « 0.29.0 deja publiee », a cesse de publier — en vert, ce qui est le pire
  des echecs.

Le commit de version est ramene dans l'historique. Le tag reste ou il est : son arbre correspond
  exactement a l'image 0.29.0 deja sur GHCR.

### Continuous Integration

- **release**: Donner une identite git au runner avant le rebase
  ([`2df86df`](https://github.com/gsoulat/sortilege/commit/2df86df0f7db5fd7765ee0b9a993ff0dab7196ae))

Trois releases d'affilee ont echoue a pousser leurs refs. Le motif soupconne — une erreur serveur
  intermittente — n'etait pas le bon. Le journal le dit clairement :

! [rejected] HEAD -> main (fetch first) Committer identity unknown fatal: empty ident name
  Everything up-to-date

Deux causes enchainees, toutes deux locales.

La poussee est d'abord refusee parce que la branche a bouge pendant la release — un commit pousse a
  la main alors que le job tournait. C'est normal et prevu : le rattrapage rejoue par-dessus.

Mais ce rattrapage echoue sur « empty ident name » : semantic-release configure une identite pour
  SON commit, pas pour le shell du job. Les trois tentatives deviennent alors inutiles, et pire, le
  rebase avorte laisse le depot dans un etat ou le push suivant annonce « Everything up-to-date »
  sans rien faire — un succes apparent sur une operation qui n'a rien pousse.

L'identite est donc posee avant toute chose, et un rebase impossible est proprement avorte plutot
  que laisse en plan. Le delai entre tentatives passe de quinze a cinq secondes : le probleme
  n'etant pas transitoire, attendre plus longtemps n'a jamais aide.

A noter pour la suite : --atomic, ajoute au commit precedent, a bien fait son travail. Ni tag ni
  branche ne sont partis, la ou la fois d'avant le tag etait parti seul et avait fige toutes les
  releases suivantes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- **corbeille**: Bouton de vidage definitif, et poussee atomique des refs
  ([`f6be944`](https://github.com/gsoulat/sortilege/commit/f6be9441b261ac3e46834665f1b8e65be7858f72))

Deux choses sans rapport, corrigees ensemble parce qu'elles bloquaient toutes deux la meme
  livraison.

**Le vidage.** Un detour par la corbeille du NAS (#Recycle) a ete envisage puis ecarte : il ne
  libere aucun espace non plus, il donne juste deux corbeilles a vider au lieu d'une. La suppression
  est donc reelle, et un bouton « Tout vider » supprime sans condition d'age.

Le plancher d'un jour est retire. Un plancher silencieux laisserait en place ce qu'on vient de
  demander de supprimer, sans le dire — le pire des comportements pour une action destructrice. La
  protection est deplacee la ou elle se voit : le serveur exige une confirmation explicite pour un
  vidage integral, et l'interface demande un second clic en annoncant le volume concerne.

**La poussee des refs devient atomique.** C'est la cause d'une panne silencieuse : GitHub a accepte
  refs/tags/v0.29.0 puis refuse refs/heads/main. Le depot s'est retrouve avec un tag pointant hors
  de toute branche, et semantic-release, voyant « 0.29.0 deja publiee », a cesse de publier — en
  VERT. Un run qui ne fait rien en annoncant un succes est bien pire qu'un run qui echoue : personne
  ne va le voir.

Avec --atomic, les deux refs partent ensemble ou aucune ne part. Le commit de version orphelin a par
  ailleurs ete ramene dans l'historique ; le tag reste ou il est, son arbre correspondant exactement
  a l'image 0.29.0 deja publiee.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **revue**: Bouton de confirmation, lecteur video, et diagnostic des echecs
  ([`0d689e2`](https://github.com/gsoulat/sortilege/commit/0d689e27fc36e0e376322e187cca75fc4bfec0db))

Trois manques dans la vue unifiee, dont un que j'y avais laisse en la creant.

**Il manquait le geste symetrique de « Ce n'est pas ca ».** On pouvait refuser une identification,
  jamais l'accepter. Or un plan a 78 % est tres souvent correct : le score dit l'incertitude de la
  MACHINE, pas celle de la personne qui regarde l'ecran. La seule facon d'accepter etait de cocher
  puis d'executer, ce qui melange deux decisions distinctes — « c'est la bonne oeuvre » et «
  range-le maintenant ». Le bouton « C'est bon » ne fait que la premiere, vaut pour toute la serie,
  et le choix est retenu pour que le scan suivant ne repose pas la question.

**Un lecteur video sur chaque ligne.** Un titre et une affiche ne disent pas si le fichier est le
  bon ; quelques secondes de video tranchent la ou aucun score ne le peut. Aucun chemin ne transite
  par le navigateur : on designe un PLAN, et le serveur sait seul quel fichier cela vise — servir un
  chemin fourni par le client donnerait la lecture de tout ce qui est monte dans le conteneur. Les
  requetes par plage sont gerees, sans quoi deplacer le curseur d'une video de trois gigaoctets
  imposerait de la telecharger depuis le debut. Le MKV n'etant lu nativement par aucun navigateur
  courant, l'interface le dit plutot que d'afficher un lecteur noir sans explication.

**Le diagnostic des echecs manquait dans la nouvelle vue.** Je l'avais bati dans l'ancienne et pas
  repris ici : « 340 en echec » s'affichait sans la cause ni le bouton d'evacuation, alors que ce
  sont precisement eux qui rendent le message exploitable. La cause dominante est desormais nommee
  dans la banniere, et l'encadre porte le compte, la marche a suivre et l'action.

10 tests sur la lecture, dont les requetes par plage et ce que l'endpoint refuse de servir.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.29.0 (2026-09-09)

### Bug Fixes

- **evacuation**: Suivre l avancement au lieu d un bouton fige
  ([`0b6a474`](https://github.com/gsoulat/sortilege/commit/0b6a47433698220ba52b5d976e0fd8144636e418))

La mise en corbeille traitait trois cents fichiers dans une seule requete synchrone, sans le moindre
  retour : le bouton affichait « Évacuation… » et plus rien ne bougeait. Impossible de savoir si
  l'outil travaillait, s'il etait bloque, ou s'il avait fini.

Pire que l'inconfort : une requete aussi longue finit par expirer cote navigateur, et le resultat
  est alors perdu alors que le serveur, lui, a termine son travail. C'est exactement le motif deja
  corrige pour le scan et le calcul des plans — il n'avait pas ete reproduit ici.

L'evacuation part donc en tache de fond et rend la main tout de suite. Une barre montre
  l'avancement, le nombre deja mis en corbeille, les refus et le fichier en cours. Les deplacements
  etant bloquants, ils tournent dans un thread : les laisser sur la boucle d'evenements figerait
  toute l'application pendant plusieurs minutes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- **entretien**: Rescan Jellyfin, vidage de la corbeille, remise en conformite
  ([`6ef593e`](https://github.com/gsoulat/sortilege/commit/6ef593e3bdea2586619c68a4343daca6c124d748))

Trois boucles laissees ouvertes, fermees ensemble.

**Le serveur multimedia n'apprenait jamais qu'un fichier venait d'arriver.** Un film range
  n'apparaissait dans Jellyfin qu'au prochain scan planifie, soit plusieurs heures. Un POST apres
  rangement le rend visible dans la minute.

C'est deliberement un appel et non une fusion. Forker Jellyfin couterait une reecriture complete (C#
  contre Python), un rebase perpetuel sur un projet dont les correctifs de securite comptent, et la
  perte de la separation qui fait qu'un bug de rangement n'empeche pas de regarder un film. L'appel
  apporte l'essentiel du benefice pour une fraction infime du cout.

L'adresse est restreinte au reseau local : le champ est saisi depuis le navigateur et appele par le
  SERVEUR, donc sans restriction il ferait du conteneur un relais de requetes. Les redirections ne
  sont pas suivies — elles meneraient hors du perimetre qu'on vient de verifier — et 169.254.0.0/16
  est ecarte : « prive » au sens de Python, mais c'est l'adresse des metadonnees d'instance, et
  aucun serveur multimedia n'y vit.

**La corbeille ne se vidait pas.** Sortilege ne supprimant jamais, elle grossissait indefiniment et
  l'espace qu'on croyait recuperer ne l'etait jamais. Elle s'inventorie maintenant lot par lot, et
  se vide au-dela d'un age choisi. Le delai EST la protection : c'est la fenetre pendant laquelle on
  peut encore s'apercevoir qu'un fichier a ete evacue a tort, et un delai nul ferait de la corbeille
  une suppression avec un detour. C'est le seul endroit de l'application qui supprime reellement ;
  un dossier au nom inattendu y est ignore plutot que supprime — s'il est la, ce n'est pas nous qui
  l'avons mis.

**Ce qui etait deja range ne suivait pas les changements de gabarit.** Le pipeline ignore
  deliberement les fichiers en bibliotheque, sans quoi chaque scan proposerait de deplacer X vers X.
  Un mode dedie compare l'existant au gabarit courant et propose ce qui differe — dans la file de
  revue, jamais directement : un renommage de masse sur une bibliotheque constituee ne doit pas
  partir d'un seul clic. Aucune identification n'est refaite, ce qui corrige la STRUCTURE et non
  l'identite : un fichier range sous « severance.s01e01 » ressort « severance - S01E01 », minuscule
  comprise. Seul le fournisseur connait la casse officielle, et on ne l'interroge pas — une mauvaise
  reponse renommerait des fichiers corrects.

26 tests, portant surtout sur ce que ces trois fonctions refusent de faire.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **mediatheque**: Filtres par type et reperage des fichiers trop lourds
  ([`072e63b`](https://github.com/gsoulat/sortilege/commit/072e63b6d87771ff8aa2771eeec3aa0964fe279c))

Savoir quoi re-telecharger ou re-encoder pour gagner de la place demandait d'ouvrir les dossiers un
  par un. La vue le dit maintenant elle-meme.

La mesure n'est pas la taille brute, qui ne dit rien : une serie de trente episodes pese forcement
  plus qu'un film, et un film de trois heures plus qu'un de quatre-vingt-dix minutes. Ce qui se
  compare, c'est le poids d'UN fichier, rapporte a l'habitude des oeuvres du MEME TYPE — comparer un
  episode a un film ferait passer tous les films pour des anomalies.

La reference est la MEDIANE, pas la moyenne. La moyenne serait tiree vers le haut par les quelques
  remux enormes qu'on cherche justement a reperer : le seuil se deplacerait avec eux et le plus gros
  fichier se cacherait lui-meme. En dessous de trois oeuvres d'un type, aucun verdict — une mediane
  sur deux valeurs ne dit rien, et signaler une anomalie sur cette base serait du bruit.

Une oeuvre a deux fois le poids habituel est signalee « ×2,1 le poids habituel », avec la
  comparaison chiffree en infobulle. Le filtre dedie les trie du plus lourd au plus leger, l'ordre
  utile quand on cherche de la place.

S'y ajoutent les filtres par type — films, series, animes — croisables avec les filtres d'etat, et
  le total occupe par la bibliotheque.

7 tests, dont la resistance de la mediane aux extremes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **mediatheque**: Une seule vue pour ce qu on possede et ce qui attend
  ([`0512bc6`](https://github.com/gsoulat/sortilege/commit/0512bc62bebf08233c78378aba8bfdff22a968f8))

« À ranger », « Ma collection » et « File de revue » montraient trois moities du meme objet.
  Repondre a « ou en est cette serie ? » demandait de passer par les trois, en recomposant
  mentalement ce que chacune ne montrait qu'a moitie.

« Ma medhiateque » les reunit : une ligne par oeuvre, portant a la fois ce qu'on possede — saisons,
  episodes manquants, doublons — et ce qui attend d'etre range. Le detail se deplie, et chaque
  oeuvre s'execute seule.

Deux partis pris :

**Rien n'attend la fin de rien.** L'etat est relu pendant qu'un travail tourne, et la liste se
  remplit. « Executer » agit sur ce qui est pret a cet instant, y compris pendant que
  l'identification continue. Sur six mille fichiers, attendre la fin d'un lot de cent avant de
  pouvoir agir n'etait pas tenable.

**Ce qui demande une action passe devant.** Le tri n'est pas alphabetique : il enterrerait les
  quelques lignes actionnables sous des centaines de lignes au repos, ce que la vue unique existe
  precisement pour eviter.

La file de revue reste un onglet a part. Sur un lot de plusieurs centaines de fichiers ambigus, une
  grille de jaquettes se parcourt mieux qu'une arborescence depliee — ce sont deux gestes
  differents, et les confondre aurait rendu l'arbitrage plus penible, pas moins.

Rien n'est perdu au passage : la mise en corbeille des doublons, seule action que « Ma collection »
  portait, est reprise telle quelle — a la corbeille, jamais a la suppression.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.28.0 (2026-09-09)

### Features

- **franchise**: Ranger les series d une meme saga sous un dossier commun
  ([`3c41e12`](https://github.com/gsoulat/sortilege/commit/3c41e12e3522039de6474c8e33775d20e53fe911))

Le jeton {collection} du prereglage Jellyfin promettait le regroupement par franchise depuis le
  debut. Rien ne l'alimentait pour les series : il n'etait renseigne que pour les films, via
  belongs_to_collection chez TMDB. Le module de deduction existait, complet et teste, et n'etait
  appele nulle part. Le dossier de franchise n'est donc jamais apparu.

TMDB ne declare aucun lien entre « Star Trek », « Star Trek: Discovery » et « Star Trek: Picard » :
  trois series sans rapport pour lui. La franchise se deduit du sous-titre, mais cette deduction ne
  peut pas se faire fichier par fichier — « Star Trek » tout court n'a rien d'une franchise tant
  qu'on ignore que « Star Trek: Discovery » existe a cote. D'ou une seconde passe, apres que tous
  les titres du lot sont resolus, qui ne redemande rien au reseau et ne change qu'une valeur du
  gabarit.

Deux corrections de fond dans la deduction :

**La serie fondatrice rejoint sa propre franchise.** « Star Trek » (1966) ne porte pas de sous-titre
  et ne revelait donc rien de lui-meme : il se retrouvait range A COTE du dossier portant son nom.
  Le regroupement laissait dehors ce qu'il regroupait.

**Un dossier de franchise a un seul element n'en est pas un.** La deduction n'est retenue que si une
  autre serie la partage — sinon « The Witcher: Blood Origin » creerait un niveau supplementaire
  pour une serie unique.

Les titres deja en bibliotheque comptent parmi les voisins : sans eux, importer « Star Trek: Picard
  » seul ne le rangerait pas avec les autres.

Le prereglage Plex gagne le meme jeton ; il ne l'avait pas, sans raison.

11 tests, dont le regroupement verifie au bout du pipeline et la garantie qu'un gabarit sans
  {collection} reste inchange.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.27.0 (2026-09-09)

### Features

- **mediatheque**: Evacuer les copies deja rangees, et socle de la vue unique
  ([`6ed9063`](https://github.com/gsoulat/sortilege/commit/6ed90634f0db4c4e0532430516f0b7455a20703b))

Trois cent quarante fichiers echouaient avec « la destination existe deja ». Ils avaient bien ete
  ranges lors d'un passage precedent ; seule une copie subsistait dans les telechargements, occupant
  la place et revenant a chaque scan.

La demande naturelle est « supprime l'ancien ». Ce n'est pas tout a fait ce qui est fait, et l'ecart
  est deliberé. Trois conditions doivent tenir :

1. Le fichier doit REELLEMENT etre a destination. Sans ce controle, on supprimerait une source qui
  n'existe nulle part ailleurs. 2. Les deux doivent avoir la MEME TAILLE. Deux encodages d'un meme
  episode visent le meme nom sans etre le meme fichier ; les confondre ferait perdre un exemplaire
  distinct. Un doute sur l'identite se tranche par un refus, pas par un pari. 3. La copie part en
  CORBEILLE et l'operation est journalisee, donc annulable. Une suppression n'est jamais rattrapable
  — c'est exactement ce qu'un outil de rangement ne doit pas se permettre.

Deux changements de fond viennent avec, qui preparent la vue unique :

**Les plans apparaissent au fil du calcul** au lieu d'attendre la fin d'un lot de cent. Sur un
  millier de fichiers, c'etaient plusieurs minutes d'ecran vide alors que les premiers resultats
  etaient exploitables depuis longtemps.

**L'identifiant d'un plan derive de sa source** au lieu d'etre tire au hasard. C'est ce qui rend la
  publication progressive possible : la seconde passe IA remplace un plan par un meilleur, et sans
  identifiant stable elle en aurait cree un second — deux lignes pour un fichier, et deux tentatives
  de le deplacer. Effet de bord bienvenu : une selection cochee survit a un recalcul.

Enfin le socle de la vue unique : un module qui rapproche les deux etats d'une meme oeuvre — ce
  qu'on possede et ce qui attend — et l'endpoint qui l'expose. Le point delicat est le rapprochement
  : une meme serie s'ecrit « Avatar Le dernier maitre de l'air » sur le disque, « Avatar The Last
  Airbender » dans la release, et « Avatar : Le dernier maitre de l'air » chez le fournisseur. Trois
  ecritures qui doivent tomber sur une seule ligne, sans quoi la vue unique serait pire que les
  trois ecrans qu'elle remplace.

26 tests de plus.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.26.0 (2026-09-09)

### Features

- **annulation**: Annuler une seule oeuvre au lieu de toute la session
  ([`a2f817b`](https://github.com/gsoulat/sortilege/commit/a2f817b1cf0810a7d986ba1a54bec25301e09b7b))

« Tout annuler (703) » etait la seule option offerte. Or le besoin reel est l'inverse : une serie
  mal identifiee au milieu de sept cents deplacements corrects. Defaire les sept cents pour corriger
  douze fichiers n'est pas une annulation, c'est une punition — et personne ne clique sur ce bouton,
  donc l'erreur reste.

Le journal retient desormais l'oeuvre concernee au moment du rangement. La file d'annulation se lit
  groupee par oeuvre, avec un bouton par ligne. Les entrees anterieures a ce champ restent
  annulables : faute de titre, l'oeuvre est deduite du dossier de destination — une deduction, d'ou
  la preference donnee au titre des qu'il existe.

Deux points qui decident si c'est utilisable :

**Ce qui n'etait pas vise ne bouge pas.** Une annulation qui deborde sur les voisins est pire que
  pas d'annulation : elle deplace des fichiers dont le rangement avait ete valide.

**Le journal reste ordonne apres coup.** Les operations annulees peuvent se trouver n'importe ou
  dedans, pas seulement a la fin ; sans remise en ordre, « annuler les N dernieres » deferait
  ensuite autre chose que les dernieres.

Annuler un episode ramene aussi ses sous-titres : un retour arriere a moitie fait laisserait la
  video sans ses compagnons, exactement la perte qu'on cherche a eviter.

14 tests.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.25.0 (2026-09-08)

### Documentation

- Corriger la section securite, qui decrivait un verrou supprime
  ([`c8a1e5a`](https://github.com/gsoulat/sortilege/commit/c8a1e5af25dc67be1026987196c9d00e5d2b1328))

Le README annoncait encore SORTILEGE_DRY_RUN comme mode par defaut. Cette variable a ete retiree il
  y a plusieurs versions au profit des deux boutons « Simuler » et « Executer » : la documentation
  promettait donc un garde-fou qui n'existe plus, ce qui est pire que de n'en promettre aucun.

Il affirmait aussi que les cles vivent uniquement dans l'environnement. C'est faux depuis que le
  fournisseur d'IA et le webhook Discord se reglent depuis l'interface. Ce qui compte vraiment est
  ailleurs et n'etait pas dit : aucun secret ne redescend au navigateur, le jeton TMDB ne passe pas
  en parametre d'URL, et les doublons partent a la corbeille et jamais a la suppression.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- **persistance**: Reprendre le scan et les plans apres un redemarrage
  ([`6d5f721`](https://github.com/gsoulat/sortilege/commit/6d5f72126103d19e167ce85543d2f42759e15f34))

Scan et plans ne vivaient qu'en memoire. Un redemarrage du conteneur — donc chaque mise a jour
  d'image — remettait tout a zero : plusieurs minutes de disque a rouvrir mille conteneurs avec
  ffprobe, puis des centaines d'appels a TMDB, pour reconstruire exactement ce qu'on venait de
  perdre. Sur une bibliotheque reelle cela suffit a rendre l'outil penible.

Les deux etats sont maintenant ecrits dans la base SQLite qui vit deja dans le volume monte, et
  relus au demarrage. L'avancement du traitement par lots en fait partie : sans les chemins deja
  planifies, « Traiter les 100 suivants » repartirait du premier lot au lieu d'avancer.

Deux precautions structurent le module :

**Un instantane est une vue, pas une verite.** Entre l'enregistrement et la relecture, un fichier a
  pu etre deplace ou supprime par autre chose. Rien n'est tenu pour acquis : un plan relu est
  verifie a l'application comme n'importe quel plan frais. La reprise economise du calcul, elle ne
  court-circuite aucun controle.

**Un format qui ne se relit pas se jette.** Le schema porte un numero ; toute divergence — version
  anterieure, champ disparu, decision inconnue — ramene a « pas d'instantane ». Perdre une reprise
  est un desagrement, ne plus demarrer est une panne.

L'encodage est ecrit a la main plutot que derive des dataclasses. C'est volontaire : un champ oublie
  par un encodage automatique ne leverait rien, il reviendrait a sa valeur par defaut et le plan
  repris serait subtilement faux — un fichier deplace au mauvais endroit sans que rien ne l'ait
  signale. Les tests verifient donc l'aller-retour de ce qui coute cher : la sonde ffprobe, les
  sous-titres accompagnants, les candidats alternatifs, et le fait qu'un choix humain reste un choix
  humain.

25 tests, dont la reprise reelle a travers la base.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.24.0 (2026-09-08)

### Bug Fixes

- **revue**: Rendre un lot executable quand rien n atteint le seuil auto
  ([`93d82fa`](https://github.com/gsoulat/sortilege/commit/93d82fa6355610ee5681ba11c745b69b762fe87c))

Un lot dont aucun fichier ne depasse le seuil automatique etait impossible a traiter : la section
  d'arbitrage s'ouvrait repliee, aucune case n'etait donc atteignable, aucune selection ne pouvait
  se former, et les boutons « Simuler » / « Executer » — conditionnes a une selection non vide — ne
  s'affichaient jamais. Le lot restait a l'ecran, complet et inerte.

Le repli avait un sens — ne pas noyer deux clics surs sous trois cents lignes a arbitrer — mais il
  etait fige a l'initialisation alors que sa raison d'etre depend de la file. Il se reevalue
  maintenant a chaque chargement : replie tant qu'il reste des plans surs, ouvert quand il n'y a
  plus qu'a arbitrer.

Deux autres corrections dans la meme zone :

Le bouton d'application automatique envoyait « tout ce qui est automatique » plutot que la liste
  affichee. Sous filtre « < 80 % » il annoncait douze plans et le serveur en aurait deplace
  quarante. Il envoie desormais les identifiants exacts de ce qu'il montre — un bouton qui ment sur
  ce qu'il deplace est plus dangereux qu'un bouton absent.

Et « Tout cocher » sur l'arbitrage visible : cent lignes a cocher une par une n'est pas une
  interface, c'est un obstacle.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Continuous Integration

- **release**: Ne plus perdre l image quand GitHub refuse la poussee
  ([`a1f1ff8`](https://github.com/gsoulat/sortilege/commit/a1f1ff835b724769d044fb6546b27928ef12ad85))

Trois releases de suite ont echoue sur « remote: fatal error in commit_refs » au moment ou le runner
  poussait le commit de version et son tag. L'erreur est cote serveur — les memes poussees depuis un
  poste passent sans probleme — mais elle emportait tout ce qui suivait, y compris la construction
  de l'image. Or c'est l'image qu'on deploie ; le tag n'est qu'une trace.

L'ordre est donc inverse. semantic-release calcule la version, modifie les fichiers, commit et tag
  EN LOCAL sans rien pousser. L'image se construit ensuite depuis cet arbre de travail, deja bumpe,
  et part sur GHCR. La poussee des refs vient en dernier, avec trois tentatives espacees et un
  rebase entre chaque au cas ou un commit serait arrive entre-temps.

Si la poussee finit par echouer malgre tout, le job termine en erreur — avec la commande exacte a
  rejouer depuis un poste. Un vert sur un depot sans tag serait pire que l'echec : la meme version
  se recalculerait au commit suivant sans qu'on comprenne pourquoi elle n'a pas bouge.

Ce que ca change concretement : un alea GitHub coute desormais une commande de rattrapage, la ou il
  coutait un build multi-architecture complet et une release a moitie faite.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- **episodes**: Numerotation absolue des animes et fichiers multi-episodes
  ([`95274a7`](https://github.com/gsoulat/sortilege/commit/95274a7028761b4925099d322650ef1bd31ab102))

Deux facons pour un episode d'etre mal range, toutes deux silencieuses.

La numerotation absolue d'abord. Les releases de fansub comptent en continu — « One Piece - 1088 » —
  la ou Jellyfin attend S21E13. Sans conversion, l'anime se range hors saison et le lecteur ne sait
  plus le relier a rien. TMDB donne les effectifs par saison en une requete : il suffit de cumuler.
  Au-dela du total connu, rien n'est affirme — une serie en cours a toujours plus d'episodes
  diffuses que le catalogue n'en connait, et un dossier fantome se rattrape moins bien qu'un fichier
  laisse en revue.

Les fichiers doubles ensuite. « S01E01E02 » etait lu comme « S01E01 » : le motif simple gagnait, et
  le second episode disparaissait sans un mot. Trois consequences a la fois — E02 porte manquant
  alors qu'il est sur le disque, meme nom de destination que le fichier simple donc ecrasement
  possible, et les deux declares doublons l'un de l'autre. Le motif de plage passe donc avant le
  motif simple, et la plage se propage jusqu'au nom rendu.

Le conditionnel de gabarit accepte desormais le meme zero-padding que le jeton simple
  ({?episode_end:02:-E$}) : « E01-E2 » a cote de « E01 » aurait ete incoherent, et c'est le nom de
  fichier qui compte pour le lecteur.

29 tests, dont les cas limites qui font tout le travail : saison 0 exclue de la numerotation, saison
  vide traversee, plage inversee ignoree, et le cout verifie a une seule requete pour trente
  episodes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **notifications**: Compte rendu Discord des cycles automatiques
  ([`ba9ab8e`](https://github.com/gsoulat/sortilege/commit/ba9ab8efef0ce31a9df87567e728f3955734f691))

Le rangement automatique tourne sans personne devant l'ecran, et le seul moment ou l'on ouvre
  l'interface c'est quand on soupconne deja un probleme — donc trop tard. Un webhook Discord inverse
  ce rapport.

Trois choix de conception, tous discutables et tous assumes :

**Le silence par defaut.** Un cycle qui n'a rien trouve n'envoie rien. Un message toutes les quinze
  minutes disant « 0 fichier range » apprend a ignorer le canal, et le jour ou un echec arrive il
  passe inapercu. On ne signale que ce qui s'est passe : des fichiers detectes, ranges, ou mis en
  attente d'arbitrage.

**Une notification ne casse jamais un cycle.** Discord injoignable, webhook revoque, reseau coupe :
  tout est journalise et avale. Le rangement compte plus que son compte rendu. Le seul endroit qui
  rapporte un echec est le bouton d'essai, ou l'utilisateur attend justement une reponse.

**L'URL est contrainte aux domaines Discord.** Elle est saisie depuis le navigateur et c'est le
  SERVEUR qui va la chercher : sans restriction d'hote, ce champ ferait du conteneur un relais
  capable d'emettre vers n'importe quelle adresse du reseau domestique — le NAS, le routeur, une
  interface d'administration. La verification a lieu avant la requete, pas apres. Comme toute saisie
  humaine, une URL refusee est refusee avec son motif et l'hote recu, jamais corrigee en silence.

L'URL n'est pas renvoyee au navigateur : elle vaut un droit d'ecriture sur le salon, et la reponse
  finit dans le cache. Meme traitement que les cles d'API — champ en ecriture seule, booleen «
  configure » en lecture.

25 tests, dont la liste des adresses qu'on refuse d'appeler.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Refactoring

- **reglages**: Retirer TheTVDB, qui ne servait a rien
  ([`ed00616`](https://github.com/gsoulat/sortilege/commit/ed00616432468b26d7ad65b836bbcbc2937e5c98))

L'interface annoncait TheTVDB comme source secondaire, avec une case « configure » qui s'allumait
  des qu'une cle etait posee dans TVDB_API_KEY. Aucune ligne de code n'a jamais interroge ce
  fournisseur : la cle etait lue, affichee, et ignoree.

Un reglage qui ment coute plus qu'une fonction absente. Il fait chercher une cle, la souscrire —
  TVDB v4 est payant — et surtout il fait croire qu'un mauvais appariement de serie vient d'un
  fournisseur mal configure alors qu'il vient d'ailleurs.

TMDB couvre les series et sait desormais traduire la numerotation absolue des animes, ce pour quoi
  TheTVDB avait ete envisage. La case est donc retiree plutot que remplie.

Ce qui reste : la lecture d'un `tvdbid` dans un .nfo. C'est un signal d'identite reel, produit par
  un autre outil, et il continue de compter dans le score.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.23.0 (2026-09-09)

### Features

- **memoire**: Retenir les identifications tranchees a la main
  ([`10e9a83`](https://github.com/gsoulat/sortilege/commit/10e9a83f5e4c590b1a4d2ddd26e6d95f30eb7972))

Points 1 et 2 de la liste. Une base SQLite dans le volume monte remplace la memoire volatile pour ce
  qui compte vraiment.

Quand tu tranches entre deux « Dark Matter », le choix est RETENU. Au scan suivant, le pipeline le
  retrouve avant meme d interroger les fournisseurs : ni recherche, ni score, ni arbitrage. C est ce
  qui fait converger une file de revue vers le vide au lieu de la voir se remplir a l identique a
  chaque fois.

La cle de rappel est le titre LU par le parseur, insensible a la casse et aux accents — pas le titre
  officiel. C est celui-la qu on reverra au prochain scan, et c est sur lui qu on doit reconnaitre
  la situation deja tranchee. Une cle trop stricte ne retrouverait jamais rien et la memoire ne
  servirait a rien.

Un nouveau choix REMPLACE l ancien : une erreur de clic doit pouvoir se corriger, sinon elle
  resterait gravee sans recours. Un bouton « Oublier » remet aussi la question en jeu.

SQLite par le module standard, sans ORM : deux tables et des requetes triviales — une couche d
  abstraction couterait une dependance et des migrations pour ne rien simplifier.

Une panne d ecriture est journalisee et avalee : perdre la reprise apres redemarrage est genant,
  interrompre un scan en cours pour cette raison le serait bien plus.

20 tests, portant surtout sur la cle de rappel et sur la survie a une reouverture de la base.

chore(release): v0.23.0 [skip ci]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.22.0 (2026-09-09)

### Testing

- Couvrir les chemins risques laisses de cote
  ([`5dbe8f2`](https://github.com/gsoulat/sortilege/commit/5dbe8f29b85507ff9070f4fab2489a49cb7a732e))

La mesure de couverture a montre un motif net : le coeur metier etait a 84-98 % et les chemins les
  plus DANGEREUX a 0-42 %. J avais teste ce qui etait facile a tester — des fonctions pures — et
  laisse ce qui demandait un montage. C est l inverse de l ordre du risque : une fonction pure qui
  casse donne un mauvais resultat, run_cycle qui casse deplace mille fichiers au mauvais endroit.

Clients IA (0 % -> 86 %) ----------------------- Le code qui parle a sept services n avait jamais
  ete execute par un test : mes tests de la seconde passe utilisaient un faux resolveur, donc ils
  verifiaient la place donnee au modele, pas le client qui l appelle. Transport simule au niveau
  HTTP — forme de la requete, en-tetes selon le fournisseur, reprise sans response_format,
  absorption des pannes.

Un vrai bug trouve : l extracteur JSON ne cherchait que « { … } », donc le repli sur une liste nue
  que je documentais n a jamais fonctionne — il attrapait le premier element au lieu de la liste.
  Corrige.

Garde-fou de chemin (non teste -> 12 cas) -----------------------------------------
  _resolve_in_library decide si un chemin venu du NAVIGATEUR peut designer un fichier a mettre en
  corbeille. C etait le seul controle de securite du projet sans test, et le plus expose. Remontees,
  chemins absolus, separateurs Windows, chemin vide : tous refuses.

Cycle automatique (41 % -> 72 %) -------------------------------- Le seul chemin ou des fichiers
  bougent sans personne devant. Ce qui est verifie est surtout ce qu il REFUSE : ne rien traiter au
  premier passage, ne rien deplacer quand apply_auto est desactive, et ne jamais toucher a ce qui
  attend un arbitrage.

chore(release): v0.22.0 [skip ci]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.21.0 (2026-09-09)

### Features

- **ui**: Afficher le numero de version dans l entete
  ([`2bf8559`](https://github.com/gsoulat/sortilege/commit/2bf85597b5883ccaa08d763102fae343413a4930))

Utile des qu on deploie souvent : savoir d un coup d oeil quelle version tourne reellement, plutot
  que de croire que le « Re-pull image » a fait son travail.

Corrige au passage une version codee en dur dans main.py, restee a 0.1.0 depuis le premier commit.
  Elle vient desormais de __init__.py, seule source de verite, et alimente a la fois FastAPI et
  /api/health.

Un test verifie que pyproject.toml et __init__.py concordent. Les deux etant mis a jour a la main
  depuis que la release automatique est hors service, ils divergeraient un jour en silence.

/api/health est volontairement accessible sans session : l entete affiche la version avant meme la
  connexion.

chore(release): v0.21.0 [skip ci]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.20.0 (2026-09-09)

### Features

- **revue**: Traiter d abord ce qui est reconnu automatiquement
  ([`e073ad8`](https://github.com/gsoulat/sortilege/commit/e073ad81c2ce24473b82c80dfcde5b50c429c510))

Toutes les listes sont triees par score DECROISSANT — l ordre du scan n a aucune valeur pour l
  arbitrage, alors que la confiance en a une : ce dont l outil est le plus sur se traite en premier,
  et souvent en un seul clic.

Le groupe « en attente d arbitrage » est desormais REPLIE par defaut. Sur un lot de cent fichiers,
  quelques dizaines de lignes a arbitrer noyaient les deux boutons qui traitaient l essentiel. Ce
  qui demande du travail ne doit pas masquer ce qui n en demande pas.

Les series du groupe d arbitrage sont classees par leur meilleur score, et les episodes a l
  interieur de chaque serie aussi.

Corrige au passage un v-if pose sur le meme element qu un v-for : cela fonctionnait parce que la
  condition n utilisait pas la variable de boucle, mais se serait casse silencieusement au premier
  changement.

chore(release): v0.20.0 [skip ci]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.19.0 (2026-09-09)

### Features

- **revue**: Planification par lots de 100, en tache de fond, avec filtres de score
  ([`8e98217`](https://github.com/gsoulat/sortilege/commit/8e982178a449fea74586002bd899cb8609c46b03))

Le calcul a echoue sur 1051 fichiers. Trois defauts distincts, dont deux que le lot seul n aurait
  pas regles.

Par lots successifs ------------------- Cent fichiers sont planifies, puis un bouton demande les
  cent suivants. Tout calculer d un coup produisait une file que personne ne relira, et faisait
  attendre de longues minutes avant le premier resultat exploitable.

Les lots precedents restent dans la file : le nouveau lot s AJOUTE au lieu de remplacer, sinon le
  travail d arbitrage deja fait serait perdu a chaque clic.

Le suivi se fait par CHEMIN et non par position : appliquer des plans retire des fichiers du scan,
  et un compteur designerait ensuite les mauvais.

En tache de fond ---------------- Le POST rendait la main seulement une fois tout calcule. Meme un
  lot peut depasser le delai d attente d un navigateur ou d un proxy : le serveur terminait son
  travail et plus personne n etait la pour le recevoir. Meme defaut que le scan avant sa correction,
  laisse ici par inadvertance.

La reference a la tache de fond est conservee : asyncio ne garde qu une reference FAIBLE vers les
  taches en cours, et le ramasse-miettes pouvait donc supprimer le calcul en plein vol, sans erreur
  ni trace. Signale par ruff.

Filtres de score ---------------- « Tout », « au moins 80 % », « moins de 80 % ». C est un seuil de
  LECTURE et non de decision : il ne change rien au traitement, il choisit ce qu on regarde. 80 %
  correspond au seuil de sollicitation de l IA, donc a la frontiere entre « l outil a su » et « il a
  hesite ».

La planification interne reste decoupee en lots de 100 : le semaphore ne laisse tourner que six
  taches, en creer mille immobilise de la memoire pour rien.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.18.0 (2026-09-09)

### Bug Fixes

- **ci**: Rendre la release rattrapable quand la poussee du tag echoue
  ([`14f35e3`](https://github.com/gsoulat/sortilege/commit/14f35e36e4880e717f03de05d9e9433706ade826))

GitHub a rejete « git push tag v0.17.0 » avec « remote: fatal error in commit_refs » — une erreur
  serveur, sans rapport avec le contenu. Le probleme n est pas l echec lui-meme mais l ETAT qu il
  laisse : le commit de version etait deja parti, le tag non. La release etait a moitie faite.

Relancer le workflow ne pouvait pas s en sortir : il repartait du commit d origine, recreait un
  commit de version, et se heurtait a un « main » distant deja en avance. Un echec transitoire
  devenait donc definitif.

Ajout d un declencheur manuel qui reconstruit et publie l image du dernier tag sans retoucher aux
  versions. Une variable unique decide de la suite, que l on vienne d un push ou d un rattrapage —
  sans elle, chaque etape aurait a connaitre les deux cas.

L etape de publication de la release GitHub reste liee au declenchement REEL : lors d un rattrapage
  la release existe deja, et son tag n est pas dans les sorties d une etape qui n a pas tourne.

Le tag v0.17.0 manquant a ete pose a la main sur son commit de version — la meme poussee a
  fonctionne du premier coup, confirmant le caractere transitoire.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.17.0 (2026-09-08)

### Features

- **collection**: Grille de la bibliotheque, episodes manquants, doublons
  ([`6efd396`](https://github.com/gsoulat/sortilege/commit/6efd3962f034f6a6b8c671a2ae5bd418f8796cf1))

Trois questions, une seule passe de regroupement — elles portent toutes sur la meme structure «
  quelles oeuvres, quels episodes, quels fichiers ».

Ma collection ------------- Une grille d affiches. Une bibliotheque de 400 fichiers ne se lit pas en
  liste ; en grille elle se parcourt d un coup d oeil. Les oeuvres incompletes et celles en double
  portent un marqueur, ce qui evite d avoir a chercher.

Episodes manquants ------------------ Compare ce que le disque a avec ce que la saison compte chez
  TheMovieDB.

Les episodes NON ENCORE DIFFUSES sont exclus — sans cela, toute serie en cours afficherait des trous
  impossibles a combler et la liste cesserait d etre lisible, donc utile. Une saison dont le
  fournisseur ne sait rien n affiche aucun manque : ne rien savoir n est pas manquer.

Doublons -------- Deux fichiers pour le meme episode ou le meme film. Le fichier a garder est
  designe par la RESOLUTION d abord, la taille ensuite : un 2160p compresse vaut mieux qu un 1080p
  volumineux.

Deux fichiers sans numero d episode ne sont jamais declares doublons — rien ne prouve qu ils
  occupent la meme place, et un faux positif couterait un fichier mis en corbeille sans raison.

Le bouton MET EN CORBEILLE, il ne supprime pas. Meme mecanisme que les restes de release : le
  fichier reste sur le disque, l operation est journalisee, et l annulation la defait. Supprimer 40
  Go sur une erreur de detection serait irreparable ; le deplacer coute un dossier a vider quand on
  a verifie.

Les chemins envoyes par le client sont resolus sous la racine et verifies, jamais utilises tels
  quels.

Sans cle TheMovieDB, la collection et les doublons restent consultables : seuls les manques en
  dependent. Une fonction en moins plutot qu un ecran vide.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.16.0 (2026-09-08)

### Features

- **destinations**: Dossier separe pour les gros fichiers, et gabarits relatifs
  ([`527dc78`](https://github.com/gsoulat/sortilege/commit/527dc78b1d12863fbdbf68fea32a392385d63275))

Correction prealable, plus importante que la fonctionnalite
  ---------------------------------------------------------- Les gabarits commencaient par « Films/
  », « Series/ », alors que la destination par type est deja reglable dans l interface. Les deux se
  superposaient : passer la destination de « Series » a « Series TV » n avait AUCUN effet, le
  gabarit imposant son prefixe. Le reglage existait et ne servait a rien.

Les gabarits sont desormais relatifs a la destination du type. Une seule source de verite pour le
  premier niveau d arborescence.

Dossier des fichiers volumineux ------------------------------- Au-dela d un seuil reglable, un
  fichier part dans une destination distincte — souvent sur un autre volume, ou simplement isole
  pour etre repere. Un remux 4K de 60 Go et un episode de 800 Mo n ont pas les memes contraintes.

La resolution de destination est INJECTEE dans le pipeline plutot que codee dedans : il n a pas a
  connaitre les preferences, et le routage par taille devient testable sans configuration.

Un test a rattrape une faute au passage : j avais fusionne les deux tables de destinations par cle
  pour les valider, ce qui faisait ecraser une destination par son homologue « volumineux ». La
  premiere echappait alors silencieusement a la validation de confinement. Les deux tables sont
  desormais parcourues separement.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.15.0 (2026-09-08)

### Features

- **auto**: Surveillance, chaine complete, affiches et correction par serie
  ([`93ccc1b`](https://github.com/gsoulat/sortilege/commit/93ccc1b37cc23fa41a314cebfe39febd95aee98c))

Surveillance et traitement automatique ------------------------------------- Une boucle detecte les
  nouveaux fichiers, scanne, identifie et remplit la file. Elle enchaine les MEMES etapes que les
  boutons : aucun chemin parallele, donc aucun risque que l automatique et le manuel divergent.

Le piege d une surveillance de dossier n est pas de reperer un fichier nouveau mais de savoir qu il
  est FINI — un fichier existe des le premier octet ecrit. Deux conditions cumulatives : la taille n
  a pas bouge entre deux observations, et la derniere modification remonte a plus de N secondes. La
  premiere detecte une extraction d archive en cours, pendant laquelle le fichier porte deja son nom
  definitif ; la seconde rattrape une ecriture lente qui donnerait deux mesures identiques par
  hasard.

Attendre trop coute un cycle. Traiter trop tot coute un fichier incomplet deplace et un journal d
  annulation qui pointe vers du vide.

Le deplacement reel reste OPT-IN : par defaut la boucle prepare la file et s arrete la. Ranger sans
  personne devant est un engagement plus lourd qu identifier. Un cycle ne chevauche jamais le
  precedent, et les preferences sont relues a chaque tour pour qu activer la surveillance ne demande
  pas de redemarrage.

Correction par serie -------------------- Choisir la bonne oeuvre corrige desormais TOUS les
  episodes du groupe. L identification porte sur l oeuvre, pas sur le fichier — corriger episode par
  episode revenait a repondre douze fois a la meme question. Chaque episode recoit une copie du
  candidat, sinon l enrichissement ecraserait le titre d episode des precedents.

Affiches dans la file --------------------- La vignette de l oeuvre retenue apparait sur chaque
  ligne. On reconnait une erreur d identification bien plus vite sur une image que sur un titre.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.14.0 (2026-09-08)

### Features

- **ia**: Choix du fournisseur, et progression du calcul des plans
  ([`9096198`](https://github.com/gsoulat/sortilege/commit/90961983ad2b21a211cd4bb16abb133f181ba082))

Fournisseurs ------------ ChatGPT, Mistral, OpenRouter, Ollama, Groq, LM Studio et Gemini exposent
  tous une API compatible OpenAI. Une seule implementation avec une URL de base configurable les
  couvre donc tous ; seul Claude a un protocole different et garde son SDK. Deux implementations
  pour sept services — et une entree « Autre » qui accueillera ceux que je n ai pas prevus.

Aucun SDK supplementaire : httpx est deja une dependance, et chaque paquet proprietaire en
  ajouterait un pour une seule requete POST.

Le prompt, le schema de reponse et les garde-fous sont partages : il n y a aucune raison que la
  qualite d identification depende de qui heberge le modele. Seul le transport change.

La lecture de reponse est tolerante sur la forme et stricte sur le fond — beaucoup de modeles
  entourent leur JSON de texte, ce qui est sans importance, mais une proposition dont l index n
  existe pas dans le lot est ecartee.

Seuil dedie (80 % par defaut) plutot que « tout ce qui n est pas automatique » : entre ce seuil et
  celui d application, le score est deja bon et une revue humaine suffit. C est ce qui borne la
  depense.

La cle vit dans les preferences et non dans l environnement — changer de fournisseur ne doit pas
  imposer de modifier la stack. Elle n est JAMAIS renvoyee au navigateur : l API n expose qu un
  booleen « configuree », et un champ vide signifie « ne change pas » plutot que « efface ».

Progression du calcul --------------------- Meme defaut que le scan avant sa correction : sur 426
  fichiers le calcul dure des minutes et le bouton restait fige. Barre d avancement, compteur, temps
  restant estime, et annonce de la seconde passe IA qui n a pas d avancement fin.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.13.1 (2026-09-08)

### Bug Fixes

- Supprimer SORTILEGE_DRY_RUN, les boutons suffisent
  ([`ec8027b`](https://github.com/gsoulat/sortilege/commit/ec8027b636c6449f7cbd82165a91b4acbce5da5e))

Une configuration qui contredit l interface est un piege, et c est exactement ce qui s est produit :
  un bouton « Executer ces 88 » bien visible, mais refuse au clic par une variable posee ailleurs. L
  information arrivait trop tard, et au mauvais endroit.

Depuis que « Simuler » et « Executer » sont deux boutons distincts, le mode est un choix explicite a
  chaque action — la variable ne faisait que dupliquer cette decision, avec le pouvoir de la
  contredire.

La seule protection conservee est le DEFAUT de la requete : simuler. Une requete qui omet le champ
  ne deplace rien. Ce defaut-la ne peut pas se desynchroniser de l interface, puisqu il vit dans le
  meme appel.

Le badge « verrouille » et le mode affiche dans l entete disparaissent avec elle : ils decrivaient
  un etat qui n existe plus.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


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
