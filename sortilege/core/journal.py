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
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from .companions import directory_now_empty, trash_destination
from .planner import Plan

logger = logging.getLogger(__name__)

# Dossier de saison tel que NOS gabarits l'ecrivent — « Season 01 », « Saison 2 ».
# Volontairement distinct du motif du parseur, qui lit des noms de release : ici
# on relit ce qu'on a soi-meme produit, et les deux n'ont pas a evoluer ensemble.
_SEASON_FOLDER = re.compile(r"^s(?:aison|eason)?[\s._-]*\d{1,2}$", re.IGNORECASE)


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

    title: str = ""
    """Oeuvre concernee, telle qu'identifiee au moment du rangement.

    Sans elle, annuler « juste cette serie » demanderait de deviner l'oeuvre a
    partir des chemins — ce qui marche tant que le gabarit n'a pas change. Les
    entrees anterieures a ce champ retombent sur cette deduction, faute de
    mieux ; les nouvelles n'en dependent pas."""

    work_kind: str = ""
    """« movie », « episode » ou « anime ». Sert a l'affichage du regroupement."""


@dataclass(slots=True)
class ApplyResult:
    plan_id: str
    ok: bool
    source: str
    destination: str | None
    message: str
    simulated: bool = False
    reason: str = ""
    """Cause stable, distincte du message.

    Le message porte le detail utile a UN fichier (le chemin, l'errno) ; il est
    donc different pour chacun. Quand trois cents plans echouent, ce qu'on veut
    savoir tient en une ligne — « ils echouent tous pour la meme raison, et
    laquelle » — et cela demande une valeur qu'on puisse regrouper."""


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
        return ApplyResult(
            plan.id, False, str(plan.source), None, "aucune destination", reason="no_destination"
        )

    if plan.is_noop:
        return ApplyResult(
            plan.id,
            True,
            str(plan.source),
            str(plan.destination),
            "deja a sa place, rien a faire",
            simulated=dry_run,
            reason="noop",
        )

    if not plan.source.is_file():
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            "fichier source introuvable",
            reason="source_missing",
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
            reason="destination_exists",
        )

    if dry_run:
        return ApplyResult(
            plan.id,
            True,
            str(plan.source),
            str(plan.destination),
            "simulation : le deplacement serait possible",
            simulated=True,
            reason="ok",
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
            reason="permission_denied" if isinstance(exc, PermissionError) else "move_failed",
        )

    _record(journal, plan, plan.source, plan.destination, method, "video")

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
        _record(journal, plan, source, target, how, "companion")
        moved_companions += 1

    trashed = _evacuate(plan, journal, trash_root)

    detail = f"deplace ({method})"
    if moved_companions:
        detail += f", {moved_companions} fichier(s) associe(s)"
    if trashed:
        detail += f", {trashed} reste(s) en corbeille"

    return ApplyResult(plan.id, True, str(plan.source), str(plan.destination), detail, reason="ok")


def evacuate_ranged_source(plan: Plan, journal: Journal, trash_root: Path | None) -> ApplyResult:
    """Evacue une source dont le fichier est DEJA a destination.

    Le cas est frequent apres un rangement interrompu ou rejoue : le fichier a
    bien ete range, mais une copie subsiste dans les telechargements. Elle
    occupe la place et sera reproposee a chaque scan.

    Trois garde-fous, dans cet ordre :

    1. **Le fichier doit reellement etre a destination.** Sans ce controle,
       cette fonction supprimerait une source qui n'a jamais ete rangee.
    2. **Les deux doivent avoir la MEME TAILLE.** Deux encodages d'un meme
       episode portent le meme nom de destination sans etre le meme fichier —
       evacuer la source ferait alors perdre un exemplaire distinct. Un doute
       sur l'identite se tranche par un refus, pas par un pari.
    3. **Rien n'est supprime.** Le fichier part a la corbeille et l'operation
       est journalisee, donc annulable comme n'importe quel deplacement. Une
       suppression vraie n'est jamais rattrapable, et c'est exactement ce qu'un
       outil de rangement ne doit pas se permettre.
    """
    if plan.destination is None:
        return ApplyResult(plan.id, False, str(plan.source), None, "aucune destination")

    if not plan.source.is_file():
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            "source deja absente",
            reason="source_missing",
        )

    if not plan.destination.is_file():
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            "le fichier n'est pas a destination — il n'a pas ete range",
            reason="not_ranged",
        )

    source_size = plan.source.stat().st_size
    target_size = plan.destination.stat().st_size
    if source_size != target_size:
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            (
                f"tailles differentes ({source_size} vs {target_size} octets) : "
                "ce n'est pas le meme fichier, rien n'a ete touche"
            ),
            reason="size_mismatch",
        )

    if trash_root is None:
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(plan.destination),
            "aucune corbeille configuree",
            reason="no_trash",
        )

    batch = datetime.now(UTC).strftime("%Y-%m-%d")
    target = trash_destination(trash_root, batch, plan.source)
    if target.exists():
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(target),
            "un fichier du meme nom occupe deja la corbeille",
            reason="destination_exists",
        )

    try:
        how = _move(plan.source, target)
    except OSError as exc:
        return ApplyResult(
            plan.id,
            False,
            str(plan.source),
            str(target),
            f"evacuation impossible : {exc}",
            reason="permission_denied" if isinstance(exc, PermissionError) else "move_failed",
        )

    _record(journal, plan, plan.source, target, how, "trash")
    return ApplyResult(
        plan.id,
        True,
        str(plan.source),
        str(target),
        "copie en double mise en corbeille",
        reason="ok",
    )


