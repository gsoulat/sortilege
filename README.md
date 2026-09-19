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

  Si aucune identité ne convient à tous tes dossiers, voir
  [`PUID=auto`](#quand-aucune-identité-ne-passe--puidauto).
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

### Quand aucune identité ne passe : `PUID=auto`

Déplacer ou supprimer un fichier n'exige aucun droit sur le fichier : cela exige
le droit d'écrire sur le **dossier qui le contient**. C'est une règle du noyau,
et aucune astuce logicielle ne la contourne. Sur un NAS, JDownloader crée ses
dossiers en `999:999 drwxr-xr-x` pendant que la bibliothèque appartient à
`1000:1000` : **aucune identité non-root n'écrit dans les deux**, et Sortilège
échoue en « Permission denied » quel que soit le `PUID` choisi.

`PUID=auto` (et `PGID=auto`) délègue le choix à Sortilège. Au démarrage, il lit
le propriétaire et le mode de chaque racine — bibliothèque, sources montées,
sources ajoutées depuis l'interface — puis :

1. si `1000:1000` écrit partout, cette identité est gardée : rien ne change ;
2. si une seule des identités **déjà propriétaires** de ces dossiers écrit
   partout, il la retient — elle existe sur ton NAS, tu la reconnaîtras ;
3. sinon **root**, en nommant le dossier qui l'y a forcé.

Sans `PUID`, rien ne change non plus : `1000:1000`, comme avant. **Le diagnostic
s'affiche dans tous les cas** — chaque racine, son propriétaire, son mode, et
« accessible en écriture » ou non pour l'identité retenue :

```bash
docker logs sortilege | head -20
```

**Le compromis root est assumé, et autant le dire franchement** : le conteneur a
alors tous les droits sur ce qui lui est monté. C'est le prix à payer pour une
application de NAS, en réseau local, dont le travail consiste précisément à
déplacer des fichiers appartenant à plusieurs comptes. Le reste du durcissement
tient : `no-new-privileges`, système de fichiers en lecture seule, et seuls les
volumes déclarés sont visibles. Les fichiers rangés, eux, prennent l'identité du
dossier qui les accueille — la bibliothèque ne se remplit pas de fichiers root.

Si ce compromis ne te convient pas, la solution est côté NAS et non côté image :
donne l'écriture au groupe sur le dossier de téléchargement (`chmod g+w`) et fixe
`PGID` sur ce groupe. Le diagnostic le confirmera au redémarrage suivant.

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

## Copier vers un disque externe

Choisir des films et des séries dans la médiathèque, choisir un disque USB :
Sortilège le parcourt, dit ce qui y est déjà (même sous un autre nom), et copie
ce qui manque **un fichier à la fois**, pour ne pas saturer le port USB. Une
série déjà présente sur le disque reçoit ses nouveaux épisodes dans son propre
dossier de saison (« Saison 1 » sur le disque, même si la médiathèque dit
« Season 01 »). Rien n'est jamais écrasé : un fichier de même nom mais de
taille différente est signalé, pas remplacé.

**Le montage — une seule configuration prise en charge.** Monte dans le
conteneur le dossier *parent* sous lequel ton NAS monte ses disques USB, avec
la propagation `rslave`, dans un **sous-dossier** de `/externes` (modifiable
par `SORTILEGE_EXTERNAL_ROOT`) — par exemple `/externes/usb` :

```yaml
    volumes:
      - /chemin/du/parent/des/disques:/externes/usb:rslave
```

Sortilège ne propose comme disque qu'un point de montage trouvé *à
l'intérieur* d'un dossier de `/externes` (un ou deux niveaux plus bas :
`/externes/usb/<disque>` ou `/externes/usb/<port>/<disque>`). Sans `rslave`, un
disque branché après le démarrage du conteneur n'apparaît jamais. **Le chemin
exact dépend du NAS et de son système** : branche le disque, puis en SSH sur le
NAS

```bash
df -h                 # la ligne du disque USB donne son point de montage
mount | grep -i usb   # ou, selon le NAS, grep -i sd
```

et monte le dossier qui *contient* ce point de montage.

**Un disque monté directement est refusé.** Un disque relié seul
(`/chemin/du/disque:/externes/USB1`) apparaît dans la liste, mais n'est jamais
proposé : débranché, son point de montage reste en place dans le conteneur et
montre un dossier de la partition système du NAS, et rien ne distingue alors le
disque absent d'un dossier vide qu'un autre service aurait rempli. Monté par son
parent en `rslave`, un disque débranché disparaît avec son montage : il n'y a
plus rien à confondre.

**Pourquoi Sortilège est si méfiant.** Un disque débranché laisse derrière lui
son point de montage : un dossier vide sur la partition système du NAS. Y
copier 500 Go remplirait cette partition et ferait tomber le NAS. Seuls les
vrais points de montage sont donc proposés ; un dossier vide est listé, mais
refusé, tout comme le volume de la médiathèque elle-même. Pendant la copie,
chaque fichier vérifie d'abord que le disque est toujours monté, et tout
s'écrit à travers le disque ouvert au départ, sans suivre aucun lien : un
disque arraché arrête toute la file, sans qu'aucune écriture n'atteigne le
dossier resté à sa place. Un lien symbolique posé sur le disque n'est jamais
suivi, ni pour écrire ni pour dire qu'un fichier « y est déjà » : l'analyse le
signale.

**Le repère du disque.** Au départ d'une copie, Sortilège pose à la racine du
disque un petit fichier `.sortilege-disque` qui contient un identifiant
aléatoire (il le garde s'il y est déjà). C'est ainsi qu'il reconnaît le disque
pour reprendre une copie : rebranché sur un autre port, un disque change de
point de montage, et un autre disque peut prendre sa place. Le supprimer rend
impossible la reprise d'une copie interrompue vers ce disque (il faudra
l'oublier). Un disque cloné porte le même repère : s'ils sont branchés
ensemble, la reprise refuse de choisir.

**Interrompre et reprendre.** Un fichier s'écrit sous `<nom>.sortilege-part` et
ne prend son nom qu'une fois complet (taille contrôlée) et forcé sur le disque.
Tous les 256 Mio, un point de contrôle est enregistré (`data/copie-reprise.json`),
après avoir forcé sur le disque ce qui précède. Après un arrêt, un disque
débranché, un disque repassé en lecture seule ou un redémarrage du conteneur,
« Reprendre la copie » garantit exactement ceci :

- la copie ne reprend que vers le disque qui porte le repère enregistré, où
  qu'il soit monté ; si aucun disque branché ne le porte, elle refuse (« le
  disque de cette copie n'est pas branché ») et n'écrit nulle part ailleurs ;
- ce qui a été terminé n'est pas recopié : l'analyse est refaite, et un fichier
  déjà sur le disque sous son nom et à la bonne taille est sauté ;
- le fichier interrompu ne reprend à son point de contrôle que si la source a
  la même identité (taille, date, inode, `ctime`), si le fichier partiel est au
  moins aussi long que le point de contrôle (ce qui le dépasse est coupé : rien
  ne garantit que c'était écrit), et si son contenu est égal à la source sur
  **tout le dernier segment** avant le point de contrôle (jusqu'à 256 Mio, là
  où frappe une coupure) et sur **cinq échantillons** d'un Mio avant lui. Un
  seul écart, et le fichier repart de zéro, en le disant ;
- ce qui n'est **pas** garanti : un octet du fichier partiel modifié par autre
  chose que Sortilège, avant le dernier segment et hors des échantillons, n'est
  pas vu — le fichier publié porterait cette différence. Relire 40 Go sur un
  port USB pour s'en assurer coûterait plus que de les recopier : si le disque
  a été manipulé ailleurs pendant l'interruption, abandonne la copie et
  relance-la.

Tant qu'une copie interrompue attend, aucune autre ne part. Trois gestes la
tranchent :

- **Reprendre la copie**, comme ci-dessus ;
- **Abandonner** : son fichier partiel est retiré du disque, qui doit être
  branché, puis la copie est oubliée. Si le fichier ne peut pas être retiré
  (disque en lecture seule, accès refusé, disque qui ne répond pas), rien n'est
  oublié et l'écran dit pourquoi : le fichier ne reste jamais sur le disque sans
  que rien ne le désigne. Une suppression commencée est toujours menée à son
  terme avant de répondre ; une suppression qui n'a pas commencé à temps ne
  commencera plus ;
- **Oublier cette copie**, proposé quand son disque n'est pas branché (un
  disque qui ne reviendra pas) : la copie est oubliée, rien n'est supprimé, et
  l'écran nomme le fichier partiel resté sur le disque, à supprimer à la main.
  Si le disque est en fait branché, Sortilège refuse d'oublier et propose de
  reprendre.

FAT32 limite un fichier à 4 Go, et exFAT/NTFS refusent
`\ : * ? " < > |` dans un nom : l'analyse le dit avant de copier, sans rien
renommer en silence.

| Route | Effet |
|---|---|
| `GET /api/copy/disks` | Les disques montés, et les dossiers refusés avec leur motif (dont les montages directs) |
| `POST /api/copy/analyze` | Ce qui est déjà sur le disque, ce qui manque, la place nécessaire |
| `POST /api/copy/start` | Refait l'analyse, pose le repère du disque puis lance la copie (`409` si elle ne tient pas, ou si une copie interrompue attend) |
| `GET /api/copy/status` | Avancement, débits, file à venir, copie à reprendre (`disk_available`, `disk_found` : le disque qui porte son repère) |
| `POST /api/copy/pause` · `resume` · `stop` | Piloter la copie en cours |
| `POST /api/copy/continue` · `discard` | Reprendre, ou abandonner, une copie interrompue (`discard` refuse — et garde la copie — si le disque est absent ou si le fichier partiel ne peut pas être retiré ; `{"forget": true}` l'oublie sans rien supprimer, seulement si son disque est absent, et le dit) |

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
