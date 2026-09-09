"""Inventaire et vidage de la corbeille.

Sortilege ne supprime jamais : restes de release, doublons, copies deja rangees
partent tous a la corbeille. C'est la bonne regle — une erreur d'identification
se rattrape, une suppression non — mais elle a une consequence que rien ne
traitait : **la corbeille ne se vide pas toute seule**. Sur une bibliotheque
active elle grossit indefiniment, et l'espace qu'on croyait recuperer ne l'est
jamais.

Le vidage est donc explicite, et gouverne par un age. Un fichier evacue
aujourd'hui peut avoir ete evacue a tort : le delai est la fenetre pendant
laquelle on peut encore s'en apercevoir. Le raccourcir a zero reviendrait a
transformer la corbeille en suppression differee de quelques secondes, ce qui
ne protege plus de rien.

La corbeille est organisee par lot journalier (« 2026-09-09/ ») : c'est le nom
du dossier qui donne l'age, pas la date de modification du fichier, laquelle
suit le fichier depuis son telechargement et n'a aucun rapport avec le moment
ou il a ete evacue.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_BATCH_DIR = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})$")

MIN_AGE_DAYS = 1
"""Plancher. Vider ce qui a ete evacue aujourd'hui n'est plus une corbeille,
c'est une suppression avec un detour."""


@dataclass(frozen=True, slots=True)
class Batch:
    """Un lot journalier de la corbeille."""

    day: date
    path: Path
    files: int
    bytes: int

    @property
    def age_days(self) -> int:
        return (datetime.now(UTC).date() - self.day).days


def _measure(directory: Path) -> tuple[int, int]:
    files = 0
    total = 0
    for entry in directory.rglob("*"):
        try:
            if entry.is_file():
                files += 1
                total += entry.stat().st_size
        except OSError:
            # Un fichier disparu ou illisible ne doit pas interrompre
            # l'inventaire : le reste du compte garde sa valeur.
            continue
    return files, total


def inventory(trash_root: Path) -> list[Batch]:
    """Ce que contient la corbeille, lot par lot, du plus ancien au plus recent.

    Le plus ancien d'abord : c'est celui qu'on vide en premier, et le voir en
    tete evite d'avoir a chercher.
    """
    if not trash_root.is_dir():
        return []

    batches: list[Batch] = []
    for entry in sorted(trash_root.iterdir()):
        if not entry.is_dir():
            continue
        match = _BATCH_DIR.match(entry.name)
        if match is None:
            # Un dossier au nom inattendu n'est pas un lot : on ne le compte
            # pas, et surtout on ne le videra pas.
            continue
        try:
            day = date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        files, size = _measure(entry)
        batches.append(Batch(day=day, path=entry, files=files, bytes=size))

    return batches


def total_bytes(batches: list[Batch]) -> int:
    return sum(b.bytes for b in batches)


@dataclass
class PurgeResult:
    removed_batches: int = 0
    removed_files: int = 0
    freed_bytes: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def purge(trash_root: Path, older_than_days: int) -> PurgeResult:
    """Supprime DEFINITIVEMENT les lots plus vieux que le delai donne.

    C'est le seul endroit de l'application qui supprime reellement, et il ne
    s'execute que sur demande explicite. Trois garde-fous :

    1. **Un plancher sur le delai.** Vider ce qui vient d'etre evacue ferait de
       la corbeille une formalite.
    2. **Seuls les dossiers de lot sont touches**, reconnus a leur nom de date.
       Un dossier inattendu sous la corbeille est ignore plutot que supprime :
       s'il est la, ce n'est pas nous qui l'avons mis.
    3. **Un echec sur un lot n'arrete pas les autres**, et il est rapporte.
    """
    days = max(MIN_AGE_DAYS, older_than_days)
    result = PurgeResult()

    for batch in inventory(trash_root):
        if batch.age_days < days:
            continue
        try:
            shutil.rmtree(batch.path)
        except OSError as exc:
            logger.warning("lot %s non supprime : %s", batch.day, exc)
            result.errors.append(f"{batch.day} : {exc}")
            continue
        result.removed_batches += 1
        result.removed_files += batch.files
        result.freed_bytes += batch.bytes

    return result
