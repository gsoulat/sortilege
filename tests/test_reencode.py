"""Ce qui ne respecte pas la strategie, et ce que le reencodage rendrait.

Ces tests portent sur une decision plus lourde qu'il n'y parait. Reencoder est
la seule operation de Sortilege qui DETRUIT de la qualite volontairement : on
echange du detail contre de la place. Une liste qui proposerait le mauvais
fichier, ou qui annoncerait une economie qui ne vient pas, ferait prendre cette
decision sur une base fausse.

Deux proprietes comptent plus que le reste :

1. « Qualité maximale » ne propose JAMAIS rien. Quelqu'un qui a demande la
   meilleure image ne doit pas se voir suggerer de la degrader.
2. On ne remonte jamais. Reencoder vers le haut ne cree aucun detail et gonfle
   le fichier : c'est la seule operation qui perd sur les deux tableaux.
"""

from __future__ import annotations

from sortilege.core.collection import FileRef, Work
from sortilege.core.quality import QualitySettings, strategy
from sortilege.core.reencode import (
    audit,
    by_target,
    estimate_bytes,
    recoverable_bytes,
    target_for,
)

GB = 1024**3


def oeuvre(kind: str, titre: str, *fichiers: FileRef) -> Work:
    work = Work(key=titre, kind=kind, title=titre)
    work.slots["film" if kind == "movie" else "S01E01"] = list(fichiers)
    return work


def fichier(resolution: str, taille: int, nom: str = "f.mkv") -> FileRef:
    return FileRef(relative_path=nom, size_bytes=taille, resolution=resolution)


# --- La cible se deduit de la strategie, elle ne s'invente pas --------------


def test_qualite_maximale_ne_propose_jamais_rien() -> None:
    """Quelqu'un qui a demande la meilleure image ne doit pas se voir suggerer
    de la degrader. C'est la propriete qui protege le reglage par defaut."""
    for resolution in ("2160p", "1080p", "720p"):
        assert target_for(strategy("quality"), resolution) == ""


def test_equilibre_ramene_la_4k_en_1080p() -> None:
    assert target_for(strategy("balanced"), "2160p") == "1080p"


def test_equilibre_laisse_le_1080p_tranquille() -> None:
    """C'est deja son palier prefere : proposer le 720p irait contre le reglage."""
    assert target_for(strategy("balanced"), "1080p") == ""


def test_economie_de_place_ramene_tout_en_720p() -> None:
    assert target_for(strategy("compact"), "2160p") == "720p"
    assert target_for(strategy("compact"), "1080p") == "720p"


def test_on_ne_remonte_jamais() -> None:
    """« Économie de place » classe la 4K derniere, mais un fichier 480p ne doit
    pas pour autant etre reencode vers le 720p : on ne cree pas de detail."""
    assert target_for(strategy("compact"), "480p") == ""


def test_une_resolution_inconnue_ne_se_juge_pas() -> None:
    """Proposer un reencodage sur une supposition, c'est detruire de la qualite
    au hasard."""
    assert target_for(strategy("compact"), None) == ""
    assert target_for(strategy("compact"), "n'importe quoi") == ""


def test_les_ecritures_equivalentes_sont_reconnues() -> None:
    assert target_for(strategy("balanced"), "4K") == "1080p"
    assert target_for(strategy("balanced"), "UHD") == "1080p"


# --- L'estimation -----------------------------------------------------------


def test_l_estimation_reste_en_dessous_du_rapport_de_pixels() -> None:
    """Diviser la hauteur par deux divise les pixels par quatre, mais le poids
    par trois environ. Annoncer un quart serait promettre une economie qui ne
    viendra pas."""
    estime = estimate_bytes(60 * GB, "2160p", "1080p")

    assert 15 * GB < estime < 25 * GB
    assert estime > 60 * GB / 4, "l'estimation ne suit pas les pixels"


def test_une_cible_egale_ou_superieure_ne_change_rien() -> None:
    assert estimate_bytes(8 * GB, "1080p", "1080p") == 8 * GB
    assert estimate_bytes(8 * GB, "720p", "1080p") == 8 * GB


# --- L'inventaire -----------------------------------------------------------


def test_seuls_les_fichiers_hors_strategie_sont_listes() -> None:
    works = [
        oeuvre("movie", "Remux", fichier("2160p", 60 * GB)),
        oeuvre("movie", "Deja bon", fichier("1080p", 8 * GB)),
    ]

    candidats = audit(works, QualitySettings(movie="balanced"))

    assert [c.title for c in candidats] == ["Remux"]
    assert candidats[0].target == "1080p"


def test_chaque_type_suit_sa_propre_strategie() -> None:
    """C'est le besoin reel : la meilleure image pour les films, la plus petite
    empreinte pour les series."""
    works = [
        oeuvre("movie", "Film 4K", fichier("2160p", 60 * GB)),
        oeuvre("episode", "Serie 1080p", fichier("1080p", 6 * GB)),
    ]

    candidats = audit(works, QualitySettings(movie="quality", episode="compact"))

    assert [c.title for c in candidats] == ["Serie 1080p"]


