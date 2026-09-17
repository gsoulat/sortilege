"""L'index de bibliotheque doit suivre le disque apres un retrait.

Signale par l'utilisateur : « quand je clique sur supprimer cela doit remettre
a jour la mediatheque pour etre sur que cela soit supprime ». L'index etait
fige jusqu'a la prochaine lecture complete : l'ecran continuait d'annoncer
« 2 fichiers, 1 doublon » apres une suppression reussie, et le meme bouton
proposait de supprimer un fichier deja parti.

Relire toute la bibliotheque apres chaque geste serait exact mais couteux —
plusieurs minutes sur un NAS. Ce qui se deduit des fichiers restants est donc
recalcule sur place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.api import collection as api_collection
from sortilege.api.collection import (
    DeleteDuplicatesRequest,
    DuplicateGroupIn,
    PruneDuplicatesRequest,
    TrashRequest,
    current_works,
    delete_duplicates,
    prune_duplicates,
    trash_duplicates,
)
from sortilege.config import get_settings
from sortilege.core.collection import SeasonHolding, forget_files, group
from sortilege.core.parser import parse
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile

GB = 1024**3


def scanne(relative: str, *, size: int = 2 * GB) -> ScannedFile:
    chemin = Path("/media") / relative
    return ScannedFile(
        path=chemin,
        size_bytes=size,
        parsed=parse(chemin, list(Path(relative).parts[:-1])),
        probe=FileProbe(),
        relative_path=relative,
    )


# --- Le recalcul, en logique pure -------------------------------------------


def test_un_doublon_retire_n_est_plus_un_doublon() -> None:
    works = forget_files(
        group([scanne("Film.2024.2160p.mkv", size=8 * GB), scanne("Film.2024.1080p.mkv")]),
        ["Film.2024.1080p.mkv"],
    )

    assert len(works) == 1
    assert works[0].duplicates == []
    assert works[0].file_count == 1
    assert works[0].total_bytes == 8 * GB


def test_une_oeuvre_dont_tous_les_fichiers_partent_disparait() -> None:
    works = forget_files(group([scanne("Film.2024.1080p.mkv")]), ["Film.2024.1080p.mkv"])

    assert works == []


def test_les_autres_oeuvres_ne_bougent_pas() -> None:
    works = forget_files(
        group([scanne("Film.2024.1080p.mkv"), scanne("Severance/Severance.S01E01.mkv")]),
        ["Film.2024.1080p.mkv"],
    )

    assert [w.title for w in works] == ["Severance"]
    assert works[0].file_count == 1


def test_un_chemin_inconnu_ne_touche_a_rien() -> None:
    works = forget_files(group([scanne("Film.2024.1080p.mkv")]), ["Films/Jamais vu.mkv"])

    assert len(works) == 1 and works[0].file_count == 1


def test_sans_chemin_l_index_est_rendu_tel_quel() -> None:
    avant = group([scanne("Film.2024.1080p.mkv")])

    assert forget_files(avant, []) is avant


# --- Les routes, qui doivent le declencher ----------------------------------


@pytest.fixture
def bibliotheque(tmp_path: Path, monkeypatch) -> Path:
    """Deux exemplaires d'un meme film, sur le disque ET dans l'index."""
    racine = tmp_path / "media"
    (racine / "Films").mkdir(parents=True)
    for nom, octets in (("Film.2024.2160p.mkv", 8000), ("Film.2024.1080p.mkv", 2000)):
        (racine / "Films" / nom).write_bytes(b"x" * octets)

    conf = get_settings()
    monkeypatch.setattr(conf, "library_root", racine)
    monkeypatch.setattr("sortilege.api.collection.get_settings", lambda: conf)

    index = group(
        [
            scanne("Films/Film.2024.2160p.mkv", size=8000),
            scanne("Films/Film.2024.1080p.mkv", size=2000),
        ]
    )
    monkeypatch.setattr(api_collection._job, "works", index)
    assert len(current_works()[0].duplicates) == 1, "l'index part bien avec un doublon"
    return racine


def test_une_suppression_reussie_sort_le_fichier_de_l_index(bibliotheque: Path) -> None:
    sortie = delete_duplicates(
        DeleteDuplicatesRequest(
            groups=[
                DuplicateGroupIn(
                    keep="Films/Film.2024.2160p.mkv", paths=["Films/Film.2024.1080p.mkv"]
                )
            ],
            confirm=True,
        )
    )

    assert sortie["deleted"] == 1
    work = current_works()[0]
    assert work.duplicates == []
    assert work.file_count == 1
    assert work.total_bytes == 8000


def test_une_mise_en_corbeille_sort_aussi_le_fichier_de_l_index(bibliotheque: Path) -> None:
    sortie = trash_duplicates(TrashRequest(paths=["Films/Film.2024.1080p.mkv"]))

    assert sortie["trashed"] == 1
    assert current_works()[0].file_count == 1


def test_un_echec_laisse_l_index_intact(bibliotheque: Path) -> None:
    """Le fichier a garder est absent : rien n'est supprime, donc rien ne doit
    quitter l'index — un index amputé ferait disparaitre de l'ecran un fichier
    toujours present."""
    sortie = delete_duplicates(
        DeleteDuplicatesRequest(
            groups=[DuplicateGroupIn(keep="Films/Absent.mkv", paths=["Films/Film.2024.1080p.mkv"])],
            confirm=True,
        )
    )

    assert sortie["deleted"] == 0
    assert current_works()[0].file_count == 2
    assert (bibliotheque / "Films" / "Film.2024.1080p.mkv").is_file()


def test_le_nettoyage_d_ensemble_laisse_un_index_sans_doublon(bibliotheque: Path) -> None:
    sortie = prune_duplicates(PruneDuplicatesRequest(confirm=True))

    assert sortie["deleted"] == 1
    assert current_works()[0].duplicates == []
    assert not (bibliotheque / "Films" / "Film.2024.1080p.mkv").exists()
    assert (bibliotheque / "Films" / "Film.2024.2160p.mkv").is_file(), "la strategie garde le 2160p"


def test_le_nettoyage_par_corbeille_vide_aussi_les_doublons(bibliotheque: Path) -> None:
    sortie = prune_duplicates(PruneDuplicatesRequest(confirm=True, trash=True))

    assert sortie["deleted"] == 1
    assert current_works()[0].duplicates == []
    assert not (bibliotheque / "Films" / "Film.2024.1080p.mkv").exists()


# --- Ce qui a ete retire ne doit pas revenir ---------------------------------


async def test_une_suppression_pendant_la_lecture_n_est_pas_effacee(
    bibliotheque: Path, monkeypatch
) -> None:
    """Le cas le plus probable : on lance « Lire la bibliothèque », puis on
    nettoie ses doublons pendant que le fournisseur repond. La lecture rendait
    la main, puis ecrasait l'index avec sa photo d'AVANT : le doublon supprime
    reapparaissait, et le clic suivant repondait « fichier introuvable »."""
    photo = [
        scanne("Films/Film.2024.2160p.mkv", size=8000),
        scanne("Films/Film.2024.1080p.mkv", size=2000),
    ]

    class Resultat:
        files = photo

    monkeypatch.setattr(api_collection, "scan", lambda *a, **k: Resultat())
    monkeypatch.setattr(api_collection._job, "running", True)

    async def pendant_l_enrichissement(works: list, tmdb: object) -> None:
        prune_duplicates(PruneDuplicatesRequest(confirm=True))

    monkeypatch.setattr(api_collection, "_enrich", pendant_l_enrichissement)

    await api_collection._build()

    work = current_works()[0]
    assert work.duplicates == [], "le doublon supprime ne revient pas"
    assert work.file_count == 1
    assert not (bibliotheque / "Films" / "Film.2024.1080p.mkv").exists()


async def test_la_lecture_suivante_repart_d_une_photo_neuve(
    bibliotheque: Path, monkeypatch
) -> None:
    """Le retrait ne doit pas etre rejoue indefiniment : un fichier revenu
    dans la bibliotheque doit reapparaitre."""
    prune_duplicates(PruneDuplicatesRequest(confirm=True))
    (bibliotheque / "Films" / "Film.2024.1080p.mkv").write_bytes(b"x" * 2000)

    photo = [
        scanne("Films/Film.2024.2160p.mkv", size=8000),
        scanne("Films/Film.2024.1080p.mkv", size=2000),
    ]

    class Resultat:
        files = photo

    monkeypatch.setattr(api_collection, "scan", lambda *a, **k: Resultat())
    await api_collection._build()

    assert current_works()[0].file_count == 2


# --- La corbeille en lot a les memes garde-fous que la suppression ----------


def test_la_corbeille_en_lot_refuse_si_l_exemplaire_garde_a_disparu(
    bibliotheque: Path,
) -> None:
    """L'index peut dater. Si l'exemplaire a garder a ete efface entre-temps
    par un autre outil, evacuer le dernier restant laisserait l'oeuvre sans
    aucun fichier — en silence, d'un seul clic."""
    (bibliotheque / "Films" / "Film.2024.2160p.mkv").unlink()

    sortie = prune_duplicates(PruneDuplicatesRequest(confirm=True, trash=True))

    assert sortie["deleted"] == 0
    assert sortie["failed"] == 1
    assert (bibliotheque / "Films" / "Film.2024.1080p.mkv").is_file()
    assert current_works()[0].file_count == 2, "rien n'a quitte l'index non plus"


