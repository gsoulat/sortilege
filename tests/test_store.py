"""Tests de la persistance : decisions retenues et etat de travail.

L'enjeu principal est la CLE de rappel : elle doit reconnaitre la meme
situation au scan suivant, alors que le nom de fichier aura peut-etre change de
casse ou d'accentuation. Une cle trop stricte ne retrouve jamais rien et la
memoire ne sert a rien ; une cle trop laxiste confond deux oeuvres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.store import Decision, Store, title_key


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def decision(**kw) -> Decision:
    base = dict(
        kind="episode",
        title_key=title_key("Dark Matter"),
        provider="tmdb",
        external_id="123456",
        title="Dark Matter",
        year=2024,
    )
    base.update(kw)
    return Decision(**base)


# --- Cle de rappel ----------------------------------------------------------


def test_la_casse_ne_change_pas_la_cle() -> None:
    assert title_key("Dark Matter") == title_key("DARK matter")


def test_les_accents_ne_changent_pas_la_cle() -> None:
    """« Amelie » lu par le parseur doit retrouver « Amélie » memorise."""
    assert title_key("Amélie Poulain") == title_key("Amelie Poulain")


def test_les_espaces_de_bord_sont_ignores() -> None:
    assert title_key("  Dune  ") == title_key("Dune")


def test_deux_titres_differents_ont_des_cles_differentes() -> None:
    assert title_key("Dark Matter") != title_key("Dune")


# --- Memoire ----------------------------------------------------------------


def test_un_choix_est_retrouve(store: Store) -> None:
    store.remember(decision())
    found = store.recall("episode", "Dark Matter")
    assert found is not None
    assert found.external_id == "123456"
    assert found.year == 2024


def test_le_rappel_tolere_une_autre_casse(store: Store) -> None:
    """C'est le point qui fait vivre la memoire : le prochain fichier
    s'appellera « dark.matter.S01E01 » et non « Dark Matter »."""
    store.remember(decision())
    assert store.recall("episode", "dark matter") is not None


def test_le_type_separe_les_memoires(store: Store) -> None:
    """Un film et une serie homonymes sont deux oeuvres distinctes."""
    store.remember(decision(kind="episode"))
    assert store.recall("movie", "Dark Matter") is None


def test_un_titre_inconnu_ne_renvoie_rien(store: Store) -> None:
    assert store.recall("episode", "Jamais Vu") is None


def test_un_titre_vide_ne_renvoie_rien(store: Store) -> None:
    """Sans titre, la cle serait vide et matcherait n'importe quoi."""
    store.remember(decision(title_key=""))
    assert store.recall("episode", "") is None


def test_changer_d_avis_remplace(store: Store) -> None:
    """Une erreur de clic doit pouvoir se corriger : sans remplacement, elle
    resterait gravee sans recours."""
    store.remember(decision(external_id="111", title="Dark Matter (2015)"))
    store.remember(decision(external_id="222", title="Dark Matter (2024)"))

    found = store.recall("episode", "Dark Matter")
    assert found.external_id == "222"
    assert len(store.decisions()) == 1


def test_oublier_une_decision(store: Store) -> None:
    store.remember(decision())
    assert store.forget("episode", title_key("Dark Matter")) is True
    assert store.recall("episode", "Dark Matter") is None


def test_oublier_ce_qui_n_existe_pas_ne_leve_pas(store: Store) -> None:
    assert store.forget("episode", "inconnu") is False


def test_les_reutilisations_sont_comptees(store: Store) -> None:
    store.remember(decision())
    key = title_key("Dark Matter")
    store.note_hit("episode", key)
    store.note_hit("episode", key)
    assert store.recall("episode", "Dark Matter").hits == 2


def test_la_memoire_survit_a_une_reouverture(tmp_path: Path) -> None:
    """C'est tout l'objet du disque : le redemarrage du conteneur."""
    path = tmp_path / "persist.db"
    Store(path).remember(decision())
    assert Store(path).recall("episode", "Dark Matter") is not None


# --- Etat de travail --------------------------------------------------------


def test_un_etat_est_relu(store: Store) -> None:
    store.save_blob("scan", {"total": 426, "files": ["a", "b"]})
    assert store.load_blob("scan")["total"] == 426


def test_un_etat_absent_renvoie_none(store: Store) -> None:
    assert store.load_blob("jamais-ecrit") is None


def test_un_etat_est_remplace(store: Store) -> None:
    store.save_blob("plans", [1, 2])
    store.save_blob("plans", [3])
    assert store.load_blob("plans") == [3]


def test_un_etat_non_serialisable_est_ignore(store: Store) -> None:
    """Une panne d'ecriture ne doit pas interrompre un scan en cours."""
    store.save_blob("bancal", {"objet": object()})
    assert store.load_blob("bancal") is None


def test_un_etat_illisible_ne_bloque_pas_le_demarrage(store: Store) -> None:
    """Un format ecrit par une version anterieure : on repart de zero plutot
    que d'echouer au lancement."""
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO blobs (key, payload, updated_at) VALUES ('casse', 'pas du json', '')"
        )
    assert store.load_blob("casse") is None


def test_supprimer_un_etat(store: Store) -> None:
    store.save_blob("scan", {"x": 1})
    store.drop_blob("scan")
    assert store.load_blob("scan") is None
