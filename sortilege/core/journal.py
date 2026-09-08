"""Application des plans et journal d'annulation.

C'est le seul module qui ecrit sur le disque. Trois regles y sont absolues :

1. **Rien n'ecrase jamais rien.** Une destination occupee fait echouer le
   deplacement, elle ne le remplace pas. Perdre un fichier a cause d'une
   homonymie serait le pire defaut possible pour un rangeur.
2. **Tout deplacement est journalise avant d'etre oublie.** Le journal est
   ecrit et vide sur le disque a chaque operation, pas a la fin du lot : une
   coupure de courant au milieu d'un rangement ne doit pas rendre le retour
   arriere impossible.
3. **Le mode simulation ne touche a rien.** Il produit exactement le meme
   resultat, sans effet de bord, ce qui permet de verifier un lot avant de
   l'appliquer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from .companions import directory_now_empty, trash_destination
from .planner import Plan

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MoveRecord:
    """Une operation reellement effectuee, telle qu'inscrite au journal."""

    timestamp: str
    plan_id: str
    source: str
    destination: str
    method: str
    """« rename » (instantane) ou « copy » (traversee de systemes de fichiers)."""

    kind: str = "video"
    """« video », « companion » ou « trash ». Valeur par defaut volontaire :
    sans elle, les entrees ecrites avant l'ajout de ce champ deviendraient
    illisibles et seraient silencieusement ignorees a la relecture — on
    perdrait la possibilite d'annuler d'anciens deplacements."""


@dataclass(slots=True)
class ApplyResult:
    plan_id: str
    ok: bool
    source: str
    destination: str | None
    message: str
    simulated: bool = False


class Journal:
    """Journal append-only, en JSON Lines.

    JSONL et non JSON : on ajoute une ligne par operation sans relire ni
    reecrire le fichier entier. Un journal tronque par une coupure reste
    exploitable jusqu'a sa derniere ligne complete, ce qu'un tableau JSON ne
    permettrait pas.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()

    def append(self, record: MoveRecord) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                handle.flush()
                # Le fsync est volontaire : sans lui, le journal peut rester en
                # cache pendant qu'un fichier est deja deplace sur le disque.
                # On perdrait la trace de l'operation qu'on vient de faire.
                os.fsync(handle.fileno())

    def read_all(self) -> list[MoveRecord]:
        if not self._path.is_file():
            return []
        records: list[MoveRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(MoveRecord(**json.loads(line)))
            except (ValueError, TypeError):
                # Ligne tronquee par une coupure : on ignore et on continue,
                # le reste du journal garde sa valeur.
                logger.warning("ligne de journal illisible, ignoree")
        return records

    def rewrite(self, records: list[MoveRecord]) -> None:
        """Reecrit le journal apres une annulation."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            content = "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in records)
            self._path.write_text(content, encoding="utf-8")


