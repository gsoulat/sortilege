# Sortilège

Range automatiquement une bibliothèque vidéo : identifie chaque fichier,
le renomme selon un gabarit que tu construis en glisser-déposer, et le déplace
au bon endroit.

Une seule application, une seule image Docker, un seul port.

## Pourquoi un de plus

Radarr importe sans jamais demander : quand il se trompe, la bibliothèque est
corrompue en silence. FileBot demande toujours : c'est fiable, mais tu redeviens
le goulot d'étranglement.

Sortilège prend le milieu : **chaque décision porte un score de confiance**.
Au-dessus d'un seuil que tu règles, il applique tout seul. En dessous, le fichier
part dans une file de revue et t'attend. Et toute application est journalisée,
donc annulable.

## Périmètre v1

Films, séries TV, animes. Pas de musique ni de manga : ce sont des moteurs
d'identification entièrement différents (empreinte acoustique, pagination), ils
ne partageraient aucun code avec la vidéo.

## Pipeline

```
Scan → Parse → Classify → Match → Score → Plan → Apply
```

Le point d'architecture qui tient tout : **`Plan` est un objet persisté, pas une
action**. Un plan dit « ce fichier ira là, sous ce nom, avec ce score ». Il est
consultable, modifiable, approuvable et réversible. Rien ne touche au disque
avant un `Apply`, et chaque `Apply` écrit un journal qui permet un `Undo`.

| Étape | Rôle |
|---|---|
| `Scan` | Parcourt les racines sources, ne retient que les fichiers vidéo |
| `Parse` | Extrait titre / année / saison / épisode / résolution / langue du nom de release |
| `Classify` | Film, série ou anime |
| `Match` | Interroge TMDB / AniList selon la classe |
| `Score` | Combine les signaux en une confiance 0→1 |
| `Plan` | Calcule la destination via le gabarit, vérifie le confinement |
| `Apply` | Déplace, écrit le journal d'annulation |

Le scan et les plans sont écrits sur disque et repris au démarrage : un scan de
mille fichiers coûte plusieurs minutes de ffprobe et des centaines d'appels
réseau, les reperdre à chaque mise à jour d'image rendrait l'outil pénible. Un
instantané illisible est ignoré, jamais fatal.

Quand le traitement automatique est activé, un webhook Discord peut rapporter
ce que chaque cycle a fait — et surtout ce qui a échoué, puisque c'est
précisément ce que personne ne va voir autrement. Un cycle qui n'a rien trouvé
n'envoie rien : un message toutes les quinze minutes finirait par être ignoré.

## Reconnaissance par le fichier lui-même

C'est ce qui sépare Plex d'un outil qui ne lit que les noms. Trois sources
s'ajoutent au parseur, et surtout elles sont **décorrélées** de lui — un nom de
release et une durée ne se trompent pas de la même façon, donc leur accord vaut
confirmation, pas redondance.

| Source | Apport |
|---|---|
| Fichier `.nfo` | Un `tmdbid` ou `imdbid` déclaré n'est pas une ressemblance, c'est une réponse. Radarr et Sonarr en écrivent partout. |
| Tags du conteneur | Titre réel, série, numéro d'épisode, souvent écrits par l'encodeur |
| Durée | Un épisode fait 20 à 60 min, un film 80 à 240. Sépare film et épisode sans lire le nom. |
| Résolution réelle | Un fichier étiqueté 1080p qui fait 1280×720 est fréquent — on range d'après la mesure, pas d'après l'étiquette |

Conséquences dans [`scoring.py`](sortilege/core/scoring.py) : un identifiant
concordant court-circuite le score et applique ; un identifiant qui désigne une
**autre** œuvre rejette, quel que soit le score ; une durée incompatible bloque
l'application automatique et renvoie en revue.