def test_les_plus_gros_gains_viennent_en_premier() -> None:
    """C'est l'ordre dans lequel on veut agir : le premier fichier rend a lui
    seul plus que les vingt suivants."""
    works = [
        oeuvre("movie", "Petit", fichier("2160p", 12 * GB)),
        oeuvre("movie", "Enorme", fichier("2160p", 60 * GB)),
    ]

    candidats = audit(works, QualitySettings(movie="balanced"))

    assert [c.title for c in candidats] == ["Enorme", "Petit"]


def test_un_gain_derisoire_n_est_pas_propose() -> None:
    """Une heure de calcul et une perte de qualite pour trois cents megaoctets
    est un mauvais marche."""
    works = [oeuvre("movie", "Leger", fichier("2160p", 1 * GB))]

    assert audit(works, QualitySettings(movie="balanced")) == []


def test_le_motif_dit_la_regle_appliquee() -> None:
    """Proposer de degrader un fichier sans dire au nom de quoi laisse
    l'utilisateur devant une suggestion qu'il ne peut pas contester."""
    works = [oeuvre("movie", "Remux", fichier("2160p", 60 * GB))]

    motif = audit(works, QualitySettings(movie="compact"))[0].reason

    assert "2160p" in motif
    assert "Économie de place" in motif
    assert "720p" in motif


def test_la_place_recuperable_est_la_somme_des_gains() -> None:
    works = [
        oeuvre("movie", "A", fichier("2160p", 60 * GB)),
        oeuvre("movie", "B", fichier("2160p", 40 * GB)),
    ]
    candidats = audit(works, QualitySettings(movie="balanced"))

    total = recoverable_bytes(candidats)

    assert total == sum(c.savings_bytes for c in candidats)
    assert total > 50 * GB


def test_l_ampleur_du_travail_se_lit_par_cible() -> None:
    """« 12 vers 1080p » se decide ; « 340 fichiers » ne se decide pas."""
    works = [
        oeuvre("movie", "A", fichier("2160p", 60 * GB)),
        oeuvre("movie", "B", fichier("2160p", 40 * GB)),
    ]

    assert by_target(audit(works, QualitySettings(movie="balanced"))) == {"1080p": 2}


def test_une_bibliotheque_conforme_ne_propose_rien() -> None:
    works = [oeuvre("movie", "Bon", fichier("1080p", 8 * GB))]

    assert audit(works, QualitySettings(movie="balanced")) == []


# --- Le budget de poids -----------------------------------------------------
#
# « Un episode doit peser 500 Mo au plus » est une phrase qui ne se traduit pas
# en resolution : deux fichiers en 1080p peuvent peser 1,2 Go et 6 Go selon
# leur debit. C'est une contrainte a part entiere, et elle attrape des fichiers
# que la strategie de resolution laisse passer.

MO = 1024**2


def test_un_fichier_au_bon_format_mais_trop_lourd_est_signale() -> None:
    """Le cas que la resolution seule ne voit pas : 1080p accepte par la
    strategie, mais quatre fois le budget."""
    works = [oeuvre("episode", "Serie", fichier("1080p", 2 * GB))]
    reglage = QualitySettings(episode="quality", max_episode_mb=500)

    candidats = audit(works, reglage)

    assert len(candidats) == 1
    assert candidats[0].over_budget is True
    assert candidats[0].target == "1080p", "on ne redimensionne pas ce qui est deja au format"


def test_sans_budget_le_meme_fichier_ne_bouge_pas() -> None:
    """Zero = aucune limite. Un budget impose d'office ferait apparaitre des
    centaines de fichiers a reencoder chez quelqu'un qui n'a rien demande."""
    works = [oeuvre("episode", "Serie", fichier("1080p", 2 * GB))]

    assert audit(works, QualitySettings(episode="quality")) == []


def test_le_budget_borne_l_estimation() -> None:
    """Le budget est une CONSIGNE, pas une prevision : l'encodeur visera ce
    poids, l'estimation par pixels ne peut pas l'emporter."""
    works = [oeuvre("episode", "Serie", fichier("1080p", 4 * GB))]
    reglage = QualitySettings(episode="quality", max_episode_mb=500)

    candidat = audit(works, reglage)[0]

    assert candidat.estimated_bytes == 500 * MO


def test_le_motif_dit_le_poids_et_le_budget() -> None:
    works = [oeuvre("episode", "Serie", fichier("1080p", 2 * GB))]

    motif = audit(works, QualitySettings(episode="quality", max_episode_mb=500))[0].reason

    assert "500 Mo" in motif
    assert "debit" in motif


def test_un_fichier_dans_le_budget_reste_tranquille() -> None:
    works = [oeuvre("episode", "Serie", fichier("720p", 400 * MO))]

    assert audit(works, QualitySettings(episode="quality", max_episode_mb=500)) == []


def test_chaque_type_a_son_budget() -> None:
    """Un film et un episode n'ont aucune raison de peser pareil."""
    reglage = QualitySettings(max_movie_mb=2000, max_episode_mb=500)

    assert reglage.budget_bytes("movie") == 2000 * MO
    assert reglage.budget_bytes("episode") == 500 * MO
    assert reglage.budget_bytes("anime") == 0