def _move(source: Path, destination: Path) -> str:
    """Deplace un fichier sans jamais ecraser. Retourne la methode employee."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # os.rename echoue si la destination existe deja sur la plupart des
        # systemes, mais pas partout : la verification prealable dans apply()
        # reste la garantie.
        os.rename(source, destination)
        return "rename"
    except OSError:
        # EXDEV : les deux chemins sont sur des systemes de fichiers
        # differents. C'est le cas quand les telechargements et la
        # bibliotheque sont sur des volumes distincts — copie puis suppression,
        # nettement plus lent.
        shutil.move(str(source), str(destination))
        return "copy"


def apply_plan(
    plan: Plan,
    journal: Journal,
    *,
    dry_run: bool = True,
    trash_root: Path | None = None,
) -> ApplyResult:
    """Execute un plan. En simulation, verifie tout sans rien deplacer."""
    if plan.destination is None:
        return ApplyResult(plan.id, False, str(plan.source), None, "aucune destination")

    if plan.is_noop:
        return ApplyResult(
            plan.id,
            True,
            str(plan.source),
            str(plan.destination),
            "deja a sa place, rien a faire",
            simulated=dry_run,
        )

    if not plan.source.is_file():
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            "fichier source introuvable",
        )

    if plan.destination.exists():
        # Jamais d'ecrasement. Deux fichiers differents peuvent produire la
        # meme destination (deux encodages du meme episode) : c'est a
        # l'utilisateur de trancher, pas a l'outil.
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            "la destination existe deja — rien n'a ete ecrase",
        )

    if dry_run:
        return ApplyResult(
            plan.id,
            True,
            str(plan.source),
            str(plan.destination),
            "simulation : le deplacement serait possible",
            simulated=True,
        )

    try:
        method = _move(plan.source, plan.destination)
    except OSError as exc:
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            f"deplacement impossible : {exc}",
        )

    _record(journal, plan.id, plan.source, plan.destination, method, "video")

    # Les compagnons suivent la video. Chacun est journalise separement : une
    # annulation doit pouvoir tout defaire, y compris un sous-titre.
    moved_companions = 0
    for source, target in plan.companions:
        if not source.is_file() or target.exists():
            continue
        try:
            how = _move(source, target)
        except OSError as exc:
            logger.warning("compagnon non deplace (%s) : %s", source.name, exc)
            continue
        _record(journal, plan.id, source, target, how, "companion")
        moved_companions += 1

    trashed = _evacuate(plan, journal, trash_root)

    detail = f"deplace ({method})"
    if moved_companions:
        detail += f", {moved_companions} fichier(s) associe(s)"
    if trashed:
        detail += f", {trashed} reste(s) en corbeille"

    return ApplyResult(plan.id, True, str(plan.source), str(plan.destination), detail)


def _record(
    journal: Journal, plan_id: str, source: Path, destination: Path, method: str, kind: str
) -> None:
    journal.append(
        MoveRecord(
            timestamp=datetime.now(UTC).isoformat(),
            plan_id=plan_id,
            source=str(source),
            destination=str(destination),
            method=method,
            kind=kind,
        )
    )


def _evacuate(plan: Plan, journal: Journal, trash_root: Path | None) -> int:
    """Deplace les restes vers la corbeille. Ne supprime jamais rien.

    Sans racine de corbeille configuree, on ne fait rien : mieux vaut laisser
    du desordre que d'inventer une destination.
    """
    if trash_root is None or not plan.leftovers:
        return 0

    batch = datetime.now(UTC).strftime("%Y-%m-%d")
    moved = 0

    for leftover in plan.leftovers:
        if not leftover.is_file():
            continue
        target = trash_destination(trash_root, batch, leftover)
        if target.exists():
            continue
        try:
            how = _move(leftover, target)
        except OSError as exc:
            logger.warning("reste non evacue (%s) : %s", leftover.name, exc)
            continue
        _record(journal, plan.id, leftover, target, how, "trash")
        moved += 1

    # Le dossier d'origine, une fois vide, n'a plus de raison d'exister. Il est
    # SUPPRIME et non mis en corbeille : un dossier vide ne contient rien a
    # recuperer, et l'annulation le recreera au besoin.
    parent = plan.source.parent
    if directory_now_empty(parent):
        try:
            parent.rmdir()
        except OSError:
            pass

    return moved


def undo_last(journal: Journal, count: int = 1) -> list[ApplyResult]:
    """Annule les dernieres operations, de la plus recente a la plus ancienne.

    L'ordre inverse n'est pas cosmetique : deux operations peuvent avoir touche
    des chemins imbriques, et defaire dans l'ordre chronologique recreerait des
    collisions que l'ordre inverse evite.
    """
    records = journal.read_all()
    if not records:
        return []

    to_undo = records[-count:]
    remaining = records[: len(records) - len(to_undo)]
    results: list[ApplyResult] = []

    for record in reversed(to_undo):
        source = Path(record.destination)
        target = Path(record.source)

        if not source.is_file():
            results.append(
                ApplyResult(
                    record.plan_id,
                    False,
                    record.destination,
                    record.source,
                    "fichier introuvable a sa destination — deja deplace ailleurs ?",
                )
            )
            continue

        if target.exists():
            results.append(
                ApplyResult(
                    record.plan_id,
                    False,
                    record.destination,
                    record.source,
                    "l'emplacement d'origine est occupe — rien n'a ete ecrase",
                )
            )
            continue

        try:
            _move(source, target)
        except OSError as exc:
            results.append(
                ApplyResult(
                    record.plan_id,
                    False,
                    record.destination,
                    record.source,
                    f"retour impossible : {exc}",
                )
            )
            continue

        results.append(
            ApplyResult(record.plan_id, True, record.destination, record.source, "annule")
        )

    # Seules les operations effectivement annulees quittent le journal : une
    # annulation partielle doit rester rejouable.
    undone = {r.plan_id for r in results if r.ok}
    kept = [r for r in to_undo if r.plan_id not in undone]
    journal.rewrite(remaining + kept)

    return results
