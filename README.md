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
- **Conteneur non-root**, UID/GID paramétrables.
- **Mode simulation par défaut** (`SORTILEGE_DRY_RUN=true`) : rien ne bouge tant
  que tu ne l'as pas désactivé explicitement.
- Clés d'API par variables d'environnement uniquement.

## Démarrage

```bash
cp .env.example .env
# remplir les cles et les chemins, puis
docker compose up -d
```

Interface sur `http://localhost:8117`.

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
