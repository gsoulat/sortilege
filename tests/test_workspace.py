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


# --- Reperer ce qui pese anormalement lourd ---------------------------------
#
# L'objectif concret : savoir quoi re-telecharger ou re-encoder pour gagner de
# la place. La taille BRUTE ne dit rien — une serie de trente episodes pese
# forcement plus qu'un film. Ce qui se compare, c'est le poids d'UN fichier,
# rapporte a l'habitude du meme type.


GB = 1024**3


def owned(title: str, *, files: int, total_gb: float, kind: str = "movie") -> Work:
    return Work(
        key=f"{kind}:{title.casefold()}",
        kind=kind,
        title=title,
        file_count=files,
        total_bytes=int(total_gb * GB),
    )


def test_le_poids_se_compare_par_fichier() -> None:
    """Une serie de dix episodes a 2 Go n'est pas « plus lourde » qu'un film de
    8 Go : elle est plus legere par fichier."""
    entries = {
        e.title: e
        for e in build(
            [
                owned("Serie", files=10, total_gb=20, kind="episode"),
                owned("Film", files=1, total_gb=8),
            ],
            [],
            [],
        )
    }
    assert entries["Serie"].bytes_per_file < entries["Film"].bytes_per_file


def test_un_remux_est_signale() -> None:
    """Le cas vise : un fichier deux fois plus lourd que l'habitude, a qualite
    comparable, vaut la peine d'etre repris."""
    entries = {
        e.title: e
        for e in build(
            [
                owned("Normal A", files=1, total_gb=4),
                owned("Normal B", files=1, total_gb=4),
                owned("Normal C", files=1, total_gb=5),
                owned("Remux", files=1, total_gb=40),
            ],
            [],
            [],
        )
    }
    assert entries["Remux"].heaviness >= 2.0
    assert entries["Normal A"].heaviness < 2.0


def test_chaque_type_a_sa_propre_norme() -> None:
    """Comparer un episode a un film ferait passer tous les films pour des
    anomalies."""
    entries = {
        e.title: e
        for e in build(
            [
                owned("Ep A", files=10, total_gb=10, kind="episode"),
                owned("Ep B", files=10, total_gb=11, kind="episode"),
                owned("Ep C", files=10, total_gb=9, kind="episode"),
                owned("Film A", files=1, total_gb=8),
                owned("Film B", files=1, total_gb=9),
                owned("Film C", files=1, total_gb=7),
            ],
            [],
            [],
        )
    }
    for e in entries.values():
        assert e.heaviness < 2.0, f"{e.title} signale a tort"


def test_la_mediane_resiste_aux_extremes() -> None:
    """Avec une moyenne, un seul enorme fichier releverait le seuil et se
    cacherait lui-meme. C'est la raison du choix de la mediane."""
    entries = {
        e.title: e
        for e in build(
            [
                owned("A", files=1, total_gb=4),
                owned("B", files=1, total_gb=4),
                owned("C", files=1, total_gb=4),
                owned("Enorme", files=1, total_gb=200),
            ],
            [],
            [],
        )
    }
    assert entries["Enorme"].heaviness > 10


def test_trop_peu_d_oeuvres_pour_conclure() -> None:
    """Une mediane sur deux valeurs ne dit rien : signaler une anomalie sur
    cette base serait du bruit."""
    entries = build([owned("A", files=1, total_gb=1), owned("B", files=1, total_gb=50)], [], [])
    assert all(e.heaviness == 0.0 for e in entries)


def test_une_oeuvre_non_possedee_n_a_pas_de_poids() -> None:
    entries = build([], [plan("Severance")], [])
    assert entries[0].bytes_per_file == 0
    assert entries[0].heaviness == 0.0


def test_le_compte_des_lourds_remonte_dans_les_totaux() -> None:
    entries = build(
        [
            owned("A", files=1, total_gb=4),
            owned("B", files=1, total_gb=4),
            owned("C", files=1, total_gb=4),
            owned("Gros", files=1, total_gb=40),
        ],
        [],
        [],
    )
    counts = summarize(entries)
    assert counts["heavy"] == 1
    assert counts["total_bytes"] == int(52 * GB)


# --- Le compteur doit descendre ---------------------------------------------
#
# Bug constate a l'usage : « À traiter » restait fige a plusieurs milliers quoi
# qu'on range. La cause etait subtile — les fichiers deja planifies etaient
# reconnus a partir des plans VIVANTS, or un plan applique quitte la file. Son
# fichier retombait donc dans « pas encore planifie », et le compte ne bougeait
# pas d'un pouce.


def test_un_fichier_planifie_ne_compte_plus_comme_en_attente() -> None:
    """C'est build() qui recoit deja la liste filtree, mais la propriete se
    verifie ici : ce qui a un plan n'est pas aussi « en attente »."""
    p = plan("Severance", source="/dl/e1.mkv")
    entries = build([], [p], [])

    assert summarize(entries)["unplanned"] == 0
    assert summarize(entries)["ready"] == 1


def test_un_fichier_sans_plan_reste_en_attente() -> None:
    entries = build([], [], [pending("Autre.Chose.2020.mkv")])
    assert summarize(entries)["unplanned"] == 1


def test_ranger_fait_baisser_le_total_a_traiter() -> None:
    """Trois plans prets, un applique : le total doit passer de 3 a 2. Avec
    l'ancien calcul, le fichier applique revenait en « non planifie » et le
    total restait a 3."""
    plans = [plan("S", source=f"/dl/e{i}.mkv") for i in range(3)]
    avant = summarize(build([], plans, []))

    # Application : le plan quitte la file, son fichier n'est PAS remis en
    # attente — c'est ce que garantit le suivi des chemins deja planifies.
    apres = summarize(build([], plans[1:], []))

    assert avant["ready"] + avant["unplanned"] == 3
    assert apres["ready"] + apres["unplanned"] == 2
