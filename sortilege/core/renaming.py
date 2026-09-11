"""Remise en conformite de ce qui est DEJA range.

Un gabarit change, une franchise se decouvre, un titre se corrige : la
bibliotheque garde alors des noms produits par une regle qui n'a plus cours.
Rien ne les rattrapait — le pipeline ignore deliberement les fichiers deja
ranges, sans quoi chaque scan proposerait de deplacer X vers X.

Ce module fait l'inverse : il ne regarde QUE ce qui est deja en place, et ne
propose que les fichiers dont le nom differerait s'ils etaient ranges
aujourd'hui.

Trois precautions, parce qu'un renommage de masse sur une bibliotheque
constituee est l'operation la plus risquee de l'application :

1. **Aucune identification n'est refaite.** On repart de ce que le fichier dit
   deja de lui-meme. Reinterroger un fournisseur ferait courir le risque qu'une
   mauvaise reponse renomme des fichiers corrects — le scenario exact que la
   validation par score existe pour eviter.

   Consequence a connaitre : ce mode corrige la STRUCTURE, pas l'identite. Un
   fichier range sous « severance.s01e01 » ressortira « severance - S01E01 »,
   avec sa minuscule : seul le fournisseur connait la casse officielle, et on
   ne l'interroge pas. Pour corriger une identite, il faut repasser par la file
   de revue.
2. **Ce qui ne change pas n'est pas propose.** La liste ne contient que de
   vraies differences, sinon elle serait illisible et personne ne la relirait.
3. **C'est un plan comme un autre.** Meme journal, meme annulation, meme refus
   d'ecraser. Un renommage de masse doit se defaire aussi facilement qu'il se
   fait.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .companions import find_companions
from .planner import Plan, build_values, plan_id_for
from .safety import PathConfinementError, resolve_within
from .scanner import ScannedFile
from .scoring import Decision
from .template import TemplateError, render, validate

logger = logging.getLogger(__name__)

RENAME_MARKER = "_mise_en_conformite"
"""Cle posee dans ``Plan.values`` : ce plan vient de la remise en conformite.

Une marque explicite plutot qu'une deduction. Un plan de renommage n'a ni
fournisseur ni valeurs de gabarit — mais un livre non plus, ni un plan
construit a la main, et tous deux doivent garder leur fiche. Le score et les
motifs ne tiennent pas davantage : la confirmation dans la file de revue les
reecrit. ``values`` est le seul champ que la confirmation ne touche pas et que
l'instantane restitue apres un redemarrage. Aucun lecteur de ``values`` ne
cherche cette cle : elle ne change rien au rendu ni aux fiches."""


def rename_plans(
    files: list[ScannedFile],
    *,
    template: str,
    destination_root: Path,
) -> list[Plan]:
    """Plans de renommage pour des fichiers deja ranges.

    Ne renvoie que ceux dont la destination differe de l'emplacement actuel.
    Les fichiers conformes sont silencieusement ecartes : les afficher pour
    dire « rien a faire » noierait les quelques lignes qui comptent.
    """
    try:
        validate(template)
    except TemplateError as exc:
        logger.warning("gabarit invalide, aucun renommage propose : %s", exc)
        return []

    plans: list[Plan] = []

    for scanned in files:
        try:
            # ``None`` en correspondance : on ne repart QUE de ce que le fichier
            # dit deja de lui-meme. Le gabarit est rendu tel quel, sans qu'aucun
            # fournisseur soit interroge.
            relative = render(template, build_values(scanned, None))
            destination = resolve_within(destination_root, relative)
        except (TemplateError, PathConfinementError) as exc:
            logger.warning("renommage impossible pour %s : %s", scanned.path.name, exc)
            continue

        destination = destination.with_suffix(scanned.path.suffix)
        if destination == scanned.path:
            # Deja conforme. L'afficher pour dire « rien a faire » noierait les
            # quelques lignes qui comptent.
            continue

        plans.append(
            Plan(
                id=plan_id_for(scanned.path),
                source=scanned.path,
                destination=destination,
                kind=str(scanned.parsed.kind),
                # Le score n'a aucun sens ici : rien n'a ete identifie. Marquer
                # ces plans AUTO serait mentir sur leur nature — ils sont
                # proposes, et c'est l'humain qui tranche.
                score=0.0,
                decision=Decision.REVIEW,
                reasons=["mise en conformite avec le gabarit courant"],
                title=scanned.parsed.title,
                year=scanned.parsed.year,
                manual=True,
                # Rien n'a ete identifie : aucune fiche ne doit etre deposee
                # a l'application (voir RENAME_MARKER).
                values={RENAME_MARKER: True},
                # Sous-titres et fiche suivent ; aucun reste a evacuer, ces fichiers
                # sont deja en bibliotheque et rien de la release ne les entoure.
                companions=_companions_for(scanned.path, destination),
            )
        )

    return plans


def _companions_for(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    """Sous-titres, pistes annexes et fiche, renommes avec la video.

    Les laisser derriere transformerait un renommage en perte : le fichier
    partirait sous son nouveau nom, et son sous-titre resterait accroche a
    l'ancien, invisible pour le lecteur.

    La fiche au nom de la video (« Film.nfo ») suit aussi. Restee derriere,
    elle ne decrirait plus aucun fichier, et l'identifiant qu'elle portait
    serait perdu pour la video : ce mode n'interroge personne, il n'aurait
    aucun moyen de le retrouver.
    """
    pairs = [
        (companion.path, companion.destination_for(destination))
        for companion in find_companions(source, artwork=False)
    ]
    fiche = source.with_suffix(".nfo")
    if fiche.is_file():
        pairs.append((fiche, destination.with_suffix(".nfo")))
    return pairs


def summarize(plans: list[Plan]) -> dict[str, int]:
    """De quoi annoncer l'ampleur avant de lancer quoi que ce soit."""
    return {
        "to_rename": len(plans),
        "works": len({p.title for p in plans if p.title}),
    }
