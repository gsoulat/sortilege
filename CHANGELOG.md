# CHANGELOG


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