def _record(
    journal: Journal, plan: Plan, source: Path, destination: Path, method: str, kind: str
) -> None:
    journal.append(
        MoveRecord(
            timestamp=datetime.now(UTC).isoformat(),
            plan_id=plan.id,
            source=str(source),
            destination=str(destination),
            method=method,
            kind=kind,
            title=plan.title,
            work_kind=plan.kind,
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
        _record(journal, plan, leftover, target, how, "trash")
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


def work_key(record: MoveRecord) -> str:
    """Identifie l'oeuvre a laquelle appartient une operation.

    Le titre inscrit au journal fait foi. Les entrees anterieures a ce champ
    n'en ont pas : on retombe alors sur le dossier de destination, qui porte le
    nom de l'oeuvre dans tous les gabarits livres. C'est une deduction, pas une
    certitude — d'ou la preference donnee au titre des qu'il existe.
    """
    if record.title:
        return record.title

    parts = Path(record.destination).parts
    # « .../Severance (2022)/Season 01/fichier.mkv » : le dossier de saison ne
    # designe pas l'oeuvre, celui du dessus si.
    if len(parts) >= 3 and _SEASON_FOLDER.match(parts[-2]):
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return record.destination


def group_by_work(records: list[MoveRecord]) -> list[dict]:
    """Regroupe les operations par oeuvre, la plus recente en tete.

    Un bouton « tout annuler » est un aveu : il suppose qu'on veuille defaire
    une session entiere, alors qu'en pratique on veut defaire UNE serie mal
    identifiee au milieu de sept cents deplacements corrects.
    """
    groups: dict[str, dict] = {}
    for record in records:
        key = work_key(record)
        group = groups.setdefault(
            key,
            {
                "key": key,
                "title": record.title or key,
                "work_kind": record.work_kind,
                "files": 0,
                "companions": 0,
                "operations": 0,
                "last_at": record.timestamp,
                "sample": record.destination,
            },
        )
        group["operations"] += 1
        if record.kind == "video":
            group["files"] += 1
        elif record.kind == "companion":
            group["companions"] += 1
        if record.timestamp > group["last_at"]:
            group["last_at"] = record.timestamp
            group["sample"] = record.destination
        if not group["work_kind"] and record.work_kind:
            group["work_kind"] = record.work_kind

    return sorted(groups.values(), key=lambda g: g["last_at"], reverse=True)


def undo_work(journal: Journal, key: str) -> list[ApplyResult]:
    """Annule tout ce qui concerne UNE oeuvre, sans toucher au reste.

    Les operations d'une meme oeuvre sont defaites de la plus recente a la plus
    ancienne, pour la meme raison que l'annulation globale : deux deplacements
    peuvent avoir touche des chemins imbriques.
    """
    records = journal.read_all()
    to_undo = [r for r in records if work_key(r) == key]
    if not to_undo:
        return []
    remaining = [r for r in records if work_key(r) != key]
    return _undo(journal, to_undo, remaining)


def undo_plans(journal: Journal, plan_ids: list[str]) -> list[ApplyResult]:
    """Annule le rangement de fichiers precis — un episode, un film.

    Le plan porte la video ET ses compagnons : annuler un episode remet aussi
    son sous-titre a sa place, sans quoi le retour arriere serait a moitie
    fait.
    """
    wanted = set(plan_ids)
    if not wanted:
        return []
    records = journal.read_all()
    to_undo = [r for r in records if r.plan_id in wanted]
    if not to_undo:
        return []
    remaining = [r for r in records if r.plan_id not in wanted]
    return _undo(journal, to_undo, remaining)


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
    return _undo(journal, to_undo, remaining)


def _undo(
    journal: Journal, to_undo: list[MoveRecord], remaining: list[MoveRecord]
) -> list[ApplyResult]:
    """Defait un lot d'operations et reecrit le journal.

    ``remaining`` est ce qui doit SUBSISTER : le calculer chez l'appelant
    permet d'annuler une selection au milieu du journal sans perdre l'ordre du
    reste.
    """
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
    undone = {(r.source, r.destination) for r in results if r.ok}
    kept = [r for r in to_undo if (r.destination, r.source) not in undone]
    # Remis dans l'ordre chronologique : les operations annulees pouvaient se
    # trouver n'importe ou dans le journal, pas seulement a la fin.
    journal.rewrite(sorted(remaining + kept, key=lambda r: r.timestamp))

    return results