# --- Les saisons suivent ce qui reste ---------------------------------------


def test_un_episode_retire_n_est_plus_compte_comme_possede() -> None:
    works = group(
        [
            scanne("Severance/Severance.S01E01.mkv"),
            scanne("Severance/Severance.S01E02.mkv"),
        ]
    )
    works[0].seasons[1].known = {1: "Bonjour", 2: "Au revoir"}

    restants = forget_files(works, ["Severance/Severance.S01E02.mkv"])

    saison = restants[0].seasons[1]
    assert saison.owned == {1}
    assert saison.missing == [2], "l'episode efface redevient un manque"
    assert saison.complete is False


def test_un_fichier_double_rend_les_deux_episodes() -> None:
    works = group([scanne("Severance/Severance.S01E01-E02.mkv")])
    assert works[0].seasons[1].owned == {1, 2}

    restants = forget_files(works, ["Severance/Severance.S01E03.mkv"])

    assert restants[0].seasons[1].owned == {1, 2}, "un chemin inconnu ne retire rien"


def test_une_saison_sans_numero_reste_telle_quelle() -> None:
    """Rien dans les emplacements ne permettrait de la reconstruire : la faire
    disparaitre serait pire que de la laisser."""
    works = group([scanne("Severance/Severance.S01E01.mkv")])
    works[0].seasons[2] = SeasonHolding(number=2, known={1: "inconnu ici"})

    restants = forget_files(works, ["Severance/Severance.S01E01.mkv"])

    assert restants == [], "plus aucun fichier : l'oeuvre disparait"
