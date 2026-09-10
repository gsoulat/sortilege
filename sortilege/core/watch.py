"""Detection des nouveaux fichiers, et surtout de leur STABILITE.

Le piege de toute surveillance de dossier n'est pas de reperer un fichier
nouveau, c'est de savoir qu'il est FINI. Un fichier apparait des la premiere
octet ecrit : le traiter tout de suite, c'est deplacer un telechargement a
moitie fait, et JDownloader ecrira ensuite dans un chemin qui n'existe plus.

Deux conditions cumulatives, volontairement conservatrices :

1. **La taille n'a pas bouge** entre deux observations. C'est le signal le plus
   fiable, et le seul qui detecte une extraction d'archive en cours — pendant
   laquelle le fichier porte deja son nom definitif.
2. **La derniere modification remonte a plus de N secondes.** Rattrape le cas
   d'une ecriture lente ou d'une pause reseau qui donnerait deux observations
   identiques par hasard.

Attendre trop longtemps ne coute qu'un cycle. Traiter trop tot coute un fichier
corrompu et un journal d'annulation qui pointe vers du vide.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import DEFAULT_RULES, ScanRules, collect

logger = logging.getLogger(__name__)

DEFAULT_QUIET_SECONDS = 120


@dataclass(slots=True)
class Observation:
    size: int
    seen_at: float
    mtime: float


@dataclass
class Watcher:
    """Suit l'etat des fichiers d'un scan a l'autre.

    L'etat vit en memoire : au redemarrage, tout est reconsidere comme nouveau.
    Sans consequence — le pipeline ignore de toute facon ce qui est deja range,
    et un fichier deja traite n'a plus de source a deplacer.
    """

    quiet_seconds: float = DEFAULT_QUIET_SECONDS
    _seen: dict[str, Observation] = field(default_factory=dict)
    _processed: set[str] = field(default_factory=set)

    def forget(self, path: Path) -> None:
        key = str(path)
        self._seen.pop(key, None)
        self._processed.discard(key)

    def mark_processed(self, paths: list[Path]) -> None:
        """Evite de replanifier indefiniment un fichier qu'on n'a pas su ranger.

        Sans cela, un fichier rejete a chaque cycle relancerait une recherche
        chez les fournisseurs toutes les quinze minutes, pour le meme echec.
        """
        self._processed.update(str(p) for p in paths)

    def poll(
        self, roots: list[Path], *, now: float | None = None, rules: ScanRules = DEFAULT_RULES
    ) -> list[Path]:
        """Renvoie les fichiers nouveaux ET stables depuis la derniere fois.

        Le recensement est la passe rapide du scanner : aucun ffprobe, aucune
        lecture de .nfo. Une surveillance qui tourne toutes les quinze minutes
        ne doit pas relire toute la bibliotheque a chaque fois.

        ``rules`` doit etre celles du scan : une surveillance qui ignore les
        exclusions de l'utilisateur reveillerait un cycle automatique sur des
        fichiers que le scan ecarte ensuite — un tour de boucle pour rien, a
        chaque intervalle.
        """
        now = now if now is not None else time.time()
        candidates, _, errors = collect(roots, rules=rules)
        for error in errors:
            logger.debug("surveillance : %s", error)

        stable: list[Path] = []
        current: dict[str, Observation] = {}

        for _root, path in candidates:
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                # Disparu entre le recensement et le stat : sans importance,
                # le prochain cycle tranchera.
                continue

            observation = Observation(size=stat.st_size, seen_at=now, mtime=stat.st_mtime)
            current[key] = observation

            if key in self._processed:
                continue

            previous = self._seen.get(key)
            if previous is None:
                # Premiere vue : on ne conclut rien. Un fichier doit avoir ete
                # observe DEUX fois pour qu'on puisse comparer sa taille.
                continue

            if previous.size != observation.size:
                continue
            if now - observation.mtime < self.quiet_seconds:
                continue

            stable.append(path)

        self._seen = current
        # Un fichier disparu ne doit pas rester marque : s'il revient, c'est
        # un nouveau fichier.
        self._processed &= set(current)

        return stable
