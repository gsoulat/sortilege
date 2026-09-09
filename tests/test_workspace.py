"""Vue unique : rapprocher les deux etats d'une meme oeuvre.

Trois ecrans montraient trois moities du meme objet. Les fusionner n'a de sens
que si une serie apparait sur UNE ligne — sinon la vue unique affiche « ce que
tu possedes » et « ce qui arrive » separement, et elle est pire que les ecrans
qu'elle remplace.

Or la meme oeuvre s'ecrit de trois facons selon d'ou on la regarde :

- rangee sur le disque : « Avatar Le dernier maitre de l'air », ecrit par nous
  et donc deja assaini de sa ponctuation ;
- en attente, avant identification : « Avatar The Last Airbender », le nom de
  la release ;
- une fois identifiee : « Avatar : Le dernier maitre de l'air », le titre du
  fournisseur avec ses deux-points.

Ces tests portent sur ce rapprochement. Le reste — compter, trier — n'est
interessant que parce qu'il en depend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.collection import Work
from sortilege.core.parser import parse
from sortilege.core.planner import Plan
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile
from sortilege.core.scoring import Decision
from sortilege.core.workspace import build, entry_key, summarize


def work(title: str, **kw) -> Work:
    base = dict(key=f"episode:{title.casefold()}", kind="episode", title=title, file_count=1)
    base.update(kw)
    return Work(**base)


def plan(title: str, *, decision=Decision.AUTO, source="/dl/x.mkv", **kw) -> Plan:
    base = dict(
        id=f"p-{title}-{source}",
        source=Path(source),
        destination=Path("/lib/x.mkv"),
        kind="episode",
        score=0.95,
        decision=decision,
        title=title,
    )
    base.update(kw)
    return Plan(**base)


def pending(name: str) -> ScannedFile:
    path = Path("/dl") / name
    return ScannedFile(
        path=path,
        size_bytes=2 * 1024**3,
        parsed=parse(path, []),
        probe=FileProbe(),
        relative_path=name,
    )


# --- La cle de rapprochement ------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Avatar : Le dernier maître de l'air", "Avatar Le dernier maitre de l'air"),
        ("Dark Matter", "dark  matter"),
        ("WALL·E", "WALL E"),
        ("Amélie", "Amelie"),
    ],
)
def test_deux_ecritures_d_un_meme_titre_se_rejoignent(a, b) -> None:
    assert entry_key(title=a) == entry_key(title=b)


def test_deux_oeuvres_differentes_restent_distinctes() -> None:
    assert entry_key(title="Dark Matter") != entry_key(title="Dune")


def test_l_identifiant_du_fournisseur_prime() -> None:
    """Entre « Dark Matter » 2015 et 2024, le titre ne suffit pas — et c'est
    exactement le cas qui a motive toute la file d'arbitrage."""
    a = entry_key(provider="tmdb", external_id="1", title="Dark Matter")
    b = entry_key(provider="tmdb", external_id="2", title="Dark Matter")
    assert a != b


def test_un_titre_vide_ne_matche_pas_tout() -> None:
    """Une cle vide rassemblerait toutes les oeuvres non identifiees en une."""
    assert entry_key(title="") == "?"
    assert entry_key(title="   ") == "?"


# --- Le rapprochement, en situation -----------------------------------------


def test_une_serie_possedee_et_en_attente_tient_sur_une_ligne() -> None:
    """LA propriete qui justifie la fusion des ecrans."""
    entries = build(
        [work("Avatar Le dernier maitre de l'air", file_count=6)],
        [plan("Avatar : Le dernier maître de l'air", provider="tmdb", external_id="95396")],
        [],
    )

    assert len(entries) == 1
    assert entries[0].owned.file_count == 6
    assert len(entries[0].pending.ready) == 1


def test_le_titre_de_la_bibliotheque_fait_foi() -> None:
    """C'est celui qu'on a ecrit nous-memes, donc celui qu'on lit sur le
    disque : afficher autre chose obligerait a traduire mentalement."""
    entries = build(
        [work("Avatar Le dernier maitre de l'air")],
        [plan("Avatar : Le dernier maître de l'air")],
        [],
    )
    assert entries[0].title == "Avatar Le dernier maitre de l'air"


def test_un_plan_donne_son_titre_a_une_oeuvre_non_possedee() -> None:
    entries = build([], [plan("Severance")], [])
    assert entries[0].title == "Severance"
    assert entries[0].is_new is True


def test_deux_plans_de_la_meme_serie_se_regroupent() -> None:
    entries = build(
        [],
        [
            plan("Severance", source="/dl/e1.mkv", provider="tmdb", external_id="95396"),
            plan("Severance", source="/dl/e2.mkv", provider="tmdb", external_id="95396"),
        ],
        [],
    )
    assert len(entries) == 1
    assert len(entries[0].pending.ready) == 2


def test_un_plan_identifie_rejoint_l_oeuvre_deja_vue_sous_son_titre() -> None:
    """Le premier plan cree la ligne sous son titre ; le suivant arrive avec un
    identifiant de fournisseur et doit tomber dans la meme."""
    entries = build(
        [work("Severance", file_count=2)],
        [
            plan("Severance", source="/dl/e1.mkv"),
            plan("Severance", source="/dl/e2.mkv", provider="tmdb", external_id="95396"),
        ],
        [],
    )
    assert len(entries) == 1
    assert len(entries[0].pending.ready) == 2


# --- Les trois etats coexistent ---------------------------------------------


def test_les_decisions_sont_separees() -> None:
    entries = build(
        [],
        [
            plan("Severance", source="/dl/a.mkv", decision=Decision.AUTO),
            plan("Severance", source="/dl/b.mkv", decision=Decision.REVIEW),
            plan("Severance", source="/dl/c.mkv", decision=Decision.REJECT),
        ],
        [],
    )
    p = entries[0].pending
    assert (len(p.ready), len(p.review), len(p.rejected)) == (1, 1, 1)


def test_un_fichier_sans_plan_apparait_quand_meme() -> None:
    """C'est ce qui permet l'affichage progressif : la ligne existe des le
    scan, et se precise quand son plan arrive."""
    entries = build([], [], [pending("Dune.2024.1080p.mkv")])

    assert entries[0].title == "Dune"
    assert entries[0].pending.unplanned
    assert entries[0].pending.total == 1


# --- Ordre et compteurs -----------------------------------------------------


def test_ce_qui_demande_une_action_passe_devant() -> None:
    """L'ordre alphabetique enterrerait les quelques lignes actionnables sous
    des centaines de lignes inertes."""
    entries = build(
        [work("Zoulou"), work("Alpha")],
        [plan("Zoulou", decision=Decision.AUTO)],
        [],
    )
    assert entries[0].title == "Zoulou"


def test_les_compteurs_portent_sur_tout() -> None:
    """La barre d'action annonce « Executer 42 prets » : si le compte ne
    portait que sur la page affichee, le bouton mentirait."""
    entries = build(
        [work("Severance", file_count=6)],
        [
            plan("Severance", source="/dl/a.mkv", decision=Decision.AUTO),
            plan("Dune", source="/dl/b.mkv", decision=Decision.REVIEW),
        ],
        [pending("Autre.Chose.2020.mkv")],
    )
    counts = summarize(entries)

    assert counts["ready"] == 1
    assert counts["review"] == 1
    assert counts["unplanned"] == 1
    assert counts["owned_files"] == 6
    assert counts["works"] == 3


def test_une_mediatheque_vide_ne_leve_pas() -> None:
    assert build([], [], []) == []
    assert summarize([])["works"] == 0
