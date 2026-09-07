"""Tests des preferences modifiables.

L'enjeu principal est le confinement : l'interface devient un moyen d'ecrire
sur le disque, elle ne doit pas pouvoir designer n'importe quelle destination.
"""

from pathlib import Path

import pytest

from sortilege.core.preferences import (
    DEFAULT_DESTINATIONS,
    PreferenceError,
    Preferences,
    PreferenceStore,
)


@pytest.fixture
def store(tmp_path: Path) -> PreferenceStore:
    library = tmp_path / "media"
    library.mkdir()
    src_a = tmp_path / "dl-a"
    src_b = tmp_path / "dl-b"
    src_a.mkdir()
    src_b.mkdir()
    return PreferenceStore(
        path=tmp_path / "data" / "preferences.json",
        library_root=library,
        source_roots=[src_a, src_b],
    )


def test_defauts_sans_fichier(store: PreferenceStore) -> None:
    prefs = store.load()
    assert prefs.destinations == DEFAULT_DESTINATIONS
    assert prefs.enabled_sources == []


def test_aller_retour(store: PreferenceStore) -> None:
    store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": "Cinema"}))
    store.invalidate()
    assert store.load().destinations["movie"] == "Cinema"


def test_fichier_corrompu_retombe_sur_les_defauts(store: PreferenceStore) -> None:
    """Un JSON casse ne doit pas empecher l'application de demarrer."""
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text("{ ceci n'est pas du json", encoding="utf-8")
    store.invalidate()
    assert store.load().destinations == DEFAULT_DESTINATIONS


# --- Confinement ------------------------------------------------------------


@pytest.mark.parametrize("hostile", ["../../etc", "/etc/passwd", "..", "  "])
def test_destination_hors_racine_refusee(store: PreferenceStore, hostile: str) -> None:
    with pytest.raises(PreferenceError):
        store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": hostile}))


def test_destination_resolue_reste_sous_la_racine(store: PreferenceStore) -> None:
    store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": "Cinema/VF"}))
    resolved = store.destination_root("movie")
    assert store._library_root in resolved.parents


def test_type_de_media_inconnu_refuse(store: PreferenceStore) -> None:
    with pytest.raises(PreferenceError, match="inconnu"):
        store.save(Preferences(destinations={"musique": "Musique"}))


# --- Sources ----------------------------------------------------------------


def test_source_non_declaree_refusee(store: PreferenceStore) -> None:
    """On ne peut pas scanner une racine absente de l'environnement."""
    with pytest.raises(PreferenceError, match="racine source"):
        store.save(Preferences(enabled_sources=["/ailleurs"]))


def test_selection_vide_signifie_toutes(store: PreferenceStore) -> None:
    assert len(store.resolved_sources()) == 2


def test_selection_partielle(store: PreferenceStore) -> None:
    first = str(store._source_roots[0])
    store.save(Preferences(enabled_sources=[first]))
    resolved = store.resolved_sources()
    assert len(resolved) == 1
    assert str(resolved[0]) == first


# --- Gabarits ---------------------------------------------------------------


def test_gabarit_invalide_refuse(store: PreferenceStore) -> None:
    with pytest.raises(PreferenceError, match="gabarit invalide"):
        store.save(Preferences(templates={"movie": "Films/{jeton_qui_nexiste_pas}"}))


def test_gabarit_valide_accepte(store: PreferenceStore) -> None:
    store.save(Preferences(templates={"movie": "Cinema/{title}{? year: ($)}"}))
    assert store.load().template_for("movie").startswith("Cinema/")


def test_gabarit_absent_retombe_sur_le_prereglage(store: PreferenceStore) -> None:
    assert "{title}" in store.load().template_for("episode")
