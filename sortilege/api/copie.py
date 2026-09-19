"""Copier des oeuvres de la mediatheque vers un disque externe USB : la couche HTTP.

Le travail vit dans ``core/copie.py``. Ici : resoudre le disque demande — un
identifiant venu du client n'est accepte que s'il designe un disque detecte a
l'instant, jamais utilise comme chemin —, refuser en 409 ce que l'etat du
moment interdit, et garantir qu'une seule copie tourne dans tout le processus.
Deux copies simultanees vers le meme port USB iraient moins vite qu'une seule,
et deux vers le meme disque se disputeraient la place annoncee libre.

Le demarrage REFAIT toujours l'analyse : une analyse envoyee par le client
serait un plan d'ecriture fourni par le navigateur, et le disque a pu changer
entre les deux clics. Le client n'envoie que l'identifiant du disque, les cles
des oeuvres et ses options.

Un disque se designe de deux facons. Pendant une requete, par son
identifiant sous la racine externe, detecte a l'instant. D'une copie a sa
reprise, par l'identifiant aleatoire de son fichier-marqueur
(``.sortilege-disque``, voir ``moteur.marquer_disque``) : rebranche sur un
autre port, un disque change de nom de montage et de numero de peripherique,
et un autre disque peut prendre sa place. La reprise cherche donc le disque
qui porte le marqueur, ou qu'il soit monte, et refuse s'il n'y en a aucun —
jamais de repli silencieux vers le disque qui occupe l'ancien emplacement.

Une copie interrompue passe avant toute nouvelle copie : tant qu'elle est
sauvegardee, ``start`` repond 409. Une nouvelle copie ecrasait son etat en
silence, et son ``.sortilege-part`` devenait un orphelin que plus rien ne
designait.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..core import copie as moteur
from ..core.collection import Work
from . import collection as index
from . import deps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/copy", tags=["copy"])

_verrou = threading.Lock()
_copie: moteur.Copie | None = None
_demarrage = False

DUREE_DISPONIBILITE = 5.0
"""Secondes pendant lesquelles on se fie a la derniere detection du disque a
reprendre. L'etat se lit par sondage : refaire l'inventaire des disques a
chaque sondage reveillerait le disque USB toutes les secondes pour rien."""

_disponibilite: tuple[float, str, str | None] | None = None
"""(instant, marqueur, disque qui le porte) : la derniere recherche du disque
d'une copie interrompue. ``/disks`` l'invalide — « Relire les disques » doit
dire ce qu'on vient de brancher, pas ce qu'on a vu il y a cinq secondes."""


class AnalyseRequest(BaseModel):
    disk: str
    """Identifiant du disque, tel que ``GET /api/copy/disks`` le donne."""

    works: list[str] = Field(default_factory=list)
    """Cles des oeuvres, telles que ``GET /api/copy/library`` les donne."""

    sidecars: bool = True
    """Copier aussi sous-titres, fiches .nfo et affiches de chaque video."""


class StartRequest(AnalyseRequest):
    max_mb_per_s: float | None = Field(default=None, gt=0, le=100_000)
    """Debit maximal en Mo/s (1 Mo = 1024 x 1024 octets, comme partout dans
    l'application). Vide = aussi vite que le disque."""


class DiscardRequest(BaseModel):
    forget: bool = False
    """Oublier la copie interrompue alors que son disque est absent. Son
    ``.sortilege-part`` reste sur le disque, et la reponse le dit : c'est la
    seule issue quand le disque ne reviendra pas, sans quoi aucune nouvelle
    copie ne pourrait partir. Refuse si le disque est branche : « Oublier »
    ne supprime JAMAIS rien, et laisser un fichier qu'on peut retirer n'est
    pas ce que l'utilisateur a confirme."""


REFUS_REPRISE_EN_ATTENTE = "Une copie interrompue attend : reprends-la ou abandonne-la d'abord."

DELAI_EXTINCTION = 8.0
"""Secondes accordees a la copie pour s'arreter proprement quand l'application
s'eteint (dernier ``fsync`` et point de controle). Docker envoie SIGKILL dix
secondes apres SIGTERM : au-dela, le point de controle serait perdu avec le
processus, et la reprise repartirait du precedent."""


def _fichier_reprise() -> Path:
    # Relu a chaque appel, et non fige a l'import : les tests detournent
    # ``deps.DATA_DIR`` avant toute utilisation.
    return deps.DATA_DIR / moteur.FICHIER_REPRISE


def _inventaire() -> moteur.Inventaire:
    conf = get_settings()
    return moteur.inventorier(conf.external_root, mediatheque=conf.library_root)


def _indice(inventaire: moteur.Inventaire) -> str | None:
    racine = inventaire.racine
    if not inventaire.existe:
        return (
            f"Le dossier « {racine} » n'existe pas dans le conteneur : monte le dossier "
            f"parent de tes disques USB dans un sous-dossier, par exemple « {racine}/usb », "
            "avec « :rslave » (voir docker-compose.yml et SORTILEGE_EXTERNAL_ROOT)."
        )
    if not any(d.refusal is None for d in inventaire.disques):
        return (
            f"Aucun disque monté sous « {racine} ». Branche le disque ; s'il a été branché "
            "après le démarrage du conteneur, il n'apparaît que si le montage porte « :rslave »."
        )
    return None


def _disque(ident: str) -> moteur.Disque:
    """Le disque designe, detecte A L'INSTANT. 404 pour tout le reste."""
    racine = get_settings().external_root
    if not moteur.identifiant_valide(ident):
        raise HTTPException(404, f"Disque inconnu : « {ident} ».")
    for disque in _inventaire().disques:
        if disque.id == ident:
            return disque
    raise HTTPException(404, f"Disque inconnu : « {ident} » n'est pas (ou plus) sous « {racine} ».")


def _disque_utilisable(ident: str) -> moteur.Disque:
    disque = _disque(ident)
    if disque.refusal:
        raise HTTPException(409, f"Disque « {disque.label} » refusé : {disque.refusal}")
    return disque


def _oeuvres(cles: list[str]) -> list[Work]:
    if index.status()["built_at"] is None:
        raise HTTPException(
            409,
            "La médiathèque n'a pas encore été lue : lance « Relire la médiathèque », "
            "puis choisis les œuvres à copier.",
        )
    par_cle = {w.key: w for w in index.current_works()}
    uniques = list(dict.fromkeys(cles))
    inconnues = [c for c in uniques if c not in par_cle]
    if inconnues:
        extrait = ", ".join(inconnues[:5]) + (" …" if len(inconnues) > 5 else "")
        raise HTTPException(
            404, f"Œuvre(s) absente(s) de l'index de la médiathèque : {extrait}. Relis-la."
        )
    return [par_cle[c] for c in uniques]


def _reprise_pour(disque: moteur.Disque) -> dict[str, Any] | None:
    """Le fichier en cours de la copie sauvegardee, s'il concerne CE disque."""
    etat = moteur.lire_reprise(_fichier_reprise())
    if etat is None or etat["disk"] != disque.id:
        return None
    return etat.get("current")


def _en_cours() -> bool:
    return _demarrage or (_copie is not None and _copie.en_cours)


@contextmanager
def _reserver() -> Iterator[None]:
    """Une seule copie (ou un seul demarrage) a la fois, dans tout le processus."""
    global _demarrage
    with _verrou:
        if _en_cours():
            raise HTTPException(
                409, "Une copie est déjà en cours : attends qu'elle se termine, ou arrête-la."
            )
        _demarrage = True
    try:
        yield
    finally:
        with _verrou:
            _demarrage = False


def _analyser(disque: moteur.Disque, oeuvres: list[Work], sidecars: bool) -> moteur.Analyse:
    return moteur.analyser(
        disque,
        oeuvres,
        get_settings().library_root,
        avec_compagnons=sidecars,
        regles=deps.scan_rules(),
    )


def _lancer(
    disque: moteur.Disque,
    oeuvres: list[Work],
    analyse: moteur.Analyse,
    *,
    sidecars: bool,
    max_mb_per_s: float | None,
    reprise: dict[str, Any] | None,
    marque: str | None,
) -> None:
    """Lance la copie. ``marque`` : l'identifiant du disque a reprendre, ou None
    pour une nouvelle copie — il est alors lu sur le disque, ou pose."""
    global _copie
    if not disque.writable:
        raise HTTPException(
            409,
            f"Le disque « {disque.label} » n'est pas accessible en écriture pour Sortilège "
            f"(identité {os.geteuid()}:{os.getegid()}).",
        )
    if not analyse.tient:
        raise HTTPException(409, analyse.motif_place())
    if marque is None:
        # Pose AVANT la premiere ecriture, et seulement une fois la copie
        # decidee : un disque refuse (place, droits) ne recoit rien, pas meme
        # ce petit fichier.
        try:
            marque = moteur.marquer_disque(disque)
        except moteur.ErreurMarque as exc:
            raise HTTPException(
                409,
                f"Le disque « {disque.label} » n'a pas accepté son repère de copie "
                f"({moteur.MARQUEUR}) : {exc}. Rien n'a été copié.",
            ) from exc
    # Desormais exige a chaque ouverture de la racine, fil de copie compris.
    disque.marque = marque
    copie = moteur.Copie(
        disque,
        analyse.taches,
        max_mb_per_s=max_mb_per_s,
        fichier_reprise=_fichier_reprise(),
        instantane={
            "disk": disque.id,
            "disk_mark": marque,
            "works": moteur.instantane_oeuvres(oeuvres),
            "sidecars": sidecars,
        },
        reprise=reprise,
    )
    copie.lancer()
    with _verrou:
        _copie = copie
    logger.info(
        "copie vers %s lancée : %s fichier(s), %s",
        disque.label,
        len(analyse.taches),
        moteur.octets_lisibles(analyse.octets),
    )


def _porteurs(etat: dict[str, Any]) -> list[moteur.Disque]:
    """Les disques montes qui portent le marqueur de la copie ``etat``.

    Celui qui occupe toujours l'emplacement enregistre d'abord : c'est le cas
    courant. Chacun sort avec ``marque`` renseignee — toute ouverture de sa
    racine l'exigera desormais.
    """
    marque = etat["disk_mark"]
    utilisables = [d for d in _inventaire().disques if d.refusal is None]
    utilisables.sort(key=lambda d: d.id != etat["disk"])
    trouves: list[moteur.Disque] = []
    for disque in utilisables:
        if moteur.lire_marque(disque) == marque:
            disque.marque = marque
            trouves.append(disque)
    return trouves


def _disque_de_la_copie(etat: dict[str, Any]) -> moteur.Disque | None:
    """Le disque de la copie interrompue, ou None s'il n'est pas branche.

    Deux disques qui portent le meme marqueur (un disque clone) : 409. Choisir
    l'un des deux serait choisir au hasard ou reprendre.
    """
    global _disponibilite
    porteurs = _porteurs(etat)
    _disponibilite = (time.monotonic(), etat["disk_mark"], porteurs[0].id if porteurs else None)
    if len(porteurs) > 1:
        noms = ", ".join(f"« {d.label} »" for d in porteurs)
        raise HTTPException(
            409,
            f"Plusieurs disques branchés portent le repère de cette copie ({noms}) — un disque "
            "cloné ? Débranche ceux qui ne sont pas le disque de la copie, puis réessaie.",
        )
    return porteurs[0] if porteurs else None


def _disque_trouve(etat: dict[str, Any]) -> str | None:
    """L'identifiant du disque qui porte le marqueur de ``etat``, avec cache (sondage)."""
    global _disponibilite
    maintenant = time.monotonic()
    if (
        _disponibilite is not None
        and _disponibilite[1] == etat["disk_mark"]
        and maintenant - _disponibilite[0] < DUREE_DISPONIBILITE
    ):
        return _disponibilite[2]
    porteurs = _porteurs(etat)
    trouve = porteurs[0].id if porteurs else None
    _disponibilite = (maintenant, etat["disk_mark"], trouve)
    return trouve


def _refus_absent(etat: dict[str, Any], geste: str) -> HTTPException:
    """Aucun disque monte ne porte le marqueur : jamais de repli vers un autre."""
    return HTTPException(
        409,
        f"Le disque de cette copie (« {etat['disk']} ») n'est pas branché — aucun disque "
        f"monté ne porte son repère {moteur.MARQUEUR} : {geste}",
    )


def _reprenable() -> dict[str, object] | None:
    """Ce qu'une copie interrompue laisse a reprendre, ou None."""
    etat = moteur.lire_reprise(_fichier_reprise())
    if etat is None:
        return None
    courant = etat.get("current")
    trouve = _disque_trouve(etat)
    return {
        "disk": etat["disk"],
        "saved_at": etat.get("saved_at"),
        "works": [str(w["key"]) for w in etat["works"]],
        "files_remaining": int(etat.get("files_remaining") or 0),
        "bytes_remaining": int(etat.get("bytes_remaining") or 0),
        "current": {
            "target": courant["target"],
            "checkpoint_bytes": courant["checkpoint"],
            "bytes_total": courant["source_size"],
        }
        if courant
        else None,
        "disk_available": trouve is not None,
        "disk_found": trouve,
    }


def _etat() -> dict[str, object]:
    copie = _copie
    etat = copie.etat() if copie is not None else moteur.etat_au_repos()
    etat["resumable"] = None if _en_cours() else _reprenable()
    return etat


# --- Routes -------------------------------------------------------------------


@router.get("/disks")
def disks() -> dict[str, object]:
    """Les disques sous la racine externe, et les dossiers qui n'en sont pas.

    Invalide aussi la derniere recherche du disque d'une copie interrompue :
    c'est le geste « Relire les disques », fait justement apres avoir rebranche
    ce disque — le ``/status`` qui suit doit le voir.
    """
    global _disponibilite
    _disponibilite = None
    inventaire = _inventaire()
    return {
        "root": str(inventaire.racine),
        "root_exists": inventaire.existe,
        "disks": [d.to_dict() for d in inventaire.disques],
        "hint": _indice(inventaire),
    }


@router.get("/library")
def library() -> dict[str, object]:
    """Les oeuvres de la mediatheque, depuis l'index deja construit."""
    construit = index.status()["built_at"] is not None
    oeuvres = index.current_works() if construit else []
    return {
        "built": construit,
        "works": [
            {
                "key": w.key,
                "title": w.title,
                "year": w.year,
                "kind": w.kind,
                "files": w.file_count,
                "bytes": w.total_bytes,
                "poster_url": w.poster_url,
            }
            for w in oeuvres
        ],
    }


@router.post("/analyze")
def analyze(body: AnalyseRequest) -> dict[str, object]:
    """Ce qui est deja sur le disque, ce qui manque, ce qui coince. N'ecrit rien."""
    disque = _disque_utilisable(body.disk)
    analyse = _analyser(disque, _oeuvres(body.works), body.sidecars)
    analyse.crediter_reprise(_reprise_pour(disque))
    return analyse.to_dict()


@router.post("/start")
def start(body: StartRequest) -> dict[str, object]:
    """Lance la copie, apres une analyse refaite ici. 409 si elle ne peut pas partir."""
    with _reserver():
        if moteur.lire_reprise(_fichier_reprise()) is not None:
            raise HTTPException(409, REFUS_REPRISE_EN_ATTENTE)
        disque = _disque_utilisable(body.disk)
        oeuvres = _oeuvres(body.works)
        analyse = _analyser(disque, oeuvres, body.sidecars)
        if not analyse.taches:
            raise HTTPException(
                409,
                "Rien à copier : ce qui a été choisi est déjà sur le disque, ou signalé "
                "dans l'analyse.",
            )
        _lancer(
            disque,
            oeuvres,
            analyse,
            sidecars=body.sidecars,
            max_mb_per_s=body.max_mb_per_s,
            reprise=None,
            marque=None,
        )
    return {"started": True, **_etat()}


@router.post("/continue")
def continue_copy() -> dict[str, object]:
    """Reprend la copie sauvegardee, la ou elle s'etait arretee.

    Le disque est celui qui porte le marqueur enregistre, ou qu'il soit monte
    maintenant : rebranche sur un autre port, il est retrouve ; un autre disque
    a sa place n'est jamais pris pour lui. L'analyse est refaite : ce qui a ete
    termine est « present » et saute, et le fichier interrompu reprend a son
    point de controle — une fois verifies l'identite de la source, le dernier
    segment relu en entier et des echantillons ailleurs (voir
    ``Copie._depart``), jamais a l'aveugle.
    """
    with _reserver():
        fichier = _fichier_reprise()
        etat = moteur.lire_reprise(fichier)
        if etat is None:
            raise HTTPException(409, "Aucune copie à reprendre.")
        disque = _disque_de_la_copie(etat)
        if disque is None:
            raise _refus_absent(
                etat, "rebranche-le, puis « Relire les disques » pour reprendre la copie."
            )
        oeuvres = moteur.oeuvres_depuis_instantane(etat["works"])
        courant = etat.get("current")
        analyse = _analyser(disque, oeuvres, bool(etat["sidecars"]))
        analyse.crediter_reprise(courant)
        if not analyse.taches:
            if courant:
                retrait = moteur.retirer_partiel(disque, courant["part"])
                if not retrait.parti:
                    raise HTTPException(
                        409,
                        "Rien à reprendre : tout est déjà sur le disque. Mais son fichier "
                        f"partiel « {courant['part']} » n'a pas pu être retiré ({retrait.motif}) : "
                        "la copie interrompue reste affichée, pour que ce fichier ne reste pas "
                        "sur le disque sans que rien ne le désigne. Réessaie « Abandonner » une "
                        "fois le disque vérifié.",
                    )
            moteur.effacer_reprise(fichier)
            raise HTTPException(409, "Rien à reprendre : tout est déjà sur le disque.")
        _lancer(
            disque,
            oeuvres,
            analyse,
            sidecars=bool(etat["sidecars"]),
            max_mb_per_s=etat.get("max_mb_per_s"),
            reprise=courant,
            marque=etat["disk_mark"],
        )
    return {"started": True, **_etat()}


@router.post("/discard")
def discard(body: DiscardRequest | None = None) -> dict[str, object]:
    """Abandonne la copie sauvegardee : son ``.sortilege-part``, puis son etat.

    Ne supprime QUE le fichier partiel que l'etat designe, sur le disque qui
    porte le marqueur de la copie, atteint depuis sa racine sans suivre aucun
    lien (voir ``moteur.retirer_partiel``). L'etat n'est efface qu'une fois ce
    fichier parti — retire a l'instant, ou deja absent. Sinon (disque absent,
    fichier inatteignable, disque en lecture seule ou muet) : 409, l'etat est
    garde, et le motif dit pourquoi. L'effacer quand meme laissait un orphelin
    de plusieurs gigaoctets que plus rien ne designait.

    ``forget`` : oublier la copie d'un disque qui ne reviendra pas. Ne supprime
    rien, jamais ; refuse si le disque de la copie est branche. La reponse dit,
    dans ``not_removed``, ce qui est reste sur le disque.

    Reponse : ``removed`` (supprime a l'instant), ``absent`` (n'y etait deja
    plus), ``not_removed`` (laisse sur le disque, par ``forget``).
    """
    oublier = body is not None and body.forget
    with _reserver():
        reponse = _abandonner(oublier)
    return {**reponse, **_etat()}


def _abandonner(oublier: bool) -> dict[str, list[str]]:
    """Le travail de ``discard``, sous reservation. Voir sa docstring."""
    global _disponibilite
    rien: dict[str, list[str]] = {"removed": [], "absent": [], "not_removed": []}
    fichier = _fichier_reprise()
    etat = moteur.lire_reprise(fichier)
    if etat is None:
        return rien
    courant = etat.get("current")
    if not courant:
        moteur.effacer_reprise(fichier)
        return rien
    partiel = str(courant["part"])
    # Ce qu'on va voir vaut mieux que ce qu'on a vu : l'ecran relit l'etat
    # juste apres, et doit dire ce que cette requete a constate.
    _disponibilite = None
    disque = _disque_de_la_copie(etat)
    if oublier:
        if disque is not None:
            raise HTTPException(
                409,
                f"Le disque de cette copie est branché (« {disque.label} ») : « Oublier » y "
                "laisserait son fichier partiel alors qu'il peut être retiré. Reprends la "
                "copie, ou abandonne-la pour retirer ce fichier.",
            )
        moteur.effacer_reprise(fichier)
        return {**rien, "not_removed": [partiel]}
    if disque is None:
        raise _refus_absent(
            etat, "rebranche le disque pour qu'on puisse retirer son fichier partiel."
        )
    retrait = moteur.retirer_partiel(disque, partiel)
    if not retrait.parti:
        raise HTTPException(
            409,
            f"Le fichier partiel « {partiel} » n'a pas pu être retiré du disque "
            f"« {disque.label} » ({retrait.motif}) : la copie interrompue est gardée. "
            "Vérifie le disque puis réessaie ; s'il ne doit pas revenir, débranche-le et "
            "« Oublier cette copie ».",
        )
    moteur.effacer_reprise(fichier)
    if retrait.etat == moteur.RETIRE:
        return {**rien, "removed": [partiel]}
    return {**rien, "absent": [partiel]}


@router.get("/status")
def status() -> dict[str, object]:
    return _etat()


@router.post("/pause")
def pause() -> dict[str, object]:
    if _copie is not None:
        _copie.pause()
    return _etat()


@router.post("/resume")
def resume() -> dict[str, object]:
    if _copie is not None:
        _copie.reprendre()
    return _etat()


@router.post("/stop")
def stop() -> dict[str, object]:
    """Arrete apres un dernier point de controle : le ``.part`` et l'etat restent."""
    if _copie is not None:
        _copie.arreter()
    return _etat()


def arreter_a_l_extinction(delai: float = DELAI_EXTINCTION) -> bool:
    """A l'extinction de l'application : arrete la copie en cours, proprement.

    Le fil de copie est un demon : sans ceci, l'arret du conteneur le tuait en
    plein bloc, et la reprise repartait du point de controle PRECEDENT —
    jusqu'a 256 Mio a recopier, ou un fichier entier si le premier point
    n'etait pas encore pose. Arreter ici, c'est le meme geste que « Arrêter » :
    un dernier ``fsync``, un point de controle, et l'etat reste a reprendre.

    Borne par ``delai`` : un disque qui ne rend pas son ``fsync`` ne doit pas
    retenir l'extinction au-dela de ce que Docker accorde. Vrai si la copie
    s'est arretee (ou ne tournait pas).
    """
    copie = _copie
    if copie is None or not copie.en_cours:
        return True
    logger.info("extinction : arrêt de la copie vers %s", copie.disque.label)
    copie.arreter()
    if copie.attendre(delai):
        return True
    logger.warning(
        "extinction : la copie vers %s ne s'est pas arrêtée en %s s — la reprise partira "
        "du dernier point de contrôle enregistré",
        copie.disque.label,
        delai,
    )
    return False
