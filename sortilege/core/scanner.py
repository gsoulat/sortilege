"""Parcours des racines sources.

Premiere etape du pipeline : trouver les fichiers video et dire ce qu'on croit
lire de chacun, sans reseau ni decision. Un scan doit pouvoir tourner sans
aucune cle d'API — c'est ce qui permet de voir immediatement ce que Sortilege
comprend d'une bibliotheque avant meme de la configurer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .parser import ParsedName, is_video, parse
from .probe import FileProbe, inspect
from .safety import PathConfinementError, assert_readable_source

logger = logging.getLogger(__name__)

# Un fichier plus petit que ca est un echantillon, une bande-annonce ou un
# telechargement avorte : le ranger polluerait la bibliotheque.
MIN_SIZE_BYTES = 50 * 1024 * 1024

# Dossiers a ne jamais parcourir : residus de clients de telechargement et
# metadonnees systeme.
SKIP_DIRS = {
    "@eaDir",  # Synology
    ".@__thumb",  # Asustor
    "#recycle",
    ".Trash-1000",
    "lost+found",
    "extras",
    "featurettes",
    "sample",
    "samples",
}

SKIP_NAME_HINTS = ("sample", "trailer", "bande-annonce", "extrait")


@dataclass(slots=True)
class ScannedFile:
    """Un fichier trouve, avec tout ce qu'on sait de lui sans reseau."""

    path: Path
    size_bytes: int
    parsed: ParsedName
    probe: FileProbe

    # Relatif a la racine source : c'est ce qu'on affiche, un chemin absolu de
    # conteneur ne parle a personne.
    relative_path: str = ""

    in_library: bool = False
    """Le fichier est deja dans la bibliotheque.

    Scanner sa bibliotheque existante est un usage legitime — la normaliser,
    corriger d'anciens noms. Mais il faut distinguer ce cas d'un import :
    proposer de deplacer un fichier deja bien range est du bruit, et l'appliquer
    serait une operation nulle qui salit le journal d'annulation."""

    @property
    def skipped_reason(self) -> str | None:
        if self.size_bytes < MIN_SIZE_BYTES:
            return "fichier trop petit (echantillon ou telechargement incomplet)"
        return None


@dataclass(slots=True)
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.files)


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _should_skip_file(path: Path) -> bool:
    lowered = path.stem.lower()
    return any(hint in lowered for hint in SKIP_NAME_HINTS)


def _under(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        resolved = path.resolve(strict=False)
        root = root.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def collect(
    roots: list[Path], limit: int | None = None
) -> tuple[list[tuple[Path, Path]], int, list[str]]:
    """Recense les fichiers video sans les analyser.

    Passe rapide et separee : elle ne fait que parcourir l'arborescence, sans
    ffprobe ni lecture de .nfo. C'est ce qui permet d'annoncer un TOTAL avant
    de commencer — sans lui, une barre de progression n'aurait pas de
    denominateur et on ne pourrait afficher qu'un compteur qui monte.
    """
    found: list[tuple[Path, Path]] = []
    skipped = 0
    errors: list[str] = []

    for root in roots:
        if not root.is_dir():
            errors.append(f"racine introuvable : {root}")
            continue

        for path in sorted(root.rglob("*")):
            if limit is not None and len(found) >= limit:
                return found, skipped, errors

            if any(_should_skip_dir(part) for part in path.relative_to(root).parts[:-1]):
                continue
            if not path.is_file() or not is_video(path):
                continue
            if _should_skip_file(path):
                skipped += 1
                continue

            found.append((root, path))

    return found, skipped, errors


PARTIAL_EVERY = 25
"""Frequence de publication du resultat partiel. Assez souvent pour que la
liste se remplisse sous les yeux, assez rare pour que le cout reste
negligeable devant l'analyse d'un fichier."""


def scan(
    roots: list[Path],
    *,
    deep: bool = True,
    limit: int | None = None,
    library_root: Path | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_partial: Callable[[ScanResult], None] | None = None,
) -> ScanResult:
    """Parcourt les racines et analyse chaque fichier video.

    ``deep`` active la lecture du fichier lui-meme (ffprobe et .nfo). C'est
    nettement plus lent — quelques dizaines de millisecondes par fichier — donc
    on peut le desactiver pour un apercu rapide sur une grosse bibliotheque.

    ``on_progress(traites, total, nom_du_fichier)`` est appele apres chaque
    fichier. C'est ce qui permet a l'interface de montrer ou en est un scan qui
    dure des minutes, plutot qu'un bouton fige.

    ``on_partial`` recoit le resultat EN COURS de constitution, regulierement.
    Une barre de progression qui avance devant une liste vide ne vaut guere
    mieux qu'un bouton fige : on voit que quelque chose se passe, sans rien
    pouvoir en faire. Avec ce rappel, les oeuvres apparaissent au fur et a
    mesure qu'elles sont reconnues.
    """
    result = ScanResult()

    candidates, skipped, errors = collect(roots, limit)
    result.skipped = skipped
    result.errors.extend(errors)

    total = len(candidates)
    if on_progress:
        on_progress(0, total, "")

    for index, (root, path) in enumerate(candidates, start=1):
        try:
            # Ceinture et bretelles : un lien symbolique pourrait pointer
            # hors de la racine declaree.
            assert_readable_source(path, [root])
            size = path.stat().st_size
        except (PathConfinementError, OSError) as exc:
            result.errors.append(f"{path.name} : {exc}")
            if on_progress:
                on_progress(index, total, path.name)
            continue

        # Toute la chaine de dossiers entre la racine et le fichier, pas
        # seulement le parent immediat : sur « Dune (2024)/CD1/film.mkv »
        # ou « Severance/Season 02/ep07.mkv », l'information utile est plus
        # haut que le dossier direct.
        ancestors = list(path.relative_to(root).parts[:-1])

        if on_partial and index % PARTIAL_EVERY == 0:
            # Le resultat est publie par paquets, pas a chaque fichier : sur
            # six mille fichiers, notifier a l'unite couterait plus que
            # l'analyse elle-meme, pour un affichage que l'oeil ne suit pas.
            #
            # Une COPIE des listes, jamais l'objet en cours : le destinataire le
            # lit depuis un autre fil pendant que le scan continue d'y ecrire.
            on_partial(
                ScanResult(
                    files=list(result.files),
                    skipped=result.skipped,
                    errors=list(result.errors),
                )
            )

        parsed = parse(path, ancestors)
        probe = inspect(path) if deep else FileProbe()

        result.files.append(
            ScannedFile(
                path=path,
                size_bytes=size,
                parsed=parsed,
                probe=probe,
                relative_path=str(path.relative_to(root)),
                in_library=_under(path, library_root),
            )
        )

        if on_progress:
            on_progress(index, total, path.name)

    return result
