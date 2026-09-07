"""Parcours des racines sources.

Premiere etape du pipeline : trouver les fichiers video et dire ce qu'on croit
lire de chacun, sans reseau ni decision. Un scan doit pouvoir tourner sans
aucune cle d'API — c'est ce qui permet de voir immediatement ce que Sortilege
comprend d'une bibliotheque avant meme de la configurer.
"""

from __future__ import annotations

import logging
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


def scan(
    roots: list[Path],
    *,
    deep: bool = True,
    limit: int | None = None,
    library_root: Path | None = None,
) -> ScanResult:
    """Parcourt les racines et analyse chaque fichier video.

    ``deep`` active la lecture du fichier lui-meme (ffprobe et .nfo). C'est
    nettement plus lent — quelques dizaines de millisecondes par fichier — donc
    on peut le desactiver pour un apercu rapide sur une grosse bibliotheque.
    """
    result = ScanResult()

    for root in roots:
        if not root.is_dir():
            result.errors.append(f"racine introuvable : {root}")
            continue

        for path in sorted(root.rglob("*")):
            if limit is not None and result.total >= limit:
                return result

            if any(_should_skip_dir(part) for part in path.relative_to(root).parts[:-1]):
                continue
            if not path.is_file() or not is_video(path):
                continue
            if _should_skip_file(path):
                result.skipped += 1
                continue

            try:
                # Ceinture et bretelles : un lien symbolique pourrait pointer
                # hors de la racine declaree.
                assert_readable_source(path, [root])
                size = path.stat().st_size
            except (PathConfinementError, OSError) as exc:
                result.errors.append(f"{path.name} : {exc}")
                continue

            # Toute la chaine de dossiers entre la racine et le fichier, pas
            # seulement le parent immediat : sur « Dune (2024)/CD1/film.mkv »
            # ou « Severance/Season 02/ep07.mkv », l'information utile est plus
            # haut que le dossier direct.
            ancestors = list(path.relative_to(root).parts[:-1])

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

    return result