Lecture via `ffprobe` (inclus dans l'image). Sans lui, le pipeline tourne avec
un signal de moins.

## Résolveur IA

Optionnel, désactivé par défaut. Il n'intervient **que** sur les fichiers dont le
score déterministe est ambigu — jamais sur le tout-venant, où le parseur suffit
et coûte zéro.

Deux garde-fous non négociables :

- **Il propose, il n'applique pas.** Sa sortie retourne dans le scoring normal
  et doit franchir les mêmes seuils qu'un match classique.
- **Il ne produit jamais un chemin**, uniquement des métadonnées. La construction
  du chemin reste au moteur de gabarit, derrière le confinement. Un nom de
  fichier est une entrée non fiable : il peut contenir une injection de prompt.

## Sécurité

- **Confinement des chemins** : toute destination est vérifiée comme descendante
  de la racine déclarée, chaque segment assaini séparément. Voir
  [`sortilege/core/safety.py`](sortilege/core/safety.py).
- **Pas d'évaluation de code dans les gabarits.** FileBot exécute du Groovy —
  puissant, et une exécution de code arbitraire sur ton NAS. Ici c'est une
  substitution de jetons restreinte, sans `eval`.
- **Conteneur non-root.** Il démarre root le temps d'ajuster son identité sur
  `PUID`/`PGID`, puis abandonne ces droits avant de lancer quoi que ce soit.
  Ce détour est nécessaire : l'identité doit correspondre au propriétaire des
  fichiers à déplacer, que seul toi connais. Un UID figé à la construction ne
  vaudrait que pour qui reconstruit l'image ; **aligne `PUID` sur le
  propriétaire de ton dossier de téléchargement**, pas sur toi — sinon
  Sortilège ne pourra ni déplacer ni supprimer ce que ton client a écrit.

      ls -ln /volume1/Download   # la 3e colonne est l'UID à reprendre
- **Rien ne bouge sans un clic.** Deux boutons distincts : « Simuler » vérifie
  tout le trajet sans toucher au disque, « Exécuter » applique la sélection.
  Un plan est un objet, pas une action.
- **Les doublons partent à la corbeille, jamais à la suppression.** Un
  algorithme qui se trompe sur un doublon fait perdre le seul exemplaire.
- **Aucun secret ne redescend au navigateur.** Clé d'IA, clé TMDB, clé du serveur
  multimédia, webhook Discord, clé d'API et jeton VIP OpenSubtitles : champs en
  écriture seule, l'API n'expose qu'un
  booléen « configuré ». Ces réponses finissent dans le cache du navigateur.
  **Une seule exception, assumée : la clé d'API de Sortilège**, qui n'existe que
  pour être recopiée dans un script — voir [Clé d'API](#clé-dapi).
- **Le schéma OpenAPI est derrière l'authentification**, sur `/api/openapi.json`,
  et sa page de lecture sur `/api/docs`. Il vivait à la racine, où le middleware
  ne passe pas : toute la surface de l'API était lisible par qui atteignait le
  port.
- **Le jeton TMDB v4 ne passe jamais en paramètre d'URL** — les URL finissent
  dans les journaux d'accès — mais dans l'en-tête `Authorization`.
- **L'URL de webhook est contrainte aux domaines Discord.** Elle est saisie
  depuis le navigateur et c'est le serveur qui va la chercher : sans cette
  restriction, le champ ferait du conteneur un relais capable d'émettre vers
  n'importe quelle adresse du réseau domestique.

## Démarrage

```bash
cp .env.example .env
# remplir les chemins, le mot de passe et la cle secrete, puis
docker compose up -d
```

Interface sur `http://localhost:8117`.

Rien d'autre à mettre dans le `.env`. La clé TheMovieDB, la langue des
métadonnées, la clé du résolveur IA, le webhook Discord et le serveur multimédia
se saisissent dans **Réglages**, et un bouton d'essai interroge réellement le
service pour dire si la clé passe. Obtenir une clé ne doit pas obliger à rouvrir
un fichier et à redémarrer la pile. `TMDB_API_KEY` reste lue en repli, pour les
installations qui la portent déjà — ce qui est enregistré dans l'interface prime.

Trois décisions du parcours s'y règlent aussi : la taille minimale d'un fichier
vidéo, et les dossiers et motifs de nom à ignorer **en plus** de ceux qui le sont
toujours (`@eaDir`, `#recycle`, `sample`…). C'est le seul moyen de sortir un
dossier personnel du périmètre, et de faire entrer un court-métrage que le
plancher de 50 Mo écartait sans le dire.

## Clé d'API

Une clé est générée au premier démarrage et vit dans les réglages ; elle se lit
et se régénère dans **Réglages → Système → Intégration**. Elle s'envoie dans
l'en-tête `X-Api-Key` et ouvre **toute** l'API, exactement ce qu'ouvre une
session, sans restriction de route : **elle vaut le mot de passe**, garde-la
comme lui. C'est le modèle de Radarr, et il tient parce qu'elle se régénère
en deux temps depuis cet écran (armer, puis confirmer), là où changer le mot
de passe impose d'éditer le `.env` et de redémarrer la pile.

**Elle se lit, contrairement à toutes les autres clés du produit.** Les autres
appartiennent à un service tiers : Sortilège s'en sert, personne n'a besoin de
les relire ici, donc elles sont en écriture seule. Celle-ci va dans l'autre
sens — elle n'a de valeur que recopiée dans un script ou dans les réglages d'un
client de téléchargement. Une clé qu'on ne peut pas lire est une fonction qui
n'existe pas. Le compromis reste borné : elle sort par une route dédiée, jamais
dans la réponse générale des réglages (demandée à chaque ouverture de l'écran,
et mise en cache par le navigateur), et il faut déjà une session pour la
demander — c'est-à-dire déjà tout pouvoir. Les deux routes qui la rendent en
clair interdisent toute mise en cache (`Cache-Control: no-store`).

| Route | Effet |
|---|---|
| `GET /api/settings/api-key` | La clé en clair, l'en-tête attendu, et un `curl` prêt à coller |
| `POST /api/settings/api-key/regenerate` | Nouvelle clé. L'ancienne cesse d'être acceptée immédiatement |
| `POST /api/integration/download-complete` | « Ce dossier vient de finir, occupe-t'en ». Répond `202` sans attendre |
| `GET /api/integration` | État du dernier déclenchement externe |

```bash
# Récupérer la clé (session ouverte dans le navigateur, ou clé déjà connue)
curl -s "http://NAS:8117/api/settings/api-key" -b cookies.txt

# Ce que le client de téléchargement appelle à la fin d'un transfert
curl -X POST "http://NAS:8117/api/integration/download-complete" \
     -H "X-Api-Key: srtl_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
     -H "Content-Type: application/json" \
     -d '{"path": "/downloads/Un.Film.2024.1080p", "name": "Un.Film.2024"}'
```

La route est **idempotente** et rend la main tout de suite : un client de
téléchargement tue son programme externe au bout de quelques secondes, alors
qu'un scan prend des minutes. Elle répond `202` — pas `200` : rien n'est rangé
quand la réponse part. Les appels rapprochés se replient sur un seul cycle,
donc appeler une fois par fichier d'un torrent de quarante épisodes produit le
même résultat qu'un seul appel. Le champ `path` est facultatif : il ne pilote
pas le traitement (le cycle parcourt les sources configurées) mais permet de
répondre tout de suite `"in_scope": false` quand le crochet pointe en dehors du
périmètre — l'erreur de branchement la plus courante, et la plus silencieuse.

## Sous-titres de toute la bibliothèque

La recherche de sous-titres s'applique d'elle-même à ce qui vient d'être rangé.
Pour rattraper une bibliothèque déjà en place, une route la parcourt **par lot** :

| Route | Effet |
|---|---|
| `POST /api/review/subtitles-library?offset=&limit=` | Complète les sous-titres manquants d'un lot de vidéos de la bibliothèque |

Chaque vidéo coûte un appel à OpenSubtitles, un service qui limite son débit :
une bibliothèque entière en une seule requête tiendrait la connexion ouverte des
minutes. La réponse porte le compte rendu du lot et de quoi enchaîner : `total`
(vidéos de la bibliothèque), `offset` (début du lot traité), `next_offset` (où
reprendre) et `remaining` (ce qui reste après ce lot). Rappeler la route avec
`offset=<next_offset>` jusqu'à ce que `remaining` vaille `0` traite tout, sans
repasser sur un lot déjà fait.

## Sauvegarde et restauration

Tout l'état vit dans un seul volume : un conteneur recréé sans lui repart de
zéro, gabarits et arbitrages compris.

| Route | Effet |
|---|---|
| `GET /api/backup` | Ce qu'une archive contiendrait aujourd'hui, et ce qu'elle laisserait |
| `GET /api/backup/archive` | Télécharge l'archive (ZIP) |
| `POST /api/backup/inspect` | Dit ce que contient une archive, sans rien écraser |
| `POST /api/backup/restore?confirm=true` | **Écrase** l'état en place |

```bash
curl -s -o sortilege.zip "http://NAS:8117/api/backup/archive" \
     -H "X-Api-Key: srtl_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

curl -X POST "http://NAS:8117/api/backup/restore?confirm=true" \
     -H "X-Api-Key: srtl_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
     -H "Content-Type: application/zip" \
     --data-binary @sortilege.zip
```

L'archive contient les réglages, les décisions mémorisées, l'état de travail et
le journal d'annulation. Elle porte un manifeste versionné : une archive
étrangère ou d'un format inconnu est refusée avec un message, jamais à moitié
appliquée. La restauration est confirmée côté serveur (`?confirm=true`) parce
qu'elle est aussi appelable depuis un script, et elle laisse l'état précédent
dans `avant-restauration.zip` à la racine du volume — si ce filet ne peut pas
être écrit, la restauration est refusée et rien n'est touché.

**Les secrets n'y sont pas.** Clé TMDB, clé d'IA, webhook Discord, clé du
serveur multimédia, clé d'API de Sortilège, clé d'API et jeton VIP
OpenSubtitles : tous retirés du fichier de réglages avant l'écriture. Une archive se télécharge, se copie sur un disque externe et se
dépose dans un nuage — y mettre une clé facturée à l'usage ou un droit
d'écriture sur un canal ferait d'un fichier de secours un fichier à protéger.
La décision est écrite dans l'archive elle-même, dans un `LISEZMOI.txt` à sa
racine, et annoncée par l'en-tête `X-Sortilege-Secrets: exclus` sur le
téléchargement. À la restauration ces champs ne sont **pas** écrasés : une
instance qui a déjà ses clés les garde, une instance neuve les laisse vides et
le compte rendu dit lesquelles ressaisir. Les notifications Discord et la
recherche de sous-titres, qui ne peuvent pas tourner sans leur clé, y sont alors
désactivées, et le compte rendu le dit aussi. Tout ce qui ne se retrouve pas en
trois clics — les arbitrages, le journal, les gabarits — est là.

## Développement

L'UI Vue est compilée au build Docker et servie en statique par FastAPI — d'où
l'absence de CORS et de second port. En développement local seulement, les deux
tournent séparément :

```bash
uv sync
uvicorn sortilege.main:app --reload --port 8117
```

```bash
cd ui && npm install && npm run dev
```

## Licence

MIT.
